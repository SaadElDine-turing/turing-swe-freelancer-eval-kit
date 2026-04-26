#!/usr/bin/env bash
set -euo pipefail

# 1) Clone repo
# 2) Reset to specific commit
# 3) Apply patch
# 4) Copy local ./issues contents into project/swelancer/issues
# 5) Update all_swelancer_tasks.csv with rows for issues in ./issues
# 6) Run UV_GIT_LFS=1 uv sync --python 3.12 inside project/swelancer (if uv exists)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_URL="https://github.com/openai/frontier-evals.git"
REPO_DIR="${SCRIPT_DIR}/frontier-evals"
COMMIT_SHA="e8aa3d93dbce973bda0673d1fadd0300ede5e034"

PATCH_FILE="${SCRIPT_DIR}/changes.patch"
ISSUES_SRC_DIR="${SCRIPT_DIR}/issues"
ISSUES_DST_DIR="${REPO_DIR}/project/swelancer/issues"

SWELANCER_DIR="${REPO_DIR}/project/swelancer"
TASKS_CSV="${SWELANCER_DIR}/all_swelancer_tasks.csv"

echo "==> Working in ${SCRIPT_DIR}"

# -----------------------
# 1) Clone
# -----------------------
if [[ ! -d "${REPO_DIR}/.git" ]]; then
  echo "==> Cloning repository..."
  git clone "${REPO_URL}" "${REPO_DIR}"
else
  echo "==> Repository already exists, skipping clone"
fi

# -----------------------
# 2) Reset to commit (only if patches are not already applied)
# -----------------------
AARCH64_PATCH="${SCRIPT_DIR}/aarch64_support.patch"
RUNTIME_PATCH="${SCRIPT_DIR}/runtime_fixes.patch"
LOCK_RUNTIME_PATCH="${SCRIPT_DIR}/lock_runtime_fixes.patch"

# Check if all patches are already applied by testing each one in reverse
all_applied=true
for pf in "${PATCH_FILE}" "${RUNTIME_PATCH}" "${AARCH64_PATCH}" "${LOCK_RUNTIME_PATCH}"; do
  if [[ -f "${pf}" ]] && ! git -C "${REPO_DIR}" apply --reverse --check "${pf}" 2>/dev/null; then
    all_applied=false
    break
  fi
done

if ${all_applied}; then
  echo "==> All patches already applied, skipping reset and patch steps"
else
  echo "==> Resetting to commit ${COMMIT_SHA}"
  git -C "${REPO_DIR}" fetch --all --prune
  git -C "${REPO_DIR}" reset --hard "${COMMIT_SHA}"
  git -C "${REPO_DIR}" clean -fd
  # -----------------------
  # 3) Apply patches
  # -----------------------
  echo "==> Applying changes.patch"
  if [[ ! -f "${PATCH_FILE}" ]]; then
    echo "ERROR: changes.patch not found in ${SCRIPT_DIR}" >&2
    exit 1
  fi
  # git -C "${REPO_DIR}" apply "${PATCH_FILE}"

  echo "==> Applying changes.patch (safe)"
  git -C "${REPO_DIR}" apply --check "${PATCH_FILE}" 2>/dev/null && \
  git -C "${REPO_DIR}" apply "${PATCH_FILE}" || \
  echo "Patch already applied or cannot be applied cleanly, skipping"

  if [[ -f "${RUNTIME_PATCH}" ]]; then
    echo "==> Applying runtime_fixes.patch"
    # git -C "${REPO_DIR}" apply "${RUNTIME_PATCH}"
    git -C "${REPO_DIR}" apply --check "${RUNTIME_PATCH}" 2>/dev/null && \
    git -C "${REPO_DIR}" apply "${RUNTIME_PATCH}" || \
    echo "Patch already applied or cannot be applied cleanly, skipping"
  else
    echo "WARNING: runtime_fixes.patch not found, skipping"
  fi

  if [[ -f "${AARCH64_PATCH}" ]]; then
    echo "==> Applying aarch64_support.patch"
    git -C "${REPO_DIR}" apply --check "${AARCH64_PATCH}" 2>/dev/null && \
    git -C "${REPO_DIR}" apply "${AARCH64_PATCH}" || \
    echo "Patch already applied or cannot be applied cleanly, skipping"
  else
    echo "WARNING: aarch64_support.patch not found, skipping"
  fi

  if [[ -f "${LOCK_RUNTIME_PATCH}" ]]; then
    echo "==> Applying lock_runtime_fixes.patch"
    git -C "${REPO_DIR}" apply --check "${LOCK_RUNTIME_PATCH}" 2>/dev/null && \
    git -C "${REPO_DIR}" apply "${LOCK_RUNTIME_PATCH}" || \
    echo "Patch already applied or cannot be applied cleanly, skipping"
  else
    echo "WARNING: lock_runtime_fixes.patch not found, skipping"
  fi
fi

# -----------------------
# 4) Copy issues
# -----------------------
echo "==> Copying issues folder contents"
if [[ ! -d "${ISSUES_SRC_DIR}" ]]; then
  echo "ERROR: Local issues directory not found in ${SCRIPT_DIR}" >&2
  exit 1
fi

mkdir -p "${ISSUES_DST_DIR}"
cp -a "${ISSUES_SRC_DIR}/." "${ISSUES_DST_DIR}/"

# -----------------------
# 5) Add rows to all_swelancer_tasks.csv for each numeric issue dir
# -----------------------
echo "==> Updating ${TASKS_CSV} with tasks for issues in ${ISSUES_SRC_DIR}"

if [[ ! -f "${TASKS_CSV}" ]]; then
  echo "ERROR: all_swelancer_tasks.csv not found at ${TASKS_CSV}" >&2
  exit 1
fi

ISSUES_SRC_DIR="${ISSUES_SRC_DIR}" TASKS_CSV="${TASKS_CSV}" python3 - <<'PY'
import csv, json, os, re, sys

issues_src = os.environ["ISSUES_SRC_DIR"]
tasks_csv  = os.environ["TASKS_CSV"]

required = [
  "question_id","variant","price","price_limit","manager_data","manager_commit",
  "acceptable_folders","cwd","set","title","description","proposals"
]

# Read existing rows and capture existing IDs + header
with open(tasks_csv, newline="", encoding="utf-8") as f:
  reader = csv.DictReader(f)
  fieldnames = reader.fieldnames
  if not fieldnames:
    print(f"ERROR: {tasks_csv} appears empty or missing header.", file=sys.stderr)
    sys.exit(1)

  missing = [c for c in required if c not in fieldnames]
  if missing:
    print(f"ERROR: {tasks_csv} missing required columns: {missing}", file=sys.stderr)
    sys.exit(1)

  existing_ids = set()
  for row in reader:
    qid = (row.get("question_id") or "").strip()
    if qid:
      existing_ids.add(qid)

def numeric_issue_dirs(base: str):
  for name in os.listdir(base):
    full = os.path.join(base, name)
    if os.path.isdir(full) and re.fullmatch(r"\d+", name):
      yield name, full

def normalize_description(s: object) -> str:
  """
  - Convert to string
  - Normalize newlines to '\n'
  - Keep as a single CSV field safely (csv module will quote as needed)
  """
  if s is None:
    return ""
  text = str(s)
  # Normalize Windows/Mac newlines to '\n' literal sequences
  text = text.replace("\r\n", "\n").replace("\r", "\n")
  # Replace real newlines with backslash-n so the CSV stays one physical line per record
  text = text.replace("\n", "\\n")
  return text

rows_to_add = []
for qid, dpath in sorted(numeric_issue_dirs(issues_src), key=lambda x: int(x[0])):
  if qid in existing_ids:
    continue

  issue_json = os.path.join(dpath, "issue_data.json")
  if not os.path.isfile(issue_json):
    print(f"WARNING: Skipping {qid} (missing issue_data.json)", file=sys.stderr)
    continue

  try:
    with open(issue_json, "r", encoding="utf-8") as jf:
      data = json.load(jf)
  except Exception as e:
    print(f"WARNING: Skipping {qid} (failed to parse issue_data.json: {e})", file=sys.stderr)
    continue

  title = data.get("title", "")
  desc  = normalize_description(data.get("html_description", ""))

  rows_to_add.append({
    "question_id": qid,
    "variant": "ic_swe",
    "price": "1000",
    "price_limit": "2000000",
    "manager_data": "",
    "manager_commit": "",
    "acceptable_folders": f"['{qid}']",
    "cwd": "/app/expensify",
    "set": "diamond",
    "title": "" if title is None else str(title),
    "description": desc,
    "proposals": "",
  })

if not rows_to_add:
  print("==> No new rows to add (either none found or all already present).")
  sys.exit(0)

# IMPORTANT: quoting=csv.QUOTE_MINIMAL will quote fields when needed (e.g., commas/quotes).
# Since we already converted newlines to literal '\n', we avoid multi-line CSV records.
with open(tasks_csv, "a", newline="", encoding="utf-8") as f:
  writer = csv.DictWriter(
    f,
    fieldnames=fieldnames,
    quoting=csv.QUOTE_MINIMAL,
    escapechar="\\",
    doublequote=True
  )
  for row in rows_to_add:
    # Preserve any extra columns in the CSV by writing blanks for them
    out = {k: row.get(k, "") for k in fieldnames}
    writer.writerow(out)

print(f"==> Added {len(rows_to_add)} row(s) to {tasks_csv}.")
PY


echo "==> Syncing local runtime script overrides"

if [[ -f "${SCRIPT_DIR}/setup_expensify.yml" ]]; then
  cp "${SCRIPT_DIR}/setup_expensify.yml" \
     "${SWELANCER_DIR}/runtime_scripts/setup_expensify.yml"
else
  echo "WARNING: local setup_expensify.yml not found, skipping override"
fi

if [[ -f "${SCRIPT_DIR}/run_tests.yml" ]]; then
  cp "${SCRIPT_DIR}/run_tests.yml" \
     "${SWELANCER_DIR}/runtime_scripts/run_tests.yml"
else
  echo "WARNING: local run_tests.yml not found, skipping override"
fi

if [[ -f "${SCRIPT_DIR}/conftest.py" ]]; then
  cp "${SCRIPT_DIR}/conftest.py" \
     "${SWELANCER_DIR}/issues/conftest.py"
else
  echo "WARNING: local conftest.py not found, skipping override"
fi

if [[ -f "${SCRIPT_DIR}/build_images.py" ]]; then
  cp "${SCRIPT_DIR}/build_images.py" \
     "${SWELANCER_DIR}/scripts/build_images.py"
else
  echo "WARNING: local build_images.py not found, skipping override"
fi

# -----------------------
# 6) uv sync with Python 3.12
# -----------------------
echo "==> Checking for uv..."

if command -v uv >/dev/null 2>&1; then
  echo "==> uv found. Running uv sync with Python 3.12"
  cd "${SWELANCER_DIR}"
  UV_GIT_LFS=1 uv sync --python 3.12
  echo "==> uv sync completed"
else
  echo "WARNING: uv is not installed."
  echo "Please install uv first: https://github.com/astral-sh/uv"
  echo
  echo "Then run manually:"
  echo "  cd frontier-evals/project/swelancer"
  echo "  UV_GIT_LFS=1 uv sync --python 3.12"
  echo
fi

echo "==> Done. Repository setup complete."
