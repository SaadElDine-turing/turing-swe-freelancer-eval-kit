#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s nullglob

WORK="${WORK:-$HOME/work/swe-freelancer-eval-colleague}"
BATCH_DIR="${1:-}"
BATCH_NAME="${2:-}"
ARCH_INPUT="${SWELANCER_ARCH:-auto}"

if [[ -z "$BATCH_DIR" ]]; then
  echo "Usage: $(basename "$0") /path/to/unzipped_batch_folder [batch_name]"
  exit 2
fi

if [[ ! -d "$WORK" ]]; then
  echo "ERROR: WORK does not exist: $WORK"
  exit 2
fi

if [[ ! -d "$BATCH_DIR" ]]; then
  echo "ERROR: batch folder does not exist: $BATCH_DIR"
  exit 2
fi

ARCH_INPUT_LC="$(printf '%s' "$ARCH_INPUT" | tr '[:upper:]' '[:lower:]')"
if [[ "$ARCH_INPUT_LC" == "auto" || -z "$ARCH_INPUT_LC" ]]; then
  HOST_ARCH="$(uname -m | tr '[:upper:]' '[:lower:]')"
  case "$HOST_ARCH" in
    arm64|aarch64)
      SWELANCER_ARCH="aarch64"
      ;;
    x86_64|amd64)
      SWELANCER_ARCH="x86"
      ;;
    *)
      echo "ERROR: Unsupported host architecture '$HOST_ARCH'. Set SWELANCER_ARCH to x86 or aarch64."
      exit 2
      ;;
  esac
else
  case "$ARCH_INPUT_LC" in
    x86|amd64|x86_64)
      SWELANCER_ARCH="x86"
      ;;
    arm64|aarch64)
      SWELANCER_ARCH="aarch64"
      ;;
    *)
      echo "ERROR: Invalid SWELANCER_ARCH='$ARCH_INPUT'. Use one of: auto, x86, aarch64."
      exit 2
      ;;
  esac
fi

if [[ "$SWELANCER_ARCH" == "x86" ]]; then
  DOCKER_PLATFORM="linux/amd64"
else
  DOCKER_PLATFORM="linux/arm64"
fi

DOCKER_IMAGE_PREFIX="${SWELANCER_DOCKER_IMAGE_PREFIX:-swelancer_${SWELANCER_ARCH}}"

ROOT_SWEL="$WORK/frontier-evals/project/swelancer"
SETUP_SH="$WORK/setup_frontier_evals.sh"
if [[ ! -x "$SETUP_SH" ]]; then
  echo "ERROR: setup script is missing or not executable: $SETUP_SH"
  exit 2
fi
if [[ ! -d "$ROOT_SWEL" ]]; then
  echo "ERROR: SWELancer folder not found: $ROOT_SWEL"
  exit 2
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
SAFE_BATCH_NAME="${BATCH_NAME:-$(basename "$BATCH_DIR")}"
SAFE_BATCH_NAME="${SAFE_BATCH_NAME// /_}"
OUT="$WORK/delivery_validation_runs/${STAMP}_${SAFE_BATCH_NAME}"
mkdir -p "$OUT" "$OUT/per_task" "$OUT/raw"

MASTER_LOG="$OUT/driver.log"
RUN_ROOT="$ROOT_SWEL/runs"
mkdir -p "$RUN_ROOT"
DEFAULT_N_TEST_RUNS="${SWELANCER_N_TEST_RUNS:-3}"
MANUAL_N_TEST_RUNS="${SWELANCER_MANUAL_N_TEST_RUNS:-1}"
JUPYTER_KERNEL_TIMEOUT_SEC="${ALCATRAZ_JUPYTER_KERNEL_STARTUP_TIMEOUT_SEC:-300}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$MASTER_LOG"
}

section() {
  echo | tee -a "$MASTER_LOG"
  printf '========== %s ==========' "$*" | tee -a "$MASTER_LOG"
  echo | tee -a "$MASTER_LOG"
}

fail_trap() {
  log "FATAL: Script failed on line $1. See $MASTER_LOG"
}
trap 'fail_trap $LINENO' ERR

require_core_files=(
  "test.py"
  "bug_reintroduce.patch"
  "commit_id.txt"
  "issue_data.json"
  "flow.mitm"
)

section "Delivery batch validation started"
log "Workspace      : $WORK"
log "Batch folder    : $BATCH_DIR"
log "Output folder   : $OUT"
log "SWELancer root  : $ROOT_SWEL"
log "Runtime arch    : $SWELANCER_ARCH ($DOCKER_PLATFORM)"
log "Image prefix    : $DOCKER_IMAGE_PREFIX"
log "Kernel timeout  : ${JUPYTER_KERNEL_TIMEOUT_SEC}s"

# ------------------------------
# 1) Intake + staging
# ------------------------------
section "Phase 1/8 - Intake and staging"

declare -a ALL_IDS=()
batch_dirs_file="$(mktemp)"
find "$BATCH_DIR" -mindepth 1 -maxdepth 1 -type d -print > "$batch_dirs_file"
sort "$batch_dirs_file" -o "$batch_dirs_file"

while IFS= read -r issue_path; do
  issue_id="$(basename "$issue_path")"
  if [[ ! "$issue_id" =~ ^[0-9]+$ ]]; then
    log "Skipping non-numeric folder: $issue_path"
    continue
  fi

  ALL_IDS+=("$issue_id")
  missing=()
  for f in "${require_core_files[@]}"; do
    [[ -f "$issue_path/$f" ]] || missing+=("$f")
  done

  dest="$WORK/issues/$issue_id"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -R "$issue_path"/. "$dest"/
  touch "$dest/revert_command.txt"

  if [[ ${#missing[@]} -gt 0 ]]; then
    log "[$issue_id] staged with warnings. Missing: ${missing[*]}"
  else
    log "[$issue_id] staged"
  fi

done < "$batch_dirs_file"
rm -f "$batch_dirs_file"

if [[ ${#ALL_IDS[@]} -eq 0 ]]; then
  log "No numeric issue folders found under $BATCH_DIR"
  exit 3
fi

printf '%s\n' "${ALL_IDS[@]}" | sort -u > "$OUT/task_ids.txt"
ALL_IDS=()
while IFS= read -r issue_id; do
  [[ -n "$issue_id" ]] && ALL_IDS+=("$issue_id")
done < "$OUT/task_ids.txt"
TOTAL_TASKS="${#ALL_IDS[@]}"
ROUGH_MIN=$(( TOTAL_TASKS * 10 ))
ROUGH_MAX=$(( TOTAL_TASKS * 18 ))
log "Detected $TOTAL_TASKS task(s)"
log "Rough ETA     : ~${ROUGH_MIN}-${ROUGH_MAX} minutes for gold+broken, plus image build time"

python3 - "$OUT" <<'PY'
import csv, sys, pathlib
out = pathlib.Path(sys.argv[1])
ids = [line.strip() for line in (out/'task_ids.txt').read_text().splitlines() if line.strip()]
with open(out/'intake.csv', 'w', newline='', encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['issue_id'])
    for i in ids:
        w.writerow([i])
PY

# ------------------------------
# 2) Sync with frontier-evals
# ------------------------------
section "Phase 2/8 - Syncing issues into frontier-evals"
cd "$WORK"
"$SETUP_SH" 2>&1 | tee "$OUT/setup.log"

# ------------------------------
# 3) CSV / registration precheck
# ------------------------------
section "Phase 3/8 - Registration precheck"
cd "$ROOT_SWEL"

declare -a CSV_OK_IDS=()

for issue_id in "${ALL_IDS[@]}"; do
  row_count="$(grep -c "^${issue_id}," all_swelancer_tasks.csv || true)"
  desc_state="$(python3 - "$issue_id" <<'PY'
import csv, sys
qid = sys.argv[1]
with open('all_swelancer_tasks.csv', newline='', encoding='utf-8') as f:
    rows = [r for r in csv.DictReader(f) if str(r.get('question_id','')).strip() == qid]
if len(rows) != 1:
    print('BAD')
else:
    desc = rows[0].get('description')
    print('OK' if isinstance(desc, str) and desc != '' else 'BAD')
PY
)"

  if [[ "$row_count" == "1" ]]; then
    CSV_OK_IDS+=("$issue_id")
    if [[ "$desc_state" == "OK" ]]; then
      log "[$issue_id] registration OK"
    else
      log "[$issue_id] registration WARN -> description=$desc_state (continuing)"
    fi
  else
    log "[$issue_id] registration BAD -> row_count=$row_count description=$desc_state"
  fi
done

if [[ ${#CSV_OK_IDS[@]} -eq 0 ]]; then
  log "No tasks passed CSV precheck. Stopping."
  exit 4
fi

# ------------------------------
# 4) Build images one by one
# ------------------------------
section "Phase 4/8 - Building task images"

declare -a BUILD_OK_IDS=()

for issue_id in "${CSV_OK_IDS[@]}"; do
  log "[$issue_id] build started"
  start_ts="$(date +%s)"
  build_log="$OUT/per_task/${issue_id}/build.log"
  mkdir -p "$OUT/per_task/${issue_id}"

  docker image rm "${DOCKER_IMAGE_PREFIX}_${issue_id}:latest" >/dev/null 2>&1 || true

  if time uv run python scripts/build_images.py "$issue_id" --skip-push --arch "$SWELANCER_ARCH" > "$build_log" 2>&1; then
    end_ts="$(date +%s)"
    dur=$(( end_ts - start_ts ))
    BUILD_OK_IDS+=("$issue_id")
    log "[$issue_id] build passed in ${dur}s"
  else
    end_ts="$(date +%s)"
    dur=$(( end_ts - start_ts ))
    log "[$issue_id] build FAILED in ${dur}s"
  fi
done

if [[ ${#BUILD_OK_IDS[@]} -eq 0 ]]; then
  log "No tasks built successfully. Stopping."
  exit 5
fi

TASKSET="["
for issue_id in "${BUILD_OK_IDS[@]}"; do
  TASKSET+="'${issue_id}',"
done
TASKSET="${TASKSET%,}]"
log "Validated taskset after build filter: $TASKSET"

copy_run_artifacts() {
  local mode="$1"
  local run_dir="$2"

  cp "$run_dir/results.csv" "$OUT/${mode}.results.csv"
  cp "$run_dir/group.log" "$OUT/${mode}.group.log" 2>/dev/null || true

  local run_logs_file
  run_logs_file="$(mktemp)"
  find "$run_dir" -name run.log -type f -print > "$run_logs_file"

  while IFS= read -r logfile; do
    local task_folder
    task_folder="$(basename "$(dirname "$logfile")")"
    local task_id
    task_id="${task_folder%%_*}"
    mkdir -p "$OUT/per_task/$task_id"
    cp "$logfile" "$OUT/per_task/$task_id/${mode}.run.log"
  done < "$run_logs_file"

  rm -f "$run_logs_file"
}

run_batch_mode() {
  local mode="$1"
  local apply_gold="$2"
  local phase_label="$3"
  local taskset_override="${4:-$TASKSET}"
  local n_test_runs_override="${5:-$DEFAULT_N_TEST_RUNS}"
  local lock_root="${ALCATRAZ_LOCK_ROOT:-$WORK/.runtime/alcatraz}"
  local ports_dir="${NANOEVAL_PORTS_DIR:-$WORK/.runtime/nanoeval/ports}"
  section "Phase ${phase_label} - Running ${mode} validation"
  local console_log="$OUT/${mode}.console.log"
  mkdir -p "$lock_root" "$ports_dir"

  time ALCATRAZ_LOCK_ROOT="$lock_root" \
    NANOEVAL_PORTS_DIR="$ports_dir" \
    ALCATRAZ_JUPYTER_KERNEL_STARTUP_TIMEOUT_SEC="$JUPYTER_KERNEL_TIMEOUT_SEC" \
    PYTHONUNBUFFERED=1 uv run python -u swelancer/run_swelancer.py \
    swelancer.split=diamond \
    swelancer.task_type=ic_swe \
    swelancer.taskset="$taskset_override" \
    swelancer.disable_internet=False \
    swelancer.n_test_runs="$n_test_runs_override" \
    swelancer.solver=swelancer.solvers.dummy.solver:DummySolver \
    swelancer.solver.test_user_tool=False \
    swelancer.solver.apply_gold_solution="$apply_gold" \
    swelancer.solver.computer_runtime=nanoeval_alcatraz.alcatraz_computer_interface:AlcatrazComputerRuntime \
    swelancer.solver.computer_runtime.env=alcatraz.clusters.local:LocalConfig \
    swelancer.solver.computer_runtime.env.pull_from_registry=False \
    swelancer.docker_image_prefix="$DOCKER_IMAGE_PREFIX" \
    swelancer.docker_image_tag=latest \
    runner.concurrency=1 \
    runner.experimental_use_multiprocessing=False \
    runner.enable_slackbot=False \
    runner.recorder=nanoeval.recorder:dummy_recorder \
    runner.max_retries=2 2>&1 | tee "$console_log"

  local run_dir
  run_dir="$(ls -td runs/* | head -n 1)"
  echo "$run_dir" > "$OUT/${mode}.run_dir.txt"
  log "${mode} run dir: $run_dir"
  copy_run_artifacts "$mode" "$run_dir"
}

# ------------------------------
# 5) Gold
# ------------------------------
run_batch_mode gold True "5/8"

# ------------------------------
# 6) Broken
# ------------------------------
run_batch_mode broken False "6/8"

# ------------------------------
# 7) Targeted rerun for infra-style timeouts
# ------------------------------
section "Phase 7/8 - Targeted rerun for dual-timeout candidates"

python3 - "$OUT" <<'PY'
import csv, pathlib, re, sys

out = pathlib.Path(sys.argv[1])
task_ids = [line.strip() for line in (out / "task_ids.txt").read_text().splitlines() if line.strip()]

def load_results(path: pathlib.Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["question_id"]: row for row in csv.DictReader(f)}

gold = load_results(out / "gold.results.csv")
broken = load_results(out / "broken.results.csv")

timeout_re = re.compile(r"Page\\.goto: Timeout \\d+ms exceeded")
assertion_re = re.compile(r"AssertionError:|TEST FAILED:|assert False")

candidates: list[str] = []
for qid in task_ids:
    g = gold.get(qid)
    b = broken.get(qid)
    if not g or not b:
        continue
    if g.get("correct") != "False" or b.get("correct") != "False":
        continue

    gold_log = (out / "per_task" / qid / "gold.run.log")
    broken_log = (out / "per_task" / qid / "broken.run.log")
    g_text = gold_log.read_text(encoding="utf-8", errors="replace") if gold_log.exists() else ""
    b_text = broken_log.read_text(encoding="utf-8", errors="replace") if broken_log.exists() else ""

    if timeout_re.search(g_text) and timeout_re.search(b_text) and not assertion_re.search(g_text) and not assertion_re.search(b_text):
        candidates.append(qid)

out.joinpath("manual_rerun_candidates.txt").write_text(
    ("\n".join(candidates) + ("\n" if candidates else "")),
    encoding="utf-8",
)
print(f"Detected {len(candidates)} manual-rerun candidate(s)")
PY

declare -a MANUAL_IDS=()
if [[ -f "$OUT/manual_rerun_candidates.txt" ]]; then
  while IFS= read -r issue_id; do
    [[ -n "$issue_id" ]] && MANUAL_IDS+=("$issue_id")
  done < "$OUT/manual_rerun_candidates.txt"
fi

if [[ ${#MANUAL_IDS[@]} -gt 0 ]]; then
  MANUAL_TASKSET="["
  for issue_id in "${MANUAL_IDS[@]}"; do
    MANUAL_TASKSET+="'${issue_id}',"
  done
  MANUAL_TASKSET="${MANUAL_TASKSET%,}]"
  log "Dual-timeout candidates detected: $MANUAL_TASKSET"
  log "Running targeted rerun with swelancer.n_test_runs=${MANUAL_N_TEST_RUNS}"

  run_batch_mode manual_gold True "7/8" "$MANUAL_TASKSET" "$MANUAL_N_TEST_RUNS"
  run_batch_mode manual_broken False "7/8" "$MANUAL_TASKSET" "$MANUAL_N_TEST_RUNS"

  python3 - "$OUT" <<'PY'
import csv, pathlib, sys

out = pathlib.Path(sys.argv[1])

def load_rows(path: pathlib.Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def write_rows(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

def merge_results(base_path: pathlib.Path, override_path: pathlib.Path) -> None:
    base_rows = load_rows(base_path)
    override_rows = load_rows(override_path)
    if not base_rows or not override_rows:
        return
    merged = {row["question_id"]: row for row in base_rows}
    for row in override_rows:
        merged[row["question_id"]] = row
    write_rows(base_path, [merged[k] for k in sorted(merged.keys())])

merge_results(out / "gold.results.csv", out / "manual_gold.results.csv")
merge_results(out / "broken.results.csv", out / "manual_broken.results.csv")
PY

  for issue_id in "${MANUAL_IDS[@]}"; do
    if [[ -f "$OUT/per_task/$issue_id/manual_gold.run.log" ]]; then
      cp "$OUT/per_task/$issue_id/manual_gold.run.log" "$OUT/per_task/$issue_id/gold.run.log"
    fi
    if [[ -f "$OUT/per_task/$issue_id/manual_broken.run.log" ]]; then
      cp "$OUT/per_task/$issue_id/manual_broken.run.log" "$OUT/per_task/$issue_id/broken.run.log"
    fi
  done
else
  log "No dual-timeout candidates detected; skipping targeted rerun."
fi

# ------------------------------
# 8) Summaries + report
# ------------------------------
section "Phase 8/8 - Summaries and final report"

python3 - "$OUT" "$MASTER_LOG" <<'PY'
import csv, json, pathlib, re, sys
from datetime import datetime

out = pathlib.Path(sys.argv[1])
master_log = pathlib.Path(sys.argv[2])

# Helpers

def load_results(path):
    data = {}
    if not path.exists():
        return data
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            data[row['question_id']] = row
    return data


def tail_text(path, max_lines=80):
    if not path.exists():
        return ''
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    return '\n'.join(lines[-max_lines:])


def extract_signal(text):
    patterns = [
        r'AssertionError: .*',
        r'playwright\._impl\._errors\.TimeoutError: .*',
        r'FAILED .*',
        r'TEST FAILED: .*',
        r'TEST PASSED: .*',
        r'1 passed in .*',
        r'1 failed in .*',
    ]
    found = []
    for pat in patterns:
        m = re.findall(pat, text)
        if m:
            found.extend(m[:3])
    return ' | '.join(dict.fromkeys(found))[:600]


task_ids = [line.strip() for line in (out/'task_ids.txt').read_text().splitlines() if line.strip()]
gold = load_results(out/'gold.results.csv')
broken = load_results(out/'broken.results.csv')

# build table from logs saved by bash env in per_task folders
summary_rows = []
counts = {
    'total_detected': len(task_ids),
    'staged_ok': 0,
    'csv_ok': 0,
    'build_ok': 0,
    'gold_pass': 0,
    'gold_fail': 0,
    'broken_pass': 0,
    'broken_fail': 0,
    'valid': 0,
    'invalid': 0,
    'need_manual_run': 0,
    'skipped': 0,
}

timeout_re = re.compile(r'Page\.goto: Timeout \d+ms exceeded')
assertion_re = re.compile(r'AssertionError:|TEST FAILED:|assert False')

# Parse intake/build status from bash-generated context by inspecting existing artifacts
for qid in task_ids:
    per_task = out/'per_task'/qid
    build_log = per_task/'build.log'
    gold_log = per_task/'gold.run.log'
    broken_log = per_task/'broken.run.log'

    staged = 'YES'  # task_ids only contains staged tasks
    csv_ok = 'YES' if (qid in gold or qid in broken or build_log.exists()) else 'UNKNOWN'
    build_ok = 'YES' if build_log.exists() and 'BUILD_OK' not in build_log.name else ('YES' if qid in gold or qid in broken else 'NO')

    g = gold.get(qid, {})
    b = broken.get(qid, {})
    g_ok = g.get('correct') == 'True'
    b_ok = b.get('correct') == 'True'

    if staged == 'YES': counts['staged_ok'] += 1
    if qid in gold or qid in broken or build_log.exists(): counts['csv_ok'] += 1
    if qid in gold or qid in broken: counts['build_ok'] += 1
    counts['gold_pass'] += int(g_ok)
    counts['gold_fail'] += int(bool(g) and not g_ok)
    counts['broken_pass'] += int(b_ok)
    counts['broken_fail'] += int(bool(b) and not b_ok)

    gold_text_full = gold_log.read_text(encoding='utf-8', errors='replace') if gold_log.exists() else ''
    broken_text_full = broken_log.read_text(encoding='utf-8', errors='replace') if broken_log.exists() else ''
    dual_infra_timeout = (
        bool(timeout_re.search(gold_text_full))
        and bool(timeout_re.search(broken_text_full))
        and not bool(assertion_re.search(gold_text_full))
        and not bool(assertion_re.search(broken_text_full))
        and not g_ok
        and not b_ok
    )

    if g and b:
        if g_ok and not b_ok:
            verdict = 'VALID'
            counts['valid'] += 1
        elif dual_infra_timeout:
            verdict = 'NEED_MANUAL_RUN'
            counts['need_manual_run'] += 1
        else:
            verdict = 'INVALID'
            counts['invalid'] += 1
    else:
        verdict = 'SKIPPED'
        counts['skipped'] += 1

    gold_signal = extract_signal(tail_text(gold_log))
    broken_signal = extract_signal(tail_text(broken_log))

    summary_rows.append({
        'issue_id': qid,
        'gold_correct': g.get('correct', ''),
        'gold_earned': g.get('earned', ''),
        'broken_correct': b.get('correct', ''),
        'broken_earned': b.get('earned', ''),
        'verdict': verdict,
        'gold_signal': gold_signal,
        'broken_signal': broken_signal,
    })

with open(out/'summary.csv', 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()) if summary_rows else [
        'issue_id','gold_correct','gold_earned','broken_correct','broken_earned','verdict','gold_signal','broken_signal'
    ])
    writer.writeheader()
    writer.writerows(summary_rows)

# Build Markdown report
report = []
report.append(f"# Delivery Validation Report\n")
report.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
report.append(f"Output folder: `{out}`\n")
report.append("## Summary\n")
report.append(f"- Total tasks detected: **{counts['total_detected']}**")
report.append(f"- Built successfully: **{counts['build_ok']}**")
report.append(f"- Gold pass: **{counts['gold_pass']}**")
report.append(f"- Gold fail: **{counts['gold_fail']}**")
report.append(f"- Broken pass: **{counts['broken_pass']}**")
report.append(f"- Broken fail: **{counts['broken_fail']}**")
report.append(f"- Final VALID: **{counts['valid']}**")
report.append(f"- Final INVALID: **{counts['invalid']}**")
report.append(f"- Final NEED_MANUAL_RUN: **{counts['need_manual_run']}**")
report.append(f"- Final SKIPPED: **{counts['skipped']}**\n")

report.append("## Verdict Criteria\n")
report.append("A task is marked **VALID** only when **gold/fixed passes** and **broken/no-gold fails**.")
report.append("A task is marked **NEED_MANUAL_RUN** when both gold and broken fail with login-style `Page.goto` timeouts and no assertion-style test signal.\n")

report.append("## Per-task Summary\n")
report.append("| Issue | Gold | Broken | Verdict |")
report.append("|---|---:|---:|---|")
for row in summary_rows:
    report.append(f"| {row['issue_id']} | {row['gold_correct']} | {row['broken_correct']} | {row['verdict']} |")
report.append("")

report.append("## Per-task Details\n")
for row in summary_rows:
    qid = row['issue_id']
    report.append(f"### {qid}\n")
    report.append(f"- Gold correct: **{row['gold_correct'] or 'N/A'}**")
    report.append(f"- Broken correct: **{row['broken_correct'] or 'N/A'}**")
    report.append(f"- Verdict: **{row['verdict']}**")
    if row['gold_signal']:
        report.append(f"- Gold signal: `{row['gold_signal']}`")
    if row['broken_signal']:
        report.append(f"- Broken signal: `{row['broken_signal']}`")
    report.append(f"- Gold run log: `per_task/{qid}/gold.run.log`")
    report.append(f"- Broken run log: `per_task/{qid}/broken.run.log`\n")

(out/'validation_report.md').write_text('\n'.join(report), encoding='utf-8')

print('\n========== DELIVERY VALIDATION SUMMARY ==========')
for k, v in counts.items():
    print(f'{k:>16}: {v}')
print(' summary_csv     :', out/'summary.csv')
print(' validation_md   :', out/'validation_report.md')
print(' driver_log      :', master_log)
print('===============================================')
PY

log "Batch validation complete"
log "Summary CSV      : $OUT/summary.csv"
log "Validation report: $OUT/validation_report.md"
log "Driver log       : $MASTER_LOG"
