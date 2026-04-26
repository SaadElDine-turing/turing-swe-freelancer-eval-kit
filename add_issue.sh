#!/usr/bin/env bash
set -euo pipefail

# Add a new issue to the SWE-Freelancer-Datapack issues directory.
#
# Usage:
#   ./add_issue.sh <ISSUE_ID> [--from <SOURCE_DIR>]
#
# Examples:
#   ./add_issue.sh 12345                          # scaffold empty issue from GitHub
#   ./add_issue.sh 12345 --from ../swe-freelancer-kit/tests/12345   # copy from kit

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISSUES_DIR="${SCRIPT_DIR}/issues"

usage() {
  echo "Usage: $0 <ISSUE_ID> [--from <SOURCE_DIR>]"
  echo ""
  echo "  <ISSUE_ID>       Numeric GitHub issue ID (e.g. 71150)"
  echo "  --from <DIR>     Copy files from an existing directory (e.g. swe-freelancer-kit/tests/71150)"
  echo ""
  echo "Without --from, creates a skeleton with issue_data.json fetched from GitHub."
  exit 1
}

# --- Parse args ---
if [[ $# -lt 1 ]]; then
  usage
fi

ISSUE_ID="$1"
shift

# Validate issue ID is numeric
if ! [[ "${ISSUE_ID}" =~ ^[0-9]+$ ]]; then
  echo "ERROR: ISSUE_ID must be numeric, got '${ISSUE_ID}'" >&2
  exit 1
fi

SOURCE_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      SOURCE_DIR="$2"
      shift 2
      ;;
    *)
      echo "ERROR: Unknown argument '$1'" >&2
      usage
      ;;
  esac
done

DEST_DIR="${ISSUES_DIR}/${ISSUE_ID}"

# --- Check if issue already exists ---
if [[ -d "${DEST_DIR}" ]]; then
  echo "ERROR: Issue ${ISSUE_ID} already exists at ${DEST_DIR}" >&2
  echo "To replace it, remove the directory first: rm -rf ${DEST_DIR}" >&2
  exit 1
fi

REQUIRED_FILES=(test.py flow.mitm commit_id.txt bug_reintroduce.patch issue_data.json)
ALLOWED_FILES=(test.py flow.mitm commit_id.txt git_tag.txt bug_reintroduce.patch issue_data.json revert_command.txt)

# Frontier-evals paths
REPO_DIR="${SCRIPT_DIR}/frontier-evals"
SWELANCER_DIR="${REPO_DIR}/project/swelancer"
ISSUES_DST_DIR="${SWELANCER_DIR}/issues"
TASKS_CSV="${SWELANCER_DIR}/all_swelancer_tasks.csv"

# --- Copy from source or scaffold ---
if [[ -n "${SOURCE_DIR}" ]]; then
  # --from mode: copy from existing directory
  if [[ ! -d "${SOURCE_DIR}" ]]; then
    echo "ERROR: Source directory not found: ${SOURCE_DIR}" >&2
    exit 1
  fi

  echo "==> Copying from ${SOURCE_DIR} to ${DEST_DIR}"
  mkdir -p "${DEST_DIR}"
  for f in "${ALLOWED_FILES[@]}"; do
    if [[ -f "${SOURCE_DIR}/${f}" ]]; then
      cp "${SOURCE_DIR}/${f}" "${DEST_DIR}/${f}"
    fi
  done

  # Ensure required empty files exist even if not in source
  for f in revert_command.txt git_tag.txt; do
    [[ -f "${DEST_DIR}/${f}" ]] || touch "${DEST_DIR}/${f}"
  done

  # Check for required files
  missing=()
  for f in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f "${DEST_DIR}/${f}" ]]; then
      missing+=("${f}")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "WARNING: Missing required files: ${missing[*]}"
    echo "  You'll need to add these before running evals."
  fi

else
  # Scaffold mode: create skeleton with GitHub metadata
  echo "==> Creating skeleton for issue ${ISSUE_ID}"
  mkdir -p "${DEST_DIR}"

  # Create empty placeholder files
  touch "${DEST_DIR}/test.py"
  touch "${DEST_DIR}/flow.mitm"
  touch "${DEST_DIR}/commit_id.txt"
  touch "${DEST_DIR}/git_tag.txt"
  touch "${DEST_DIR}/bug_reintroduce.patch"
  touch "${DEST_DIR}/revert_command.txt"

  # Fetch issue_data.json from GitHub
  if command -v gh >/dev/null 2>&1; then
    echo "==> Fetching issue metadata from GitHub..."
    ISSUE_JSON=$(gh issue view "${ISSUE_ID}" --repo Expensify/App --json title,body,labels 2>/dev/null || echo "")

    if [[ -n "${ISSUE_JSON}" ]]; then
      # Fetch HTML description
      HTML_BODY=$(curl -s \
        -H "Accept: application/vnd.github.v3.html+json" \
        "https://api.github.com/repos/Expensify/App/issues/${ISSUE_ID}" 2>/dev/null \
        | python3 -c "import json,sys; print(json.load(sys.stdin).get('body_html',''))" 2>/dev/null || echo "")

      ISSUE_ID="${ISSUE_ID}" HTML_BODY="${HTML_BODY}" python3 -c "
import json, os, re, sys

data = json.loads('''${ISSUE_JSON}''')
labels = [l['name'] for l in data.get('labels', [])]
price_label = next((l for l in labels if re.search(r'\\\$\d+', l)), None)
price = float(re.search(r'\\\$(\d+)', price_label).group(1)) if price_label else 0.0

body = data.get('body', '')
steps_match = re.search(r'(##\s*Action Performed.*?)(?=##\s*Expected Result)', body, re.DOTALL)
issue_repo_steps = steps_match.group(1).strip() if steps_match else body[:500]

out = {
    'price': price,
    'title': data['title'],
    'issue_repo_steps': issue_repo_steps,
    'html_description': os.environ.get('HTML_BODY', ''),
    'difficulty': '0',
    'issue_clarity': '0',
    'test_quality': '3'
}
print(json.dumps(out, indent=2, ensure_ascii=False))
" > "${DEST_DIR}/issue_data.json"
      echo "==> Fetched issue_data.json from GitHub"
    else
      echo "WARNING: Could not fetch issue from GitHub. Creating empty issue_data.json"
      echo '{}' > "${DEST_DIR}/issue_data.json"
    fi
  else
    echo "WARNING: gh CLI not found. Creating empty issue_data.json"
    echo "  Install with: brew install gh"
    echo '{}' > "${DEST_DIR}/issue_data.json"
  fi
fi

# --- Summary ---
echo ""
echo "==> Issue ${ISSUE_ID} added to ${DEST_DIR}"
echo ""
echo "Files:"
ls -la "${DEST_DIR}/"
echo ""

# Check completeness
missing=()
for f in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "${DEST_DIR}/${f}" ]] || [[ ! -s "${DEST_DIR}/${f}" ]]; then
    missing+=("${f}")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "Still need content in: ${missing[*]}"
fi

# --- Copy to frontier-evals ---
if [[ -d "${SWELANCER_DIR}" ]]; then
  echo "==> Copying issue to frontier-evals"
  mkdir -p "${ISSUES_DST_DIR}/${ISSUE_ID}"
  cp -a "${DEST_DIR}/." "${ISSUES_DST_DIR}/${ISSUE_ID}/"

  # --- Update CSV ---
  if [[ -f "${TASKS_CSV}" ]]; then
    ISSUE_ID="${ISSUE_ID}" DEST_DIR="${DEST_DIR}" TASKS_CSV="${TASKS_CSV}" python3 - <<'PY'
import csv, json, os, sys

issue_id = os.environ["ISSUE_ID"]
dest_dir = os.environ["DEST_DIR"]
tasks_csv = os.environ["TASKS_CSV"]

with open(tasks_csv, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    for row in reader:
        if (row.get("question_id") or "").strip() == issue_id:
            print(f"==> Issue {issue_id} already in CSV, skipping")
            sys.exit(0)

issue_json = os.path.join(dest_dir, "issue_data.json")
if not os.path.isfile(issue_json):
    print(f"WARNING: No issue_data.json, skipping CSV update", file=sys.stderr)
    sys.exit(0)

with open(issue_json, "r", encoding="utf-8") as jf:
    data = json.load(jf)

title = str(data.get("title", ""))
desc = data.get("html_description", "") or data.get("issue_repo_steps", "") or ""
desc = str(desc).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")

row = {k: "" for k in fieldnames}
row.update({
    "question_id": issue_id,
    "variant": "ic_swe",
    "price": "1000",
    "price_limit": "2000000",
    "acceptable_folders": f"['{issue_id}']",
    "cwd": "/app/expensify",
    "set": "diamond",
    "title": title,
    "description": desc,
})

with open(tasks_csv, "a", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL, doublequote=True)
    writer.writerow(row)

print(f"==> Added {issue_id} to CSV")
PY
  else
    echo "WARNING: all_swelancer_tasks.csv not found, skipping CSV update"
  fi
else
  echo "WARNING: frontier-evals not set up yet. Run ./setup_frontier_evals.sh first."
fi

echo ""
echo "Next steps:"
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "  1. Fill in missing files: ${missing[*]}"
  echo "  2. Re-run: ./add_issue.sh ${ISSUE_ID} --from ${DEST_DIR}  (to re-sync)"
  echo "  3. Build: cd frontier-evals/project/swelancer && uv run python scripts/build_images.py ${ISSUE_ID} --skip-push --arch aarch64"
else
  echo "  Build: cd frontier-evals/project/swelancer && uv run python scripts/build_images.py ${ISSUE_ID} --skip-push --arch aarch64"
fi
