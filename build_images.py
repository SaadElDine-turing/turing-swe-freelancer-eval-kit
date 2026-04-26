#!/usr/bin/env python3

"""
Build the x86 base Docker image, then one or more per-issue images.

Usage
-----
    ./build_images.py                        # build the base image (tag: latest), then all issues/* with 4 workers
    ./build_images.py 42 43 44               # build the base image and the given issues (default tag: latest)
    ./build_images.py --tag v1.2.3            # build and tag images with :v1.2.3
    ./build_images.py 28565_1001 --tag beta   # build issue 28565_1001 only with tag :beta
    ./build_images.py -w 1                    # same as above but sequential (workers = 1)
    ./build_images.py -w 8                    # use up to 8 concurrent workers
    ./build_images.py --clean-up              # build all issues and delete per-issue images after push
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def get_root() -> Path:
    """Get the root directory of the repository."""

    return Path(__file__).resolve().parent.parent


def run(cmd: list[str], cwd: Path) -> None:
    """Wrapper around subprocess.run with `check=True`."""

    subprocess.run(cmd, cwd=cwd, check=True)


def get_current_builder() -> str | None:
    """Return the currently selected buildx builder name, if available."""

    res = subprocess.run(
        ["docker", "buildx", "ls"],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None

    for line in res.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        token = parts[0]
        if token.startswith("NAME/") or token == "\\_":
            continue
        if token.endswith("*"):
            return token[:-1]
    return None


def inspect_builder(builder: str) -> tuple[str | None, bool | None]:
    """Inspect a buildx builder and return (driver, auto_load_images_to_engine)."""

    res = subprocess.run(
        ["docker", "buildx", "inspect", builder],
        check=False,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None, None

    driver: str | None = None
    auto_load: bool | None = None
    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("Driver:"):
            driver = line.split(":", 1)[1].strip()
        elif "Automatically load images to the Docker Engine image store:" in line:
            value = line.rsplit(":", 1)[1].strip().lower()
            if value in {"true", "false"}:
                auto_load = value == "true"

    return driver, auto_load


def pick_builder(explicit_builder: str | None) -> str | None:
    """Pick a builder that can see locally loaded images used by per-task Dockerfiles."""

    chosen = explicit_builder or os.environ.get("SWELANCER_DOCKER_BUILDER")
    if chosen:
        driver, auto_load = inspect_builder(chosen)
        if driver is None:
            raise RuntimeError(f"Requested buildx builder '{chosen}' is not available")
        logging.info("Using buildx builder %s (driver=%s, auto-load=%s)", chosen, driver, auto_load)
        if driver != "docker" and auto_load is not True:
            logging.warning(
                "Builder '%s' may not resolve local base images. "
                "If you hit 'pull access denied', use --builder desktop-linux or --builder default.",
                chosen,
            )
        return chosen

    current = get_current_builder()
    candidates: list[str] = []
    if current:
        candidates.append(current)
    for fallback in ("desktop-linux", "default"):
        if fallback not in candidates:
            candidates.append(fallback)

    inspected: list[tuple[str, str, bool | None]] = []
    for candidate in candidates:
        driver, auto_load = inspect_builder(candidate)
        if driver is not None:
            inspected.append((candidate, driver, auto_load))

    for candidate, driver, auto_load in inspected:
        if driver == "docker" or auto_load is True:
            if current and candidate != current:
                logging.info(
                    "Current buildx builder '%s' is not ideal for local image reuse; switching to '%s'.",
                    current,
                    candidate,
                )
            logging.info(
                "Using buildx builder %s (driver=%s, auto-load=%s)",
                candidate,
                driver,
                auto_load,
            )
            return candidate

    if inspected:
        candidate, driver, auto_load = inspected[0]
        logging.warning(
            "No builder with automatic Docker image loading detected. Using '%s' (driver=%s, auto-load=%s).",
            candidate,
            driver,
            auto_load,
        )
        return candidate

    logging.warning("Could not inspect any buildx builder; using Docker default builder selection.")
    return None


def buildx_cmd(builder: str | None) -> list[str]:
    """Construct a docker buildx command with an optional builder override."""

    cmd = ["docker", "buildx", "build"]
    if builder:
        cmd += ["--builder", builder]
    return cmd


def build_base_image(arch: str, builder: str | None) -> None:
    """Build the common base image (swelancer_[x86|aarch64])."""

    logging.info(f"Building Docker base image swelancer_{arch}")
    dockerfile_name = f"Dockerfile_{arch}_base"
    dockerfile = get_root() / dockerfile_name

    assert dockerfile.is_file(), f"Required Dockerfile not found: {dockerfile}"

    cmd = buildx_cmd(builder) + [
        "-f",
        dockerfile_name,
        "--platform",
        "linux/amd64" if arch == "x86" else "linux/arm64",
        "--load",
    ]

    ssh_sock = os.environ.get("SSH_AUTH_SOCK")

    if ssh_sock:
        cmd += ["--ssh", f"default={ssh_sock}"]

    cmd += ["-t", f"swelancer_{arch}:latest", "."]

    run(cmd, cwd=get_root())


def build_issue_image(issue_id: str, tag: str, arch: str, builder: str | None) -> None:
    """Build the per-issue image (swelancer_[x86|aarch64]_<ISSUE_ID>)."""

    logging.info("Building Docker image for issue %s", issue_id)
    dockerfile_name = f"Dockerfile_{arch}_per_task"
    dockerfile = get_root() / dockerfile_name

    assert dockerfile.is_file(), f"Required Dockerfile not found: {dockerfile}"

    cmd = buildx_cmd(builder) + [
        "--build-arg",
        f"ISSUE_ID={issue_id}",
        "-f",
        dockerfile_name,
        "--platform",
        "linux/amd64" if arch == "x86" else "linux/arm64",
        "--load",
        "-t",
        f"swelancer_{arch}_{issue_id}:{tag}",
        ".",
    ]

    run(cmd, cwd=get_root())


def build_monolith_image(tag: str, arch: str, builder: str | None) -> None:
    """Build the monolith image"""

    logging.info("Building Monolith Docker image")
    dockerfile_name = f"Dockerfile_{arch}_monolith"
    dockerfile = get_root() / dockerfile_name

    assert dockerfile.is_file(), f"Required Dockerfile not found: {dockerfile}"

    cmd = buildx_cmd(builder) + [
        "-f",
        dockerfile_name,
        "--platform",
        "linux/amd64" if arch == "x86" else "linux/arm64",
        "--load",
        "-t",
        f"swelancer_{arch}_monolith:{tag}",
        ".",
    ]

    run(cmd, cwd=get_root())


def push_image(issue_id: str, tag: str, registry: str, arch: str) -> None:
    """Push the per-issue image to the container registry."""

    logging.info("Pushing image for issue %s to %s", issue_id, registry)

    local_tag = f"swelancer_{arch}_{issue_id}:{tag}"
    remote_tag = f"{registry}/swelancer_{arch}_{issue_id}:{tag}"

    run(["docker", "tag", local_tag, remote_tag], cwd=get_root())
    run(["docker", "push", remote_tag], cwd=get_root())


def push_monolith_image(tag: str, registry: str, arch: str) -> None:
    """Push the monolith image to the container registry."""

    logging.info("Pushing monolith image to %s", registry)

    local_tag = f"swelancer_{arch}_monolith:{tag}"
    remote_tag = f"{registry}/swelancer_{arch}_monolith:{tag}"

    run(["docker", "tag", local_tag, remote_tag], cwd=get_root())
    run(["docker", "push", remote_tag], cwd=get_root())


def issue_worker(
    issue_id: str, push: bool, cleanup: bool, tag: str, registry: str, arch: str, builder: str | None
) -> None:
    logging.info("Worker started for issue %s", issue_id)

    build_issue_image(issue_id, tag, arch, builder)
    if push:
        push_image(issue_id, tag, registry, arch)

    if cleanup:
        logging.info("Removing image swelancer_%s_%s:%s", arch, issue_id, tag)
        run(["docker", "rmi", f"swelancer_{arch}_{issue_id}:{tag}"], cwd=get_root())


def monolith_worker(push: bool, cleanup: bool, tag: str, registry: str, arch: str, builder: str | None) -> None:
    logging.info("Building monolithic image swelancer_%s_monolith", arch)

    build_monolith_image(tag, arch, builder)
    if push:
        push_monolith_image(tag, registry, arch)

    if cleanup:
        logging.info("Removing image swelancer_%s_monolith:%s", arch, tag)
        run(["docker", "rmi", f"swelancer_{arch}_monolith:{tag}"], cwd=get_root())


def worker(
    issue_id: str, push: bool, cleanup: bool, tag: str, registry: str, arch: str, builder: str | None
) -> None:
    """Build an issue image, push it, and optionally remove it afterwards."""

    if issue_id == "monolith":
        monolith_worker(push, cleanup, tag, registry, arch, builder)
    else:
        issue_worker(issue_id, push, cleanup, tag, registry, arch, builder)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the base image and per-issue images, tagging them with the specified tag (default: latest)."
    )
    parser.add_argument(
        "issue_ids",
        nargs="*",
        metavar="ISSUE_ID",
        help="Optional one or more ISSUE_IDs (otherwise builds all issues/*)",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=4,
        help="Number of concurrent workers (default: 4; use 1 for sequential execution).",
    )
    parser.add_argument(
        "--tag",
        default="latest",
        help="Tag to apply to built images (prefix ':' optional, default: latest)",
    )
    parser.add_argument(
        "-c",
        "--clean-up",
        dest="cleanup",
        action="store_true",
        help="Delete per-issue images after a successful build.",
    )
    parser.add_argument(
        "-a",
        "--arch",
        dest="arch",
        type=str,
        choices=["x86", "aarch64"],
        default="x86",
        help="Sets Docker image architecture (default: x86).",
    )
    parser.add_argument(
        "--skip-push",
        dest="skip_push",
        action="store_true",
        help="Skip pushing images to the container registry",
    )
    parser.add_argument(
        "--registry",
        help="Container registry (required unless --skip-push is passed)",
    )
    parser.add_argument(
        "--builder",
        help=(
            "Optional docker buildx builder name. "
            "If omitted, script auto-selects a local-image-friendly builder when possible."
        ),
    )
    args = parser.parse_args()

    if not args.skip_push and not args.registry:
        parser.error("--registry is required unless --skip-push is passed")

    tag = args.tag.lstrip(":")
    registry = args.registry.rstrip("/") if args.registry else ""
    issues_dir = get_root() / "issues"

    if not issues_dir.is_dir():
        sys.exit("No issues/ directory found")

    builder = pick_builder(args.builder)
    build_base_image(args.arch, builder)

    issue_ids = args.issue_ids or [p.name for p in sorted(issues_dir.iterdir()) if p.is_dir()]

    push = not args.skip_push
    cleanup = args.cleanup

    if args.workers <= 1 or len(issue_ids) <= 1:
        for issue in issue_ids:
            worker(issue, push, cleanup, tag, registry, args.arch, builder)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            pool.map(
                lambda iid: worker(iid, push, cleanup, tag, registry, args.arch, builder),
                issue_ids,
            )


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, Exception) as exc:
        sys.exit(str(exc))
