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

## Validate a Batch

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
