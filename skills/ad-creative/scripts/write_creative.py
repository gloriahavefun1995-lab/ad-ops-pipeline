#!/usr/bin/env python3
"""Write generated ad-creative copy back to a Google Sheet.

Replaces the manual `values.batchUpdate` boilerplate that agents previously had
to write inline.  Handles:

  - 7-col (English / Chinese) and 8-col (other languages with 翻译) optimization
    area layouts automatically
  - CJK character counting (zh/ja/ko x2 per Google Ads spec)
  - Performance state transition: pending row's `进行中` → `待确认` once copy is
    filled (matching kept-tab convention)
  - Per-row character-limit validation before any write happens

Input: a JSON array of items, each:
  {
    "tab": "<sheet_name>",
    "row": <1-based row number>,
    "asset_type": "Headline" | "Description",
    "asset": "<new copy text>",
    "translation": "<English translation>",   # optional, only for non-en/zh tabs
    "strategy": "<中文优化思路>",
    "round_label": "二轮" | "一轮优化" | ...    # optional; preserve existing if absent
  }

The script auto-detects each tab's column layout (col_map) by inspecting the
optimization-area header row.  It NEVER writes outside the targeted row, so
neighbouring data is untouched.

Usage:
  python3 scripts/write_creative.py \
    --sheet-url "https://docs.google.com/.../edit" \
    --input creative.json \
    [--credentials credentials.json] \
    [--write-date 5/7] \
    [--perf-after 待确认] \
    [--dry-run]
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
except ImportError:
    print("请先安装依赖：pip install google-auth google-api-python-client", file=sys.stderr)
    sys.exit(1)

DEFAULT_CREDS = Path.home() / ".claude/credentials/google-workspace/authorized_user.json"

OPTIMIZATION_HEADER_ALIASES = {
    "asset_type": ["Asset type", "素材类型"],
    "round": ["优化轮次", "对比方案", "轮次"],
    "perf": ["Performance", "表现"],
    "rank": ["Cost排序", "消耗排名"],
    "ctr": ["Ctr", "CTR", "点击率"],
    "asset": ["Asset", "素材", "新文案"],
    "translation": ["翻译", "Translation"],
    "strategy": ["优化思路", "思路"],
    "chars": ["字符数", "字数"],
    "period": ["数据周期", "周期"],
}

CJK_LANG_HINTS = {"日语", "ja", "jp", "中文", "zh", "cn", "tw", "hk", "韩语", "ko", "kr"}

LIMITS = {"Headline": 30, "Description": 90}


def _looks_like_cjk_tab(tab: str) -> bool:
    suffix = tab.split("-", 1)[1] if "-" in tab else ""
    return suffix.lower().strip() in CJK_LANG_HINTS or suffix.strip() in CJK_LANG_HINTS


def char_count(text: str, tab: str) -> int:
    """Google Ads counts CJK chars (zh/ja/ko) as 2; others as 1."""
    if not _looks_like_cjk_tab(tab):
        return len(text)
    n = 0
    for ch in text:
        cp = ord(ch)
        # Hiragana/Katakana/CJK Unified/Hangul Syllables — count as 2
        if (0x3040 <= cp <= 0x30FF) or (0x4E00 <= cp <= 0x9FFF) or (0xAC00 <= cp <= 0xD7AF):
            n += 2
        else:
            n += 1
    return n


def get_spreadsheet_id(url: str) -> str:
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        raise ValueError(f"无法从 URL 中解析 spreadsheet ID：{url}")
    return m.group(1)


def load_credentials(creds_path: Path) -> Credentials:
    with creds_path.open("r", encoding="utf-8") as fh:
        info = json.load(fh)
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


def detect_col_map(header_row: List[str]) -> Dict[str, Optional[int]]:
    """Map alias key → 0-based column index based on the actual header row."""
    col_map: Dict[str, Optional[int]] = {}
    for key, aliases in OPTIMIZATION_HEADER_ALIASES.items():
        col_map[key] = None
        for idx, cell in enumerate(header_row):
            if (cell or "").strip() in aliases:
                col_map[key] = idx
                break
    return col_map


def find_opt_header_row(rows: List[List[str]]) -> int:
    """Find the optimization area header row (1-based).  Looks for the row that
    contains both the 'Asset' alias and the round (对比方案/优化轮次) alias."""
    asset_aliases = set(OPTIMIZATION_HEADER_ALIASES["asset"])
    round_aliases = set(OPTIMIZATION_HEADER_ALIASES["round"])
    last = None
    for idx, row in enumerate(rows, start=1):
        cells = {(c or "").strip() for c in row}
        if cells & asset_aliases and cells & round_aliases:
            last = idx
    if last is None:
        raise RuntimeError("未找到优化区表头行（包含 Asset / 对比方案）")
    return last


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-url", required=True)
    parser.add_argument("--input", required=True, type=Path,
                        help="JSON file with array of {tab,row,asset_type,asset,translation?,strategy,round_label?}")
    parser.add_argument("--credentials", type=Path, default=DEFAULT_CREDS)
    parser.add_argument("--write-date", default=None,
                        help="数据周期 value to write, e.g. '5/7'.  Default: today as M/D")
    parser.add_argument("--perf-after", default="待确认",
                        help="Performance value to set on filled rows (default: 待确认)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate inputs (incl. char limits) and print plan without writing")
    args = parser.parse_args()

    if not args.credentials.exists():
        print(f"❌ 凭证不存在：{args.credentials}", file=sys.stderr)
        return 2

    items = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        print("❌ --input must contain a JSON array", file=sys.stderr)
        return 2

    write_date = args.write_date or f"{date.today().month}/{date.today().day}"

    # ── Validate char limits ────────────────────────────────────────────────
    overruns = []
    for it in items:
        n = char_count(it["asset"], it["tab"])
        it["__char_count"] = n
        limit = LIMITS.get(it["asset_type"])
        if limit and n > limit:
            overruns.append({"tab": it["tab"], "row": it["row"],
                             "asset_type": it["asset_type"], "char_count": n, "limit": limit})

    if overruns:
        print(json.dumps({"status": "char_limit_exceeded", "overruns": overruns},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 3

    creds = load_credentials(args.credentials)
    svc = build("sheets", "v4", credentials=creds)
    spreadsheet_id = get_spreadsheet_id(args.sheet_url)

    # ── Group by tab to minimise reads ───────────────────────────────────────
    items_by_tab: Dict[str, List[dict]] = {}
    for it in items:
        items_by_tab.setdefault(it["tab"], []).append(it)

    # Single batchGet for all tabs (1 read total instead of N)
    ranges = [f"'{tab}'!A1:H80" for tab in items_by_tab]
    res = svc.spreadsheets().values().batchGet(
        spreadsheetId=spreadsheet_id, ranges=ranges,
    ).execute()

    tab_layouts: Dict[str, Dict] = {}
    for tab, vr in zip(items_by_tab.keys(), res.get("valueRanges", [])):
        rows = vr.get("values", [])
        try:
            opt_header_row = find_opt_header_row(rows)
        except RuntimeError as exc:
            print(f"[skip] {tab}: {exc}", file=sys.stderr)
            continue
        header_row = rows[opt_header_row - 1]
        col_map = detect_col_map(header_row)
        tab_layouts[tab] = {
            "rows": rows,
            "opt_header_row": opt_header_row,
            "col_map": col_map,
            "col_count": len(header_row),
        }

    # ── Build batchUpdate writes ────────────────────────────────────────────
    data_writes: List[dict] = []
    plan: List[dict] = []
    for tab, items_list in items_by_tab.items():
        layout = tab_layouts.get(tab)
        if layout is None:
            continue
        col_map = layout["col_map"]
        col_count = layout["col_count"]

        for it in items_list:
            row_idx = it["row"]
            # Build a row write that only updates specific columns; preserve the rest.
            row_values = [""] * col_count
            existing = layout["rows"][row_idx - 1] if row_idx - 1 < len(layout["rows"]) else []
            for i in range(min(col_count, len(existing))):
                row_values[i] = existing[i]

            updates: Dict[str, str] = {
                "asset": it["asset"],
                "strategy": it.get("strategy", ""),
                "chars": str(it["__char_count"]),
                "perf": args.perf_after,
                "period": write_date,
            }
            if "translation" in it and col_map.get("translation") is not None:
                updates["translation"] = it["translation"]
            if "round_label" in it and col_map.get("round") is not None and it.get("round_label"):
                updates["round"] = it["round_label"]

            for key, val in updates.items():
                ci = col_map.get(key)
                if ci is None:
                    continue
                row_values[ci] = val

            rng = f"'{tab}'!A{row_idx}:{chr(65 + col_count - 1)}{row_idx}"
            data_writes.append({"range": rng, "values": [row_values]})
            plan.append({"tab": tab, "row": row_idx, "asset_type": it["asset_type"],
                         "char_count": it["__char_count"], "asset_preview": it["asset"][:60]})

    if args.dry_run:
        print(json.dumps({
            "status": "dry_run",
            "would_write_rows": len(data_writes),
            "tabs_affected": sorted(items_by_tab.keys()),
            "plan": plan,
        }, ensure_ascii=False, indent=2))
        return 0

    if not data_writes:
        print(json.dumps({"status": "nothing_to_write"}, ensure_ascii=False))
        return 0

    resp = svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data_writes},
    ).execute()

    print(json.dumps({
        "status": "ok",
        "rows_written": len(data_writes),
        "cells_updated": resp.get("totalUpdatedCells"),
        "sheets_touched": resp.get("totalUpdatedSheets"),
        "tabs": sorted(items_by_tab.keys()),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
