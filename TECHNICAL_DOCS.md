# Technical Documentation — admin-utils

## Overview

`admin-utils` is a lightweight shell-based utility for resolving Atlassian Account IDs to email addresses. It reads a CSV file containing account IDs (e.g. exported from Atlassian's active-usage report), queries the [Atlassian Admin REST API](https://developer.atlassian.com/cloud/admin/), and writes the resolved email addresses back to the CSV — touching only the rows that were actually resolved in each run.

---

## Repository Structure

```
admin-utils/
├── fetch_user_emails.sh       # Main script
├── config.properties          # Runtime configuration (credentials, file paths, column names)
└── sample_active_usage.csv    # Example input CSV
```

---

## Dependencies

| Dependency | Version | Notes |
|---|---|---|
| `bash` | 3.2+ | Compatible with the macOS system shell |
| `curl` | any | Pre-installed on macOS |
| `jq` | any | Pre-installed on macOS |

No package manager, Docker image, or additional runtime is required.

---

## Configuration

All runtime settings are stored in `config.properties` (key=value format, `#` comments supported).

| Key | Required | Description |
|---|---|---|
| `ATLASSIAN_ORG_ID` | ✅ | Atlassian Organisation ID. Found in the Admin URL: `admin.atlassian.com/o/<ORG_ID>/` |
| `ATLASSIAN_API_TOKEN` | ✅ | API token for the org admin account. Generate at `id.atlassian.com/manage-profile/security/api-tokens` |
| `ATLASSIAN_ADMIN_EMAIL` | — | Email of the admin account (informational only, not used in API auth) |
| `INPUT_FILE` | ✅ | Path to the input CSV. Relative paths are resolved from the repo root; absolute paths are used as-is. |
| `OUTPUT_FILE` | — | Path for the output CSV. Leave blank to update `INPUT_FILE` in-place. |
| `CSV_USER_ID_COLUMN` | — | Name of the column containing account IDs. Default: `user_id` |
| `CSV_EMAIL_COLUMN` | — | Name of the column to write resolved emails into. Default: `email` |

Any config value can be overridden at runtime via CLI flags (see [CLI Reference](#cli-reference)).

---

## Input CSV Format

The script expects a CSV with at minimum one column holding Atlassian Account IDs (default column name: `user_id`). A second column for emails (default: `email`) is updated in-place; if it does not exist it is inserted immediately after the `user_id` column.

**Example** (`sample_active_usage.csv`):

```
user_id,email,primary_email_domain,job_title,...
712020:2fe0b840-93bb-4138-83bb-78e994889a96,,sample.com,null,...
6420f0fc67102fc717c1ccc9,NOT_FOUND,sample.com,Web Developer,...
```

Rows are considered **unresolved** (and therefore candidates for processing) when the email field is:
- blank / empty
- equal to `NOT_FOUND`
- not a valid email format (`user@domain.tld`)

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│  1. Parse CLI args and load config.properties            │
│  2. Scan CSV → collect unresolved account IDs            │
│  3. GET /admin/v2/orgs/{orgId}/directories               │
│     └─ prompt user to pick a directory                   │
│  4. Batch-GET /admin/v2/orgs/{orgId}/directories/        │
│       {dirId}/users?accountIds[]=...  (50 IDs per call)  │
│  5. Write resolved email addresses back to CSV            │
│     └─ only resolved rows are modified                   │
│  6. Report any unresolved IDs                            │
└─────────────────────────────────────────────────────────┘
```

### Key design decisions

- **Idempotent partial updates** — each run only overwrites rows it successfully resolved. Already-resolved rows are skipped, so re-running after picking a different directory is safe.
- **Batch API calls** — account IDs are sent to the API in batches of 50 to stay within query-string length limits.
- **Atomic CSV write** — the output is built in a temp file (`mktemp`) and then moved into place, avoiding a partially written file on error.
- **Temp file cleanup** — `trap 'rm -f ...' EXIT` ensures temp files are removed even if the script is interrupted.

---

## CLI Reference

```
Usage: fetch_user_emails.sh [OPTIONS]

Options:
  --config FILE       Path to config file (default: ./config.properties)
  --input  FILE       Input CSV path (overrides INPUT_FILE in config)
  --output FILE       Output CSV path (overrides OUTPUT_FILE in config)
  --org-id ID         Atlassian Org ID (overrides ATLASSIAN_ORG_ID in config)
  --api-token TOKEN   API token (overrides ATLASSIAN_API_TOKEN in config)
  --dry-run           Preview resolved emails without writing any files
  --help              Show help message
```

### Examples

```bash
# Dry run — preview what would be resolved
./fetch_user_emails.sh --dry-run

# Standard run — update the input CSV in-place
./fetch_user_emails.sh

# Write results to a separate file, leaving the original untouched
./fetch_user_emails.sh --output results.csv

# Use an alternate config and a different input file
./fetch_user_emails.sh --config prod.properties --input /data/users.csv
```

---

## API Reference

The script calls two Atlassian Admin V2 endpoints (both require Bearer token auth):

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/admin/v2/orgs/{orgId}/directories` | List all identity directories in the organisation |
| `GET` | `/admin/v2/orgs/{orgId}/directories/{dirId}/users?accountIds[]=...` | Resolve specific account IDs to user records (email extracted from response) |

Authentication uses an **API token** sent as a `Bearer` token in the `Authorization` header. The token must belong to an org admin account.

---

## Error Handling

| Condition | Behaviour |
|---|---|
| Config file missing | Fatal error with descriptive message |
| `ATLASSIAN_ORG_ID` or `ATLASSIAN_API_TOKEN` not set / still placeholder | Lists all config errors and exits |
| Input CSV not found | Fatal error |
| `user_id` column not found in CSV | Fatal error listing available columns |
| API call fails (`curl --fail`) | Fatal error identifying the failed URL |
| No directories found in org | Fatal error |
| Some account IDs not found in the selected directory | Non-fatal warning; unresolved IDs listed; script suggests re-running with a different directory |

---

## Re-running for Users in Multiple Directories

Atlassian organisations may have users spread across several identity directories. If some account IDs are not resolved in the first run:

1. Re-run `./fetch_user_emails.sh` without any changes.
2. When prompted, select a **different directory**.
3. The script automatically skips already-resolved rows, so no data is overwritten.

Repeat for each directory until all (or as many as possible) account IDs are resolved.

---

## Security Notes

- `config.properties` contains a live API token — **do not commit real credentials**. The file is listed in `.gitignore`.
- The API token is passed as an HTTP header (`Authorization: Bearer …`) and never written to disk or echoed to stdout.
- Temp files created by `mktemp` are removed on script exit (including interruption).
