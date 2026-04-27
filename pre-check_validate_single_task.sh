#!/usr/bin/env bash
set -euo pipefail

TASK_DIR="${1:?Usage: $0 <task_dir_or_tests/ISSUE_ID>}"

if [[ ! -d "$TASK_DIR" ]]; then
  echo "ERROR: task folder does not exist: $TASK_DIR" >&2
  exit 2
fi

TASK_DIR_ABS="$(cd "$(dirname "$TASK_DIR")" && pwd -P)/$(basename "$TASK_DIR")"
ISSUE_ID="$(basename "$TASK_DIR_ABS")"

if [[ ! "$ISSUE_ID" =~ ^[0-9]+$ ]]; then
  echo "ERROR: task folder basename must be numeric issue ID. Got: $ISSUE_ID" >&2
  echo "Example: ./pre-check_validate_single_task.sh tests/48761" >&2
  exit 2
fi

# Lead pre-review defaults:
# - Skip Expensify repo/commit/worktree checks because SWELancer will run later.
# - Skip runtime gold/broken semantics because SWELancer will run later.
# - GitHub is off by default to avoid rate-limit noise; enable manually if needed.
#
# Valid env vars:
#   PRECHECK_GITHUB_CHECK=off|auto|on
#   PRECHECK_ENABLE_EXPENSIFY_CHECKS=0|1
#   PRECHECK_REPORT_ONLY=0|1
#   EXPENSIFY_DIR=/path/to/Expensify/App
#
# Backward-compatible with PHASE2_GITHUB_CHECK if someone already uses it.
GITHUB_MODE="${PRECHECK_GITHUB_CHECK:-${PHASE2_GITHUB_CHECK:-off}}"
ENABLE_EXPENSIFY_CHECKS="${PRECHECK_ENABLE_EXPENSIFY_CHECKS:-0}"
EXPENSIFY_DIR_VALUE="${EXPENSIFY_DIR:-}"

STAMP="$(date +%Y%m%d_%H%M%S)"
TMP_ROOT=".precheck_single_task_batches/${ISSUE_ID}_${STAMP}"
TMP_BATCH="$TMP_ROOT/batch"
OUT_DIR="precheck_quality_gate_runs/single_${ISSUE_ID}_${STAMP}"

mkdir -p "$TMP_BATCH" "$OUT_DIR"

if ! ln -s "$TASK_DIR_ABS" "$TMP_BATCH/$ISSUE_ID" 2>/dev/null; then
  cp -R "$TASK_DIR_ABS" "$TMP_BATCH/$ISSUE_ID"
fi

echo
echo "========== SINGLE TASK PRE-CHECK / PRE-REVIEW =========="
echo "Issue ID        : $ISSUE_ID"
echo "Task dir        : $TASK_DIR_ABS"
echo "Temp batch      : $TMP_BATCH"
echo "Output dir      : $OUT_DIR"
echo "GitHub check    : $GITHUB_MODE"
echo "Expensify check : $([[ "$ENABLE_EXPENSIFY_CHECKS" == "1" ]] && echo "ON" || echo "SKIPPED")"
echo "Profile         : Lead pre-review / before SWELancer"
echo "Assume Phase 1  : 0"
echo "Hide warnings   : 0"
echo "========================================================"
echo

echo "[1/6] Checking required task package files..."
for f in test.py flow.mitm commit_id.txt bug_reintroduce.patch issue_data.json; do
  if [[ -f "$TASK_DIR_ABS/$f" ]]; then
    size="$(wc -c < "$TASK_DIR_ABS/$f" | tr -d ' ')"
    if [[ "$size" == "0" ]]; then
      echo "  ✗ $f exists but is EMPTY"
    else
      echo "  ✓ $f (${size} bytes)"
    fi
  else
    echo "  ✗ missing $f"
  fi
done

echo
echo "[2/6] Checking local debug blockers with AST-aware detector..."
python3 - "$TASK_DIR_ABS/test.py" <<'PY'
import ast
import sys
from pathlib import Path

p = Path(sys.argv[1])
if not p.exists():
    print(f"  ✗ missing {p}")
    raise SystemExit(0)

src = p.read_text(encoding="utf-8", errors="replace")
try:
    tree = ast.parse(src, filename=str(p))
except SyntaxError as e:
    print(f"  ✗ syntax error: {e}")
    raise SystemExit(0)

found = False
for node in ast.walk(tree):
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "input":
            print(f"  ✗ real input(...) call at line {node.lineno}")
            found = True
        if isinstance(node.func, ast.Attribute) and node.func.attr == "pause":
            print(f"  ✗ real *.pause(...) call at line {node.lineno}")
            found = True

if not found:
    print("  ✓ no real input(...) or *.pause(...) calls found")
PY

echo
echo "[3/6] Running pre-check engine..."

ARGS=(
  python3 scripts/phase2_quality_gate.py
  --kit-root "$PWD"
  --batch-dir "$TMP_BATCH"
  --batch-name "single_${ISSUE_ID}"
  --validation-run static
  --github-check "$GITHUB_MODE"
  --out-dir "$OUT_DIR"
)

if [[ "$ENABLE_EXPENSIFY_CHECKS" == "1" ]]; then
  if [[ -z "$EXPENSIFY_DIR_VALUE" ]]; then
    echo "WARNING: PRECHECK_ENABLE_EXPENSIFY_CHECKS=1 but EXPENSIFY_DIR is not set. Skipping Expensify checks."
  elif [[ ! -d "$EXPENSIFY_DIR_VALUE" ]]; then
    echo "WARNING: EXPENSIFY_DIR does not exist: $EXPENSIFY_DIR_VALUE. Skipping Expensify checks."
  else
    ARGS+=(--expensify-dir "$EXPENSIFY_DIR_VALUE")
  fi
fi

set +e
PHASE2_ASSUME_PHASE1_PASSED=0 \
PHASE2_HIDE_NOISY_WARNINGS=0 \
PHASE2_SKIP_EXPENSIFY_CHECKS="$([[ "$ENABLE_EXPENSIFY_CHECKS" == "1" ]] && echo 0 || echo 1)" \
PHASE2_SKIP_RUNTIME_CHECKS=1 \
PHASE2_GITHUB_CHECK="$GITHUB_MODE" \
"${ARGS[@]}"
ENGINE_EXIT=$?
set -e

echo
echo "[4/6] Building action summary..."
if [[ -f "$OUT_DIR/phase2_full.json" ]]; then
  python3 scripts/phase2_human_summary.py --json "$OUT_DIR/phase2_full.json" --show-warnings || true
else
  echo "ERROR: missing pre-check JSON output: $OUT_DIR/phase2_full.json" >&2
  exit 2
fi

echo
echo "[5/6] Report files"
echo "  Markdown : $OUT_DIR/phase2_report.md"
echo "  CSV      : $OUT_DIR/phase2_summary.csv"
echo "  JSON     : $OUT_DIR/phase2_full.json"

echo
echo "[6/6] Gate interpretation"
if [[ "$ENGINE_EXIT" -eq 0 ]]; then
  echo "  ✓ PRE-CHECK PASS: task can move to manual review."
else
  echo "  ✗ PRE-CHECK BLOCKED: fix the listed blockers before review."
fi

if [[ "${PRECHECK_REPORT_ONLY:-0}" == "1" || "${PHASE2_REPORT_ONLY:-0}" == "1" ]]; then
  echo
  echo "Report-only mode enabled: returning exit 0 for inspection."
  exit 0
fi

exit "$ENGINE_EXIT"
