#!/usr/bin/env python3
"""
Phase 2 QA gate for SWE Freelancer delivery batches.

This script performs deterministic package/static/runtime checks that reviewers can miss,
and writes readable Markdown/CSV/JSON logs for delivery managers.

It intentionally uses only the Python standard library so it can run on macOS/Linux/WSL
without extra installs.
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

MIN_RECOMMENDED_VERSION = (9, 3, 42)
MAX_FLOW_BYTES = 5 * 1024 * 1024
MIN_FLOW_BYTES_WARN = 8 * 1024

REQUIRED_FILES = [
    "test.py",
    "bug_reintroduce.patch",
    "commit_id.txt",
    "issue_data.json",
    "flow.mitm",
]
OPTIONAL_ALLOWED_FILES = {
    "git_tag.txt",
    "revert_command.txt",
    "review.md",
    "README.md",
    "notes.md",
}
GENERATED_OR_BAD_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}
BAD_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "delivery_validation_runs",
    "__MACOSX",
    ".git",
}

PATCH_BLOCKED_EXACT = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "npm-shrinkwrap.json",
    ".env",
    ".env.production",
    ".env.staging",
}
PATCH_BLOCKED_SUFFIXES = (
    ".lock",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".mitm",
    ".mp4",
    ".mov",
)
PATCH_CONFIG_PATTERNS = (
    "/.github/",
    "webpack.config",
    "babel.config",
    "metro.config",
    "jest.config",
    "tsconfig",
    "eslint",
    "prettier",
)

ASSERTION_PATTERNS = [
    r"\bassert\b",
    r"expect\s*\(",
    r"raise\s+AssertionError",
    r"pytest\.fail\s*\(",
    r"TEST FAILED",
]
GOOD_BROKEN_FAILURE_PATTERNS = [
    r"AssertionError",
    r"pytest\.fail",
    r"TEST FAILED",
    r"expect\([^\n]+\)\.to_",
    r"E\s+AssertionError",
]
BAD_TIMEOUT_PATTERNS = [
    r"TimeoutError",
    r"Page\.goto: Timeout",
    r"Locator\.click: Timeout",
    r"Locator\.fill: Timeout",
    r"waiting for locator",
    r"waiting for selector",
    r"Target page, context or browser has been closed",
]
REPLAY_PROBLEM_PATTERNS = [
    r"You appear to be offline",
    r"Update Required",
    r"Please update to the latest version",
    r"ERR_PROXY_CONNECTION_FAILED",
    r"ECONNREFUSED.*8080",
    r"No matching response",
    r"mitmproxy.*error",
    r"Pusher.*error",
]

SEVERITY_ORDER = {"INFO": 0, "WARN": 1, "ERROR": 2}
RUBRIC_ORDER = [
    "G1-package-completeness",
    "G2-metadata-issue-consistency",
    "G3-commit-version-reproducibility",
    "G4-patch-correctness-scope",
    "G5-test-static-quality",
    "G6-flow-replay-quality",
    "G7-runtime-gold-broken-semantics",
    "G8-batch-copy-paste-contamination",
]


@dataclass
class Finding:
    severity: str
    code: str
    rubric: str
    message: str
    path: str = ""
    hint: str = ""


@dataclass
class IssueReport:
    issue_id: str
    path: Path
    findings: list[Finding] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add(self, severity: str, code: str, rubric: str, message: str, path: str = "", hint: str = "") -> None:
        self.findings.append(Finding(severity, code, rubric, message, path, hint))

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "ERROR")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "WARN")

    @property
    def status(self) -> str:
        if self.error_count:
            return "FAIL"
        if self.warn_count:
            return "WARN"
        return "PASS"

    def rubric_status(self, rubric: str) -> str:
        levels = [SEVERITY_ORDER[f.severity] for f in self.findings if f.rubric == rubric]
        if not levels:
            return "PASS"
        max_level = max(levels)
        if max_level >= SEVERITY_ORDER["ERROR"]:
            return "FAIL"
        if max_level >= SEVERITY_ORDER["WARN"]:
            return "WARN"
        return "PASS"


@dataclass
class Context:
    batch_dir: Path
    kit_root: Path
    out_dir: Path
    validation_run: Path | None
    expensify_dir: Path | None
    github_check: str
    strict: bool
    timeout_sec: int
    reports: dict[str, IssueReport] = field(default_factory=dict)
    batch_findings: list[Finding] = field(default_factory=list)
    github_cache: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_batch(self, severity: str, code: str, rubric: str, message: str, path: str = "", hint: str = "") -> None:
        self.batch_findings.append(Finding(severity, code, rubric, message, path, hint))


def rel(path: Path, root: Path | None = None) -> str:
    try:
        if root:
            return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        pass
    return str(path)


def run_cmd(cmd: list[str], cwd: Path | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", e.stderr or f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except FileNotFoundError as e:
        return 127, "", str(e)


def read_text(path: Path, max_bytes: int | None = None) -> str:
    try:
        if max_bytes is not None:
            data = path.read_bytes()[:max_bytes]
            return data.decode("utf-8", errors="replace")
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def version_tuple(text: str) -> tuple[int, int, int] | None:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not m:
        return None
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def version_lt(a: tuple[int, int, int] | None, b: tuple[int, int, int]) -> bool:
    if a is None:
        return False
    return a < b


def strip_markup(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[#*_>`\[\]()]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def discover_issue_dirs(batch_dir: Path) -> list[Path]:
    if not batch_dir.exists():
        return []
    dirs = [p for p in batch_dir.iterdir() if p.is_dir() and re.fullmatch(r"\d+", p.name)]
    return sorted(dirs, key=lambda p: int(p.name))


def resolve_latest_run(kit_root: Path, batch_name: str | None = None) -> Path | None:
    runs_root = kit_root / "delivery_validation_runs"
    if not runs_root.exists():
        return None
    candidates = [p for p in runs_root.iterdir() if p.is_dir()]
    if batch_name:
        safe = batch_name.replace(" ", "_")
        named = [p for p in candidates if p.name.endswith("_" + safe)]
        if named:
            candidates = named
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def maybe_fetch_github_issue(ctx: Context, issue_id: str) -> dict[str, Any] | None:
    if ctx.github_check == "off":
        return None
    if issue_id in ctx.github_cache:
        return ctx.github_cache[issue_id]

    url = f"https://api.github.com/repos/Expensify/App/issues/{issue_id}"
    headers = {
        "Accept": "application/vnd.github.full+json",
        "User-Agent": "phase2-quality-gate",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=ctx.timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            ctx.github_cache[issue_id] = data
            return data
    except Exception as e:
        ctx.github_cache[issue_id] = {"__fetch_error__": str(e)}
        return ctx.github_cache[issue_id]


def check_package_completeness(ctx: Context, rep: IssueReport) -> None:
    issue_dir = rep.path
    for filename in REQUIRED_FILES:
        p = issue_dir / filename
        if not p.exists():
            rep.add("ERROR", "MISSING_REQUIRED_FILE", "G1-package-completeness", f"Missing required file `{filename}`.", rel(p, ctx.batch_dir), "Every delivery issue must include test.py, flow.mitm, commit_id.txt, bug_reintroduce.patch, and issue_data.json.")
        elif p.is_file() and p.stat().st_size == 0:
            rep.add("ERROR", "EMPTY_REQUIRED_FILE", "G1-package-completeness", f"Required file `{filename}` exists but is empty.", rel(p, ctx.batch_dir), "Regenerate or copy the correct file before delivery.")

    if not (issue_dir / "git_tag.txt").exists():
        rep.add("WARN", "MISSING_GIT_TAG_FILE", "G1-package-completeness", "`git_tag.txt` is missing. It may be intentionally empty, but the file should exist for a consistent package.", rel(issue_dir / "git_tag.txt", ctx.batch_dir), "Run: touch issues/<ID>/git_tag.txt")

    for p in issue_dir.rglob("*"):
        if p == issue_dir:
            continue
        parts = set(p.relative_to(issue_dir).parts)
        bad_dirs = parts.intersection(BAD_DIR_NAMES)
        if bad_dirs:
            rep.add("ERROR", "BAD_GENERATED_DIRECTORY", "G1-package-completeness", f"Generated/unwanted directory included: `{sorted(bad_dirs)[0]}`.", rel(p, ctx.batch_dir), "Remove cache folders, nested repos, __MACOSX, and delivery_validation_runs before packaging.")
            continue
        if p.is_file():
            name = p.name
            top_name = p.relative_to(issue_dir).parts[0]
            if name in GENERATED_OR_BAD_NAMES:
                rep.add("ERROR", "BAD_GENERATED_FILE", "G1-package-completeness", f"Generated OS file included: `{name}`.", rel(p, ctx.batch_dir), "Remove it from the issue folder.")
            elif top_name not in set(REQUIRED_FILES) | OPTIONAL_ALLOWED_FILES:
                rep.add("WARN", "UNEXPECTED_FILE", "G1-package-completeness", f"Unexpected file included: `{p.relative_to(issue_dir)}`.", rel(p, ctx.batch_dir), "Keep the package minimal unless this file is explicitly required by the eval.")


def check_issue_metadata(ctx: Context, rep: IssueReport) -> None:
    path = rep.path / "issue_data.json"
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        data = json.loads(read_text(path))
        rep.metadata["issue_data"] = data
    except Exception as e:
        rep.add("ERROR", "ISSUE_DATA_INVALID_JSON", "G2-metadata-issue-consistency", f"issue_data.json is not valid JSON: {e}", rel(path, ctx.batch_dir), "Fix JSON syntax and rerun Phase 2.")
        return

    required = ["price", "title", "issue_repo_steps", "html_description", "difficulty", "issue_clarity", "test_quality"]
    for key in required:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            severity = "ERROR" if key in {"price", "title", "issue_repo_steps", "html_description"} else "WARN"
            rep.add(severity, "ISSUE_DATA_MISSING_FIELD", "G2-metadata-issue-consistency", f"issue_data.json missing or empty field `{key}`.", rel(path, ctx.batch_dir), "Regenerate metadata from the GitHub issue and fill scoring fields manually.")

    title = str(data.get("title", ""))
    if title and not re.search(r"\[\$\d+(?:\.\d+)?\]", title):
        rep.add("ERROR", "TITLE_HAS_NO_PAYOUT", "G2-metadata-issue-consistency", "Issue title does not contain a payout marker like `[$250]`.", rel(path, ctx.batch_dir), "Client can reject tasks without a bounty amount in the issue title.")

    try:
        price = float(str(data.get("price", "0")).replace("$", ""))
        if price <= 0:
            rep.add("ERROR", "PRICE_NOT_POSITIVE", "G2-metadata-issue-consistency", "`price` is missing or not positive.", rel(path, ctx.batch_dir), "Use the dollar amount from the issue title/labels.")
    except Exception:
        rep.add("ERROR", "PRICE_NOT_NUMERIC", "G2-metadata-issue-consistency", "`price` must be numeric.", rel(path, ctx.batch_dir), "Example: 250 or 250.0")

    for numeric_key in ["difficulty", "issue_clarity", "test_quality"]:
        raw = str(data.get(numeric_key, "")).strip()
        if raw:
            try:
                val = int(float(raw))
                lo, hi = (0, 3) if numeric_key in {"difficulty", "issue_clarity"} else (1, 5)
                if not (lo <= val <= hi):
                    rep.add("WARN", "SCORE_OUT_OF_RANGE", "G2-metadata-issue-consistency", f"`{numeric_key}` value `{raw}` is outside expected range {lo}-{hi}.", rel(path, ctx.batch_dir), "Use the project rubric scoring range.")
            except Exception:
                rep.add("WARN", "SCORE_NOT_NUMERIC", "G2-metadata-issue-consistency", f"`{numeric_key}` should be numeric, got `{raw}`.", rel(path, ctx.batch_dir), "Use the project rubric scoring range.")

    html_desc = str(data.get("html_description", ""))
    steps = str(data.get("issue_repo_steps", ""))
    if html_desc:
        plain_desc = strip_markup(html_desc)
        if len(plain_desc) < 300:
            rep.add("ERROR", "HTML_DESCRIPTION_TOO_SHORT", "G2-metadata-issue-consistency", "`html_description` looks too short; it may be a summary or truncated body.", rel(path, ctx.batch_dir), "Fetch the full rendered HTML body from GitHub, not a short summary.")
        if "Action Performed" in steps and "Expected Result" not in html_desc and "Actual Result" not in html_desc:
            rep.add("WARN", "HTML_DESCRIPTION_MISSING_EXPECTED_SECTIONS", "G2-metadata-issue-consistency", "`html_description` does not appear to contain the usual issue sections.", rel(path, ctx.batch_dir), "Manually compare it with the GitHub issue body.")

    gh = maybe_fetch_github_issue(ctx, rep.issue_id)
    if gh:
        if "__fetch_error__" in gh:
            severity = "ERROR" if ctx.github_check == "on" else "WARN"
            rep.add(severity, "GITHUB_FETCH_FAILED", "G2-metadata-issue-consistency", f"Could not fetch GitHub issue for cross-check: {gh['__fetch_error__']}", f"Expensify/App#{rep.issue_id}", "Set GITHUB_TOKEN/GH_TOKEN if you are hitting rate limits, or rerun with --github-check off.")
        else:
            gh_title = str(gh.get("title", ""))
            if gh_title and title and gh_title.strip() != title.strip():
                rep.add("ERROR", "GITHUB_TITLE_MISMATCH", "G2-metadata-issue-consistency", "issue_data.title does not exactly match the live GitHub issue title.", rel(path, ctx.batch_dir), f"Expected: {gh_title}")
            gh_body = str(gh.get("body_html") or gh.get("body") or "")
            if gh_body and html_desc:
                gh_plain = strip_markup(gh_body)
                desc_plain = strip_markup(html_desc)
                if len(gh_plain) > 500 and len(desc_plain) < min(450, int(0.5 * len(gh_plain))):
                    rep.add("ERROR", "GITHUB_BODY_TRUNCATED", "G2-metadata-issue-consistency", "html_description is much shorter than the live GitHub issue body.", rel(path, ctx.batch_dir), "Regenerate issue_data.json with full HTML body.")


def check_commit_and_version(ctx: Context, rep: IssueReport) -> None:
    commit_path = rep.path / "commit_id.txt"
    tag_path = rep.path / "git_tag.txt"
    if not commit_path.exists() or commit_path.stat().st_size == 0:
        return

    commit = read_text(commit_path).strip().split()[0] if read_text(commit_path).strip() else ""
    rep.metadata["commit_id"] = commit
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        rep.add("ERROR", "BAD_COMMIT_ID_FORMAT", "G3-commit-version-reproducibility", f"commit_id.txt should contain one 40-char Git SHA, got `{commit}`.", rel(commit_path, ctx.batch_dir), "Replace it with `git rev-parse HEAD` from the fixed-state commit.")
        return

    git_tag = read_text(tag_path).strip() if tag_path.exists() else ""
    if git_tag:
        tag_version = version_tuple(git_tag)
        if tag_version is None:
            rep.add("WARN", "GIT_TAG_VERSION_UNPARSEABLE", "G3-commit-version-reproducibility", f"git_tag.txt has an unparseable version: `{git_tag}`.", rel(tag_path, ctx.batch_dir), "Use a recent app version like 9.3.54-3 only when the commit is old.")
        elif version_lt(tag_version, MIN_RECOMMENDED_VERSION):
            rep.add("ERROR", "GIT_TAG_TOO_OLD", "G3-commit-version-reproducibility", f"git_tag.txt version `{git_tag}` is older than recommended {'.'.join(map(str, MIN_RECOMMENDED_VERSION))}.", rel(tag_path, ctx.batch_dir), "Use a recent version tag to avoid the Update Required banner.")
        if "staging" in git_tag.lower() and tag_version and version_lt(tag_version, MIN_RECOMMENDED_VERSION):
            rep.add("ERROR", "OLD_STAGING_TAG", "G3-commit-version-reproducibility", f"Old staging git_tag detected: `{git_tag}`.", rel(tag_path, ctx.batch_dir), "Use a recent tag or leave git_tag empty for latest main.")

    if ctx.expensify_dir is None:
        rep.add("WARN", "EXPENSIFY_REPO_NOT_PROVIDED", "G3-commit-version-reproducibility", "Skipping local commit/patch apply checks because --expensify-dir was not provided.", "", "Provide --expensify-dir /path/to/Expensify/App for the strongest Phase 2 gate.")
        return

    repo = ctx.expensify_dir
    if not (repo / ".git").exists():
        rep.add("ERROR", "EXPENSIFY_REPO_INVALID", "G3-commit-version-reproducibility", f"--expensify-dir is not a Git repo: {repo}", str(repo), "Point to your local Expensify/App clone.")
        return

    code, _, err = run_cmd(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo, timeout=ctx.timeout_sec)
    if code != 0:
        rep.add("ERROR", "COMMIT_NOT_FOUND_LOCALLY", "G3-commit-version-reproducibility", f"Commit `{commit}` was not found in the local Expensify repo.", rel(commit_path, ctx.batch_dir), "Run `git fetch --all --prune` in Expensify/App, then rerun Phase 2.")
        return

    code, package_json, _ = run_cmd(["git", "show", f"{commit}:package.json"], cwd=repo, timeout=ctx.timeout_sec)
    if code == 0 and package_json.strip():
        try:
            version = str(json.loads(package_json).get("version", ""))
        except Exception:
            version = ""
        commit_version = version_tuple(version)
        rep.metadata["commit_package_version"] = version
        if commit_version and version_lt(commit_version, MIN_RECOMMENDED_VERSION) and not git_tag:
            rep.add("ERROR", "OLD_COMMIT_WITHOUT_GIT_TAG", "G3-commit-version-reproducibility", f"Commit package version `{version}` is old and git_tag.txt is empty.", rel(commit_path, ctx.batch_dir), "Add a recent git_tag.txt to avoid the Update Required banner.")

    # Main ancestry is a warning, not a hard fail: some valid fallback tasks use PR branches.
    run_cmd(["git", "fetch", "origin", "main", "--quiet"], cwd=repo, timeout=max(ctx.timeout_sec, 60))
    code, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", commit, "origin/main"], cwd=repo, timeout=ctx.timeout_sec)
    if code != 0:
        rep.add("WARN", "COMMIT_NOT_ON_ORIGIN_MAIN", "G3-commit-version-reproducibility", "Commit is not an ancestor of origin/main in the local repo.", rel(commit_path, ctx.batch_dir), "Not always blocking, but verify it is the intended fixed-state commit and builds in SWELancer.")


def parse_patch_files(patch_text: str) -> list[str]:
    files: list[str] = []
    for m in re.finditer(r"^diff --git a/(.*?) b/(.*?)$", patch_text, flags=re.M):
        for group in (1, 2):
            f = m.group(group).strip()
            if f != "/dev/null" and f not in files:
                files.append(f)
    # Fallback for patches without diff --git lines.
    if not files:
        for m in re.finditer(r"^(?:---|\+\+\+)\s+(?:a/|b/)?([^\t\n]+)", patch_text, flags=re.M):
            f = m.group(1).strip()
            if f != "/dev/null" and f not in files:
                files.append(f)
    return files


def check_patch(ctx: Context, rep: IssueReport) -> None:
    patch_path = rep.path / "bug_reintroduce.patch"
    if not patch_path.exists() or patch_path.stat().st_size == 0:
        return
    text = read_text(patch_path)
    if "diff --git" not in text and "---" not in text:
        rep.add("ERROR", "PATCH_NOT_UNIFIED_DIFF", "G4-patch-correctness-scope", "bug_reintroduce.patch does not look like a unified git diff.", rel(patch_path, ctx.batch_dir), "Regenerate it with `git diff > bug_reintroduce.patch` from fixed → broken state.")
        return

    files = parse_patch_files(text)
    rep.metadata["patch_files"] = files
    if not files:
        rep.add("ERROR", "PATCH_HAS_NO_FILES", "G4-patch-correctness-scope", "Could not detect changed files in bug_reintroduce.patch.", rel(patch_path, ctx.batch_dir), "Regenerate the patch from a clean git diff.")
    if len(files) > 8:
        rep.add("WARN", "PATCH_TOUCHES_MANY_FILES", "G4-patch-correctness-scope", f"Patch touches {len(files)} files, which is unusually broad for a bug reintroduction.", rel(patch_path, ctx.batch_dir), "Manually verify it only undoes the fix and does not include unrelated refactors.")

    for f in files:
        name = Path(f).name
        lower = f.lower()
        if name in PATCH_BLOCKED_EXACT or lower.endswith(PATCH_BLOCKED_SUFFIXES):
            rep.add("ERROR", "PATCH_TOUCHES_BLOCKED_FILE", "G4-patch-correctness-scope", f"Patch touches blocked/unrelated file `{f}`.", rel(patch_path, ctx.batch_dir), "Remove lockfiles, package-version bumps, binaries, env files, and flow files from the patch.")
        if any(pat in lower for pat in PATCH_CONFIG_PATTERNS):
            rep.add("WARN", "PATCH_TOUCHES_CONFIG", "G4-patch-correctness-scope", f"Patch touches config-like file `{f}`.", rel(patch_path, ctx.batch_dir), "Only acceptable if the actual fix was in that config; otherwise remove it.")
        if lower.startswith("tests/") or "/__tests__/" in lower or lower.endswith(".spec.ts") or lower.endswith(".test.ts"):
            rep.add("WARN", "PATCH_TOUCHES_TEST_FILE", "G4-patch-correctness-scope", f"Patch touches test file `{f}`.", rel(patch_path, ctx.batch_dir), "Bug reintroduction patches normally modify source code, not tests.")

    if re.search(r"^Binary files ", text, flags=re.M) or "GIT binary patch" in text:
        rep.add("ERROR", "PATCH_HAS_BINARY_CONTENT", "G4-patch-correctness-scope", "Patch contains binary content.", rel(patch_path, ctx.batch_dir), "Regenerate patch with source-code-only changes.")

    if ctx.expensify_dir is not None and (ctx.expensify_dir / ".git").exists():
        commit = rep.metadata.get("commit_id") or read_text(rep.path / "commit_id.txt").strip().split()[0]
        if re.fullmatch(r"[0-9a-fA-F]{40}", str(commit)):
            check_patch_applies_in_worktree(ctx, rep, str(commit), patch_path)


def check_patch_applies_in_worktree(ctx: Context, rep: IssueReport, commit: str, patch_path: Path) -> None:
    repo = ctx.expensify_dir
    assert repo is not None
    temp_root = Path(tempfile.mkdtemp(prefix=f"phase2_{rep.issue_id}_"))
    worktree = temp_root / "app"
    try:
        code, out, err = run_cmd(["git", "worktree", "add", "--detach", "--force", str(worktree), commit], cwd=repo, timeout=max(ctx.timeout_sec, 120))
        if code != 0:
            rep.add("ERROR", "WORKTREE_CREATE_FAILED", "G4-patch-correctness-scope", f"Could not create git worktree for patch check: {err.strip() or out.strip()}", rel(patch_path, ctx.batch_dir), "Check the local Expensify repo state and disk space.")
            return
        code, out, err = run_cmd(["git", "apply", "--check", str(patch_path)], cwd=worktree, timeout=ctx.timeout_sec)
        if code != 0:
            rep.add("ERROR", "PATCH_DOES_NOT_APPLY", "G4-patch-correctness-scope", f"bug_reintroduce.patch does not apply cleanly to commit_id: {err.strip() or out.strip()}", rel(patch_path, ctx.batch_dir), "The commit_id may be wrong, the patch may be stale, or the patch was generated from the wrong base.")
            return
        code, out, err = run_cmd(["git", "apply", str(patch_path)], cwd=worktree, timeout=ctx.timeout_sec)
        if code != 0:
            rep.add("ERROR", "PATCH_APPLY_FAILED", "G4-patch-correctness-scope", f"git apply failed after --check passed: {err.strip() or out.strip()}", rel(patch_path, ctx.batch_dir), "Inspect the patch manually.")
            return
        code, out, err = run_cmd(["git", "apply", "-R", "--check", str(patch_path)], cwd=worktree, timeout=ctx.timeout_sec)
        if code != 0:
            rep.add("ERROR", "PATCH_REVERSE_CHECK_FAILED", "G4-patch-correctness-scope", f"Patch applies but cannot be reversed cleanly: {err.strip() or out.strip()}", rel(patch_path, ctx.batch_dir), "A clean bug_reintroduce.patch must apply to fixed state and reverse back to fixed state.")
        code, out, err = run_cmd(["git", "diff", "--check"], cwd=worktree, timeout=ctx.timeout_sec)
        if code != 0:
            rep.add("WARN", "PATCH_WHITESPACE_ERRORS", "G4-patch-correctness-scope", f"Patch introduces whitespace/check errors: {out.strip() or err.strip()}", rel(patch_path, ctx.batch_dir), "Usually not blocking, but clean it up if possible.")
    finally:
        if repo:
            run_cmd(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo, timeout=max(ctx.timeout_sec, 120))
            run_cmd(["git", "worktree", "prune"], cwd=repo, timeout=ctx.timeout_sec)
        shutil.rmtree(temp_root, ignore_errors=True)


def check_test_static(ctx: Context, rep: IssueReport) -> None:
    path = rep.path / "test.py"
    if not path.exists() or path.stat().st_size == 0:
        return
    text = read_text(path)

    try:
        tree = ast.parse(text)
        test_funcs = [n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
        if not test_funcs:
            rep.add("ERROR", "NO_PYTEST_TEST_FUNCTION", "G5-test-static-quality", "test.py has no pytest test function named `test_*`.", rel(path, ctx.batch_dir), "Wrap the flow in `def test_<ISSUE_ID>():`.")
        elif len(test_funcs) > 2:
            rep.add("WARN", "MANY_TEST_FUNCTIONS", "G5-test-static-quality", f"test.py has multiple test functions: {', '.join(test_funcs)}.", rel(path, ctx.batch_dir), "Usually one focused test per issue is easier to validate and debug.")
    except SyntaxError as e:
        rep.add("ERROR", "TEST_SYNTAX_ERROR", "G5-test-static-quality", f"test.py has a Python syntax error: line {e.lineno}: {e.msg}", rel(path, ctx.batch_dir), "Fix syntax before running SWELancer.")
        return
    except Exception as e:
        rep.add("WARN", "TEST_AST_PARSE_FAILED", "G5-test-static-quality", f"Could not parse test.py for static AST checks: {e}", rel(path, ctx.batch_dir), "Manual review required.")

    checks_error = [
        (r"page\.pause\s*\(", "PAGE_PAUSE_LEFT", "page.pause() is left in test.py and will hang automated eval."),
        (r"\bbreakpoint\s*\(", "BREAKPOINT_LEFT", "breakpoint() is left in test.py and will hang/interrupt eval."),
        (r"\bpdb\.set_trace\s*\(", "PDB_LEFT", "pdb.set_trace() is left in test.py."),
        (r"\binput\s*\(", "INPUT_LEFT", "input() is left in test.py and will block automated eval."),
        (r"from\s+email_handler\s+import\b|import\s+email_handler\b", "BAD_EMAIL_HANDLER_IMPORT", "Use `from utils.email_handler import ...`, not `from email_handler import ...`, for SWELancer packaging."),
    ]
    for pat, code, msg in checks_error:
        if re.search(pat, text):
            rep.add("ERROR", code, "G5-test-static-quality", msg, rel(path, ctx.batch_dir), "Update the final submitted test.py.")

    if re.search(r"from\s+online_guard\s+import\b|import\s+online_guard\b", text):
        rep.add("WARN", "NON_UTILS_ONLINE_GUARD_IMPORT", "G5-test-static-quality", "test.py imports online_guard without the `utils.` prefix. This may work locally but can fail depending on PYTHONPATH in the eval image.", rel(path, ctx.batch_dir), "Prefer `from utils.online_guard import install_online_guard_sync` if the runtime exposes utils as a package.")

    if "install_online_guard_sync" in text and not re.search(r"install_online_guard_sync\s*\(", text):
        rep.add("ERROR", "ONLINE_GUARD_IMPORTED_NOT_CALLED", "G5-test-static-quality", "install_online_guard_sync is imported/referenced but never called.", rel(path, ctx.batch_dir), "Call it immediately after `context.new_page()` for mobile/offline-sensitive replay tests.")

    mobile_context = bool(re.search(r"p\.devices\s*\[|is_mobile\s*=\s*True|viewport\s*=.*isMobile", text, re.S))
    if mobile_context and "install_online_guard_sync" not in text:
        rep.add("ERROR", "MOBILE_WITHOUT_ONLINE_GUARD", "G5-test-static-quality", "Mobile/emulated-device test detected without online_guard.", rel(path, ctx.batch_dir), "Add online_guard to avoid false offline banners during replay.")

    if not re.search(r"proxy\s*=\s*\{[^}]*localhost:8080|proxy\s*=\s*\{[^}]*127\.0\.0\.1:8080", text, flags=re.S):
        rep.add("ERROR", "NO_LOCAL_PROXY_CONFIG", "G5-test-static-quality", "test.py does not clearly route Playwright through mitmproxy on localhost:8080.", rel(path, ctx.batch_dir), "Final replay tests must use proxy={\"server\": \"http://localhost:8080\"} or equivalent.")

    if re.search(r"https://(?:staging\.|new\.)?expensify\.com", text) and "dev.new.expensify.com:8082" not in text:
        rep.add("ERROR", "USES_LIVE_EXPENSIFY_URL", "G5-test-static-quality", "test.py appears to use a live Expensify URL instead of the local dev URL.", rel(path, ctx.batch_dir), "Use https://dev.new.expensify.com:8082/ for local replay validation.")

    if not any(re.search(pat, text) for pat in ASSERTION_PATTERNS):
        rep.add("ERROR", "NO_ASSERTION_SIGNAL", "G5-test-static-quality", "test.py has no clear assertion/expect/pytest.fail signal.", rel(path, ctx.batch_dir), "Add a final assertion that passes fixed state and fails broken state.")

    if re.search(r"time\.sleep\s*\(\s*(?:[6-9]|[1-9]\d+)", text):
        rep.add("WARN", "LONG_TIME_SLEEP", "G5-test-static-quality", "Long time.sleep() found; this often causes flaky or slow evals.", rel(path, ctx.batch_dir), "Prefer Playwright waits on specific UI state.")

    if re.search(r"slow_mo\s*=\s*(?:[1-9]\d*)", text):
        rep.add("WARN", "SLOW_MO_LEFT", "G5-test-static-quality", "slow_mo is left in Playwright launch options.", rel(path, ctx.batch_dir), "Remove or reduce it for final eval unless needed for stability.")

    if re.search(r"headless\s*=\s*False", text):
        rep.add("WARN", "HEADLESS_FALSE_LEFT", "G5-test-static-quality", "headless=False is left in the final test.", rel(path, ctx.batch_dir), "Usually final eval should not require headed browsers.")

    if re.search(r"uuid\.|random\.|secrets\.|time\.time\s*\(", text) and re.search(r"@|email", text, re.I):
        rep.add("WARN", "NON_DETERMINISTIC_EMAIL", "G5-test-static-quality", "Test appears to generate random/time-based emails.", rel(path, ctx.batch_dir), "Use deterministic derived emails tied to the issue ID so replay remains stable.")

    uses_validate_code = "validateCode" in text or re.search(r"OTP|otp|magic code|EmailHandler", text, re.I)
    if uses_validate_code:
        has_email_handler_active = "EmailHandler" in text and not re.search(r"#\s*with\s+EmailHandler", text)
        has_hardcoded_otp = bool(re.search(r"otp\s*=\s*['\"]\d{4,8}['\"]", text, re.I))
        if has_email_handler_active and not has_hardcoded_otp:
            rep.add("ERROR", "LIVE_OTP_HANDLER_IN_REPLAY_TEST", "G5-test-static-quality", "OTP path detected but no hardcoded replay OTP was found; EmailHandler seems active.", rel(path, ctx.batch_dir), "Final replay-mode test.py should use the OTP captured during recording.")

    if re.search(r"get_by_text\s*\(\s*['\"][^'\"]{0,4}['\"]", text):
        rep.add("WARN", "WEAK_SHORT_TEXT_SELECTOR", "G5-test-static-quality", "Very short text selector detected.", rel(path, ctx.batch_dir), "Prefer test IDs, roles with accessible names, or stronger selectors.")

    if rep.issue_id not in text:
        rep.add("WARN", "ISSUE_ID_NOT_IN_TEST", "G5-test-static-quality", "Issue ID is not present in test.py.", rel(path, ctx.batch_dir), "Not strictly required, but embedding it in email suffix/test function helps avoid copy-paste mistakes.")


def check_flow(ctx: Context, rep: IssueReport) -> None:
    path = rep.path / "flow.mitm"
    if not path.exists() or path.stat().st_size == 0:
        return
    size = path.stat().st_size
    rep.metadata["flow_size_bytes"] = size
    if size > MAX_FLOW_BYTES:
        rep.add("ERROR", "FLOW_TOO_LARGE", "G6-flow-replay-quality", f"flow.mitm is {size / 1024 / 1024:.1f}MB, larger than the 5MB QA limit.", rel(path, ctx.batch_dir), "Filter the flow to Expensify/Pusher traffic and re-record if needed.")
    elif size < MIN_FLOW_BYTES_WARN:
        rep.add("WARN", "FLOW_VERY_SMALL", "G6-flow-replay-quality", f"flow.mitm is very small ({size} bytes).", rel(path, ctx.batch_dir), "Verify the proxy was enabled and the file contains the required API/Pusher traffic.")

    blob = path.read_bytes()[: min(size, 2 * 1024 * 1024)]
    ascii_blob = blob.decode("latin-1", errors="ignore").lower()
    has_expensify = "expensify.com" in ascii_blob
    has_pusher = "pusher" in ascii_blob
    if not has_expensify:
        rep.add("WARN", "FLOW_NO_EXPENSIFY_STRING", "G6-flow-replay-quality", "Could not find `expensify.com` in the first part of flow.mitm.", rel(path, ctx.batch_dir), "This can be a false alarm for binary mitm files, but often means the proxy/recording was wrong.")
    if not has_pusher:
        rep.add("INFO", "FLOW_NO_PUSHER_STRING", "G6-flow-replay-quality", "No obvious Pusher traffic string found in flow.mitm.", rel(path, ctx.batch_dir), "Not every test needs Pusher, but verify replay stability if chat/events are involved.")


def load_results_csv(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        with path.open(newline="", encoding="utf-8") as f:
            return {str(row.get("question_id", "")).strip(): row for row in csv.DictReader(f) if row.get("question_id")}
    except Exception:
        return {}


def tail(path: Path, max_chars: int = 12000) -> str:
    if not path.exists():
        return ""
    try:
        data = path.read_bytes()
        return data[-max_chars:].decode("utf-8", errors="replace")
    except Exception:
        return ""


def bool_str(x: str | None) -> bool | None:
    if x is None:
        return None
    if str(x).strip().lower() == "true":
        return True
    if str(x).strip().lower() == "false":
        return False
    return None


def check_runtime_results(ctx: Context, rep: IssueReport) -> None:
    if ctx.validation_run is None:
        rep.add("WARN", "NO_VALIDATION_RUN_PROVIDED", "G7-runtime-gold-broken-semantics", "No delivery_validation_run was provided/found; skipping runtime gold/broken semantics checks.", "", "Run validate_delivery_batch.sh first, then Phase 2 with --validation-run latest.")
        return

    gold_rows = load_results_csv(ctx.validation_run / "gold.results.csv")
    broken_rows = load_results_csv(ctx.validation_run / "broken.results.csv")
    summary_rows = load_results_csv(ctx.validation_run / "summary.csv")
    qid = rep.issue_id
    g = gold_rows.get(qid)
    b = broken_rows.get(qid)
    s = summary_rows.get(qid)
    gold_log = ctx.validation_run / "per_task" / qid / "gold.run.log"
    broken_log = ctx.validation_run / "per_task" / qid / "broken.run.log"
    g_text = tail(gold_log)
    b_text = tail(broken_log)

    if not g:
        rep.add("ERROR", "MISSING_GOLD_RESULT", "G7-runtime-gold-broken-semantics", "No gold.results.csv row found for this issue.", rel(ctx.validation_run, ctx.kit_root), "The task may not have built or did not run. Check build.log and driver.log.")
    if not b:
        rep.add("ERROR", "MISSING_BROKEN_RESULT", "G7-runtime-gold-broken-semantics", "No broken.results.csv row found for this issue.", rel(ctx.validation_run, ctx.kit_root), "The task may not have built or did not run. Check build.log and driver.log.")
    if not g or not b:
        return

    g_correct = bool_str(g.get("correct"))
    b_correct = bool_str(b.get("correct"))
    rep.metadata["gold_correct"] = g.get("correct")
    rep.metadata["broken_correct"] = b.get("correct")
    if s:
        rep.metadata["existing_verdict"] = s.get("verdict", "")

    if g_correct is not True:
        rep.add("ERROR", "GOLD_DID_NOT_PASS", "G7-runtime-gold-broken-semantics", f"Gold/fixed validation did not pass (correct={g.get('correct')}).", rel(gold_log, ctx.kit_root), "Client expects fixed/gold state to pass reliably.")
    if b_correct is True:
        rep.add("ERROR", "BROKEN_PASSED", "G7-runtime-gold-broken-semantics", "Broken/no-gold validation passed, which means the test does not detect the bug or assertion is inverted.", rel(broken_log, ctx.kit_root), "Rewrite the assertion or bug_reintroduce.patch so broken state fails.")

    if g_correct is True and b_correct is False:
        bad_timeout = any(re.search(p, b_text, re.I) for p in BAD_TIMEOUT_PATTERNS)
        replay_problem = any(re.search(p, b_text, re.I) for p in REPLAY_PROBLEM_PATTERNS)
        good_assert = any(re.search(p, b_text, re.I) for p in GOOD_BROKEN_FAILURE_PATTERNS)
        if bad_timeout and not good_assert:
            rep.add("ERROR", "BROKEN_FAILS_BY_TIMEOUT", "G7-runtime-gold-broken-semantics", "Broken state fails by timeout/locator/navigation error, not by a clear assertion.", rel(broken_log, ctx.kit_root), "The test should reach the target UI in both states and fail with an assertion signal.")
        elif replay_problem:
            rep.add("ERROR", "BROKEN_FAILS_BY_REPLAY_INFRA", "G7-runtime-gold-broken-semantics", "Broken state failure appears related to replay/offline/update/proxy infrastructure.", rel(broken_log, ctx.kit_root), "Fix flow recording/replay or version/git_tag before delivery.")
        elif not good_assert:
            rep.add("WARN", "BROKEN_FAILURE_SIGNAL_UNCLEAR", "G7-runtime-gold-broken-semantics", "Broken state fails, but Phase 2 could not find a clear assertion signal in the log tail.", rel(broken_log, ctx.kit_root), "Manually inspect the log. Prefer AssertionError / pytest.fail / expect assertion failures.")

    if any(re.search(p, g_text, re.I) for p in REPLAY_PROBLEM_PATTERNS):
        rep.add("ERROR", "GOLD_HAS_REPLAY_INFRA_SIGNAL", "G7-runtime-gold-broken-semantics", "Gold log contains replay/offline/update/proxy problem signals.", rel(gold_log, ctx.kit_root), "Even if it passed once, this is risky for client validation.")


def check_batch_duplicates(ctx: Context) -> None:
    seen_hashes: dict[tuple[str, str], str] = {}
    seen_titles: dict[str, str] = {}
    seen_commits: dict[str, list[str]] = {}
    for issue_id, rep in ctx.reports.items():
        for filename in ["test.py", "bug_reintroduce.patch", "flow.mitm"]:
            p = rep.path / filename
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                digest = sha256_file(p)
                key = (filename, digest)
                if key in seen_hashes:
                    other = seen_hashes[key]
                    msg = f"`{filename}` is byte-identical to issue {other}."
                    severity = "ERROR" if filename in {"test.py", "flow.mitm"} else "WARN"
                    rep.add(severity, "DUPLICATE_FILE_HASH", "G8-batch-copy-paste-contamination", msg, rel(p, ctx.batch_dir), "This is usually a copy-paste/package contamination issue unless the tasks are intentionally identical.")
                else:
                    seen_hashes[key] = issue_id
        data = rep.metadata.get("issue_data") or {}
        title = str(data.get("title", "")).strip().lower()
        if title:
            if title in seen_titles:
                rep.add("ERROR", "DUPLICATE_ISSUE_TITLE", "G8-batch-copy-paste-contamination", f"Duplicate issue title also used by issue {seen_titles[title]}.", rel(rep.path / "issue_data.json", ctx.batch_dir), "Verify issue_data.json was not copied from another issue.")
            else:
                seen_titles[title] = issue_id
        commit = str(rep.metadata.get("commit_id") or "").strip()
        if commit:
            seen_commits.setdefault(commit, []).append(issue_id)

    for commit, ids in seen_commits.items():
        if len(ids) >= 3:
            for issue_id in ids:
                ctx.reports[issue_id].add("WARN", "COMMIT_REUSED_MANY_TIMES", "G8-batch-copy-paste-contamination", f"Same commit_id is used by {len(ids)} issues in this batch: {', '.join(ids)}.", rel(ctx.reports[issue_id].path / "commit_id.txt", ctx.batch_dir), "This can be valid if all use latest main, but check for accidental copy-paste.")



def _phase2_env_bool(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _issue_price_and_title(rep: IssueReport) -> tuple[float, str, bool]:
    data = rep.metadata.get("issue_data") or {}
    title = str(data.get("title", ""))
    raw_price = data.get("price", 0)
    try:
        price = float(str(raw_price).replace("$", "").strip())
    except Exception:
        price = 0.0
    has_payout_in_title = bool(re.search(r"\[\$\d+(?:\.\d+)?\]", title))
    return price, title, has_payout_in_title



def _real_python_call_lines(path: Path, call_kind: str) -> list[int]:
    """
    Return real Python call line numbers using AST, not raw text.
    call_kind:
      - input: matches input(...)
      - page_pause: matches *.pause(...)
    """
    try:
        import ast
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except Exception:
        return []

    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if call_kind == "input":
            if isinstance(node.func, ast.Name) and node.func.id == "input":
                lines.append(getattr(node, "lineno", 0))

        elif call_kind == "page_pause":
            if isinstance(node.func, ast.Attribute) and node.func.attr == "pause":
                lines.append(getattr(node, "lineno", 0))

    return sorted(x for x in set(lines) if x)


def _resolve_finding_path(ctx: Context, finding: Finding) -> Path | None:
    if not finding.path:
        return None

    raw = Path(finding.path)

    candidates = [
        ctx.batch_dir / raw,
        ctx.kit_root / raw,
        raw,
    ]

    for c in candidates:
        if c.exists():
            return c

    return None


def normalize_findings_for_delivery_manager(ctx: Context, rep: IssueReport) -> None:
    """
    Delivery Manager profile.

    Intended for accumulated delivery folders where all tasks already passed
    SWELancer/Phase 1. In this mode Phase 2 should not re-block on runtime/build
    checks that were already validated. It should focus on client-facing package,
    metadata, debug, and hygiene issues.
    """
    assume_phase1_passed = _phase2_env_bool("PHASE2_ASSUME_PHASE1_PASSED", "1")
    hide_noisy = _phase2_env_bool("PHASE2_HIDE_NOISY_WARNINGS", "1")
    relax_metadata_all = _phase2_env_bool("PHASE2_RELAX_METADATA_PRICE", "0")

    price, title, has_payout_in_title = _issue_price_and_title(rep)

    drop_codes = set()

    # In lead pre-review mode, Expensify commit/worktree checks and runtime
    # gold/broken checks are intentionally skipped because SWELancer will run later.
    if _phase2_env_bool("PHASE2_SKIP_EXPENSIFY_CHECKS", "0"):
        drop_codes.add("EXPENSIFY_REPO_NOT_PROVIDED")

    if _phase2_env_bool("PHASE2_SKIP_RUNTIME_CHECKS", "0"):
        drop_codes.add("NO_VALIDATION_RUN_PROVIDED")

    if assume_phase1_passed:
        # Runtime/build/commit existence were already covered by Phase 1.
        drop_codes.update({
            "NO_VALIDATION_RUN_PROVIDED",
            "EXPENSIFY_REPO_NOT_PROVIDED",
        })

        # GitHub API fetch failure in auto mode is not task evidence.
        if ctx.github_check == "auto":
            drop_codes.add("GITHUB_FETCH_FAILED")

    if hide_noisy:
        # These produce huge noise and are not client blockers after Phase 1 passed.
        drop_codes.update({
            "HEADLESS_FALSE_LEFT",
            "SLOW_MO_LEFT",
            "COMMIT_REUSED_MANY_TIMES",
        })

    new_findings: list[Finding] = []

    for f in rep.findings:
        if f.code in drop_codes:
            continue

        # Avoid false positives from strings/comments such as
        # print("looking for username/email input field").
        # Only block real AST calls: input(...) or *.pause(...).
        if f.code == "INPUT_LEFT":
            test_path = _resolve_finding_path(ctx, f)
            real_lines = _real_python_call_lines(test_path, "input") if test_path else []
            if not real_lines:
                continue
            f = Finding(
                severity=f.severity,
                code=f.code,
                rubric=f.rubric,
                message=f"input() is left in test.py and will block automated eval. Real call line(s): {', '.join(map(str, real_lines))}.",
                path=f"{f.path}:{real_lines[0]}",
                hint=f.hint,
            )

        if f.code == "PAGE_PAUSE_LEFT":
            test_path = _resolve_finding_path(ctx, f)
            real_lines = _real_python_call_lines(test_path, "page_pause") if test_path else []
            if not real_lines:
                continue
            f = Finding(
                severity=f.severity,
                code=f.code,
                rubric=f.rubric,
                message=f"page.pause() is left in test.py and will hang automated eval. Real call line(s): {', '.join(map(str, real_lines))}.",
                path=f"{f.path}:{real_lines[0]}",
                hint=f.hint,
            )

        severity = f.severity

        # If SWELancer/Phase 1 executed the task successfully, these are QA warnings,
        # not blockers. Keep them visible for manual review but do not fail the gate.
        if assume_phase1_passed and f.severity == "ERROR" and f.code in {
            "NO_PYTEST_TEST_FUNCTION",
            "LIVE_OTP_HANDLER_IN_REPLAY_TEST",
        }:
            severity = "WARN"

        # Expensify/GitHub titles often change after payment to "Due for payment".
        # If issue_data.price is positive, missing [$XXX] in the title should not
        # block delivery; keep it as a metadata warning.
        if f.severity == "ERROR" and f.code == "TITLE_HAS_NO_PAYOUT":
            if relax_metadata_all or price > 0:
                severity = "WARN"

        # If title has [$XXX], a stale/missing price field can be recovered from
        # title, so warn instead of blocking. If both title and price are missing,
        # keep blocking.
        if f.severity == "ERROR" and f.code == "PRICE_NOT_POSITIVE":
            if relax_metadata_all or has_payout_in_title:
                severity = "WARN"

        if severity == f.severity:
            new_findings.append(f)
        else:
            new_findings.append(Finding(
                severity=severity,
                code=f.code,
                rubric=f.rubric,
                message=f.message,
                path=f.path,
                hint=f.hint,
            ))

    rep.findings = new_findings


def write_reports(ctx: Context, start_time: float) -> tuple[Path, Path, Path]:
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = ctx.out_dir / "phase2_summary.csv"
    json_path = ctx.out_dir / "phase2_full.json"
    md_path = ctx.out_dir / "phase2_report.md"

    rows: list[dict[str, Any]] = []
    for issue_id in sorted(ctx.reports, key=lambda x: int(x)):
        rep = ctx.reports[issue_id]
        row: dict[str, Any] = {
            "issue_id": issue_id,
            "status": rep.status,
            "errors": rep.error_count,
            "warnings": rep.warn_count,
        }
        for rubric in RUBRIC_ORDER:
            row[rubric] = rep.rubric_status(rubric)
        rows.append(row)

    with summary_csv.open("w", newline="", encoding="utf-8") as f:
        fieldnames = ["issue_id", "status", "errors", "warnings"] + RUBRIC_ORDER
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    all_data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "batch_dir": str(ctx.batch_dir),
        "kit_root": str(ctx.kit_root),
        "validation_run": str(ctx.validation_run) if ctx.validation_run else None,
        "expensify_dir": str(ctx.expensify_dir) if ctx.expensify_dir else None,
        "github_check": ctx.github_check,
        "runtime_seconds": round(time.time() - start_time, 2),
        "batch_findings": [f.__dict__ for f in ctx.batch_findings],
        "issues": {
            issue_id: {
                "status": rep.status,
                "errors": rep.error_count,
                "warnings": rep.warn_count,
                "metadata": rep.metadata,
                "rubrics": {rubric: rep.rubric_status(rubric) for rubric in RUBRIC_ORDER},
                "findings": [f.__dict__ for f in rep.findings],
            }
            for issue_id, rep in sorted(ctx.reports.items(), key=lambda kv: int(kv[0]))
        },
    }
    json_path.write_text(json.dumps(all_data, indent=2, ensure_ascii=False), encoding="utf-8")

    total_errors = sum(r.error_count for r in ctx.reports.values()) + sum(1 for f in ctx.batch_findings if f.severity == "ERROR")
    total_warnings = sum(r.warn_count for r in ctx.reports.values()) + sum(1 for f in ctx.batch_findings if f.severity == "WARN")
    pass_count = sum(1 for r in ctx.reports.values() if r.status == "PASS")
    warn_count = sum(1 for r in ctx.reports.values() if r.status == "WARN")
    fail_count = sum(1 for r in ctx.reports.values() if r.status == "FAIL")

    lines: list[str] = []
    lines.append("# Phase 2 Delivery QA Gate Report")
    lines.append("")
    lines.append(f"Generated: **{datetime.now().isoformat(timespec='seconds')}**")
    lines.append(f"Batch folder: `{ctx.batch_dir}`")
    lines.append(f"Validation run: `{ctx.validation_run}`" if ctx.validation_run else "Validation run: **not provided/found**")
    lines.append(f"Expensify repo checks: `{ctx.expensify_dir}`" if ctx.expensify_dir else "Expensify repo checks: **skipped**")
    lines.append(f"GitHub cross-check: **{ctx.github_check}**")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    lines.append(f"- Issues checked: **{len(ctx.reports)}**")
    lines.append(f"- PASS: **{pass_count}**")
    lines.append(f"- WARN only: **{warn_count}**")
    lines.append(f"- FAIL: **{fail_count}**")
    lines.append(f"- Blocking errors: **{total_errors}**")
    lines.append(f"- Non-blocking warnings: **{total_warnings}**")
    lines.append("")
    lines.append("## Gate Rubrics")
    lines.append("")
    lines.append("- **G1 Package completeness:** required files, no generated junk, no nested runtime outputs.")
    lines.append("- **G2 Metadata & issue consistency:** issue_data fields, price/title, full html_description, optional GitHub cross-check.")
    lines.append("- **G3 Commit/version reproducibility:** valid commit, version/git_tag risk, optional local commit existence.")
    lines.append("- **G4 Patch correctness & scope:** patch format, applies/reverses on fixed commit, no lockfiles/config/binary noise.")
    lines.append("- **G5 Test static quality:** pytest shape, proxy, local URL, assertions, OTP replay readiness, no debug pauses.")
    lines.append("- **G6 Flow replay quality:** flow size and recording sanity.")
    lines.append("- **G7 Runtime semantics:** fixed/gold passes; broken fails by assertion, not timeout/infrastructure.")
    lines.append("- **G8 Batch contamination:** duplicate test/flow/title/package copy-paste indicators.")
    lines.append("")

    if ctx.batch_findings:
        lines.append("## Batch-level Findings")
        lines.append("")
        for f in sorted(ctx.batch_findings, key=lambda x: -SEVERITY_ORDER[x.severity]):
            lines.append(f"- **{f.severity} {f.code}** ({f.rubric}): {f.message}")
            if f.path:
                lines.append(f"  - Path: `{f.path}`")
            if f.hint:
                lines.append(f"  - Fix: {f.hint}")
        lines.append("")

    lines.append("## Per-task Summary")
    lines.append("")
    header = "| Issue | Status | Errors | Warnings | " + " | ".join(r.split("-", 1)[0] for r in RUBRIC_ORDER) + " |"
    sep = "|---|---:|---:|---:|" + "---:|" * len(RUBRIC_ORDER)
    lines.append(header)
    lines.append(sep)
    for row in rows:
        vals = [row["issue_id"], row["status"], str(row["errors"]), str(row["warnings"])] + [row[r] for r in RUBRIC_ORDER]
        lines.append("| " + " | ".join(vals) + " |")
    lines.append("")

    lines.append("## Per-task Details")
    lines.append("")
    for issue_id in sorted(ctx.reports, key=lambda x: int(x)):
        rep = ctx.reports[issue_id]
        lines.append(f"### {issue_id} — {rep.status}")
        lines.append("")
        if not rep.findings:
            lines.append("No Phase 2 findings.")
            lines.append("")
            continue
        for f in sorted(rep.findings, key=lambda x: (-SEVERITY_ORDER[x.severity], x.rubric, x.code)):
            lines.append(f"- **{f.severity} {f.code}** ({f.rubric}): {f.message}")
            if f.path:
                lines.append(f"  - Path/log: `{f.path}`")
            if f.hint:
                lines.append(f"  - Fix: {f.hint}")
        lines.append("")

    lines.append("## Output Files")
    lines.append("")
    lines.append(f"- CSV summary: `{summary_csv}`")
    lines.append(f"- Full JSON: `{json_path}`")
    lines.append(f"- Markdown report: `{md_path}`")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_csv, json_path, md_path


def validate_paths(args: argparse.Namespace) -> tuple[Path, Path, Path | None, Path | None, Path]:
    kit_root = Path(args.kit_root).expanduser().resolve()
    batch_dir = Path(args.batch_dir).expanduser().resolve()
    if not batch_dir.exists():
        raise SystemExit(f"ERROR: batch-dir does not exist: {batch_dir}")
    if not kit_root.exists():
        raise SystemExit(f"ERROR: kit-root does not exist: {kit_root}")

    validation_run: Path | None = None
    validation_arg = str(args.validation_run or "").strip()
    if validation_arg:
        mode = validation_arg.lower()
        if mode == "latest":
            validation_run = resolve_latest_run(kit_root, args.batch_name)
        elif mode == "static":
            validation_run = None
        else:
            validation_run = Path(validation_arg).expanduser().resolve()
            if not validation_run.exists():
                raise SystemExit(f"ERROR: validation-run does not exist: {validation_run}")

    expensify_dir = Path(args.expensify_dir).expanduser().resolve() if args.expensify_dir else None
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else None
    if out_dir is None:
        if validation_run:
            out_dir = validation_run / "phase2_quality_gate"
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_dir = kit_root / "phase2_quality_gate_runs" / f"{stamp}_{args.batch_name or batch_dir.name}"
    return batch_dir, kit_root, validation_run, expensify_dir, out_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mandatory Phase 2 QA gate for SWE Freelancer delivery batches.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--batch-dir", required=True, help="Delivery batch folder containing numeric issue subfolders.")
    parser.add_argument("--kit-root", default=".", help="Root of turing-swe-freelancer-eval-kit.")
    parser.add_argument("--batch-name", default="", help="Batch name used to resolve latest delivery_validation_runs/*_<batch>.")
    parser.add_argument("--validation-run", default="static", help="Path to an existing delivery_validation_runs/<run> folder, 'latest', 'static', or empty. 'static'/empty skip runtime checks.")
    parser.add_argument("--expensify-dir", default=os.getenv("EXPENSIFY_DIR", ""), help="Optional local Expensify/App repo for commit and patch apply checks.")
    parser.add_argument("--github-check", choices=["off", "auto", "on"], default="auto", help="Cross-check title/body with GitHub issue API. 'on' makes fetch failure blocking.")
    parser.add_argument("--out-dir", default="", help="Output directory for Phase 2 logs/reports.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as nonzero exit too.")
    parser.add_argument("--timeout-sec", type=int, default=30, help="Timeout for each git/GitHub helper command.")
    args = parser.parse_args(argv)

    start = time.time()
    batch_dir, kit_root, validation_run, expensify_dir, out_dir = validate_paths(args)
    ctx = Context(
        batch_dir=batch_dir,
        kit_root=kit_root,
        out_dir=out_dir,
        validation_run=validation_run,
        expensify_dir=expensify_dir,
        github_check=args.github_check,
        strict=bool(args.strict),
        timeout_sec=args.timeout_sec,
    )

    issue_dirs = discover_issue_dirs(batch_dir)
    if not issue_dirs:
        ctx.add_batch("ERROR", "NO_NUMERIC_ISSUE_DIRS", "G1-package-completeness", f"No numeric issue folders found under {batch_dir}.", str(batch_dir), "Pass the unzipped delivery batch root, not a parent folder.")

    for issue_dir in issue_dirs:
        rep = IssueReport(issue_id=issue_dir.name, path=issue_dir)
        ctx.reports[rep.issue_id] = rep
        check_package_completeness(ctx, rep)
        check_issue_metadata(ctx, rep)
        check_commit_and_version(ctx, rep)
        check_patch(ctx, rep)
        check_test_static(ctx, rep)
        check_flow(ctx, rep)
        check_runtime_results(ctx, rep)

    check_batch_duplicates(ctx)

    # Apply delivery-manager normalization after all checks, including duplicate checks.
    for rep in ctx.reports.values():
        normalize_findings_for_delivery_manager(ctx, rep)

    summary_csv, json_path, md_path = write_reports(ctx, start)

    total_errors = sum(r.error_count for r in ctx.reports.values()) + sum(1 for f in ctx.batch_findings if f.severity == "ERROR")
    total_warnings = sum(r.warn_count for r in ctx.reports.values()) + sum(1 for f in ctx.batch_findings if f.severity == "WARN")
    pass_count = sum(1 for r in ctx.reports.values() if r.status == "PASS")
    warn_count = sum(1 for r in ctx.reports.values() if r.status == "WARN")
    fail_count = sum(1 for r in ctx.reports.values() if r.status == "FAIL")

    print("\n========== PHASE 2 QA GATE SUMMARY ==========")
    print(f"Batch dir        : {batch_dir}")
    print(f"Validation run   : {validation_run or 'SKIPPED'}")
    print(f"Output dir       : {out_dir}")
    print(f"Issues checked   : {len(ctx.reports)}")
    print(f"PASS/WARN/FAIL   : {pass_count}/{warn_count}/{fail_count}")
    print(f"Errors/Warnings  : {total_errors}/{total_warnings}")
    print(f"CSV summary      : {summary_csv}")
    print(f"Markdown report  : {md_path}")
    print(f"Full JSON        : {json_path}")
    print("============================================")

    if total_errors:
        return 20
    if args.strict and total_warnings:
        return 10
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
