#!/usr/bin/env python3
"""
fetch_user_emails.py
--------------------
Reads a CSV file containing Atlassian Account IDs, queries the Atlassian
Organization API across ALL directories to resolve each account ID to an
email address, and overwrites the existing "email" column in the CSV.

Strategy:
  1. Load config from a .properties file (default: config.properties).
  2. Fetch all directories for the org:
       GET /admin/v2/orgs/{orgId}/directories
  3. For each directory, paginate through its users:
       GET /admin/v2/orgs/{orgId}/directories/{directoryId}/users
  4. Also fetch unmanaged/invited users not in any directory:
       GET /admin/v1/orgs/{orgId}/users
  5. Build a combined account_id -> email map, then enrich the CSV.

Configuration (config.properties):
  ATLASSIAN_ORG_ID       - Your Atlassian Organization ID
  ATLASSIAN_ADMIN_EMAIL  - Org Admin email (for reference)
  ATLASSIAN_API_TOKEN    - API token for the Org Admin account
  INPUT_FILE             - Path to the input CSV (relative or absolute)
  OUTPUT_FILE            - Path for the output CSV (blank = overwrite input)
  CSV_USER_ID_COLUMN     - Column holding account IDs  [default: user_id]
  CSV_EMAIL_COLUMN       - Column to write emails into [default: email]

Usage:
  python fetch_user_emails.py [--config config.properties]
                              [--input path/to/file.csv]
                              [--output path/to/out.csv]
                              [--dry-run]

Dependencies:
  pip install requests
"""

import argparse
import csv
import os
import sys
import time
from urllib.parse import urlparse, parse_qs

import requests

BASE_URL = "https://api.atlassian.com/admin"
DEFAULT_CONFIG_FILE = "config.properties"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Config loading from .properties file
# ---------------------------------------------------------------------------

def load_properties(path: str) -> dict:
    """Parse a Java-style .properties file into a dict."""
    # Resolve relative paths from the script's own directory
    if not os.path.isabs(path):
        path = os.path.join(SCRIPT_DIR, path)

    if not os.path.isfile(path):
        print(f"ERROR: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)

    props = {}
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if "=" not in line:
                print(f"WARNING: Skipping malformed line {lineno} in {path}: {raw.rstrip()}",
                      file=sys.stderr)
                continue
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    return props


def resolve_file_path(path: str) -> str:
    """
    If path is absolute, return as-is.
    Otherwise resolve relative to the script's directory (repo root).
    """
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return os.path.join(SCRIPT_DIR, path)


def build_config(props: dict, cli_overrides: dict) -> dict:
    """Merge properties file values with CLI overrides (CLI wins)."""
    return {
        "org_id":         cli_overrides.get("org_id")        or props.get("ATLASSIAN_ORG_ID", ""),
        "admin_email":    cli_overrides.get("admin_email")    or props.get("ATLASSIAN_ADMIN_EMAIL", ""),
        "api_token":      cli_overrides.get("api_token")      or props.get("ATLASSIAN_API_TOKEN", ""),
        "input_file":     cli_overrides.get("input_file")     or props.get("INPUT_FILE", ""),
        "output_file":    cli_overrides.get("output_file")    or props.get("OUTPUT_FILE", ""),
        "user_id_column": cli_overrides.get("user_id_column") or props.get("CSV_USER_ID_COLUMN", "user_id"),
        "email_column":   cli_overrides.get("email_column")   or props.get("CSV_EMAIL_COLUMN", "email"),
        "max_retries":    3,
        "retry_backoff":  2,
    }


def validate_config(cfg: dict) -> None:
    errors = []
    if not cfg["org_id"] or cfg["org_id"] == "YOUR_ORG_ID_HERE":
        errors.append("  • ATLASSIAN_ORG_ID is not set in config.properties")
    if not cfg["api_token"] or cfg["api_token"] == "YOUR_API_TOKEN_HERE":
        errors.append("  • ATLASSIAN_API_TOKEN is not set in config.properties")
    if not cfg["input_file"]:
        errors.append("  • INPUT_FILE is not set (config.properties or --input flag)")
    if errors:
        print("Configuration errors:\n" + "\n".join(errors), file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_session(api_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    })
    return session


def _get_with_retry(session: requests.Session, url: str, params: dict = None,
                    max_retries: int = 3, backoff: int = 2) -> dict:
    """GET with exponential-backoff retry and rate-limit handling."""
    delay = backoff
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, params=params, timeout=30)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", delay))
                print(f"  Rate-limited. Waiting {retry_after}s …", file=sys.stderr)
                time.sleep(retry_after)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            print(f"  Request failed ({exc}). Retry {attempt}/{max_retries} in {delay}s …",
                  file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    return {}


def _extract_cursor(next_link: str) -> str | None:
    """Extract pagination cursor from a full URL or return it as-is."""
    if not next_link:
        return None
    if next_link.startswith("http"):
        qs = parse_qs(urlparse(next_link).query)
        return qs.get("cursor", [None])[0]
    return next_link


# ---------------------------------------------------------------------------
# API: Directories
# ---------------------------------------------------------------------------

def fetch_directories(session: requests.Session, org_id: str,
                      max_retries: int, backoff: int) -> list[dict]:
    """Return all directories in the org (paginated)."""
    url = f"{BASE_URL}/v2/orgs/{org_id}/directories"
    directories, cursor = [], None

    print("Fetching directories …")
    while True:
        params = {"cursor": cursor} if cursor else {}
        data = _get_with_retry(session, url, params=params,
                               max_retries=max_retries, backoff=backoff)
        batch = data.get("data", [])
        directories.extend(batch)
        noun = "directory" if len(batch) == 1 else "directories"
        print(f"  Found {len(batch)} {noun} (total: {len(directories)})")
        cursor = _extract_cursor(data.get("links", {}).get("next"))
        if not cursor:
            break

    return directories


# ---------------------------------------------------------------------------
# API: Users per directory  (v2 — richer data)
# ---------------------------------------------------------------------------

def fetch_users_in_directory(session: requests.Session, org_id: str,
                             directory: dict, max_retries: int,
                             backoff: int) -> dict[str, str]:
    """Return account_id -> email map for one directory (paginated)."""
    dir_id   = directory["directoryId"]
    dir_name = directory.get("name", dir_id)
    url      = f"{BASE_URL}/v2/orgs/{org_id}/directories/{dir_id}/users"
    account_map, cursor, page = {}, None, 1

    print(f"  Directory '{dir_name}':")
    while True:
        params = {"cursor": cursor} if cursor else {}
        data = _get_with_retry(session, url, params=params,
                               max_retries=max_retries, backoff=backoff)
        users = data.get("data", [])
        for user in users:
            acct_id = user.get("accountId") or user.get("account_id", "")
            if acct_id:
                account_map[acct_id] = user.get("email", "")
        print(f"    Page {page}: {len(users)} users (directory total: {len(account_map)})")
        page  += 1
        cursor = _extract_cursor(data.get("links", {}).get("next"))
        if not cursor:
            break

    return account_map


# ---------------------------------------------------------------------------
# API: Org-level users  (v1 — catches unmanaged/invited accounts)
# ---------------------------------------------------------------------------

def fetch_org_level_users(session: requests.Session, org_id: str,
                          max_retries: int, backoff: int) -> dict[str, str]:
    """Return account_id -> email map for org-level users (paginated)."""
    url = f"{BASE_URL}/v1/orgs/{org_id}/users"
    account_map, cursor, page = {}, None, 1

    print("  Org-level users (v1 — unmanaged/invited):")
    while True:
        params = {"cursor": cursor} if cursor else {}
        data = _get_with_retry(session, url, params=params,
                               max_retries=max_retries, backoff=backoff)
        users = data.get("data", [])
        for user in users:
            acct_id = user.get("account_id") or user.get("accountId", "")
            if acct_id:
                account_map[acct_id] = user.get("email", "")
        print(f"    Page {page}: {len(users)} users (total: {len(account_map)})")
        page  += 1
        cursor = _extract_cursor(data.get("links", {}).get("next"))
        if not cursor:
            break

    return account_map


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def build_account_map(session: requests.Session, org_id: str,
                      max_retries: int, backoff: int) -> dict[str, str]:
    """Build a full account_id -> email map across all directories."""
    combined: dict[str, str] = {}

    directories = fetch_directories(session, org_id, max_retries, backoff)

    print(f"\nFetching users from {len(directories)} "
          f"director{'y' if len(directories) == 1 else 'ies'} …")
    for directory in directories:
        partial = fetch_users_in_directory(session, org_id, directory,
                                           max_retries, backoff)
        combined.update(partial)

    print("\nFetching org-level users (v1) …")
    for acct_id, email in fetch_org_level_users(
            session, org_id, max_retries, backoff).items():
        if acct_id not in combined:          # directory data takes priority
            combined[acct_id] = email

    print(f"\nTotal unique accounts resolved: {len(combined)}\n")
    return combined


# ---------------------------------------------------------------------------
# CSV processing
# ---------------------------------------------------------------------------

def load_csv(input_path: str, user_id_col: str) -> tuple[list[dict], list[str]]:
    """Read the CSV and return (rows, fieldnames). Validates required column."""
    with open(input_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            print("ERROR: CSV file appears to be empty.", file=sys.stderr)
            sys.exit(1)
        if user_id_col not in reader.fieldnames:
            print(f"ERROR: Column '{user_id_col}' not found in CSV.\n"
                  f"  Available columns: {list(reader.fieldnames)}", file=sys.stderr)
            sys.exit(1)
        return list(reader), list(reader.fieldnames)


def enrich_rows(rows: list[dict], account_map: dict[str, str],
                fieldnames: list[str],
                user_id_col: str, email_col: str) -> tuple[list[dict], int, int]:
    """
    Add/overwrite the email_col for each row.
    Inserts email_col right after user_id_col if it doesn't exist yet.
    Returns (enriched_rows, found_count, not_found_count).
    """
    if email_col not in fieldnames:
        idx = fieldnames.index(user_id_col) + 1
        fieldnames.insert(idx, email_col)

    found = not_found = 0
    for row in rows:
        acct_id = row.get(user_id_col, "").strip()
        if acct_id in account_map:
            row[email_col] = account_map[acct_id]
            found += 1
        else:
            if not row.get(email_col, "").strip():
                row[email_col] = "NOT_FOUND"
            not_found += 1

    return rows, found, not_found


def print_dry_run(rows: list[dict], user_id_col: str, email_col: str) -> None:
    """Print a preview table of resolved emails without writing any files."""
    col_w = 45
    print("\n── DRY RUN PREVIEW ─────────────────────────────────────────────")
    print(f"  {'user_id':<{col_w}}  {'email'}")
    print(f"  {'-'*col_w}  {'-'*35}")
    for row in rows:
        uid   = row.get(user_id_col, "")
        email = row.get(email_col, "")
        print(f"  {uid:<{col_w}}  {email}")
    print("────────────────────────────────────────────────────────────────\n")


def write_csv(output_path: str, rows: list[dict], fieldnames: list[str]) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve Atlassian Account IDs in a CSV to email addresses."
    )
    parser.add_argument("--config", "-c", default=DEFAULT_CONFIG_FILE,
                        help=f"Path to the .properties config file (default: {DEFAULT_CONFIG_FILE}).")
    parser.add_argument("--input", "-i", default=None,
                        help="Input CSV path (overrides INPUT_FILE in config).")
    parser.add_argument("--output", "-o", default=None,
                        help="Output CSV path (overrides OUTPUT_FILE in config). "
                             "Omit to overwrite the input file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch emails and print a preview table — do NOT write any files.")
    parser.add_argument("--org-id",         default=None, help="Override ATLASSIAN_ORG_ID.")
    parser.add_argument("--admin-email",    default=None, help="Override ATLASSIAN_ADMIN_EMAIL.")
    parser.add_argument("--api-token",      default=None, help="Override ATLASSIAN_API_TOKEN.")
    parser.add_argument("--user-id-column", default=None, help="Override CSV_USER_ID_COLUMN.")
    parser.add_argument("--email-column",   default=None, help="Override CSV_EMAIL_COLUMN.")
    args = parser.parse_args()

    props = load_properties(args.config)
    cfg = build_config(props, {
        "org_id":         args.org_id,
        "admin_email":    args.admin_email,
        "api_token":      args.api_token,
        "input_file":     args.input,
        "output_file":    args.output,
        "user_id_column": args.user_id_column,
        "email_column":   args.email_column,
    })

    validate_config(cfg)

    # Resolve file paths (supports relative-to-repo-root and absolute)
    input_path  = resolve_file_path(cfg["input_file"])
    output_path = resolve_file_path(cfg["output_file"]) or input_path

    if not os.path.isfile(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    # Print run summary
    print(f"Config file    : {args.config}")
    print(f"Org ID         : {cfg['org_id']}")
    print(f"Admin email    : {cfg['admin_email']}")
    print(f"Input CSV      : {input_path}")
    print(f"Output CSV     : {'(dry run — no file written)' if args.dry_run else output_path}")
    print(f"user_id column : {cfg['user_id_column']}")
    print(f"email column   : {cfg['email_column']}")
    if args.dry_run:
        print("Mode           : DRY RUN")
    print()

    # Fetch data from Atlassian API
    session     = _make_session(cfg["api_token"])
    account_map = build_account_map(session, cfg["org_id"],
                                    cfg["max_retries"], cfg["retry_backoff"])

    # Load and enrich CSV
    rows, fieldnames = load_csv(input_path, cfg["user_id_column"])
    rows, found, not_found = enrich_rows(
        rows, account_map, fieldnames,
        cfg["user_id_column"], cfg["email_column"]
    )

    if args.dry_run:
        print_dry_run(rows, cfg["user_id_column"], cfg["email_column"])
        print(f"  ✓ Would resolve : {found}")
        print(f"  ✗ Not in org    : {not_found}")
        print("\nDry run complete — no files were written.")
    else:
        write_csv(output_path, rows, fieldnames)
        print(f"Results written to : {output_path}")
        print(f"  ✓ Emails resolved : {found}")
        print(f"  ✗ Not found in org: {not_found}")


if __name__ == "__main__":
    main()
