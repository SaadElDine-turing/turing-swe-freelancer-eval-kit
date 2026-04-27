#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


BLOCKER_PRIORITY = {
    "INPUT_LEFT": "P0",
    "PAGE_PAUSE_LEFT": "P0",
    "BREAKPOINT_LEFT": "P0",
    "PDB_LEFT": "P0",
    "EMPTY_REQUIRED_FILE": "P0",
    "MISSING_REQUIRED_FILE": "P0",
    "BAD_GENERATED_FILE": "P1",
    "PRICE_NOT_POSITIVE": "P1",
    "TITLE_HAS_NO_PAYOUT": "P1",
}

FIX_HINTS = {
    "INPUT_LEFT": "Remove/comment input() from test.py. It blocks automated execution.",
    "PAGE_PAUSE_LEFT": "Remove page.pause() from test.py. It hangs automated execution.",
    "EMPTY_REQUIRED_FILE": "Replace the empty required file with the correct delivered artifact.",
    "MISSING_REQUIRED_FILE": "Copy/regenerate the missing required file before delivery.",
    "BAD_GENERATED_FILE": "Delete generated OS/cache files such as .DS_Store.",
    "PRICE_NOT_POSITIVE": "Update issue_data.json price using the real payout amount.",
    "TITLE_HAS_NO_PAYOUT": "Update issue_data.json title to include the payout marker, or confirm metadata source.",
    "NO_PYTEST_TEST_FUNCTION": "Since Phase 1 passed, keep as warning unless client requires pytest shape.",
    "LIVE_OTP_HANDLER_IN_REPLAY_TEST": "Since Phase 1 replay passed, keep as warning unless replay instability appears.",
    "NON_DETERMINISTIC_EMAIL": "Prefer deterministic derived emails tied to issue ID for replay stability.",
    "WEAK_SHORT_TEXT_SELECTOR": "Prefer test IDs, roles, or stronger selectors if task is flaky.",
    "PATCH_TOUCHES_TEST_FILE": "Manually verify bug_reintroduce.patch only reintroduces the app bug.",
    "PATCH_TOUCHES_MANY_FILES": "Manually verify broad patch scope is only undoing the fix.",
    "UNEXPECTED_FILE": "Remove unnecessary files unless they are required by the kit.",
}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True, help="Path to phase2_full.json")
    ap.add_argument("--show-warnings", action="store_true")
    args = ap.parse_args()

    path = Path(args.json).expanduser().resolve()
    data = load_json(path)

    issues = data.get("issues", {})
    status_counts = Counter(v.get("status", "UNKNOWN") for v in issues.values())

    error_counts = Counter()
    warning_counts = Counter()
    failed_tasks = defaultdict(list)
    warning_tasks = defaultdict(list)

    for issue_id, rec in issues.items():
        for f in rec.get("findings", []):
            sev = f.get("severity")
            code = f.get("code", "UNKNOWN")
            item = {
                "code": code,
                "message": f.get("message", ""),
                "path": f.get("path", ""),
                "rubric": f.get("rubric", ""),
                "hint": f.get("hint", ""),
            }
            if sev == "ERROR":
                error_counts[code] += 1
                failed_tasks[issue_id].append(item)
            elif sev == "WARN":
                warning_counts[code] += 1
                warning_tasks[issue_id].append(item)

    print()
    print("========== PHASE 2 ACTIONABLE RESULT ==========")
    print(f"Report JSON       : {path}")
    print(f"Batch dir         : {data.get('batch_dir')}")
    print(f"Validation run    : {data.get('validation_run') or 'SKIPPED / trusted from Phase 1'}")
    print(f"GitHub check      : {data.get('github_check')}")
    print(f"Issues checked    : {len(issues)}")
    print(f"PASS/WARN/FAIL    : {status_counts.get('PASS', 0)}/{status_counts.get('WARN', 0)}/{status_counts.get('FAIL', 0)}")
    print(f"Blocking errors   : {sum(error_counts.values())}")
    print(f"Warnings          : {sum(warning_counts.values())}")

    decision = "BLOCKED" if error_counts else "READY WITH WARNINGS" if warning_counts else "READY"
    print(f"Delivery decision : {decision}")
    print("===============================================")

    if error_counts:
        print()
        print("BLOCKING ERROR COUNTS")
        for code, n in error_counts.most_common():
            print(f"  {n:3}  {code:<28} {BLOCKER_PRIORITY.get(code, 'P2')}")

        print()
        print("FAILED TASKS / REQUIRED ACTIONS")
        for issue_id in sorted(failed_tasks, key=lambda x: int(x) if x.isdigit() else x):
            print(f"\n  Issue {issue_id}")
            for f in failed_tasks[issue_id]:
                code = f["code"]
                print(f"    [{BLOCKER_PRIORITY.get(code, 'P2')}] {code}")
                print(f"        Path : {f['path']}")
                print(f"        Why  : {f['message']}")
                print(f"        Fix  : {FIX_HINTS.get(code, f.get('hint') or 'Review manually.')}")

    if warning_counts:
        print()
        print("WARNING COUNTS")
        for code, n in warning_counts.most_common():
            print(f"  {n:3}  {code}")

        if args.show_warnings:
            print()
            print("WARNING TASKS")
            for issue_id in sorted(warning_tasks, key=lambda x: int(x) if x.isdigit() else x):
                print(f"\n  Issue {issue_id}")
                for f in warning_tasks[issue_id]:
                    print(f"    [WARN] {f['code']} — {f['path']}")
                    print(f"           {f['message']}")

    print()
    print("NEXT COMMANDS")
    print("  # Inspect blocking debug leftovers")
    print("  grep -RInE '\\\\binput\\\\s*\\\\(|page\\\\.pause\\\\s*\\\\(' batch_2/48761/test.py batch_2/73686/test.py batch_2/77034/test.py batch_2/84220/test.py")
    print()
    print("  # Inspect empty flows")
    print("  ls -lh batch_2/61275/flow.mitm batch_2/74746/flow.mitm")
    print()
    print("  # Remove generated OS junk")
    print("  find batch_2 -name '.DS_Store' -delete")
    print()

    return 20 if error_counts else 0


if __name__ == "__main__":
    raise SystemExit(main())
