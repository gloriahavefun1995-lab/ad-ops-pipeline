#!/usr/bin/env python3
"""
读取 Google Sheets 所有 sheet（含隐藏），与传入的广告组列表对比：
- sheet 名中不含 ad group id → 新建 sheet（命名：{id}-{lang_key}）
- 存在且可见 → 保持不动
- 存在但隐藏 → 取消隐藏
- 可见但不在收集组中 → 隐藏 sheet（仅处理名称含 10 位以上数字 ID 的 sheet）
输出 JSON 到 stdout
"""
import argparse
import json
import re
import sys
from pathlib import Path

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
except ImportError:
    print("请先安装依赖：pip install google-auth google-api-python-client", file=sys.stderr)
    sys.exit(1)

DEFAULT_CREDS_PATH = Path.home() / ".claude/credentials/google-workspace/authorized_user.json"


def get_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"无法从 URL 中解析 spreadsheet ID：{url}")
    return m.group(1)


def load_credentials(creds_path: Path) -> Credentials:
    with open(creds_path) as f:
        info = json.load(f)
    creds = Credentials(
        token=info.get("token"),
        refresh_token=info.get("refresh_token"),
        token_uri=info.get("token_uri"),
        client_id=info.get("client_id"),
        client_secret=info.get("client_secret"),
        scopes=info.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-url", required=True, help="Google Sheets 完整 URL")
    parser.add_argument("--groups", required=True,
                        help='JSON 数组，如 [{"id":"123","lang_key":"en"},...]')
    parser.add_argument("--credentials", default=None,
                        help=f"凭证文件路径（默认：{DEFAULT_CREDS_PATH}）")
    args = parser.parse_args()

    creds_path = Path(args.credentials) if args.credentials else DEFAULT_CREDS_PATH
    if not creds_path.exists():
        print(f"❌ 凭证文件不存在：{creds_path}", file=sys.stderr)
        print("请通过 --credentials 参数指定正确路径，或将凭证放至默认位置", file=sys.stderr)
        sys.exit(1)

    groups = json.loads(args.groups)
    spreadsheet_id = get_spreadsheet_id(args.sheet_url)
    creds = load_credentials(creds_path)
    service = build("sheets", "v4", credentials=creds)

    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing_sheets = [
        {
            "sheet_id": s["properties"]["sheetId"],
            "title": s["properties"]["title"],
            "hidden": s["properties"].get("hidden", False),
        }
        for s in meta.get("sheets", [])
    ]

    target_ids = {g["id"] for g in groups}

    kept = []
    to_unhide = []
    to_create = []
    to_hide = []

    # ── 操作1/2/3：对比收集到的广告组 ──────────────────────────────
    for group in groups:
        ad_id = group["id"]
        matched = [s for s in existing_sheets if ad_id in s["title"]]

        if not matched:
            to_create.append(group)
        else:
            visible = [s for s in matched if not s["hidden"]]
            if visible:
                kept.append({"id": ad_id, "sheet_name": visible[0]["title"]})
            else:
                to_unhide.append({"id": ad_id, "sheet": matched[0]})

    # ── 操作4：可见但不在收集组中的广告组 sheet → 隐藏 ──────────────
    for sheet in existing_sheets:
        if sheet["hidden"]:
            continue
        # 只处理名称中含 10位以上数字 ID 的 sheet（广告组 sheet 特征）
        if not re.search(r'\d{10,}', sheet["title"]):
            continue
        if not any(ad_id in sheet["title"] for ad_id in target_ids):
            to_hide.append(sheet)

    # ── 批量执行 ────────────────────────────────────────────────────
    requests = []

    for item in to_unhide:
        requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": item["sheet"]["sheet_id"], "hidden": False},
                "fields": "hidden",
            }
        })

    for group in to_create:
        requests.append({
            "addSheet": {
                "properties": {"title": f"{group['id']}-{group.get('lang_key', '')}"}
            }
        })

    for sheet in to_hide:
        requests.append({
            "updateSheetProperties": {
                "properties": {"sheetId": sheet["sheet_id"], "hidden": True},
                "fields": "hidden",
            }
        })

    unhidden_result = []
    created_result = []
    hidden_result = []

    if requests:
        response = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
        replies = response.get("replies", [])

        reply_idx = 0
        for item in to_unhide:
            unhidden_result.append({"id": item["id"], "sheet_name": item["sheet"]["title"]})
            reply_idx += 1

        for group in to_create:
            sheet_name = f"{group['id']}-{group.get('lang_key', '')}"
            new_sheet_id = None
            if reply_idx < len(replies) and "addSheet" in replies[reply_idx]:
                new_sheet_id = replies[reply_idx]["addSheet"]["properties"]["sheetId"]
            created_result.append({
                "id": group["id"],
                "sheet_name": sheet_name,
                "new_sheet_id": new_sheet_id,
            })
            reply_idx += 1

        for sheet in to_hide:
            hidden_result.append({"sheet_name": sheet["title"]})
            reply_idx += 1

    result = {
        "kept": kept,
        "unhidden": unhidden_result,
        "created": created_result,
        "hidden": hidden_result,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
