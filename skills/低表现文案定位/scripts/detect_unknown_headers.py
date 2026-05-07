#!/usr/bin/env python3
"""Detect which optimization-area columns in the target sheets are unrecognized.

Run this BEFORE `sync_low_assets.py` whenever the user's table may have
non-default column names. The output JSON tells the agent exactly which
"用途" keys still have no header match, and which header cells in the sheet
were not matched to any known key — so the agent can ask the user to map them
and persist the answer in `README.md` (字段名映射 table) before running sync.

Usage:
    python3 scripts/detect_unknown_headers.py \
        --sheet-url "https://..." \
        --sheets "tab1" "tab2" ...

Output (stdout JSON):
    {
      "readme_overrides_applied": [...],
      "sheets_checked": [...],
      "per_sheet": {
        "tab1": {
          "header_row_number": 17,
          "matched": {"asset_type": "Asset type", "round": "对比方案", ...},
          "unmapped_keys": ["rank", "ctr"],
          "unknown_in_sheet": ["对比方案"]
        },
        ...
      },
      "consolidated": {
        "unmapped_keys_any_sheet": ["rank", "ctr"],
        "unknown_in_any_sheet": [{"header": "对比方案", "sheets": ["tab1"]}],
        "needs_user_input": true
      }
    }

Exit codes:
    0 — all required keys matched in every sheet (or only optional keys missing)
    2 — at least one required key (asset, strategy) unmapped in some sheet
    1 — runtime error
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Reuse logic from the main script
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_low_assets import (  # type: ignore
    OPTIMIZATION_HEADER_ALIASES,
    apply_user_field_aliases_from_readme,
    detect_sheet_layout,
    load_client,
    normalize_header_label,
    resolve_google_workspace_credentials,
    user_skill_root,
)

# 用途 (display label) <-> alias key — kept in sync with README_FIELD_LABEL_TO_KEY
KEY_TO_DISPLAY_LABEL = {
    "asset_type": "素材类型",
    "round": "优化轮次",
    "perf": "表现标签",
    "rank": "消耗排名",
    "ctr": "点击率",
    "asset": "新文案",
    "translation": "翻译",
    "strategy": "优化思路",
    "chars": "字符数",
    "period": "数据周期",
}
# Required keys that must match — script will fail at sync time without these.
REQUIRED_KEYS = {"asset", "strategy"}


def _fallback_header_row(values: List[List[str]]) -> Optional[int]:
    """When detect_sheet_layout fails, scan for the row that looks most like an
    optimization-area header — i.e. the row containing the most cells that
    match ANY known alias (across all keys). 'Asset type' appears in both raw-
    data and optimization sections, so a max-match heuristic is more robust
    than first-match.

    Returns 1-based row number, or None if no candidate has at least 2 matches.
    """
    all_aliases: set = set()
    for aliases in OPTIMIZATION_HEADER_ALIASES.values():
        all_aliases.update(normalize_header_label(a) for a in aliases)

    best_idx: Optional[int] = None
    best_score = 0
    for idx, row in enumerate(values, start=1):
        score = sum(1 for cell in row if normalize_header_label(cell) in all_aliases)
        if score > best_score:
            best_score = score
            best_idx = idx
    return best_idx if best_score >= 2 else None


def detect_for_sheet(ws_values: List[List[str]]) -> dict:
    """Inspect one worksheet's values; return matched/unmapped/unknown breakdown.

    Returns dict with keys: header_row_number (int|None), matched (dict),
    unmapped_keys (list[str]), unknown_in_sheet (list[str]), error (str|None),
    used_fallback (bool).
    """
    used_fallback = False
    error: Optional[str] = None
    try:
        layout = detect_sheet_layout(ws_values)
        header_row_idx = layout["optimization_header_row"]
    except RuntimeError as exc:
        # Layout detection requires asset_type+round+perf in one row. If the
        # user's table renames any of those, layout fails — fall back to the
        # 'Asset type' anchor alone so we can still surface unknown headers.
        error = str(exc)
        header_row_idx = _fallback_header_row(ws_values)
        if header_row_idx is None:
            return {
                "header_row_number": None,
                "matched": {},
                "unmapped_keys": list(OPTIMIZATION_HEADER_ALIASES.keys()),
                "unknown_in_sheet": [],
                "error": error,
                "used_fallback": False,
            }
        used_fallback = True
    header_row = ws_values[header_row_idx - 1]
    norm_cells = [(idx, cell, normalize_header_label(cell)) for idx, cell in enumerate(header_row) if cell.strip()]

    matched: Dict[str, str] = {}
    matched_cell_indices: set = set()
    for key, aliases in OPTIMIZATION_HEADER_ALIASES.items():
        norm_aliases = {normalize_header_label(a) for a in aliases}
        for idx, raw, norm in norm_cells:
            if norm in norm_aliases:
                matched[key] = raw
                matched_cell_indices.add(idx)
                break

    unmapped_keys = [k for k in OPTIMIZATION_HEADER_ALIASES if k not in matched]
    unknown_in_sheet = [raw for idx, raw, _ in norm_cells if idx not in matched_cell_indices]
    return {
        "header_row_number": header_row_idx,
        "matched": matched,
        "unmapped_keys": unmapped_keys,
        "unknown_in_sheet": unknown_in_sheet,
        "error": error,
        "used_fallback": used_fallback,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect unmapped column headers in target worksheets.")
    parser.add_argument("--sheet-url", required=True, help="Google Sheets URL or spreadsheet ID.")
    parser.add_argument("sheets", nargs="+", help="One or more worksheet titles to inspect.")
    parser.add_argument("--credentials", help="Google Workspace OAuth credentials JSON path.")
    parser.add_argument("--authorized-user", help="Google Workspace authorized user JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Apply README overrides FIRST so we don't ask about already-mapped fields.
    overrides = apply_user_field_aliases_from_readme(user_skill_root() / "README.md")

    credentials_path, token_path = resolve_google_workspace_credentials(args.credentials, args.authorized_user)
    client = load_client(credentials_path, token_path)
    spreadsheet = client.open_by_url(args.sheet_url)

    per_sheet: Dict[str, dict] = {}
    for title in args.sheets:
        try:
            ws = spreadsheet.worksheet(title)
        except Exception as exc:
            per_sheet[title] = {"error": f"worksheet not found: {exc}"}
            continue
        values = ws.get_all_values()
        per_sheet[title] = detect_for_sheet(values)

    # Consolidate across sheets
    unmapped_any: set = set()
    unknown_collector: Dict[str, List[str]] = {}
    has_required_gap = False
    for title, info in per_sheet.items():
        if info.get("error") and "header_row_number" not in info:
            continue
        for k in info.get("unmapped_keys", []):
            unmapped_any.add(k)
            if k in REQUIRED_KEYS:
                has_required_gap = True
        for h in info.get("unknown_in_sheet", []):
            unknown_collector.setdefault(h, []).append(title)

    needs_user_input = bool(unmapped_any) and bool(unknown_collector)
    consolidated = {
        "unmapped_keys_any_sheet": sorted(unmapped_any),
        "unmapped_display_labels": [KEY_TO_DISPLAY_LABEL.get(k, k) for k in sorted(unmapped_any)],
        "unknown_in_any_sheet": [
            {"header": h, "sheets": s} for h, s in sorted(unknown_collector.items())
        ],
        "needs_user_input": needs_user_input,
        "has_required_field_gap": has_required_gap,
    }

    # When the user/agent needs to make a mapping decision, surface concrete
    # next-step guidance so they don't have to dig through README + script.
    if needs_user_input:
        suggestions = []
        # Pick the most likely candidate header per unmapped purpose, preferring
        # the most-shared header across sheets (highest sheet count).
        candidates_sorted = sorted(
            unknown_collector.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        for purpose_key in sorted(unmapped_any):
            display = KEY_TO_DISPLAY_LABEL.get(purpose_key, purpose_key)
            if candidates_sorted:
                top = candidates_sorted[0]
                suggestions.append({
                    "purpose_key": purpose_key,
                    "display_label": display,
                    "candidate_headers": [h for h, _ in candidates_sorted[:5]],
                    "ask_user": (
                        f"你的表里 `{display}` 列叫什么？候选：{', '.join(h for h, _ in candidates_sorted[:5])}（或回答\"无此列\"跳过）"
                    ),
                    "readme_edit_hint": (
                        f"在低表现文案定位/README.md 的「字段名映射」表里把 `{display}` 行的「你的表格标题」列填上对应表头名。"
                    ),
                })
        consolidated["resolution_hint"] = (
            "存在未匹配的「用途字段」+ 无法识别的「实际表头」。"
            "对每个 purpose_key 选一个候选表头（或确认无此列），在 README 字段映射里写入对应行后重跑本脚本。"
        )
        consolidated["suggestions"] = suggestions

    if has_required_gap:
        consolidated["resolution_hint_required"] = (
            "必填字段（asset / strategy）在某个 sheet 完全缺失或所在 sheet 的优化区不存在。"
            "若是「优化区域不存在」(error: Missing optimization section header)：可让该 sheet 跳过；"
            "若是「优化区存在但缺必填列」：必须在表头里加上对应列，否则后续无法插入文案行。"
        )

    output = {
        "readme_overrides_applied": overrides,
        "sheets_checked": list(args.sheets),
        "per_sheet": per_sheet,
        "consolidated": consolidated,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))

    return 2 if has_required_gap else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
