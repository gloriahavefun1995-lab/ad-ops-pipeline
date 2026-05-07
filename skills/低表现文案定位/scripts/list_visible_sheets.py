#!/usr/bin/env python3
"""List visible sheets between two anchor tabs in a Google Spreadsheet.

Usage:
    python list_visible_sheets.py --sheet-url URL --from-sheet NAME --to-sheet NAME

Outputs a JSON array of visible sheet names in tab order between the two anchors
(inclusive). Hidden sheets (tab hidden via Google Sheets UI) are excluded.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env).expanduser() if env else Path.home() / ".codex"


def iter_credential_candidates(filename: str) -> List[Tuple[Path, bool]]:
    return [
        (codex_home() / "credentials" / "google-workspace" / filename, False),
        (Path.home() / ".codex" / "credentials" / "google-workspace" / filename, False),
        (Path.home() / ".claude" / "credentials" / "google-workspace" / filename, True),
    ]


def resolve_google_workspace_path(explicit_path: Optional[str], env_var: str, filename: str) -> Path:
    candidates: List[Tuple[Path, bool]] = []
    if explicit_path:
        candidates.append((Path(explicit_path).expanduser(), False))
    env_value = os.environ.get(env_var)
    if env_value:
        candidates.append((Path(env_value).expanduser(), False))
    candidates.extend(iter_credential_candidates(filename))

    for path, is_legacy in candidates:
        if not path.exists():
            continue
        if is_legacy:
            print(
                f"Deprecated Google Workspace credential path in use: {path}. "
                "Move credentials into $CODEX_HOME/credentials/google-workspace/ when possible.",
                file=sys.stderr,
            )
        return path

    raise RuntimeError(
        f"Missing Google Workspace {filename}. Provide it via CLI, environment variables, "
        "or place it under $CODEX_HOME/credentials/google-workspace/."
    )


def extract_spreadsheet_id(sheet_url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheet_url)
    if m:
        return m.group(1)
    # Treat bare ID
    if re.fullmatch(r"[a-zA-Z0-9_-]+", sheet_url):
        return sheet_url
    raise ValueError(f"Cannot extract spreadsheet ID from: {sheet_url}")


def get_visible_sheets_in_range(
    spreadsheet_id: str,
    from_sheet: str,
    to_sheet: str,
    token_path: Path,
    credentials_path: Optional[Path] = None,
) -> List[str]:
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    service = build("sheets", "v4", credentials=creds)

    resp = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(title,hidden))",
    ).execute()

    all_sheets = resp.get("sheets", [])

    # Build ordered list of visible sheet titles
    visible_titles = [
        s["properties"]["title"]
        for s in all_sheets
        if not s["properties"].get("hidden", False)
    ]

    if from_sheet not in visible_titles:
        raise ValueError(f"Anchor sheet not found among visible sheets: {from_sheet!r}")
    if to_sheet not in visible_titles:
        raise ValueError(f"Anchor sheet not found among visible sheets: {to_sheet!r}")

    i = visible_titles.index(from_sheet)
    j = visible_titles.index(to_sheet)
    if i > j:
        i, j = j, i  # allow specifying anchors in either order

    return visible_titles[i : j + 1]


def main():
    parser = argparse.ArgumentParser(description="List visible sheets between two anchor tabs.")
    parser.add_argument("--sheet-url", required=True, help="Google Sheets URL or spreadsheet ID")
    parser.add_argument("--from-sheet", required=True, help="Starting anchor sheet name (inclusive)")
    parser.add_argument("--to-sheet", required=True, help="Ending anchor sheet name (inclusive)")
    parser.add_argument("--credentials", help="OAuth credentials.json path")
    parser.add_argument("--authorized-user", help="authorized_user.json path")
    parser.add_argument(
        "--format",
        choices=["json", "args"],
        default="json",
        help="Output format: json (default) or args (space-separated, shell-quoted)",
    )
    args = parser.parse_args()

    token_path = resolve_google_workspace_path(
        args.authorized_user, "GOOGLE_WORKSPACE_TOKEN", "authorized_user.json"
    )
    credentials_path = resolve_google_workspace_path(
        args.credentials, "GOOGLE_WORKSPACE_CREDENTIALS", "credentials.json"
    )

    spreadsheet_id = extract_spreadsheet_id(args.sheet_url)
    sheets = get_visible_sheets_in_range(
        spreadsheet_id, args.from_sheet, args.to_sheet, token_path, credentials_path
    )

    if args.format == "json":
        print(json.dumps(sheets, ensure_ascii=False, indent=2))
    else:
        import shlex
        print(" ".join(shlex.quote(s) for s in sheets))


if __name__ == "__main__":
    main()
