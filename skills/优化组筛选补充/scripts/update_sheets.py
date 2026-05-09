#!/usr/bin/env python3
"""
读取 Google Sheets 所有 sheet（含隐藏），与传入的广告组列表对比：
- sheet 名中不含 ad group id → 新建 sheet（命名：{id}-{中文语言名}），并预置「优化方案&数据结果」区段（区段标题、列表头、样式、合并），方便下游 `低表现文案定位` skill 直接识别
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

# Languages where the optimization area should NOT include a 翻译 column.
# Everything else gets 8 cols (with 翻译 between Asset and 优化思路).
NO_TRANSLATION_LANGS = {"en", "zh"}

# Loaded lazily — falls back to lang_key as display name if LANG_SPECS unavailable.
_LANG_SPECS_CACHE = None


def get_lang_specs():
    """Import LANG_SPECS from sibling analyze_excel.py.  Returns {} on failure."""
    global _LANG_SPECS_CACHE
    if _LANG_SPECS_CACHE is not None:
        return _LANG_SPECS_CACHE
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from analyze_excel import LANG_SPECS  # type: ignore
        _LANG_SPECS_CACHE = LANG_SPECS
    except Exception:
        _LANG_SPECS_CACHE = {}
    return _LANG_SPECS_CACHE


def chinese_display_name(lang_key: str) -> str:
    """Return Chinese-first display name for a language key.  Falls back to the key."""
    specs = get_lang_specs()
    if lang_key in specs:
        return specs[lang_key].get("display_name") or lang_key
    return lang_key


def has_translation_column(lang_key: str) -> bool:
    """True if optimization area should include a 翻译 column for this language."""
    return lang_key not in NO_TRANSLATION_LANGS


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


def build_optimization_skeleton_requests(sheet_id: int, has_translation: bool) -> list:
    """Return batchUpdate requests that pre-fill the optimization area on a fresh sheet.

    Layout:
      R16: '优化方案&数据结果' (区段标题, A:G 合并, 浅蓝底 #cad9f7, 加粗)
      R17: 表头 — 7 列 (no translation) 或 8 列 (with translation), 浅灰底 #f3f3f3, 加粗居中
      R18+: 数据区基础格式 (灰字 #5f5f5f / Helvetica Neue / 11pt / 左对齐)
            + Performance 列 (col G or F) 下拉验证 ["Low","Good","Best","进行中","待确认"]

    The 区段标题 merges A:G to match the existing kept-tab convention; the
    optional 翻译 column (D) when present extends the data rows but the
    section title row keeps its A:G merge.

    数据区格式 + Performance dropdown 让下游 `低表现文案定位` 插入新行时，
    格式天然继承自这个预设范围，不再依赖参考行复制。
    """
    if has_translation:
        header_row = ["Asset type", "对比方案", "Asset", "翻译", "优化思路", "字符数", "Performance", "数据周期"]
        col_count = 8
    else:
        header_row = ["Asset type", "对比方案", "Asset", "优化思路", "字符数", "Performance", "数据周期"]
        col_count = 7

    bg_section = {"red": 0.788, "green": 0.854, "blue": 0.969}
    bg_header  = {"red": 0.949, "green": 0.949, "blue": 0.949}

    requests = []

    # Write section title (R16, A column only — merge will span A:G visually)
    requests.append({
        "updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": "优化方案&数据结果"}}]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": sheet_id, "rowIndex": 15, "columnIndex": 0},
        }
    })

    # Format section title A16:G16 (7 cols regardless of translation column)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 15, "endRowIndex": 16,
                "startColumnIndex": 0, "endColumnIndex": 7,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg_section,
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": True, "fontSize": 11},
            }},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
        }
    })

    # Merge A16:G16
    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 15, "endRowIndex": 16,
                "startColumnIndex": 0, "endColumnIndex": 7,
            },
            "mergeType": "MERGE_ALL",
        }
    })

    # Write header row at R17
    requests.append({
        "updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": v}} for v in header_row]}],
            "fields": "userEnteredValue",
            "start": {"sheetId": sheet_id, "rowIndex": 16, "columnIndex": 0},
        }
    })

    # Format header row A17:?17
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 16, "endRowIndex": 17,
                "startColumnIndex": 0, "endColumnIndex": col_count,
            },
            "cell": {"userEnteredFormat": {
                "backgroundColor": bg_header,
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": True, "fontSize": 11},
            }},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)",
        }
    })

    # ── 数据区基础格式 (R18:R1000) ─────────────────────────────────────
    # 让下游 `低表现文案定位` 插入的行天然继承字体颜色 / 字号 / 对齐，跟
    # kept tab 视觉一致（gray #5f5f5f, Helvetica Neue, 11pt, 左对齐）。
    # 实测一个 kept tab (159746412889-西班牙语) R19+ 的 effectiveFormat 抄过来。
    data_text_color = {"red": 0.37254903, "green": 0.37254903, "blue": 0.37254903}
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 17, "endRowIndex": 1000,
                "startColumnIndex": 0, "endColumnIndex": col_count,
            },
            "cell": {"userEnteredFormat": {
                "textFormat": {
                    "foregroundColor": data_text_color,
                    "fontFamily": "Helvetica Neue",
                    "fontSize": 11,
                },
                "horizontalAlignment": "LEFT",
                "verticalAlignment": "BOTTOM",
                "wrapStrategy": "OVERFLOW_CELL",
            }},
            "fields": "userEnteredFormat(textFormat.foregroundColor,textFormat.fontFamily,textFormat.fontSize,horizontalAlignment,verticalAlignment,wrapStrategy)",
        }
    })

    # ── Performance 列下拉 (R18:R1000) ─────────────────────────────────
    # 实测 kept tab 的实际下拉值是 ["Low","Good","Best","进行中","待确认"]
    # （不含 "Learning"）；strict=True 限制只能选这 5 个。
    perf_col_idx = 6 if has_translation else 5  # G or F (0-indexed)
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 17, "endRowIndex": 1000,
                "startColumnIndex": perf_col_idx,
                "endColumnIndex": perf_col_idx + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": "Low"},
                        {"userEnteredValue": "Good"},
                        {"userEnteredValue": "Best"},
                        {"userEnteredValue": "进行中"},
                        {"userEnteredValue": "待确认"},
                    ],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }
    })

    return requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-url", required=True, help="Google Sheets 完整 URL")
    parser.add_argument("--groups", required=True,
                        help='JSON 数组，如 [{"id":"123","lang_key":"en"},...] 或 [{"id":"123","lang_key":"en","lang_name":"英语"},...]')
    parser.add_argument("--credentials", default=None,
                        help=f"凭证文件路径（默认：{DEFAULT_CREDS_PATH}）")
    parser.add_argument("--name-style", choices=("chinese", "iso"), default="chinese",
                        help="新建 sheet 命名风格：chinese（默认，{id}-{中文语言名}）/ iso（{id}-{lang_key}）")
    parser.add_argument("--skip-skeleton", action="store_true",
                        help="跳过为新建 sheet 预置优化区结构（默认会预置区段标题 + 表头 + 样式）")
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
        if not re.search(r"\d{10,}", sheet["title"]):
            continue
        if not any(ad_id in sheet["title"] for ad_id in target_ids):
            to_hide.append(sheet)

    # ── 第一阶段批量：unhide / create / hide  ────────────────────────
    requests_phase1 = []

    for item in to_unhide:
        requests_phase1.append({
            "updateSheetProperties": {
                "properties": {"sheetId": item["sheet"]["sheet_id"], "hidden": False},
                "fields": "hidden",
            }
        })

    # Build sheet name per --name-style
    def sheet_name_for(group):
        ad_id = group["id"]
        lang_key = group.get("lang_key", "")
        lang_name = group.get("lang_name")  # explicit override from caller
        if args.name_style == "iso" or not lang_key:
            suffix = lang_key or ""
        else:
            suffix = lang_name or chinese_display_name(lang_key)
        return f"{ad_id}-{suffix}" if suffix else ad_id

    create_plan = []
    for group in to_create:
        sheet_name = sheet_name_for(group)
        requests_phase1.append({
            "addSheet": {"properties": {"title": sheet_name}}
        })
        create_plan.append({"group": group, "sheet_name": sheet_name})

    for sheet in to_hide:
        requests_phase1.append({
            "updateSheetProperties": {
                "properties": {"sheetId": sheet["sheet_id"], "hidden": True},
                "fields": "hidden",
            }
        })

    unhidden_result = []
    created_result = []
    hidden_result = []

    if requests_phase1:
        response = service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests_phase1},
        ).execute()
        replies = response.get("replies", [])

        reply_idx = 0
        for item in to_unhide:
            unhidden_result.append({"id": item["id"], "sheet_name": item["sheet"]["title"]})
            reply_idx += 1

        for plan in create_plan:
            new_sheet_id = None
            if reply_idx < len(replies) and "addSheet" in replies[reply_idx]:
                new_sheet_id = replies[reply_idx]["addSheet"]["properties"]["sheetId"]
            created_result.append({
                "id": plan["group"]["id"],
                "sheet_name": plan["sheet_name"],
                "lang_key": plan["group"].get("lang_key"),
                "new_sheet_id": new_sheet_id,
                "skeleton_added": False,
            })
            reply_idx += 1

        for sheet in to_hide:
            hidden_result.append({"sheet_name": sheet["title"]})
            reply_idx += 1

    # ── 第二阶段批量：为新建 sheet 预置优化区结构 ────────────────────
    if not args.skip_skeleton and created_result:
        skeleton_requests = []
        for entry in created_result:
            sid = entry["new_sheet_id"]
            if sid is None:
                continue
            has_trans = has_translation_column(entry.get("lang_key") or "")
            skeleton_requests.extend(build_optimization_skeleton_requests(sid, has_trans))
            entry["skeleton_added"] = True

        if skeleton_requests:
            # Send in chunks of 50 to stay well under per-request limits
            CHUNK = 50
            for i in range(0, len(skeleton_requests), CHUNK):
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={"requests": skeleton_requests[i:i + CHUNK]},
                ).execute()

    result = {
        "kept": kept,
        "unhidden": unhidden_result,
        "created": created_result,
        "hidden": hidden_result,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
