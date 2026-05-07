#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".codex"


def iter_credential_candidates(filename: str) -> List[Tuple[Path, bool]]:
    return [
        (codex_home() / "credentials" / "google-workspace" / filename, False),
        (Path.home() / ".codex" / "credentials" / "google-workspace" / filename, False),
        (Path.home() / ".claude" / "credentials" / "google-workspace" / filename, True),
    ]


def resolve_google_workspace_path(explicit_path: str, env_var: str, filename: str) -> Path:
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


def resolve_google_workspace_credentials(
    credentials_file: str,
    authorized_user_file: str,
) -> Tuple[Path, Path]:
    return (
        resolve_google_workspace_path(credentials_file, "GOOGLE_WORKSPACE_CREDENTIALS", "credentials.json"),
        resolve_google_workspace_path(authorized_user_file, "GOOGLE_WORKSPACE_TOKEN", "authorized_user.json"),
    )


def parse_plan(value: str) -> Tuple[str, int, int]:
    parts = value.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            f"Invalid plan {value!r}. Expected format worksheet:source_row:target_row"
        )
    title, source_row, target_row = parts
    return title, int(source_row), int(target_row)


def row_in_first_col_merge(row_num: int, merges: List[dict]) -> bool:
    for merge in merges:
        if merge.get("startColumnIndex") == 0 and merge.get("endColumnIndex") == 1:
            if merge.get("startRowIndex", -1) < row_num <= merge.get("endRowIndex", -1):
                return True
    return False


def build_copy_requests(
    sheet_id: int,
    source_row: int,
    target_row: int,
    start_column: int,
    end_column: int,
) -> List[dict]:
    source = {
        "sheetId": sheet_id,
        "startRowIndex": source_row - 1,
        "endRowIndex": source_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }
    destination = {
        "sheetId": sheet_id,
        "startRowIndex": target_row - 1,
        "endRowIndex": target_row,
        "startColumnIndex": start_column,
        "endColumnIndex": end_column,
    }
    return [
        {
            "copyPaste": {
                "source": source,
                "destination": destination,
                "pasteType": "PASTE_FORMAT",
                "pasteOrientation": "NORMAL",
            }
        },
        {
            "copyPaste": {
                "source": source,
                "destination": destination,
                "pasteType": "PASTE_DATA_VALIDATION",
                "pasteOrientation": "NORMAL",
            }
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy Google Sheets row formatting and data validation from source rows to target rows."
    )
    parser.add_argument("--sheet-url", required=True, help="Google Sheets URL")
    parser.add_argument("--credentials", default="", help="Google Workspace OAuth credentials JSON path.")
    parser.add_argument("--authorized-user", default="", help="Google Workspace authorized user JSON path.")
    parser.add_argument(
        "--plan",
        action="append",
        required=True,
        type=parse_plan,
        help="Formatting copy plan in worksheet:source_row:target_row format. Repeat for multiple rows.",
    )
    args = parser.parse_args()

    credentials_path, token_path = resolve_google_workspace_credentials(
        args.credentials,
        args.authorized_user,
    )
    client = gspread.oauth(
        credentials_filename=str(credentials_path),
        authorized_user_filename=str(token_path),
        scopes=SCOPES,
    )
    spreadsheet = client.open_by_url(args.sheet_url)
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    service = build("sheets", "v4", credentials=creds)
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet.id,
        includeGridData=False,
    ).execute()
    merge_map: Dict[str, List[dict]] = {
        sheet["properties"]["title"]: sheet.get("merges", [])
        for sheet in metadata.get("sheets", [])
    }

    requests: List[dict] = []
    for title, source_row, target_row in args.plan:
        worksheet = spreadsheet.worksheet(title)
        used_column_count = max((len(row) for row in worksheet.get_all_values()), default=0)
        end_column = used_column_count or worksheet.col_count
        merges = merge_map.get(title, [])
        start_column = 1 if row_in_first_col_merge(source_row, merges) or row_in_first_col_merge(target_row, merges) else 0
        requests.extend(build_copy_requests(worksheet.id, source_row, target_row, start_column, end_column))

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet.id,
        body={"requests": requests},
    ).execute()

    for title, source_row, target_row in args.plan:
        print(f"{title}: copied row {source_row} format+validation to row {target_row}")


if __name__ == "__main__":
    main()
