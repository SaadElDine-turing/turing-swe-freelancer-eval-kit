# SWELancer Delivery Kit

This repository contains issue definitions and a reproducible validation kit for the [SWELancer](http://github.com/openai/frontier-evals/tree/main/project/swelancer) benchmark.

## Team Setup (macOS, Linux, Windows)

All commands below should be run in:
- Terminal on macOS/Linux
- Ubuntu WSL shell on Windows (recommended)

### 1. Install Required Tools

#### macOS

```bash
brew install git gh
brew install --cask docker
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### Linux (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y git gh docker.io docker-buildx-plugin curl
sudo usermod -aG docker "$USER"
newgrp docker
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### Windows (recommended: WSL2 + Ubuntu)

Run once in PowerShell (as Administrator):

```powershell
wsl --install -d Ubuntu
```

Then:
- Install Docker Desktop on Windows.
- In Docker Desktop, enable WSL integration for Ubuntu.
- Open Ubuntu from Start menu and run the Linux commands above.

### 2. Clone the Repo

```bash
git clone https://github.com/<ORG_OR_USER>/<REPO>.git
cd <REPO>
```

### 3. Run Setup

```bash
chmod +x setup_frontier_evals.sh validate_delivery_batch.sh add_issue.sh
./setup_frontier_evals.sh
```

This script:
- Clones the `frontier-evals` repository and pins to a known commit.
- Applies all patches (`changes.patch`, `runtime_fixes.patch`, `aarch64_support.patch`, `lock_runtime_fixes.patch`).
- Copies issue definitions into SWELancer.
- Updates `all_swelancer_tasks.csv` with task metadata.
- Syncs root runtime overrides into `frontier-evals` (`setup_expensify.yml`, `run_tests.yml`, `conftest.py`).
- Installs dependencies using `uv`.

The script is idempotent. Re-running it is safe.

Important: `setup_frontier_evals.sh` may run `git reset --hard` and `git clean -fd` inside `frontier-evals`. Treat files under this repo root as the source of truth and avoid manual edits inside `frontier-evals/`.

### 4. Quick Verification

```bash
test -d frontier-evals/project/swelancer && echo "Setup complete"
docker --version
uv --version
```

## Publish This Folder to GitHub (Repo Owner)

You said you will create the repository in GitHub UI manually. After creating an empty repo, run these exact commands from this folder.

### 1. Login to GitHub

```bash
gh auth login --hostname github.com --git-protocol https --web
gh auth status
```

### 2. Initialize, Commit, and Push

```bash
cd /absolute/path/to/turing-swe-freelancer-eval-kit

git init
git config --global user.name "Your Name"
git config --global user.email "you@company.com"
git add .
git commit -m "Initial commit"

git branch -M main
git remote add origin https://github.com/<ORG_OR_USER>/<REPO>.git
git push -u origin main
```

If `origin` already exists:

```bash
git remote set-url origin https://github.com/<ORG_OR_USER>/<REPO>.git
git push -u origin main
```

## What You Received

- A task batch under `issues/`, where each numeric folder (`issues/<issue_id>/`) is one SWELancer task representing a real Expensify bug-fix scenario.
- A reproducible setup script (`setup_frontier_evals.sh`) that prepares dependencies and applies required runtime patches.
- A batch validator (`validate_delivery_batch.sh`) that builds images, runs gold/broken checks, and generates per-task verdicts.

Count tasks in this package:

```bash
find issues -mindepth 1 -maxdepth 1 -type d | wc -l
```

## Validate a Delivery Batch

Use `validate_delivery_batch.sh` to run end-to-end validation for all issue folders in a delivery batch.

```bash
KIT_ROOT=/path/to/turing-swe-freelancer-eval-kit
BATCH_DIR=/path/to/unzipped_batch_folder
BATCH_NAME=batch_name

cd "$KIT_ROOT"
WORK="$KIT_ROOT" ./validate_delivery_batch.sh "$BATCH_DIR" "$BATCH_NAME"
```

Force x86 explicitly (for example on Linux CI):

```bash
cd "$KIT_ROOT"
WORK="$KIT_ROOT" SWELANCER_ARCH=x86 ./validate_delivery_batch.sh "$BATCH_DIR" "$BATCH_NAME"
```

What it does:
- Phases 1-4: intake, repo sync, CSV registration check, per-task image builds.
- Phases 5-6: gold and broken validation runs.
- Phase 7: targeted rerun for dual-timeout candidates.
- Phase 8: summary tables and final report artifacts.
- Auto-detects host architecture (`x86_64/amd64 -> x86`, `arm64/aarch64 -> aarch64`) for image build/run.

Architecture controls (optional):
- `SWELANCER_ARCH=auto|x86|aarch64` (default: `auto`)
- `SWELANCER_DOCKER_IMAGE_PREFIX=...` (default: `swelancer_<resolved_arch>`)

Where results go:
- `delivery_validation_runs/<timestamp>_<batch_name>/driver.log`
- `delivery_validation_runs/<timestamp>_<batch_name>/validation_report.md`
- `delivery_validation_runs/<timestamp>_<batch_name>/summary.csv`
- `delivery_validation_runs/<timestamp>_<batch_name>/per_task/<issue_id>/...`

`delivery_validation_runs/` is generated output from local validation runs and can be regenerated at any time.

Verdict note:
- `NEED_MANUAL_RUN` means both gold and broken produced infrastructure-style timeout signals and require a manual verification rerun.

## Timeout Policy

This kit includes timeout hardening in root-level runtime overrides, and `setup_frontier_evals.sh` re-copies them on every setup run.

- `run_tests.yml`
  - `wait_for` timeout for mitmproxy (`127.0.0.1:8080`) is `300` seconds.
  - `wait_for` timeout for Expensify dev server (`127.0.0.1:8082`) is `300` seconds.
- `conftest.py`
  - Global Playwright timeout floor is `300000` ms (5 minutes).
  - `Page.goto` is monkey-patched (sync and async) to enforce at least the floor timeout.
  - `SWELANCER_PLAYWRIGHT_TIMEOUT_MS` can increase the timeout, but cannot lower it below `300000` ms.

## Adding a New Issue

Use `add_issue.sh` to add new issues to the eval pipeline.

From an existing directory (example):

```bash
./add_issue.sh 12345 --from ../swe-freelancer-kit/tests/12345
```

Scaffold from GitHub:

```bash
./add_issue.sh 12345
```

This creates a skeleton directory with `issue_data.json` fetched from GitHub. You still need to fill in `test.py`, `flow.mitm`, `bug_reintroduce.patch`, and `commit_id.txt`.

The script automatically:
- Copies only required files (filters out `__pycache__`, extra flow files, and other noise).
- Creates `revert_command.txt` and `git_tag.txt` if missing.
- Syncs the issue to `frontier-evals/project/swelancer/issues/`.
- Adds a row to `all_swelancer_tasks.csv`.

## Build Docker Image for an Issue

On macOS (Apple Silicon):

```bash
cd frontier-evals/project/swelancer
uv run python scripts/build_images.py 78192 --skip-push --arch aarch64
```

On Linux (x86):

```bash
cd frontier-evals/project/swelancer
uv run python scripts/build_images.py 78192 --skip-push
```

This builds the base image first (cached after first run), then the per-issue image. The first build takes a while because it downloads dependencies.

If you modified runtime files (`online_guard.py`, `replay.py`), rebuild the base image with `--no-cache` to pick up changes:

```bash
docker buildx build --no-cache -f Dockerfile_aarch64_base --platform linux/arm64 -t swelancer_aarch64:latest .
```

## Run SWELancer with Gold Patch

This runs the eval for an issue using the gold (correct) solution to verify the task end to end.

On macOS (Apple Silicon):

```bash
KIT_ROOT=/path/to/turing-swe-freelancer-eval-kit
cd "$KIT_ROOT/frontier-evals/project/swelancer"

ALCATRAZ_LOCK_ROOT="$KIT_ROOT/.runtime/alcatraz" \
NANOEVAL_PORTS_DIR="$KIT_ROOT/.runtime/nanoeval/ports" \
uv run python swelancer/run_swelancer.py \
  swelancer.split=diamond \
  swelancer.task_type=ic_swe \
  swelancer.taskset="['78192']" \
  swelancer.disable_internet=False \
  swelancer.solver=swelancer.solvers.dummy.solver:DummySolver \
  swelancer.solver.test_user_tool=False \
  swelancer.solver.apply_gold_solution=True \
  swelancer.solver.computer_runtime=nanoeval_alcatraz.alcatraz_computer_interface:AlcatrazComputerRuntime \
  swelancer.solver.computer_runtime.env=alcatraz.clusters.local:LocalConfig \
  swelancer.solver.computer_runtime.env.pull_from_registry=False \
  swelancer.docker_image_prefix=swelancer_aarch64 \
  swelancer.docker_image_tag=latest \
  runner.concurrency=1 \
  runner.experimental_use_multiprocessing=False \
  runner.enable_slackbot=False \
  runner.recorder=nanoeval.recorder:dummy_recorder \
  runner.max_retries=2
```

On Linux (x86), run the same command and change:

```text
swelancer.docker_image_prefix=swelancer_x86
```

Check results under `frontier-evals/project/swelancer/runs/<timestamp>/`.

## Running Multiple Issues

Build and run multiple issues:

```bash
cd frontier-evals/project/swelancer

# Build all issues
uv run python scripts/build_images.py --skip-push --arch aarch64

# Run selected issues
uv run python swelancer/run_swelancer.py \
  swelancer.taskset="['78192', '79442']" \
  ...
```

## Troubleshooting

| Problem | Solution |
|---|---|
| Docker build fails with architecture error | Use `--arch aarch64` on Apple Silicon Mac |
| `aarch64_support.patch` fails (files already exist) | Re-run `setup_frontier_evals.sh`; patching is guarded and safe to retry |
| `uv: command not found` | Install uv: `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `setup_frontier_evals.sh` permission denied | Run `chmod +x setup_frontier_evals.sh` |
| Docker build hangs or is very slow | First build downloads large dependencies; this is expected |
| `AttributeError: 'float' object has no attribute 'encode'` | `description` field in CSV is empty. Ensure `issue_data.json` has `html_description` or `issue_repo_steps` |
| Batch appears stuck at Phase 5 after dummy recorder message | Ensure writable lock dirs are used (`ALCATRAZ_LOCK_ROOT` and `NANOEVAL_PORTS_DIR`) and re-run |
| Permission denied under `~/.alcatraz` or `/tmp/nanoeval/ports` | Use writable lock env vars or run through `validate_delivery_batch.sh` |
| `page.goto: Timeout 300000ms exceeded` | Startup is still not ready within 5 minutes; inspect `npm_run_dev.log` |
| Runtime changes not taking effect | Rebuild base image with `--no-cache` (Docker cache can serve stale layers) |

## Project Structure

```text
turing-swe-freelancer-eval-kit/
├── README.md                   # This file
├── setup_frontier_evals.sh     # Setup script (idempotent)
├── validate_delivery_batch.sh  # Batch validator (gold + broken + summary)
├── add_issue.sh                # Add new issues to the pipeline
├── changes.patch               # Patches for frontier-evals
├── runtime_fixes.patch         # Online guard + replay header fixes
├── aarch64_support.patch       # Mac ARM64 Docker support
├── lock_runtime_fixes.patch    # Writable lock path fixes for nanoeval/alcatraz
├── setup_expensify.yml         # Runtime setup override copied into frontier-evals
├── run_tests.yml               # Runtime test-runner override copied into frontier-evals
├── conftest.py                 # Playwright behavior override copied into frontier-evals
└── issues/                     # Issue definitions
    └── <issue_id>/
        ├── test.py                 # Playwright test
        ├── flow.mitm               # Recorded network traffic for replay
        ├── bug_reintroduce.patch   # Patch to reintroduce the bug
        ├── commit_id.txt           # Git commit for the Expensify repo
        ├── git_tag.txt             # Optional version override
        ├── issue_data.json         # Issue metadata (title, price, etc.)
        └── revert_command.txt      # Optional revert command
```

## Issue Format

The issues follow the same format as the official [SWELancer project](http://github.com/openai/frontier-evals/tree/main/project/swelancer), ensuring compatibility with the SWELancer evaluation framework.
