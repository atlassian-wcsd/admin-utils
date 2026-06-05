#!/usr/bin/env bash
# =============================================================================
# fetch_user_emails.sh
# Resolves Atlassian Account IDs in a CSV to email addresses.
#
# Flow:
#   1. Read account IDs from CSV (only rows where email is missing/NOT_FOUND)
#   2. List directories in the org and ask the user to pick one
#   3. Batch-query that directory for those specific account IDs
#   4. Update only the resolved rows in the CSV (never wipe unresolved ones)
#   5. Report any still-unresolved IDs and suggest re-running
#
# Requires: curl, jq (both pre-installed on macOS)
# Compatible with bash 3.2+ (macOS default)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.properties"
BASE_URL="https://api.atlassian.com/admin"
DRY_RUN=false
BATCH_SIZE=50   # accountIds per API request

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --config FILE       Path to config file (default: config.properties)
  --input FILE        Input CSV path (overrides INPUT_FILE in config)
  --output FILE       Output CSV path (overrides OUTPUT_FILE in config)
  --dry-run           Preview what would be resolved without writing files
  --help              Show this message
EOF
  exit 0
}

# ---------------------------------------------------------------------------
# Parse CLI args
# ---------------------------------------------------------------------------
CLI_INPUT="" CLI_OUTPUT="" CLI_ORG_ID="" CLI_TOKEN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config)    CONFIG_FILE="$2"; shift 2 ;;
    --input)     CLI_INPUT="$2";   shift 2 ;;
    --output)    CLI_OUTPUT="$2";  shift 2 ;;
    --org-id)    CLI_ORG_ID="$2";  shift 2 ;;
    --api-token) CLI_TOKEN="$2";   shift 2 ;;
    --dry-run)   DRY_RUN=true;     shift   ;;
    --help)      usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# ---------------------------------------------------------------------------
# Load config.properties
# ---------------------------------------------------------------------------
[[ ! -f "$CONFIG_FILE" ]] && { echo "ERROR: Config file not found: $CONFIG_FILE" >&2; exit 1; }

while IFS='=' read -r key value; do
  [[ "$key" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${key// }"           ]] && continue
  key="${key// /}"; value="${value## }"; value="${value%% }"
  export "PROP_${key}=${value}"
done < "$CONFIG_FILE"

ORG_ID="${CLI_ORG_ID:-${PROP_ATLASSIAN_ORG_ID:-}}"
API_TOKEN="${CLI_TOKEN:-${PROP_ATLASSIAN_API_TOKEN:-}}"
ADMIN_EMAIL="${PROP_ATLASSIAN_ADMIN_EMAIL:-}"
USER_ID_COL="${PROP_CSV_USER_ID_COLUMN:-user_id}"
EMAIL_COL="${PROP_CSV_EMAIL_COLUMN:-email}"

resolve_path() {
  local p="$1"
  [[ "$p" = /* ]] && echo "$p" || echo "${SCRIPT_DIR}/${p}"
}

INPUT_FILE="$(resolve_path "${CLI_INPUT:-${PROP_INPUT_FILE:-}}")"
OUTPUT_RAW="${CLI_OUTPUT:-${PROP_OUTPUT_FILE:-}}"
OUTPUT_FILE="$([[ -n "$OUTPUT_RAW" ]] && resolve_path "$OUTPUT_RAW" || echo "$INPUT_FILE")"

# ---------------------------------------------------------------------------
# Validate config
# ---------------------------------------------------------------------------
errors=()
[[ -z "$ORG_ID"    || "$ORG_ID"    == "YOUR_ORG_ID_HERE"   ]] && errors+=("  • ATLASSIAN_ORG_ID is not set")
[[ -z "$API_TOKEN" || "$API_TOKEN" == "YOUR_API_TOKEN_HERE" ]] && errors+=("  • ATLASSIAN_API_TOKEN is not set")
[[ -z "$INPUT_FILE"                                         ]] && errors+=("  • INPUT_FILE is not set")
if [[ ${#errors[@]} -gt 0 ]]; then
  echo "Configuration errors:"; printf '%s\n' "${errors[@]}"; exit 1
fi
[[ ! -f "$INPUT_FILE" ]] && { echo "ERROR: Input file not found: $INPUT_FILE" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Temp files (cleaned up on exit)
# ---------------------------------------------------------------------------
TMPMAP=$(mktemp)      # accountId<TAB>email  — resolved pairs
TMPIDS=$(mktemp)      # one unresolved accountId per line
TMPOUT=$(mktemp)      # output CSV being built
trap 'rm -f "$TMPMAP" "$TMPIDS" "$TMPOUT"' EXIT

# ---------------------------------------------------------------------------
# API helper
# ---------------------------------------------------------------------------
api_get() {
  local url="$1"
  curl --silent --fail --show-error \
    --header "Authorization: Bearer ${API_TOKEN}" \
    --header "Accept: application/json" \
    "$url" || { echo "ERROR: API call failed: $url" >&2; exit 1; }
}

# ---------------------------------------------------------------------------
# Print run summary
# ---------------------------------------------------------------------------
echo "========================================"
echo " Atlassian User Email Fetcher"
echo "========================================"
echo "Org ID         : $ORG_ID"
echo "Admin email    : $ADMIN_EMAIL"
echo "Input CSV      : $INPUT_FILE"
echo "Output CSV     : $($DRY_RUN && echo '(dry run — no file written)' || echo "$OUTPUT_FILE")"
$DRY_RUN && echo "Mode           : DRY RUN"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Find CSV column indices
# ---------------------------------------------------------------------------
header=$(head -1 "$INPUT_FILE")
IFS=',' read -ra cols <<< "$header"

uid_idx=-1; email_idx=-1
for i in "${!cols[@]}"; do
  col="${cols[$i]//[$'\r']/}"
  [[ "$col" == "$USER_ID_COL" ]] && uid_idx=$i
  [[ "$col" == "$EMAIL_COL"   ]] && email_idx=$i
done

[[ $uid_idx -eq -1 ]] && {
  echo "ERROR: Column '$USER_ID_COL' not found. Available: $header" >&2; exit 1
}

# ---------------------------------------------------------------------------
# Step 2: Collect only unresolved account IDs
#         (skip rows that already have a real email — not blank, not NOT_FOUND)
# ---------------------------------------------------------------------------
echo "Reading CSV …"
tail -n +2 "$INPUT_FILE" | awk -F',' -v ui="$uid_idx" -v ei="$email_idx" '
{
  gsub(/\r/, "")
  uid = $(ui+1); gsub(/^[[:space:]]+|[[:space:]]+$/, "", uid)
  if (uid == "") next
  email = (ei >= 0) ? $(ei+1) : ""
  gsub(/^[[:space:]]+|[[:space:]]+$/, "", email)
  if (email == "" || email == "NOT_FOUND" || email !~ /^[^@]+@[^@]+\.[^@]+$/) print uid
}' > "$TMPIDS"

total_ids=$(wc -l < "$TMPIDS" | tr -d ' ')

if [[ "$total_ids" -eq 0 ]]; then
  echo "✓ All rows already have emails — nothing to do."
  exit 0
fi
echo "  Found $total_ids account ID(s) needing resolution."
echo ""

# ---------------------------------------------------------------------------
# Step 3: List directories and let user pick one
# ---------------------------------------------------------------------------
echo "Fetching directories for org '$ORG_ID' …"
dir_response=$(api_get "${BASE_URL}/v2/orgs/${ORG_ID}/directories")
dir_count=$(echo "$dir_response" | jq '.data | length')

[[ "$dir_count" -eq 0 ]] && { echo "ERROR: No directories found in org '$ORG_ID'." >&2; exit 1; }

echo ""
echo "Available directories:"
for i in $(seq 0 $((dir_count - 1))); do
  name=$(echo "$dir_response" | jq -r ".data[$i].name")
  did=$(echo  "$dir_response" | jq -r ".data[$i].directoryId")
  printf "  [%d] %s  (%s)\n" "$((i+1))" "$name" "$did"
done
echo ""

if [[ "$dir_count" -eq 1 ]]; then
  SELECTED=0
  echo "Auto-selected the only directory: $(echo "$dir_response" | jq -r '.data[0].name')"
else
  while true; do
    read -rp "Select a directory [1-${dir_count}]: " choice
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= dir_count )); then
      SELECTED=$((choice - 1)); break
    fi
    echo "  Invalid choice, please enter a number between 1 and ${dir_count}."
  done
fi

DIR_ID=$(echo   "$dir_response" | jq -r ".data[$SELECTED].directoryId")
DIR_NAME=$(echo "$dir_response" | jq -r ".data[$SELECTED].name")
echo ""
echo "Using directory: '$DIR_NAME' ($DIR_ID)"
echo ""

# ---------------------------------------------------------------------------
# Step 4: Batch-query the directory for the specific account IDs
#         Results written to TMPMAP as: accountId<TAB>email
# ---------------------------------------------------------------------------
total_batches=$(( (total_ids + BATCH_SIZE - 1) / BATCH_SIZE ))
echo "Querying $total_ids account ID(s) in $total_batches batch(es) of up to $BATCH_SIZE …"

batch_num=0
batch=()

process_batch() {
  [[ ${#batch[@]} -eq 0 ]] && return
  batch_num=$((batch_num + 1))

  # Build query string: accountIds[]=id1&accountIds[]=id2&...
  query_string=""
  for id in "${batch[@]}"; do
    encoded_id="${id//:/%3A}"
    query_string+="accountIds[]=${encoded_id}&"
  done
  query_string="${query_string%&}"

  echo "  Batch ${batch_num}/${total_batches}: ${#batch[@]} IDs …"
  response=$(api_get "${BASE_URL}/v2/orgs/${ORG_ID}/directories/${DIR_ID}/users?${query_string}")

  # Append resolved pairs to TMPMAP
  echo "$response" | jq -r '.data[] | "\(.accountId)\t\(.email // "")"' >> "$TMPMAP"
}

while IFS= read -r id; do
  batch+=("$id")
  if [[ ${#batch[@]} -eq $BATCH_SIZE ]]; then
    process_batch
    batch=()
  fi
done < "$TMPIDS"
process_batch   # flush remaining

resolved_count=$(wc -l < "$TMPMAP" | tr -d ' ')
unresolved_count=$((total_ids - resolved_count))

echo ""
echo "  ✓ Resolved   : $resolved_count"
echo "  ✗ Unresolved : $unresolved_count"
echo ""

# ---------------------------------------------------------------------------
# Step 5: Dry-run preview — show what would be written
# ---------------------------------------------------------------------------
if $DRY_RUN; then
  echo "── DRY RUN PREVIEW ──────────────────────────────────────────────"
  printf "  %-45s  %s\n" "$USER_ID_COL" "$EMAIL_COL"
  printf "  %-45s  %s\n" "---------------------------------------------" "-----------------------------------"
  while IFS= read -r id; do
    email=$(awk -F'\t' -v uid="$id" '$1==uid{print $2; exit}' "$TMPMAP")
    [[ -z "$email" ]] && email="NOT_FOUND"
    printf "  %-45s  %s\n" "$id" "$email"
  done < "$TMPIDS"
  echo "────────────────────────────────────────────────────────────────"
  echo ""
  echo "Dry run complete — no files were written."

# ---------------------------------------------------------------------------
# Step 6: Update CSV — only touch rows that were resolved this run
# ---------------------------------------------------------------------------
else
  echo "Updating CSV …"

  insert_col="false"
  [[ $email_idx -eq -1 ]] && insert_col="true"

  awk -v uid_idx="$uid_idx" \
      -v email_idx="$email_idx" \
      -v mapfile="$TMPMAP" \
      -v email_col="$EMAIL_COL" \
      -v insert_col="$insert_col" \
      'BEGIN {
        FS=OFS=","
        while ((getline line < mapfile) > 0) {
          n = split(line, parts, "\t")
          if (n >= 1) resolved[parts[1]] = (n >= 2 ? parts[2] : "")
        }
        updated=0
      }
      NR==1 {
        if (insert_col == "true") {
          out=""
          for (i=1; i<=NF; i++) {
            out = out (i>1?OFS:"") $i
            if (i == uid_idx+1) out = out OFS email_col
          }
          print out
        } else { print }
        next
      }
      {
        gsub(/\r/, "")
        uid = $(uid_idx+1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", uid)

        if (uid in resolved) {
          if (insert_col == "true") {
            out=""
            for (i=1; i<=NF; i++) {
              out = out (i>1?OFS:"") $i
              if (i == uid_idx+1) out = out OFS resolved[uid]
            }
            print out
          } else {
            $(email_idx+1) = resolved[uid]
            print
          }
          updated++
        } else {
          print   # leave row exactly as-is
        }
      }
      END { print updated > "/dev/stderr" }' "$INPUT_FILE" > "$TMPOUT" 2>/tmp/awk_updated.tmp

  updated_rows=$(cat /tmp/awk_updated.tmp)
  mv "$TMPOUT" "$OUTPUT_FILE"
  echo "  Rows updated   : $updated_rows"
  echo "  File written to: $OUTPUT_FILE"
fi

# ---------------------------------------------------------------------------
# Step 7: Report unresolved IDs
# ---------------------------------------------------------------------------
if [[ "$unresolved_count" -gt 0 ]]; then
  echo ""
  echo "⚠️  $unresolved_count account ID(s) were NOT found in directory '$DIR_NAME':"
  while IFS= read -r id; do
    found=$(awk -F'\t' -v uid="$id" '$1==uid{print 1; exit}' "$TMPMAP")
    [[ -z "$found" ]] && echo "    • $id"
  done < "$TMPIDS"
  echo ""
  echo "These users may belong to a different directory."
  echo "Re-run the script and select a different directory to resolve them."
  echo "(Already-resolved rows will be skipped automatically.)"
fi

echo ""
echo "Done."
