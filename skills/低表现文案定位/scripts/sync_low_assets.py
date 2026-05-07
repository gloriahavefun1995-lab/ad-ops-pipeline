#!/usr/bin/env python3
"""Filter low-performing KPI ad assets and insert placeholder rows into Google Sheets.

This script queries KPI data, identifies low-performing assets, and inserts
properly formatted empty rows into the optimization area of each target
worksheet.  Content fields (Asset, 优化思路) are left blank for downstream
processes or manual entry to fill in.  The 翻译 field is pre-filled with an
English translation when --translations-json is supplied (used for non-EN/ZH
language assets).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
OUTPUT_DIR = Path("output/low-asset-sheet-sync")  # legacy subdir name; do not rename to avoid breaking existing report paths
SKILL_NAME = "低表现文案定位"

TODAY = date.fromisoformat(os.environ["CODEX_TODAY"]) if os.environ.get("CODEX_TODAY") else date.today()
TODAY_SHORT = f"{TODAY.year % 100}/{TODAY.month}/{TODAY.day}-"

def _build_cn_num_map(max_n: int = 50) -> dict:
    _digits = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
    _tens   = ["", "十", "二十", "三十", "四十", "五十"]
    result = {}
    for n in range(1, max_n + 1):
        t, o = divmod(n, 10)
        s = (_tens[t] if t else "") + (_digits[o] if o else "")
        result[s] = n
    return result

CN_NUMS = _build_cn_num_map(50)
NUM_TO_CN = {v: k for k, v in CN_NUMS.items()}

OPTIMIZATION_HEADER_ALIASES = {
    "asset_type": ["Asset type"],
    "round": ["优化轮次"],
    "perf": ["Performance"],
    "rank": ["Cost排序", "消耗排名"],
    "rank_delta": ["Cost趋势"],
    "ctr": ["Ctr", "CTR"],
    "asset": ["Asset", "素材"],
    "translation": ["翻译", "Translation"],
    "strategy": ["优化思路", "思路"],
    "chars": ["字符数"],
    "period": ["数据周期"],
}

SUMMARY_HEADER_ALIASES = {
    "round": ["优化轮次"],
    "bg_rate": ["Best/Good率", "Best&good率"],
    "count": ["优化组数"],
    "period": ["数据周期"],
    "remark": ["备注"],
}

# 用途列（README "字段名映射" 表第一列）→ alias key
README_FIELD_LABEL_TO_KEY = {
    "素材类型": "asset_type",
    "优化轮次": "round",
    "表现标签": "perf",
    "消耗排名": "rank",
    "点击率": "ctr",
    "新文案": "asset",
    "翻译": "translation",
    "优化思路": "strategy",
    "字符数": "chars",
    "数据周期": "period",
}
# alias key 同时出现在 SUMMARY_HEADER_ALIASES 时也要应用用户配置
README_KEYS_IN_SUMMARY = {"round", "period"}


def apply_user_field_aliases_from_readme(readme_path: Path) -> List[str]:
    """Parse README.md "字段名映射" table; append user-supplied titles to alias dicts.

    Returns a list of human-readable lines describing applied overrides (for logging).
    Silently no-ops if README missing or section absent.
    """
    applied: List[str] = []
    if not readme_path.is_file():
        return applied
    try:
        text = readme_path.read_text(encoding="utf-8")
    except Exception:
        return applied
    # Find the section by header marker
    section_match = re.search(r"##\s*字段名映射\s*\n([\s\S]*?)(?:\n##\s|\Z)", text)
    if not section_match:
        return applied
    section = section_match.group(1)
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        # Skip header / separator rows
        if "用途" in line and "默认值" in line:
            continue
        if re.match(r"^\|\s*-+", line):
            continue
        # Cells: first/last pipes are borders; split and strip
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        label = cells[0]
        user_title = cells[1].strip().strip("`")
        if not user_title:
            continue
        key = README_FIELD_LABEL_TO_KEY.get(label)
        if not key:
            continue
        # Append to OPTIMIZATION_HEADER_ALIASES (and SUMMARY_HEADER_ALIASES if relevant)
        for target in (OPTIMIZATION_HEADER_ALIASES, SUMMARY_HEADER_ALIASES):
            if key in target and user_title not in target[key]:
                target[key].append(user_title)
        applied.append(f"{label} → {user_title} (key={key})")
    return applied

DISPLAY_FIELD_NAMES = {
    "asset_type": "Asset type",
    "round": "优化轮次",
    "perf": "Performance",
    "rank_num": "Cost排序",
    "rank_delta": "Cost趋势",
    "ctr": "Ctr",
    "asset": "Asset",
    "translation": "翻译",
    "strategy": "优化思路",
    "chars": "字符数",
    "period": "数据周期",
    "bg_rate": "Best/Good率",
    "count": "优化组数",
    "remark": "备注",
}

KPI_STATUS_OK = "ok"
KPI_STATUS_EMPTY_DATA = "empty_data"
KPI_STATUS_BUSINESS_ERROR = "business_error"
KPI_STATUS_INVALID_PAYLOAD = "invalid_payload"
KPI_SOURCE_DATA_DIR = "kpi_data_dir"
KPI_SOURCE_SESSION = "kpi_session"

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def col_letter(idx: int) -> str:
    result = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".codex"


def user_skill_root() -> Path:
    # Use script-relative path so the skill dir is found regardless of SKILL_NAME or CODEX_HOME
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Google Workspace credential resolution
# ---------------------------------------------------------------------------

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


def resolve_google_workspace_credentials(
    credentials_file: Optional[str],
    authorized_user_file: Optional[str],
) -> Tuple[Path, Path]:
    creds_path = resolve_google_workspace_path(
        credentials_file, "GOOGLE_WORKSPACE_CREDENTIALS", "credentials.json",
    )
    token_path = resolve_google_workspace_path(
        authorized_user_file, "GOOGLE_WORKSPACE_TOKEN", "authorized_user.json",
    )
    return creds_path, token_path


def load_client(credentials_path: Path, token_path: Path) -> gspread.Client:
    return gspread.oauth(
        credentials_filename=str(credentials_path),
        authorized_user_filename=str(token_path),
        scopes=SCOPES,
    )


def load_sheets_service(token_path: Path):
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    return build("sheets", "v4", credentials=creds)


# ---------------------------------------------------------------------------
# KPI session & data
# ---------------------------------------------------------------------------

def parse_cookie_header(value: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in value.split(";"):
        if "=" not in part:
            continue
        name, cookie_value = part.split("=", 1)
        cookies[name.strip()] = cookie_value.strip()
    return cookies


def load_kpi_session(session_file: Optional[str]):
    import requests
    session = requests.Session()
    cookie_header = os.environ.get("KPI_COOKIE_HEADER", "").strip()
    cookies_json = os.environ.get("KPI_COOKIES_JSON", "").strip()
    session_path = session_file or os.environ.get("KPI_SESSION_FILE", "").strip()

    if cookie_header:
        session.headers["Cookie"] = cookie_header
        for name, value in parse_cookie_header(cookie_header).items():
            session.cookies.set(name, value, domain="kpi.drojian.dev", path="/")
        return session

    if cookies_json:
        parsed = json.loads(cookies_json)
    elif session_path:
        parsed = json.loads(Path(session_path).expanduser().read_text(encoding="utf-8"))
    else:
        raise RuntimeError(
            "Missing KPI session input. Provide --kpi-session-file, KPI_SESSION_FILE, "
            "KPI_COOKIE_HEADER, or KPI_COOKIES_JSON."
        )

    if isinstance(parsed, dict) and isinstance(parsed.get("cookie_header"), str):
        session.headers["Cookie"] = parsed["cookie_header"]
    cookie_items = parsed.get("cookies", parsed) if isinstance(parsed, dict) else parsed
    if not isinstance(cookie_items, list):
        raise RuntimeError("Invalid KPI session payload. Expected a cookie list or {cookies:[...]} object.")
    for cookie in cookie_items:
        if not isinstance(cookie, dict):
            continue
        session.cookies.set(
            cookie.get("name", ""),
            cookie.get("value", ""),
            domain=cookie.get("domain") or "kpi.drojian.dev",
            path=cookie.get("path", "/"),
        )
    return session


def summarize_kpi_payload(payload) -> str:
    if isinstance(payload, dict):
        parts = []
        if payload.get("code") is not None:
            parts.append(f"code={payload.get('code')}")
        if payload.get("msg"):
            parts.append(f"msg={payload.get('msg')}")
        if not parts:
            parts.append(f"keys={','.join(sorted(str(key) for key in payload.keys())[:6])}")
        return ", ".join(parts)
    return f"payload_type={type(payload).__name__}"


def parse_kpi_payload(payload, source: str) -> Tuple[List[dict], str, str]:
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            data = payload["data"]
            status = KPI_STATUS_EMPTY_DATA if not data else KPI_STATUS_OK
            return _parse_kpi_response(data), status, ""
        if payload.get("code") is not None or payload.get("msg") is not None:
            summary = summarize_kpi_payload(payload)
            raise RuntimeError(f"{source}: KPI business error ({summary})")
        raise RuntimeError(f"{source}: Invalid KPI payload shape ({summarize_kpi_payload(payload)})")
    if isinstance(payload, list):
        status = KPI_STATUS_EMPTY_DATA if not payload else KPI_STATUS_OK
        return _parse_kpi_response(payload), status, ""
    raise RuntimeError(f"{source}: Invalid KPI payload type {type(payload).__name__}")


def fetch_text_assets(session, ad_group_id: str, app_id: int, kpi_start: str, kpi_end: str) -> Tuple[List[dict], str, str]:
    url = (
        "https://kpi.drojian.dev/google/asset/index-ajax"
        f"?page=1&limit=50&ad_group_id={ad_group_id}"
        f"&date={kpi_start}+-+{kpi_end}"
        f"&start_date={kpi_start}&end_date={kpi_end}"
        "&sort_field=cost&sort_type=desc&sort_value=3"
        f"&asset=&search_type=0&app_id={app_id}&tag="
    )
    response = session.get(
        url,
        timeout=30,
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        snippet = response.text[:120].strip().replace("\n", " ")
        if "<html" in response.text.lower() or "text/html" in content_type.lower():
            raise RuntimeError(
                f"ad_group_id {ad_group_id}: KPI returned HTML instead of JSON; "
                "the session may be unauthenticated or the endpoint is failing."
            ) from exc
        raise RuntimeError(
            f"ad_group_id {ad_group_id}: KPI returned non-JSON response ({content_type or 'unknown content-type'}): "
            f"{snippet}"
        ) from exc
    return parse_kpi_payload(payload, f"ad_group_id {ad_group_id}")


def _parse_kpi_response(data: list) -> List[dict]:
    """Parse raw KPI API response data into the normalized asset list."""
    text_rows = [row for row in data if str(row.get("asset_type", "")).lower() == "text"]
    return [
        {
            "rank": idx,
            "asset": str(row.get("asset", "")).strip(),
            "perf": str(row.get("performance_label", "")).strip(),
            "ctr": str(row.get("ctr", "")).strip(),
            "type": str(row.get("field_type", "")).strip() or "Headline",
        }
        for idx, row in enumerate(text_rows, start=1)
    ]


def load_kpi_from_file(data_dir: str, ad_group_id: str) -> Tuple[List[dict], str, str]:
    """Load KPI data from a pre-fetched JSON file in *data_dir*.

    Expected file: ``{data_dir}/{ad_group_id}.json``
    The file should contain the raw KPI API JSON response (``{"data": [...]}``).
    """
    path = Path(data_dir).expanduser() / f"{ad_group_id}.json"
    if not path.exists():
        raise RuntimeError(
            f"KPI data file not found: {path}. "
            f"Fetch it first by visiting the KPI API URL for ad_group_id {ad_group_id} "
            "in a logged-in browser and saving the JSON response."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid KPI data file {path}: file is not valid JSON.") from exc
    return parse_kpi_payload(payload, f"KPI data file {path}")


def load_kpi_assets_for_sheet(
    *,
    use_browser_data: bool,
    kpi_data_dir: Optional[str],
    session,
    ad_group_id: str,
    app_id: int,
    kpi_start: str,
    kpi_end: str,
) -> Tuple[List[dict], str, str, str]:
    if use_browser_data:
        assets, status, error = load_kpi_from_file(kpi_data_dir, ad_group_id)
        return assets, status, error, KPI_SOURCE_DATA_DIR
    assets, status, error = fetch_text_assets(session, ad_group_id, app_id, kpi_start, kpi_end)
    return assets, status, error, KPI_SOURCE_SESSION


# ---------------------------------------------------------------------------
# Date window
# ---------------------------------------------------------------------------

def normalize_window_label(value: str) -> str:
    return value.strip().replace(" ", "")


def compute_date_range(window_label: str) -> Tuple[str, str]:
    normalized = normalize_window_label(window_label)
    if normalized in {"近30日", "近30天", "近30日内"}:
        return (TODAY - timedelta(days=30)).isoformat(), (TODAY - timedelta(days=1)).isoformat()
    raise RuntimeError(f"Unsupported query window {window_label!r}. Please extend the script to support it.")


# ---------------------------------------------------------------------------
# Task file parsing (simplified — no strategy fields)
# ---------------------------------------------------------------------------

def parse_task_file(task_file: str) -> dict:
    text = Path(task_file).read_text(encoding="utf-8")

    def extract(label: str) -> str:
        pattern = rf"- {re.escape(label)}：(.+)"
        match = re.search(pattern, text)
        return match.group(1).strip() if match else ""

    sheet_url = extract("在线表格链接")
    app_name = extract("目标 app")
    sheets_raw = extract("本次需要处理的 sheet")
    query_window = extract("本次数据查询口径")
    app_id_raw = extract("KPI app_id")
    sheets = [item.strip() for item in re.split(r"[，,]", sheets_raw) if item.strip()]
    return {
        "sheet_url": sheet_url,
        "app_name": app_name,
        "sheets": sheets,
        "query_window": query_window,
        "app_id": int(app_id_raw) if app_id_raw.isdigit() else None,
    }


# ---------------------------------------------------------------------------
# App ID resolution
# ---------------------------------------------------------------------------

def iter_app_map_candidates(task_file: Optional[str]) -> List[Path]:
    candidates: List[Path] = []
    candidates.append(user_skill_root() / "references" / "app-id-map.json")
    if task_file:
        task_dir = Path(task_file).expanduser().resolve().parent
        candidates.append(task_dir / "app_id_map.json")
    return candidates


def find_app_map_file(app_map_file: Optional[str], task_file: Optional[str]) -> Optional[Path]:
    if app_map_file:
        path = Path(app_map_file).expanduser()
        return path if path.exists() else None
    for path in iter_app_map_candidates(task_file):
        if path.exists():
            return path
    return None


def fetch_app_list_from_kpi(session: "requests.Session") -> list:
    url = "https://kpi.drojian.dev/work/app/index-ajax?page=1&limit=200"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    return r.json().get("data", [])


def load_app_id(
    app_name: str,
    app_map_file: Optional[str],
    cli_app_id: Optional[int],
    task_app_id: Optional[int],
    task_file: Optional[str],
    session: Optional["requests.Session"] = None,
) -> int:
    if cli_app_id is not None:
        return cli_app_id
    if task_app_id is not None:
        return task_app_id
    mapping_path = find_app_map_file(app_map_file, task_file)
    if mapping_path is not None:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        if app_name in mapping:
            return int(mapping[app_name])
    if session is not None and app_name:
        needle = app_name.lower().replace(" ", "").replace("_", "")
        apps = fetch_app_list_from_kpi(session)
        matches = [
            a for a in apps
            if needle in (a.get("app", "") + a.get("pkg", "")).lower().replace(" ", "").replace("_", "")
        ]
        if len(matches) == 1:
            found = matches[0]
            print(f"[app] {app_name!r} → id={found['id']}  ({found['app']})", file=sys.stderr)
            return int(found["id"])
        if len(matches) > 1:
            names = ", ".join(f"{a['app']}(id={a['id']})" for a in matches)
            raise RuntimeError(f"app_name {app_name!r} 匹配到多个 app，请用 --app-id 精确指定：{names}")
    raise RuntimeError(
        f"Could not resolve app_id for app {app_name!r}. "
        "Add it to task md as `- KPI app_id：...` or provide an app-id map."
    )


# ---------------------------------------------------------------------------
# Sheet layout detection
# ---------------------------------------------------------------------------

def normalize_header_label(value: str) -> str:
    return re.sub(r"\s+", "", value.strip()).lower()


def find_header_index_by_aliases(row: List[str], aliases: List[str]) -> Optional[int]:
    normalized_aliases = {normalize_header_label(alias) for alias in aliases}
    for idx, cell in enumerate(row):
        if normalize_header_label(cell) in normalized_aliases:
            return idx
    return None


def find_header_indices_by_aliases(row: List[str], aliases: List[str]) -> List[int]:
    normalized_aliases = {normalize_header_label(alias) for alias in aliases}
    return [idx for idx, cell in enumerate(row) if normalize_header_label(cell) in normalized_aliases]


def row_contains_aliases(row: List[str], aliases: List[str]) -> bool:
    return find_header_index_by_aliases(row, aliases) is not None


def row_contains_all_alias_groups(row: List[str], alias_groups: List[List[str]]) -> bool:
    return all(row_contains_aliases(row, aliases) for aliases in alias_groups)


def detect_sheet_layout(values: List[List[str]]) -> dict:
    _OPT_ALIAS_GROUPS = [
        OPTIMIZATION_HEADER_ALIASES["asset_type"],
        OPTIMIZATION_HEADER_ALIASES["round"],
        OPTIMIZATION_HEADER_ALIASES["perf"],
    ]

    # Pass 1: find the LAST optimization header row.
    # Sheets with multiple optimization sections (e.g. 一轮, 二轮 each with its own header)
    # should always target the most recent (highest row number) section for new inserts.
    optimization_header_row = None
    for idx, row in enumerate(values, start=1):
        if row_contains_all_alias_groups(row, _OPT_ALIAS_GROUPS):
            optimization_header_row = idx  # keep updating → ends up as the last match

    if optimization_header_row is None:
        raise RuntimeError("Missing optimization section header")

    # Pass 2: find summary header strictly after the last optimization header.
    # Using a separate pass avoids mistaking an intermediate optimization-section header
    # (which shares column names like 数据周期 / 对比方案) for a summary row.
    summary_header_row = None
    for idx, row in enumerate(values[optimization_header_row:], start=optimization_header_row + 1):
        has_period = row_contains_aliases(row, SUMMARY_HEADER_ALIASES["period"])
        has_summary_signal = any(
            row_contains_aliases(row, SUMMARY_HEADER_ALIASES[key])
            for key in ("round", "bg_rate", "count", "remark")
        )
        if has_period and has_summary_signal:
            summary_header_row = idx
            break

    optimization_data_end = summary_header_row or (len(values) + 1)
    return {
        "optimization_header_row": optimization_header_row,
        "optimization_data_start": optimization_header_row + 1,
        "optimization_data_end": optimization_data_end,
        "summary_header_row": summary_header_row,
        "summary_data_start": summary_header_row + 1 if summary_header_row is not None else None,
    }


def extract_ad_group_id_from_title(title: str) -> Optional[str]:
    match = re.search(r"(?<!\d)(\d{6,})(?!\d)", title)
    return match.group(1) if match else None


def build_col_map(header_row: List[str], aliases: Dict[str, List[str]]) -> Dict[str, Optional[int]]:
    rank_headers = find_header_indices_by_aliases(header_row, aliases.get("rank", []))
    rank_delta = find_header_index_by_aliases(header_row, aliases.get("rank_delta", []))
    return {
        "asset_type": find_header_index_by_aliases(header_row, aliases.get("asset_type", [])),
        "round": find_header_index_by_aliases(header_row, aliases.get("round", [])),
        "perf": find_header_index_by_aliases(header_row, aliases.get("perf", [])),
        "rank_num": rank_headers[0] if rank_headers else None,
        "rank_delta": rank_delta if rank_delta is not None else (rank_headers[1] if len(rank_headers) > 1 else None),
        "ctr": find_header_index_by_aliases(header_row, aliases.get("ctr", [])),
        "asset": find_header_index_by_aliases(header_row, aliases.get("asset", [])),
        "translation": find_header_index_by_aliases(header_row, aliases.get("translation", [])),
        "strategy": find_header_index_by_aliases(header_row, aliases.get("strategy", [])),
        "chars": find_header_index_by_aliases(header_row, aliases.get("chars", [])),
        "period": find_header_index_by_aliases(header_row, aliases.get("period", [])),
        "bg_rate": find_header_index_by_aliases(header_row, aliases.get("bg_rate", [])),
        "count": find_header_index_by_aliases(header_row, aliases.get("count", [])),
        "remark": find_header_index_by_aliases(header_row, aliases.get("remark", [])),
    }


# ---------------------------------------------------------------------------
# Missing field classification
# ---------------------------------------------------------------------------

def normalize_missing_field_key(fields: List[str]) -> tuple:
    return tuple(sorted(fields))


def classify_missing_fields(opt_col_map: Dict[str, Optional[int]]) -> Tuple[List[str], List[str]]:
    fatal_missing: List[str] = []
    continuable_missing: List[str] = []
    if opt_col_map.get("asset") is None:
        fatal_missing.append(DISPLAY_FIELD_NAMES["asset"])
    if opt_col_map.get("strategy") is None:
        fatal_missing.append(DISPLAY_FIELD_NAMES["strategy"])

    optional_pairs = [
        ("asset_type", DISPLAY_FIELD_NAMES["asset_type"]),
        ("round", DISPLAY_FIELD_NAMES["round"]),
        ("perf", DISPLAY_FIELD_NAMES["perf"]),
        ("rank_num", DISPLAY_FIELD_NAMES["rank_num"]),
        ("rank_delta", DISPLAY_FIELD_NAMES["rank_delta"]),
        ("ctr", DISPLAY_FIELD_NAMES["ctr"]),
        ("translation", DISPLAY_FIELD_NAMES["translation"]),
        ("chars", DISPLAY_FIELD_NAMES["chars"]),
        ("period", DISPLAY_FIELD_NAMES["period"]),
    ]
    for key, label in optional_pairs:
        if opt_col_map.get(key) is None:
            continuable_missing.append(label)
    return fatal_missing, continuable_missing


def confirm_continue_for_missing_fields(
    worksheet_title: str,
    missing_fields: List[str],
    approved_missing_sets: set,
    auto_continue: bool = False,
) -> None:
    field_key = normalize_missing_field_key(missing_fields)
    if field_key in approved_missing_sets:
        return
    if auto_continue:
        print(f"{worksheet_title}: 【{'、'.join(missing_fields)}】字段缺失，自动继续 (--auto-continue)", file=sys.stderr)
        approved_missing_sets.add(field_key)
        return
    prompt = f"{worksheet_title}: 【{'、'.join(missing_fields)}】字段缺失，是否继续？输入 继续 或 不继续: "
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError as exc:
            raise RuntimeError(
                f"{worksheet_title}: missing fields require confirmation but no input was available: {', '.join(missing_fields)}"
            ) from exc
        if answer in {"继续", "y", "yes", "continue"}:
            approved_missing_sets.add(field_key)
            return
        if answer in {"不继续", "n", "no", "stop"}:
            raise RuntimeError(f"{worksheet_title}: user stopped because fields were missing: {', '.join(missing_fields)}")


# ---------------------------------------------------------------------------
# Row search & manipulation
# ---------------------------------------------------------------------------

def find_existing_asset_row(
    values: List[List[str]],
    opt_header: int,
    opt_end: int,
    asset_col: int,
    round_col: Optional[int],
    asset_text: str,
) -> Optional[Tuple[int, Optional[int]]]:
    matched_row = None
    matched_round = None
    for row_idx in range(opt_header + 1, opt_end):
        row = values[row_idx - 1]
        asset_value = row[asset_col].strip() if len(row) > asset_col else ""
        round_value = row[round_col].strip() if round_col is not None and len(row) > round_col else ""
        if asset_value == asset_text:
            matched_row = row_idx
            matched_round = parse_round(round_value) if round_col is not None else None
    if matched_row is None:
        return None
    # Scan empty-asset rows below matched_row to find the actual max round number
    if round_col is not None:
        for row_idx in range(matched_row + 1, opt_end):
            row = values[row_idx - 1]
            asset_value = row[asset_col].strip() if len(row) > asset_col else ""
            if asset_value:
                break  # hit a different asset block
            round_value = row[round_col].strip() if len(row) > round_col else ""
            parsed = parse_round(round_value)
            if parsed is not None and (matched_round is None or parsed > matched_round):
                matched_round = parsed
    return matched_row, matched_round


def has_pending_optimization_row(
    values: List[List[str]],
    matched_row: int,
    opt_end: int,
    asset_col: int,
    perf_col: Optional[int],
) -> bool:
    """Return True if a blank 'in-progress' optimization row already exists below matched_row.

    Scans from matched_row+1 until a row with non-empty asset content is found (a different
    asset block starts) or opt_end is reached. If any row in that gap has an empty asset cell
    and Performance='进行中', the optimization row was already inserted and insertion should
    be skipped to prevent duplicates.
    """
    if perf_col is None:
        return False
    for row_idx in range(matched_row + 1, opt_end):
        row = values[row_idx - 1]
        asset_val = row[asset_col].strip() if len(row) > asset_col else ""
        perf_val = row[perf_col].strip() if len(row) > perf_col else ""
        if perf_val == "进行中":
            return True
        if asset_val:
            break
    return False


def last_nonempty_opt_row(values: List[List[str]], opt_header: int, opt_end: int) -> int:
    last = opt_header
    for idx in range(opt_header + 1, opt_end):
        row = values[idx - 1]
        if any(cell.strip() for cell in row):
            last = idx
    return last


def last_nonempty_summary_row(values: List[List[str]], summary_header: Optional[int]) -> Optional[int]:
    if summary_header is None:
        return None
    last = summary_header
    for idx in range(summary_header + 1, len(values) + 1):
        row = values[idx - 1]
        if any(cell.strip() for cell in row):
            last = idx
        elif idx > summary_header + 1:
            break
    return last


def clone_row(values: List[List[str]], row_idx: Optional[int], width: int) -> List[str]:
    if row_idx is None or row_idx <= 0 or row_idx > len(values):
        return [""] * width
    row = list(values[row_idx - 1])
    if len(row) < width:
        row.extend([""] * (width - len(row)))
    return row[:width]


def set_row_values(row: List[str], mapping: Dict[str, Optional[int]], data: dict) -> List[str]:
    new_row = list(row)

    def put(key: str, value: str) -> None:
        idx = mapping.get(key)
        if idx is None:
            return
        while len(new_row) <= idx:
            new_row.append("")
        new_row[idx] = value

    for key, value in data.items():
        put(key, value)
    return new_row


def write_single_row(ws: gspread.Worksheet, row_idx: int, values: List[str]) -> None:
    ws.update(range_name=f"A{row_idx}:{col_letter(ws.col_count)}{row_idx}", values=[values[: ws.col_count]])


def parse_round(value: str) -> Optional[int]:
    value = value.strip()
    # Accept both "N轮优化" and "N轮" formats
    if value.endswith("轮优化"):
        prefix = value[:-3]
    elif value.endswith("轮"):
        prefix = value[:-1]
    else:
        return None
    return CN_NUMS.get(prefix)


def detect_round_suffix(values: List[List[str]], opt_header: int, opt_end: int, round_col: Optional[int]) -> str:
    """Scan existing rows to detect whether this sheet uses '轮优化' or '轮' convention."""
    if round_col is None:
        return "轮优化"
    for row_idx in range(opt_header + 1, opt_end):
        row = values[row_idx - 1]
        val = row[round_col].strip() if len(row) > round_col else ""
        if val.endswith("轮优化"):
            return "轮优化"
        if val.endswith("轮") and not val.endswith("轮优化"):
            return "轮"
    return "轮优化"


def format_round(num: int, suffix: str = "轮优化") -> str:
    cn = NUM_TO_CN.get(num, str(num))
    return f"{cn}{suffix}"


def compute_bg_rate(kpi_assets: List[dict]) -> str:
    total = len(kpi_assets)
    good = sum(1 for item in kpi_assets if item["perf"] in {"Best", "Good"})
    return f"{round(good * 100 / total)}%>"


def get_effective_asset_type(
    values: List[List[str]],
    opt_header: int,
    row_idx: int,
    asset_type_col: Optional[int],
) -> str:
    if asset_type_col is None:
        return ""
    for probe_idx in range(row_idx, opt_header, -1):
        row = values[probe_idx - 1]
        if len(row) <= asset_type_col:
            continue
        asset_type_value = row[asset_type_col].strip()
        if asset_type_value:
            return asset_type_value
    return ""


def find_reference_row(
    values: List[List[str]],
    opt_header: int,
    opt_end: int,
    asset_type_col: Optional[int],
    round_col: Optional[int],
    asset_type: str,
    want_original: bool,
    strategy_col: Optional[int] = None,
    asset_col: Optional[int] = None,
) -> Optional[int]:
    for row_idx in range(opt_end - 1, opt_header, -1):
        row = values[row_idx - 1]
        max_index = max(idx for idx in (asset_type_col, round_col, strategy_col, asset_col) if idx is not None) if any(
            idx is not None for idx in (asset_type_col, round_col, strategy_col, asset_col)
        ) else -1
        if max_index >= 0 and len(row) <= max_index:
            continue
        if asset_type_col is not None and get_effective_asset_type(values, opt_header, row_idx, asset_type_col) != asset_type:
            continue
        round_value = row[round_col].strip() if round_col is not None and len(row) > round_col else ""
        strategy_value = row[strategy_col].strip() if strategy_col is not None and len(row) > strategy_col else ""
        asset_value = row[asset_col].strip() if asset_col is not None and len(row) > asset_col else ""
        if want_original:
            if round_col is not None:
                if round_value == "原方案":
                    return row_idx
            elif asset_value and not strategy_value:
                return row_idx
        else:
            if round_col is not None:
                if parse_round(round_value) is not None:
                    return row_idx
            elif strategy_value:
                return row_idx
    # Fallback: scan original data section (rows above opt_header) for a row whose
    # cells contain the target asset_type value.  This handles the case where the
    # optimization area only has rows of the other type (e.g. only Headline rows exist
    # but we are inserting a Description), so we borrow format from the raw-data block.
    if asset_type:
        for row_idx in range(opt_header - 1, 0, -1):
            row = values[row_idx - 1]
            if any(c.strip().lower() == asset_type.lower() for c in row):
                return row_idx

    if want_original:
        if asset_col is not None:
            for row_idx in range(opt_end - 1, opt_header, -1):
                row = values[row_idx - 1]
                asset_value = row[asset_col].strip() if len(row) > asset_col else ""
                if asset_value:
                    return row_idx
        return None
    for row_idx in range(opt_end - 1, opt_header, -1):
        row = values[row_idx - 1]
        if round_col is not None and len(row) > round_col and parse_round(row[round_col].strip()) is not None:
            return row_idx
        if strategy_col is not None and len(row) > strategy_col and row[strategy_col].strip():
            return row_idx
    return None


# ---------------------------------------------------------------------------
# Format alignment & merge helpers
# ---------------------------------------------------------------------------

def iter_helper_candidates(task_file: Optional[str]) -> List[Path]:
    candidates: List[Path] = []
    if task_file:
        task_dir = Path(task_file).expanduser().resolve().parent
        candidates.append(task_dir / "scripts" / "align_sheet_format.py")
    candidates.append(Path(__file__).resolve().parent / "align_sheet_format.py")
    candidates.append(user_skill_root() / "scripts" / "align_sheet_format.py")
    return candidates


def find_helper_script(explicit_path: Optional[str], task_file: Optional[str]) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.exists():
            return path
        raise RuntimeError(f"Explicit align helper not found: {path}")
    for path in iter_helper_candidates(task_file):
        if path.exists():
            return path
    raise RuntimeError(
        "Could not find align_sheet_format.py. Provide --align-helper or restore the helper "
        f"under this skill's scripts/ directory ({SKILL_NAME})."
    )


def run_alignment(sheet_url: str, plans: List[Tuple[str, int, int]], helper: Path) -> None:
    if not plans:
        return
    cmd = ["python3", str(helper), "--sheet-url", sheet_url]
    for title, source_row, target_row in plans:
        cmd.extend(["--plan", f"{title}:{source_row}:{target_row}"])
    subprocess.run(cmd, check=True)


def get_worksheet_first_col_merges(service, spreadsheet_id: str, worksheet_title: str) -> Tuple[int, List[dict]]:
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, includeGridData=False,
    ).execute()
    for sheet in metadata.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") != worksheet_title:
            continue
        merges = [
            merge for merge in sheet.get("merges", [])
            if merge.get("startColumnIndex") == 0 and merge.get("endColumnIndex") == 1
        ]
        return props["sheetId"], merges
    raise RuntimeError(f"Worksheet metadata not found for {worksheet_title}")


def find_first_col_merge_for_row(merges: List[dict], row_num: int) -> Optional[dict]:
    target = row_num - 1
    for merge in merges:
        if merge.get("startRowIndex", -1) <= target < merge.get("endRowIndex", -1):
            return merge
    return None


def find_overlapping_first_col_merges(
    merges: List[dict], start_row_index: int, end_row_index: int,
) -> List[dict]:
    overlapping: List[dict] = []
    for merge in merges:
        merge_start = merge.get("startRowIndex", -1)
        merge_end = merge.get("endRowIndex", -1)
        if merge_end <= start_row_index or merge_start >= end_row_index:
            continue
        overlapping.append(merge)
    return overlapping


def ensure_asset_type_merge(
    service, spreadsheet_id: str, worksheet_title: str,
    start_row: int, end_row: int, top_left_value: str,
) -> None:
    if end_row <= start_row:
        return
    sheet_id, merges = get_worksheet_first_col_merges(service, spreadsheet_id, worksheet_title)
    containing_merge = find_first_col_merge_for_row(merges, start_row)
    desired_start = containing_merge["startRowIndex"] if containing_merge else start_row - 1
    desired_end = end_row
    overlapping = find_overlapping_first_col_merges(merges, desired_start, desired_end)

    requests = []
    for merge in overlapping:
        if merge is None:
            continue
        requests.append({
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": merge["startRowIndex"],
                    "endRowIndex": merge["endRowIndex"],
                    "startColumnIndex": 0, "endColumnIndex": 1,
                }
            }
        })
    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": desired_start,
                "endRowIndex": desired_end,
                "startColumnIndex": 0, "endColumnIndex": 1,
            },
            "mergeType": "MERGE_ALL",
        }
    })
    requests.append({
        "updateCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": desired_start,
                "endRowIndex": desired_start + 1,
                "startColumnIndex": 0, "endColumnIndex": 1,
            },
            "rows": [{"values": [{"userEnteredValue": {"stringValue": top_left_value}}]}],
            "fields": "userEnteredValue",
        }
    })
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests},
    ).execute()


def extend_asset_type_merge_down_one_row(
    service, spreadsheet_id: str, worksheet_title: str, anchor_row: int,
) -> None:
    sheet_id, merges = get_worksheet_first_col_merges(service, spreadsheet_id, worksheet_title)
    containing_merge = find_first_col_merge_for_row(merges, anchor_row)
    if containing_merge is None:
        start_row_index = anchor_row - 1
        end_row_index = anchor_row + 1
    else:
        start_row_index = containing_merge["startRowIndex"]
        end_row_index = max(containing_merge["endRowIndex"], anchor_row + 1)

    overlapping = find_overlapping_first_col_merges(merges, start_row_index, end_row_index)

    requests = []
    for merge in overlapping:
        requests.append({
            "unmergeCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": merge["startRowIndex"],
                    "endRowIndex": merge["endRowIndex"],
                    "startColumnIndex": 0, "endColumnIndex": 1,
                }
            }
        })
    requests.append({
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row_index,
                "endRowIndex": end_row_index,
                "startColumnIndex": 0, "endColumnIndex": 1,
            },
            "mergeType": "MERGE_ALL",
        }
    })
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests},
    ).execute()


def copy_asset_type_format(
    service, spreadsheet_id: str, worksheet_id: int,
    source_row: Optional[int], target_row: int,
) -> None:
    if source_row is None:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{
            "copyPaste": {
                "source": {
                    "sheetId": worksheet_id,
                    "startRowIndex": source_row - 1, "endRowIndex": source_row,
                    "startColumnIndex": 0, "endColumnIndex": 1,
                },
                "destination": {
                    "sheetId": worksheet_id,
                    "startRowIndex": target_row - 1, "endRowIndex": target_row,
                    "startColumnIndex": 0, "endColumnIndex": 1,
                },
                "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
            }
        }]},
    ).execute()


def copy_cell_format_and_validation(
    service, spreadsheet_id: str, worksheet_id: int,
    source_row: Optional[int], target_row: int,
    column_idx: Optional[int], clear_value: bool = False,
) -> None:
    if source_row is None or column_idx is None:
        return
    start_col = column_idx
    end_col = column_idx + 1
    requests = [
        {
            "copyPaste": {
                "source": {
                    "sheetId": worksheet_id,
                    "startRowIndex": source_row - 1, "endRowIndex": source_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "destination": {
                    "sheetId": worksheet_id,
                    "startRowIndex": target_row - 1, "endRowIndex": target_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
            }
        },
        {
            "copyPaste": {
                "source": {
                    "sheetId": worksheet_id,
                    "startRowIndex": source_row - 1, "endRowIndex": source_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "destination": {
                    "sheetId": worksheet_id,
                    "startRowIndex": target_row - 1, "endRowIndex": target_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "pasteType": "PASTE_DATA_VALIDATION", "pasteOrientation": "NORMAL",
            }
        },
    ]
    if clear_value:
        requests.append({
            "updateCells": {
                "range": {
                    "sheetId": worksheet_id,
                    "startRowIndex": target_row - 1, "endRowIndex": target_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "rows": [{"values": [{}]}],
                "fields": "userEnteredValue",
            }
        })
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests},
    ).execute()


def has_expected_rank_delta_validation(
    service, spreadsheet_id: str, worksheet_title: str,
    row_idx: int, column_idx: Optional[int],
) -> bool:
    if column_idx is None:
        return False
    col_name = col_letter(column_idx + 1)
    response = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        ranges=[f"'{worksheet_title}'!{col_name}{row_idx}:{col_name}{row_idx}"],
        includeGridData=True,
    ).execute()
    data = response.get("sheets", [{}])[0].get("data", [{}])[0]
    cell = data.get("rowData", [{}])[0].get("values", [{}])[0]
    validation = cell.get("dataValidation") or {}
    condition = validation.get("condition") or {}
    values = [item.get("userEnteredValue") for item in condition.get("values", [])]
    formatted_value = cell.get("formattedValue")
    return (
        condition.get("type") == "ONE_OF_LIST"
        and values == ["不变", "上升", "下降"]
        and formatted_value in (None, "")
    )


def ensure_rank_delta_dropdown(
    service, spreadsheet_id: str, worksheet_title: str,
    worksheet_id: int, source_row: Optional[int],
    target_row: int, column_idx: Optional[int],
) -> None:
    if source_row is None or column_idx is None:
        return
    copy_cell_format_and_validation(
        service, spreadsheet_id, worksheet_id, source_row, target_row, column_idx, clear_value=True,
    )
    if has_expected_rank_delta_validation(service, spreadsheet_id, worksheet_title, target_row, column_idx):
        return
    copy_cell_format_and_validation(
        service, spreadsheet_id, worksheet_id, source_row, target_row, column_idx, clear_value=True,
    )


def copy_row_format_and_validation(
    service, spreadsheet_id: str, worksheet_id: int,
    source_row: Optional[int], target_row: int,
    start_col: int, end_col: int,
) -> None:
    if source_row is None or end_col <= start_col:
        return
    requests = [
        {
            "copyPaste": {
                "source": {
                    "sheetId": worksheet_id,
                    "startRowIndex": source_row - 1, "endRowIndex": source_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "destination": {
                    "sheetId": worksheet_id,
                    "startRowIndex": target_row - 1, "endRowIndex": target_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "pasteType": "PASTE_FORMAT", "pasteOrientation": "NORMAL",
            }
        },
        {
            "copyPaste": {
                "source": {
                    "sheetId": worksheet_id,
                    "startRowIndex": source_row - 1, "endRowIndex": source_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "destination": {
                    "sheetId": worksheet_id,
                    "startRowIndex": target_row - 1, "endRowIndex": target_row,
                    "startColumnIndex": start_col, "endColumnIndex": end_col,
                },
                "pasteType": "PASTE_DATA_VALIDATION", "pasteOrientation": "NORMAL",
            }
        },
    ]
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()
    except HttpError as exc:
        message = str(exc)
        if "copyPaste" not in message or "合并单元格" not in message:
            raise
        for column_idx in range(start_col, end_col):
            copy_cell_format_and_validation(
                service,
                spreadsheet_id,
                worksheet_id,
                source_row,
                target_row,
                column_idx,
            )


# ---------------------------------------------------------------------------
# Worksheet discovery
# ---------------------------------------------------------------------------

def discover_target_titles(spreadsheet, service) -> List[str]:
    metadata = service.spreadsheets().get(
        spreadsheetId=spreadsheet.id, includeGridData=False,
    ).execute()
    titles: List[str] = []
    for sheet in metadata.get("sheets", []):
        props = sheet.get("properties", {})
        title = str(props.get("title", "")).strip()
        if not title or props.get("hidden"):
            continue
        if not extract_ad_group_id_from_title(title):
            continue
        titles.append(title)
    return titles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Filter low-performing KPI assets and insert placeholder rows into Google Sheets."
    )
    parser.add_argument("sheets", nargs="*", help="Worksheet titles to process. If omitted, all configured worksheets run.")
    parser.add_argument("--task-file", help="Task md file path. Used only for sheet URL, app, sheets, and query window.")
    parser.add_argument("--sheet-url", help="Google Sheets URL override.")
    parser.add_argument("--app-id", type=int, help="KPI app_id override.")
    parser.add_argument("--app-name", help="App name override.")
    parser.add_argument("--query-window", help="Query window override, such as 近30日.")
    parser.add_argument("--app-map-file", help="JSON file mapping app names to KPI app ids.")
    parser.add_argument("--align-helper", help="Optional path to align_sheet_format.py.")
    parser.add_argument(
        "--auto-continue",
        action="store_true",
        help="Automatically continue when optional fields are missing (skip interactive prompt).",
    )
    parser.add_argument(
        "--kpi-data-dir",
        help="Directory with pre-fetched KPI JSON files ({ad_group_id}.json). "
             "When provided, skips HTTP KPI access entirely (recommended: use browser to fetch).",
    )
    parser.add_argument("--kpi-session-file", help="Fallback: JSON file with KPI cookies/session data.")
    parser.add_argument("--credentials", help="Google Workspace OAuth credentials JSON path.")
    parser.add_argument("--authorized-user", help="Google Workspace authorized user JSON path.")
    parser.add_argument(
        "--translations-json",
        default="{}",
        help='JSON string mapping original asset text to its English translation, '
             'e.g. \'{"原文": "English translation"}\'. Used when asset language is '
             'neither English nor Chinese.',
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full JSON report to stdout. By default only a compact summary is printed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan-only mode: list candidate assets per sheet (with mode and language) "
             "without writing to Google Sheets. Useful as a pre-flight before "
             "translating non-EN/ZH assets and re-running with --translations-json.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Summary output
# ---------------------------------------------------------------------------

def _print_summary(report: dict, report_path: Path) -> None:
    """Print a compact human-readable summary instead of the full JSON report."""
    lines = [
        f"app: {report.get('app_name') or report.get('app_id')}  "
        f"kpi: {report.get('kpi_date_range')}",
    ]
    total_inserted = 0
    skipped = []
    errors = []

    is_dry_run_report = any(s.get("dry_run") for s in report.get("sheets", []))
    total_candidates = 0
    needs_translation_assets: List[Tuple[str, str]] = []  # (sheet, asset)

    for sheet in report.get("sheets", []):
        title = sheet["worksheet"]
        n = sheet.get("optimized", 0)
        total_inserted += n
        skip_reason = sheet.get("skipped", "")
        kpi_err = sheet.get("kpi_error", "")

        if kpi_err:
            errors.append(f"  ERROR  {title}: {kpi_err[:120]}")
        elif skip_reason:
            skipped.append(f"  SKIP   {title}: {skip_reason}")
        elif sheet.get("dry_run"):
            cands = sheet.get("candidates", [])
            total_candidates += len(cands)
            tag = " [需翻译]" if sheet.get("needs_translation") else ""
            mode_counts = {"no_history": 0, "with_history": 0, "with_history_skipped_duplicate": 0}
            for c in cands:
                mode_counts[c.get("mode", "")] = mode_counts.get(c.get("mode", ""), 0) + 1
                if sheet.get("needs_translation") and c.get("mode") in ("no_history", "with_history"):
                    needs_translation_assets.append((title, c["asset"]))
            mode_str = ", ".join(f"{k}={v}" for k, v in mode_counts.items() if v)
            lines.append(f"  PLAN   {title}{tag}: {len(cands)}候选 ({mode_str})")
        else:
            row_parts = []
            for r in sheet.get("inserted_rows", []):
                mode = r.get("mode", "")
                if mode == "no_history":
                    row_parts.append(f"原方案@{r['original_at']}+优化行@{r['optimization_at']}")
                elif mode == "with_history":
                    row_parts.append(f"优化行@{r['inserted_at']}")
                elif mode == "with_history_skipped_duplicate":
                    row_parts.append(f"(dup skip @{r['matched_row']})")
            bg = f"  B/G率={sheet['best_good_rate']}" if sheet.get("best_good_rate") else ""
            summary_flag = "  摘要✓" if sheet.get("summary_written") else ""
            lines.append(f"  OK     {title}: {n}行  {', '.join(row_parts)}{bg}{summary_flag}")

    if errors:
        lines.append("--- errors ---")
        lines.extend(errors)
    if skipped:
        lines.append("--- skipped ---")
        lines.extend(skipped)

    if is_dry_run_report:
        lines.append(f"[DRY-RUN] {total_candidates} 候选素材  未写入表格  完整报告: {report_path}")
        if needs_translation_assets:
            lines.append(f"[DRY-RUN] 待翻译素材 ({len(needs_translation_assets)}):")
            for sheet_title, asset in needs_translation_assets:
                lines.append(f"  - [{sheet_title}] {asset}")
    else:
        lines.append(f"共插入 {total_inserted} 行  完整报告: {report_path}")
    print("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    translations: Dict[str, str] = json.loads(args.translations_json)
    task_file_data = parse_task_file(args.task_file) if args.task_file else {}

    readme_overrides = apply_user_field_aliases_from_readme(user_skill_root() / "README.md")
    if readme_overrides:
        print(f"[config] README field-name overrides applied: {'; '.join(readme_overrides)}", file=sys.stderr)

    sheet_url = args.sheet_url or task_file_data.get("sheet_url")
    if not sheet_url:
        raise RuntimeError("Missing sheet URL. Provide --sheet-url or --task-file with 在线表格链接.")
    app_name = args.app_name or task_file_data.get("app_name") or ""
    query_window = args.query_window or task_file_data.get("query_window") or "近30日"
    kpi_start, kpi_end = compute_date_range(query_window)

    credentials_path, token_path = resolve_google_workspace_credentials(args.credentials, args.authorized_user)
    client = load_client(credentials_path, token_path)
    spreadsheet = client.open_by_url(sheet_url)
    sheets_service = load_sheets_service(token_path)

    use_browser_data = bool(args.kpi_data_dir)
    session = None
    if not use_browser_data:
        session = load_kpi_session(args.kpi_session_file)

    app_id = load_app_id(app_name, args.app_map_file, args.app_id, task_file_data.get("app_id"), args.task_file, session)

    align_helper = find_helper_script(args.align_helper, args.task_file)

    requested_sheet_names = args.sheets or task_file_data.get("sheets") or []
    requested_titles = set(requested_sheet_names) if requested_sheet_names else None
    target_titles = requested_sheet_names or discover_target_titles(spreadsheet, sheets_service)
    if not target_titles:
        raise RuntimeError("No target worksheets found. Provide explicit sheet names or ensure visible ad-group tabs exist.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "app_name": app_name,
        "app_id": app_id,
        "sheet_url": sheet_url,
        "query_window": query_window,
        "kpi_date_range": f"{kpi_start} - {kpi_end}",
        "align_helper": str(align_helper),
        "sheets": [],
    }
    approved_missing_sets: set = set()

    for title in target_titles:
        if requested_titles is not None and title not in requested_titles:
            continue

        try:
            ws = spreadsheet.worksheet(title)
            resolved_title = title
        except Exception:
            # Fuzzy fallback: find a sheet whose title contains the same ad_group_id
            ad_group_id_candidate = extract_ad_group_id_from_title(title)
            resolved_title = None
            if ad_group_id_candidate:
                for ws_meta in spreadsheet.worksheets():
                    if ad_group_id_candidate in ws_meta.title:
                        resolved_title = ws_meta.title
                        ws = spreadsheet.worksheet(resolved_title)
                        print(f"[INFO] '{title}' not found; resolved to '{resolved_title}' via ad_group_id {ad_group_id_candidate}.")
                        break
            if resolved_title is None:
                raise RuntimeError(f"Worksheet not found: '{title}' (no sheet contains ad_group_id matching it)")
        title = resolved_title
        ad_group_id = extract_ad_group_id_from_title(title)
        if not ad_group_id:
            raise RuntimeError(f"Could not extract ad_group_id from worksheet title: {title}")
        try:
            kpi_assets, kpi_status, kpi_error, kpi_source = load_kpi_assets_for_sheet(
                use_browser_data=use_browser_data,
                kpi_data_dir=args.kpi_data_dir,
                session=session,
                ad_group_id=ad_group_id,
                app_id=app_id,
                kpi_start=kpi_start,
                kpi_end=kpi_end,
            )
        except RuntimeError as exc:
            message = str(exc)
            lowered = message.lower()
            if "business error" in lowered:
                kpi_status = KPI_STATUS_BUSINESS_ERROR
            else:
                kpi_status = KPI_STATUS_INVALID_PAYLOAD
            kpi_source = KPI_SOURCE_DATA_DIR if use_browser_data else KPI_SOURCE_SESSION
            report["sheets"].append(
                {
                    "worksheet": title,
                    "optimized": 0,
                    "kpi_source": kpi_source,
                    "kpi_status": kpi_status,
                    "kpi_error": message,
                    "skipped": "kpi payload unusable",
                    "format_aligned": False,
                }
            )
            continue

        if kpi_status == KPI_STATUS_EMPTY_DATA:
            report["sheets"].append(
                {
                    "worksheet": title,
                    "optimized": 0,
                    "kpi_source": kpi_source,
                    "kpi_status": kpi_status,
                    "kpi_error": "",
                    "skipped": "kpi returned empty data",
                    "format_aligned": False,
                }
            )
            continue

        _seen_assets: set = set()
        bottom_three = []
        for _item in kpi_assets[-3:]:
            if _item["perf"] in {"Low", "Learning"} and _item["asset"] not in _seen_assets:
                _seen_assets.add(_item["asset"])
                bottom_three.append(_item)
        if not bottom_three:
            report["sheets"].append(
                {
                    "worksheet": title,
                    "optimized": 0,
                    "kpi_source": kpi_source,
                    "kpi_status": kpi_status,
                    "kpi_error": kpi_error,
                    "skipped": "bottom three are not low/learning",
                    "format_aligned": False,
                }
            )
            continue

        values = ws.get_all_values()
        layout = detect_sheet_layout(values)
        opt_header = layout["optimization_header_row"]
        opt_end = layout["optimization_data_end"]
        col_map = build_col_map(values[opt_header - 1], OPTIMIZATION_HEADER_ALIASES)
        asset_col = col_map["asset"]
        round_col = col_map["round"]
        asset_type_col = col_map["asset_type"]
        strategy_col = col_map["strategy"]
        round_suffix = detect_round_suffix(values, opt_header, opt_end, round_col)
        fatal_missing, continuable_missing = classify_missing_fields(col_map)
        if fatal_missing:
            raise RuntimeError(f"{title}: 【{'、'.join(fatal_missing)}】字段缺失，无法安全继续")
        if continuable_missing:
            confirm_continue_for_missing_fields(title, continuable_missing, approved_missing_sets, args.auto_continue)

        # Language extraction from sheet title suffix (after the first '-')
        title_suffix = title.split("-", 1)[1] if "-" in title else ""
        en_markers = {"英语", "en", "us", "gb"}
        zh_markers = {"中文", "zh", "cn", "tw", "hk"}
        needs_translation = bool(title_suffix) and title_suffix not in en_markers and title_suffix not in zh_markers

        if args.dry_run:
            # Plan-only: list candidates with mode (with_history vs no_history) without writing.
            dry_candidates = []
            for item in bottom_three:
                existing_row = find_existing_asset_row(values, opt_header, opt_end, asset_col, round_col, item["asset"])
                if existing_row is not None:
                    matched_row, _ = existing_row
                    perf_col = col_map.get("perf")
                    if has_pending_optimization_row(values, matched_row, opt_end, asset_col, perf_col):
                        plan_mode = "with_history_skipped_duplicate"
                    else:
                        plan_mode = "with_history"
                else:
                    plan_mode = "no_history"
                dry_candidates.append({
                    "asset": item["asset"],
                    "perf": item.get("perf"),
                    "rank": item.get("rank"),
                    "ctr": item.get("ctr"),
                    "type": item.get("type"),
                    "mode": plan_mode,
                })
            report["sheets"].append({
                "worksheet": title,
                "language_suffix": title_suffix,
                "needs_translation": needs_translation,
                "kpi_source": kpi_source,
                "kpi_status": kpi_status,
                "kpi_error": kpi_error,
                "candidates": dry_candidates,
                "missing_fields": continuable_missing,
                "dry_run": True,
            })
            continue

        inserted_rows_info = []
        for item in bottom_three:
            # Re-read sheet state for each asset (may have shifted after previous insert)
            values = ws.get_all_values()
            layout = detect_sheet_layout(values)
            opt_header = layout["optimization_header_row"]
            opt_end = layout["optimization_data_end"]
            col_map = build_col_map(values[opt_header - 1], OPTIMIZATION_HEADER_ALIASES)
            asset_col = col_map["asset"]
            round_col = col_map["round"]
            asset_type_col = col_map["asset_type"]
            strategy_col = col_map["strategy"]

            existing_row = find_existing_asset_row(values, opt_header, opt_end, asset_col, round_col, item["asset"])

            if existing_row is not None:
                # ---- WITH HISTORY: insert empty row below last match ----
                matched_row, matched_round = existing_row
                perf_col = col_map.get("perf")
                if has_pending_optimization_row(values, matched_row, opt_end, asset_col, perf_col):
                    print(f"[SKIP] {title}: '{item['asset']}' already has a pending '进行中' optimization row below row {matched_row}. Skipping to avoid duplicate.")
                    inserted_rows_info.append({
                        "asset": item["asset"],
                        "mode": "with_history_skipped_duplicate",
                        "matched_row": matched_row,
                    })
                    continue
                next_round = (matched_round or 0) + 1
                before_count = sum(
                    1
                    for row_idx in range(opt_header + 1, opt_end)
                    if len(values[row_idx - 1]) > asset_col and values[row_idx - 1][asset_col].strip() == item["asset"]
                )
                source_row_values = values[matched_row - 1]
                ws.insert_row([""] * ws.col_count, index=matched_row + 1, inherit_from_before=True)
                row_values = [""] * ws.col_count
                if asset_type_col is not None and len(source_row_values) > asset_type_col:
                    row_values[asset_type_col] = source_row_values[asset_type_col]
                # Fill metadata fields only; leave content fields (asset, strategy) empty.
                # translation: pre-fill with English translation when provided (non-EN/ZH languages).
                row_values = set_row_values(
                    row_values, col_map,
                    {
                        "round": format_round(next_round, round_suffix) if round_col is not None else "",
                        "perf": "进行中",
                        "rank_num": f"{item['rank']}>",
                        "rank_delta": "",
                        "ctr": f"{item['ctr']}>",
                        "asset": "",
                        "strategy": "",
                        "chars": "",
                        "period": TODAY_SHORT,
                        "translation": translations.get(item["asset"], ""),
                    },
                )
                write_single_row(ws, matched_row + 1, row_values)
                run_alignment(sheet_url, [(title, matched_row, matched_row + 1)], align_helper)
                if col_map.get("rank_delta") is not None:
                    ensure_rank_delta_dropdown(
                        sheets_service, spreadsheet.id, title, ws.id,
                        matched_row, matched_row + 1, col_map.get("rank_delta"),
                    )
                if asset_type_col is not None:
                    extend_asset_type_merge_down_one_row(
                        sheets_service, spreadsheet.id, title, matched_row,
                    )
                # Verify: original asset rows not lost
                reread_values = ws.get_all_values()
                reread_layout = detect_sheet_layout(reread_values)
                reread_opt_end = reread_layout["optimization_data_end"]
                after_count = sum(
                    1
                    for row_idx in range(opt_header + 1, reread_opt_end)
                    if len(reread_values[row_idx - 1]) > asset_col and reread_values[row_idx - 1][asset_col].strip() == item["asset"]
                )
                if after_count < before_count:
                    raise RuntimeError(f"History row count decreased for {item['asset']} in {title}")
                inserted_rows_info.append({
                    "asset": item["asset"],
                    "mode": "with_history",
                    "inserted_at": matched_row + 1,
                })
            else:
                # ---- NO HISTORY: insert 原方案 + empty optimization row ----
                insert_anchor_row = last_nonempty_opt_row(values, opt_header, opt_end) + 1
                ws.insert_row([""] * ws.col_count, index=insert_anchor_row, inherit_from_before=True)
                ws.insert_row([""] * ws.col_count, index=insert_anchor_row + 1, inherit_from_before=True)
                original_row_idx = insert_anchor_row
                new_row_idx = insert_anchor_row + 1

                original_source_row = find_reference_row(
                    values, opt_header, opt_end, asset_type_col, round_col,
                    item["type"], want_original=True,
                    strategy_col=strategy_col, asset_col=asset_col,
                )
                if original_source_row is None:
                    raise RuntimeError(
                        f"Missing original-row style reference for no-history insert in {title} ({item['asset']})"
                    )
                # 原方案 row: fill with original asset text.
                # translation: pre-fill with English translation when provided (non-EN/ZH languages).
                original_row = [""] * ws.col_count
                original_row = set_row_values(
                    original_row, col_map,
                    {
                        "asset_type": item["type"],
                        "round": "原方案",
                        "asset": item["asset"],
                        "translation": translations.get(item["asset"], ""),
                        "perf": "",
                        "rank_num": "",
                        "rank_delta": "",
                        "ctr": "",
                        "strategy": "",
                        "chars": "",
                        "period": "",
                    },
                )
                write_single_row(ws, original_row_idx, original_row)

                optimization_source_row = find_reference_row(
                    values, opt_header, opt_end, asset_type_col, round_col,
                    item["type"], want_original=False,
                    strategy_col=strategy_col, asset_col=asset_col,
                )
                if optimization_source_row is None:
                    raise RuntimeError(
                        f"Missing optimization-row style reference for no-history insert in {title} ({item['asset']})"
                    )
                # Optimization row: metadata only, content fields empty
                new_row = [""] * ws.col_count
                new_row = set_row_values(
                    new_row, col_map,
                    {
                        "asset_type": item["type"] if asset_type_col is not None else "",
                        "round": format_round(1, round_suffix) if round_col is not None else "",
                        "perf": "进行中",
                        "rank_num": f"{item['rank']}>",
                        "rank_delta": "",
                        "ctr": f"{item['ctr']}>",
                        "asset": "",
                        "strategy": "",
                        "chars": "",
                        "period": TODAY_SHORT,
                        "translation": "",
                    },
                )
                write_single_row(ws, new_row_idx, new_row)

                row_alignment_plans: List[Tuple[str, int, int]] = []
                if original_source_row is not None:
                    row_alignment_plans.append((title, original_source_row, original_row_idx))
                if optimization_source_row is not None:
                    row_alignment_plans.append((title, optimization_source_row, new_row_idx))
                run_alignment(sheet_url, row_alignment_plans, align_helper)

                copy_row_format_and_validation(
                    sheets_service, spreadsheet.id, ws.id,
                    original_source_row, original_row_idx, 1, ws.col_count,
                )
                copy_row_format_and_validation(
                    sheets_service, spreadsheet.id, ws.id,
                    optimization_source_row, new_row_idx, 1, ws.col_count,
                )
                if col_map.get("perf") is not None:
                    copy_cell_format_and_validation(
                        sheets_service, spreadsheet.id, ws.id,
                        optimization_source_row, new_row_idx, col_map.get("perf"),
                    )
                if col_map.get("rank_delta") is not None:
                    ensure_rank_delta_dropdown(
                        sheets_service, spreadsheet.id, title, ws.id,
                        optimization_source_row, new_row_idx, col_map.get("rank_delta"),
                    )
                if asset_type_col is not None:
                    copy_asset_type_format(
                        sheets_service, spreadsheet.id, ws.id,
                        original_source_row or optimization_source_row,
                        original_row_idx,
                    )
                    copy_asset_type_format(
                        sheets_service, spreadsheet.id, ws.id,
                        optimization_source_row,
                        new_row_idx,
                    )
                    ensure_asset_type_merge(
                        sheets_service, spreadsheet.id, title,
                        original_row_idx, new_row_idx, item["type"],
                    )

                # Verify: 原方案 row has original asset
                reread_values = ws.get_all_values()
                inserted_original = reread_values[original_row_idx - 1]
                original_asset = inserted_original[asset_col].strip() if len(inserted_original) > asset_col else ""
                if original_asset != item["asset"]:
                    raise RuntimeError(f"No-history placement verification failed for {item['asset']} in {title}")

                inserted_rows_info.append({
                    "asset": item["asset"],
                    "mode": "no_history",
                    "original_at": original_row_idx,
                    "optimization_at": new_row_idx,
                })

        # Reconcile recorded row indices: later inserts in the same sheet may have
        # shifted earlier-recorded rows downward. Re-read once and look up each
        # asset's current 原方案 / "进行中" optimization row by content.
        if inserted_rows_info:
            final_values = ws.get_all_values()
            final_layout = detect_sheet_layout(final_values)
            final_opt_header = final_layout["optimization_header_row"]
            final_opt_end = final_layout["optimization_data_end"]
            final_col_map = build_col_map(final_values[final_opt_header - 1], OPTIMIZATION_HEADER_ALIASES)
            f_asset_col = final_col_map.get("asset")
            f_round_col = final_col_map.get("round")
            f_perf_col = final_col_map.get("perf")
            for entry in inserted_rows_info:
                asset_text = entry.get("asset", "")
                if not asset_text or f_asset_col is None:
                    continue
                # Walk optimization area; pair "原方案" rows and adjacent "进行中" rows.
                last_yuanfangan_row = None
                for r_idx in range(final_opt_header + 1, final_opt_end):
                    row = final_values[r_idx - 1]
                    if len(row) <= f_asset_col:
                        continue
                    cell_asset = row[f_asset_col].strip()
                    cell_round = row[f_round_col].strip() if (f_round_col is not None and len(row) > f_round_col) else ""
                    cell_perf = row[f_perf_col].strip() if (f_perf_col is not None and len(row) > f_perf_col) else ""
                    if entry["mode"] == "no_history":
                        if cell_asset == asset_text and cell_round == "原方案":
                            entry["original_at"] = r_idx
                            last_yuanfangan_row = r_idx
                            continue
                        if last_yuanfangan_row is not None and r_idx == last_yuanfangan_row + 1 and cell_perf == "进行中":
                            entry["optimization_at"] = r_idx
                            last_yuanfangan_row = None
                    elif entry["mode"] == "with_history":
                        # Find the "进行中" optimization row whose immediately-preceding row contains the asset.
                        if cell_perf == "进行中" and r_idx > final_opt_header + 1:
                            prev = final_values[r_idx - 2]
                            if len(prev) > f_asset_col and prev[f_asset_col].strip() == asset_text:
                                entry["inserted_at"] = r_idx

        # ---- SUMMARY ROW ----
        values = ws.get_all_values()
        layout = detect_sheet_layout(values)
        summary_header = layout["summary_header_row"]
        summary_detected = summary_header is not None
        summary_written = False
        summary_skipped_reason = ""
        next_round_num = None
        best_good_rate = ""

        if summary_header is not None:
            summary_last_row = last_nonempty_summary_row(values, summary_header)
            if summary_last_row is not None:
                next_summary_row = summary_last_row + 1
                if next_summary_row > ws.row_count:
                    ws.add_rows(10)
                summary_header_row = values[summary_header - 1]
                summary_col_map = build_col_map(summary_header_row, SUMMARY_HEADER_ALIASES)
                summary_row = [""] * ws.col_count
                writeable_summary = False
                if summary_col_map.get("round") is not None:
                    last_round_value = ""
                    for idx in range(summary_last_row, summary_header, -1):
                        row = values[idx - 1]
                        if len(row) > summary_col_map["round"] and row[summary_col_map["round"]].strip():
                            last_round_value = row[summary_col_map["round"]].strip()
                            break
                    next_round_num = (parse_round(last_round_value) or 0) + 1
                    summary_row[summary_col_map["round"]] = format_round(next_round_num, round_suffix)
                    writeable_summary = True
                if summary_col_map.get("period") is not None:
                    summary_row[summary_col_map["period"]] = TODAY_SHORT
                    writeable_summary = True
                if summary_col_map.get("bg_rate") is not None:
                    best_good_rate = compute_bg_rate(kpi_assets)
                    summary_row[summary_col_map["bg_rate"]] = best_good_rate
                    writeable_summary = True
                if summary_col_map.get("count") is not None:
                    summary_row[summary_col_map["count"]] = str(len(bottom_three))
                    writeable_summary = True
                # Dedup: skip if a summary row for today already exists
                period_col = summary_col_map.get("period")
                already_written_today = period_col is not None and any(
                    len(values[idx - 1]) > period_col
                    and values[idx - 1][period_col].strip() == TODAY_SHORT
                    for idx in range(summary_header + 1, summary_last_row + 1)
                )
                if already_written_today:
                    writeable_summary = False
                    summary_skipped_reason = "already written today"
                if writeable_summary:
                    ws.update(
                        range_name=f"A{next_summary_row}:{col_letter(ws.col_count)}{next_summary_row}",
                        values=[summary_row[: ws.col_count]],
                    )
                    run_alignment(sheet_url, [(title, summary_last_row, next_summary_row)], align_helper)
                    summary_written = True
                else:
                    if not summary_skipped_reason:
                        summary_skipped_reason = "summary fields not writable"
        else:
            summary_skipped_reason = "summary section not found"

        report["sheets"].append({
            "worksheet": title,
            "optimized": len(inserted_rows_info),
            "kpi_source": kpi_source,
            "kpi_status": kpi_status,
            "kpi_error": kpi_error,
            "inserted_rows": inserted_rows_info,
            "missing_fields": continuable_missing,
            "summary_detected": summary_detected,
            "summary_written": summary_written,
            "summary_skipped_reason": summary_skipped_reason,
            "summary_round": format_round(next_round_num) if next_round_num is not None else "",
            "best_good_rate": best_good_rate,
            "format_aligned": True,
        })

    out_path = OUTPUT_DIR / "sync_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.verbose:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_summary(report, out_path)


if __name__ == "__main__":
    main()
