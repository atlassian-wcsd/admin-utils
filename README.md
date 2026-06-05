# Atlassian User Email Fetcher

Resolves Atlassian Account IDs in a CSV file to email addresses by querying your Atlassian Organization API, then writes the emails back into the CSV.

## Files

| File | Description |
|---|---|
| `fetch_user_emails.py` | Main script |
| `config.properties` | All configuration (credentials, file paths, column names) |
| `sample_active_usage.csv` | Input CSV template |
| `requirements.txt` | Python dependencies |

## Setup

```bash
pip install requests
```

Edit `config.properties` with your credentials:

```properties
ATLASSIAN_ORG_ID=your-org-id        # From admin.atlassian.com/o/<ORG_ID>/...
ATLASSIAN_ADMIN_EMAIL=admin@co.com  # Org Admin account email (for reference)
ATLASSIAN_API_TOKEN=your-token      # https://id.atlassian.com/manage-profile/security/api-tokens
INPUT_FILE=sample_active_usage.csv  # Relative to repo root, or absolute path
OUTPUT_FILE=                        # Leave blank to overwrite INPUT_FILE in-place
```

## Usage

```bash
# Standard run (reads INPUT_FILE from config, writes emails in-place)
python fetch_user_emails.py

# Write to a separate output file
python fetch_user_emails.py --output results.csv

# Preview resolved emails without writing anything
python fetch_user_emails.py --dry-run

# Use a different config or input file
python fetch_user_emails.py --config prod.properties --input /abs/path/to/users.csv

# One-off credential override (no config file edit needed)
python fetch_user_emails.py --org-id abc123 --api-token mytoken
```

## How it works

1. **Discovers directories** — `GET /admin/v2/orgs/{orgId}/directories`
2. **Fetches users per directory** — `GET /admin/v2/orgs/{orgId}/directories/{dirId}/users` (paginated, runs for each directory)
3. **Fetches org-level users** — `GET /admin/v1/orgs/{orgId}/users` (catches invited/unmanaged accounts not in any directory)
4. **Enriches the CSV** — matches `user_id` → email, overwrites the `email` column; rows with no match get `NOT_FOUND`

## Input CSV format

The script expects a column named `user_id` (configurable via `CSV_USER_ID_COLUMN`) containing Atlassian Account IDs. All other columns are preserved as-is.

```
user_id,email,primary_email_domain,...
712020:2fe0b840-...,placeholder,example.com,...
6420f0fc67102fc717c1ccc9,placeholder,example.com,...
```

## Notes

- `INPUT_FILE` / `OUTPUT_FILE` accept both **relative paths** (resolved from the repo root) and **absolute paths**.
- Rate limits are handled automatically with exponential backoff.
- All CLI flags override their `config.properties` equivalents.
