# Atlassian User Email Fetcher

Resolves Atlassian Account IDs in a CSV file to email addresses by querying a specific directory in your Atlassian Organization, then updates only the resolved rows in the CSV.

**No installation required** — uses `curl` and `jq`, both pre-installed on macOS.

## Files

| File | Description |
|---|---|
| `fetch_user_emails.sh` | Main script |
| `config.properties` | Credentials, file paths, column names |
| `sample_active_usage.csv` | Input CSV template |

## Setup

Edit `config.properties` with your credentials:

```properties
ATLASSIAN_ORG_ID=your-org-id        # From admin.atlassian.com/o/<ORG_ID>/...
ATLASSIAN_ADMIN_EMAIL=admin@co.com  # Org Admin email (for reference)
ATLASSIAN_API_TOKEN=your-token      # https://id.atlassian.com/manage-profile/security/api-tokens
INPUT_FILE=sample_active_usage.csv  # Relative to repo root, or absolute path
OUTPUT_FILE=                        # Leave blank to update the input file in-place
```

## Usage

```bash
# Preview what would be resolved without writing anything
./fetch_user_emails.sh --dry-run

# Standard run — interactively pick a directory, then update the CSV
./fetch_user_emails.sh

# Write to a separate output file (input file stays untouched)
./fetch_user_emails.sh --output results.csv

# Use a different config or input file
./fetch_user_emails.sh --config prod.properties --input /abs/path/to/users.csv
```

## How it works

1. **Reads only unresolved rows** — skips any row that already has a real email (not blank, not `NOT_FOUND`)
2. **Lists directories** — fetches all directories in your org and prompts you to pick one (auto-selects if only one exists)
3. **Batch-queries** — sends the account IDs to `GET /admin/v2/orgs/{orgId}/directories/{dirId}/users?accountIds[]=...` in batches of 50
4. **Partial update** — only overwrites rows that were successfully resolved; unresolved rows are left exactly as-is
5. **Reports unresolved IDs** — if any account IDs weren't found in the chosen directory, lists them and suggests re-running with a different directory

## Re-running for unresolved users

If some users belong to a different directory, just re-run the script and pick that directory. Already-resolved rows are skipped automatically, so you won't overwrite good data.

```bash
./fetch_user_emails.sh   # pick directory 2 this time
```

## Notes

- `INPUT_FILE` / `OUTPUT_FILE` accept both **relative** (repo root) and **absolute** paths
- Requires `curl` and `jq` — both included with macOS by default
- The CSV is never fully overwritten — only resolved rows are updated per run
