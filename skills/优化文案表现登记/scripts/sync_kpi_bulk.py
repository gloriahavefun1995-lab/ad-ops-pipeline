#!/usr/bin/env python3
import argparse
import calendar
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

import gspread
import requests
from googleapiclient.discovery import build


SHEET_ID_IN_TITLE_REGEX = re.compile(r"(?<!\d)(\d{9,})(?!\d)")
ID_REGEX = re.compile(r"^\d{9,}$")
DEFAULT_DATE_PRESET = "近30天"
DEFAULT_STATUS_MARKER = "进行中"
DEFAULT_REPORT_PATH = "./output/kpi_bulk_sync_report.json"
LEGACY_FIELDS = ["Performance", "cost排序", "数据周期", "Best/Good率"]
DATE_PRESET_ALIASES = {
    "近7日": "近7天",
    "近7天": "近7天",
    "近14日": "近14天",
    "近14天": "近14天",
    "近30日": "近30天",
    "近30天": "近30天",
    "本月": "本月",
    "上月": "上月",
}
ALLOWED_FIELDS = {
    "Status",
    "Performance",
    "cost排序",
    "消耗排名",
    "Cost趋势",
    "Ctr",
    "数据周期",
    "Best/Good率",
}
# 字段别名：统一归一化为内部标准名
FIELD_ALIASES: Dict[str, str] = {
    "消耗排名": "cost排序",
}
BLOCK_HEADER_PREFIX = ["Asset", "Status", "Asset Type", "Performance"]
SKILL_README_PATH = Path(__file__).parent.parent / "README.md"
ZH_REASON_MAP = {
    "missing_opt_header": ("缺少基础表头", "没有找到文案区表头，无法定位文案数据。"),
    "missing_asset_column": ("缺少基础表头", "没有找到 Asset 列，无法匹配 KPI 文案。"),
    "missing_status_column": ("缺少基础表头", "没有找到用于登记 Status 的列。"),
    "missing_performance_column": ("缺少基础表头", "没有找到用于登记 Performance 的列。"),
    "missing_marker_column": ("缺少基础表头", "没有找到用于判断目标状态的 Performance 列。"),
    "missing_block_for_ad_group": ("未找到对应广告组区块", "当前 worksheet 中没有找到对应 ad group 的文案区块。"),
    "no_matching_marker_rows": ("未找到目标状态文案", "当前 sheet 里没有需要登记的目标状态文案。"),
    "unmatched_assets": ("表格文案与KPI文案未能精确匹配", "存在候选文案，但无法与 KPI 文案安全对应。"),
    "duplicate_assets_in_kpi": ("KPI文案存在重复", "KPI 返回了重复文案，无法安全写回对应行。"),
    "no_text_assets": ("KPI中没有可登记的文本素材", "当前 ad group 没有返回可登记的文本素材。"),
    "no_updatable_fields": ("没有可登记字段", "当前 sheet 中没有找到任何可安全更新的字段。"),
}

DEFAULT_HEADER_ALIASES: Dict[str, List[str]] = {
    "Ad Group ID": ["Ad Group ID"],
    "Asset": ["Asset"],
    "Status": ["Status"],
    "Asset Type": ["Asset Type"],
    "Performance": ["Performance"],
    "优化轮次": ["优化轮次"],
    "cost排序": ["cost排序", "Cost排序", "消耗排名"],
    "消耗排名": ["消耗排名", "cost排序", "Cost排序"],
    "Cost趋势": ["Cost趋势"],
    "Ctr": ["Ctr", "CTR", "点击率"],
    "数据周期": ["数据周期"],
    "Best/Good率": ["Best/Good率", "Best/Good"],
}

RUNTIME_HEADER_ALIASES: Dict[str, List[str]] = {
    key: list(value) for key, value in DEFAULT_HEADER_ALIASES.items()
}


@dataclass
class KPIAsset:
    asset: str
    asset_status: str
    performance: str
    ctr: str
    rank: int
    field_type: str


@dataclass
class DateWindow:
    preset: str
    start: str
    end: str


@dataclass
class WorksheetBlock:
    ad_group_id: str
    meta_header_row: int
    meta_value_row: int
    asset_header_row: int
    data_start_row: int
    data_end_row: int
    columns: Dict[str, int]


@dataclass
class SkillConfig:
    date_preset: str = DEFAULT_DATE_PRESET
    status_marker: str = DEFAULT_STATUS_MARKER
    write_fields: List[str] = None
    report_path: str = DEFAULT_REPORT_PATH
    header_aliases: Dict[str, List[str]] = None

    def __post_init__(self) -> None:
        if self.write_fields is None:
            self.write_fields = list(LEGACY_FIELDS)
        if self.header_aliases is None:
            self.header_aliases = {
                key: list(value) for key, value in DEFAULT_HEADER_ALIASES.items()
            }


def first_nonempty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def normalize_header_label(value: str) -> str:
    return re.sub(r"[\s_\-/]+", "", str(value or "").strip().lower())


def unique_preserve_order(values: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def aliases_for(target: str) -> List[str]:
    aliases = RUNTIME_HEADER_ALIASES.get(target, [target])
    return unique_preserve_order([target, *aliases])


def cell_matches_target(cell: str, target: str) -> bool:
    normalized = normalize_header_label(cell)
    return normalized in {
        normalize_header_label(alias) for alias in aliases_for(target)
    }


def parse_bool_flag(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"是", "yes", "y", "true", "1"}


def extract_markdown_section(text: str, title: str) -> str:
    pattern = rf"^##\s+{re.escape(title)}\s*$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
    end = start + next_heading.start() if next_heading else len(text)
    return text[start:end]


def extract_ad_group_id_from_title(title: str) -> Optional[str]:
    match = SHEET_ID_IN_TITLE_REGEX.search(title or "")
    return match.group(1) if match else None


def fail(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(2)


def translate_reason(reason_key: str) -> Tuple[str, str]:
    return ZH_REASON_MAP[reason_key]


def normalize_missing_field_key(fields: Sequence[str]) -> Tuple[str, ...]:
    return tuple(sorted({field for field in fields if field}))


def build_missing_field_prompt(fields: Sequence[str]) -> str:
    return f"有【{'、'.join(fields)}】缺失，是否继续。输入 继续 或 不继续: "


def confirm_continue_for_missing_fields(
    missing_fields: Sequence[str],
    approved_missing_sets: Set[Tuple[str, ...]],
) -> None:
    field_key = normalize_missing_field_key(missing_fields)
    if not field_key or field_key in approved_missing_sets:
        return
    prompt = build_missing_field_prompt(list(field_key))
    while True:
        try:
            answer = input(prompt).strip().lower()
        except EOFError as exc:
            raise RuntimeError(
                "缺失字段需要确认但未收到输入: " + "、".join(field_key)
            ) from exc
        if answer in {"继续", "y", "yes", "continue"}:
            approved_missing_sets.add(field_key)
            return
        if answer in {"不继续", "n", "no", "stop"}:
            raise RuntimeError(
                "用户因缺失字段停止任务: " + "、".join(field_key)
            )


def resolve_google_auth_paths(
    credentials_path: Optional[str], token_path: Optional[str]
) -> Tuple[str, str]:
    resolved_credentials = first_nonempty(
        credentials_path,
        os.getenv("GOOGLE_WORKSPACE_CREDENTIALS"),
    )
    resolved_token = first_nonempty(
        token_path,
        os.getenv("GOOGLE_WORKSPACE_AUTHORIZED_USER"),
    )
    if not resolved_credentials:
        raise ValueError(
            "Missing Google credentials. Provide --google-credentials or set "
            "GOOGLE_WORKSPACE_CREDENTIALS. Provide --google-token or set "
            "GOOGLE_WORKSPACE_AUTHORIZED_USER when OAuth user credentials are required."
        )
    return os.path.expanduser(resolved_credentials), os.path.expanduser(resolved_token) if resolved_token else ""


def load_gspread_client(
    credentials_path: Optional[str], token_path: Optional[str]
) -> gspread.Client:
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    resolved_credentials, resolved_token = resolve_google_auth_paths(
        credentials_path, token_path
    )
    if not os.path.exists(resolved_credentials):
        raise FileNotFoundError(
            "Google credentials file not found: "
            f"{resolved_credentials}. Provide --google-credentials or set "
            "GOOGLE_WORKSPACE_CREDENTIALS."
        )
    with open(resolved_credentials, encoding="utf-8") as f:
        creds_data = json.load(f)
    if "installed" in creds_data or "web" in creds_data:
        if not resolved_token:
            raise ValueError(
                "Missing Google authorized user token. Provide --google-token or set "
                "GOOGLE_WORKSPACE_AUTHORIZED_USER when using OAuth client credentials."
            )
        return gspread.oauth(
            credentials_filename=resolved_credentials,
            authorized_user_filename=resolved_token,
            scopes=scopes,
        )

    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_file(
        resolved_credentials, scopes=scopes
    )
    return gspread.authorize(creds)


def load_cookies_from_session_json(session_path: str) -> List[dict]:
    state = json.loads(Path(session_path).read_text(encoding="utf-8"))
    if isinstance(state, list):
        cookies = state
    elif isinstance(state, dict) and isinstance(state.get("cookies"), list):
        cookies = state["cookies"]
    else:
        raise ValueError(
            "Unsupported KPI session JSON. Expected a cookie array or an object "
            "with a top-level 'cookies' list."
        )
    return cookies


def load_cookies_from_cookie_file(cookie_file: str) -> Tuple[Optional[str], List[dict]]:
    raw = Path(cookie_file).read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"KPI cookie file is empty: {cookie_file}")
    if raw.startswith("{") or raw.startswith("["):
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("cookies"), list):
            return None, parsed["cookies"]
        if isinstance(parsed, list):
            return None, parsed
        raise ValueError(
            "Unsupported KPI cookie file JSON. Expected a cookie array or an "
            "object with a top-level 'cookies' list."
        )
    return raw, []


def apply_cookie_string(session: requests.Session, cookie_string: str) -> None:
    for item in cookie_string.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        session.cookies.set(
            name.strip(), value.strip(), domain="kpi.drojian.dev", path="/"
        )


def apply_cookie_objects(session: requests.Session, cookies: Sequence[dict]) -> None:
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        if not name or value is None:
            continue
        domain = cookie.get("domain", "kpi.drojian.dev")
        if "kpi.drojian.dev" not in domain:
            continue
        session.cookies.set(
            str(name),
            str(value),
            domain=domain,
            path=cookie.get("path", "/"),
        )


def load_kpi_session(
    kpi_cookie: Optional[str],
    kpi_cookie_file: Optional[str],
    kpi_session_json: Optional[str],
) -> requests.Session:
    session = requests.Session()
    resolved_cookie = first_nonempty(kpi_cookie, os.getenv("KPI_COOKIE"))
    resolved_cookie_file = first_nonempty(kpi_cookie_file, os.getenv("KPI_COOKIE_FILE"))
    resolved_session_json = first_nonempty(
        kpi_session_json, os.getenv("KPI_SESSION_JSON")
    )

    if resolved_cookie:
        apply_cookie_string(session, resolved_cookie)
        return session

    if resolved_cookie_file:
        cookie_string, cookies = load_cookies_from_cookie_file(
            os.path.expanduser(resolved_cookie_file)
        )
        if cookie_string:
            apply_cookie_string(session, cookie_string)
        else:
            apply_cookie_objects(session, cookies)
        return session

    if resolved_session_json:
        cookies = load_cookies_from_session_json(os.path.expanduser(resolved_session_json))
        apply_cookie_objects(session, cookies)
        return session

    raise ValueError(
        "Missing KPI authentication. Provide --kpi-cookie, --kpi-cookie-file, "
        "or --kpi-session-json, or set KPI_COOKIE, KPI_COOKIE_FILE, or "
        "KPI_SESSION_JSON."
    )


def validate_kpi_session(session: requests.Session) -> None:
    if not session.cookies:
        raise ValueError("No usable cookies were loaded for kpi.drojian.dev.")


def fetch_app_list(session: requests.Session) -> List[Dict]:
    url = "https://kpi.drojian.dev/work/app/index-ajax?page=1&limit=200"
    r = session.get(url, timeout=15)
    r.raise_for_status()
    payload = r.json()
    return payload.get("data", [])


def _skill_app_map_path() -> Path:
    return Path(__file__).parent.parent / "references" / "app-id-map.json"


def resolve_app_id(app_name: Optional[str], cli_app_id: Optional[int], session: requests.Session) -> int:
    if cli_app_id is not None:
        return cli_app_id
    if not app_name:
        raise ValueError("必须通过 --app-id 或 --app-name 指定目标 app。")
    needle = app_name.lower().replace(" ", "").replace("_", "")
    # 1. 先查静态映射文件（快速，无需网络）
    map_path = _skill_app_map_path()
    if map_path.exists():
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
        for name, aid in mapping.items():
            if needle in name.lower().replace(" ", "").replace("_", ""):
                print(f"[app] {app_name!r} → id={aid}  ({name}，来自静态映射)", file=sys.stderr)
                return int(aid)
    # 2. 回退到动态 API 查询
    apps = fetch_app_list(session)
    matches = [
        a for a in apps
        if needle in (a.get("app", "") + a.get("pkg", "")).lower().replace(" ", "").replace("_", "")
    ]
    if len(matches) == 1:
        found = matches[0]
        print(f"[app] {app_name!r} → id={found['id']}  ({found['app']}，来自 KPI 后台)", file=sys.stderr)
        return int(found["id"])
    if len(matches) > 1:
        names = ", ".join(f"{a['app']}(id={a['id']})" for a in matches)
        raise ValueError(f"--app-name {app_name!r} 匹配到多个 app，请用 --app-id 精确指定：{names}")
    all_names = ", ".join(f"{a['app']}(id={a['id']})" for a in apps)
    raise ValueError(f"--app-name {app_name!r} 未在 KPI 后台找到匹配的 app。可用列表：{all_names}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheet-url", required=True)
    parser.add_argument("--worksheet")
    parser.add_argument("--start-worksheet")
    parser.add_argument("--end-worksheet")
    parser.add_argument("--app-id", type=int, default=None, help="KPI app_id。与 --app-name 二选一。")
    parser.add_argument("--app-name", default=None, help="KPI app 名称，自动从后台查询对应 app_id。与 --app-id 二选一。")
    parser.add_argument("--kpi-session-json")
    parser.add_argument("--kpi-cookie-file")
    parser.add_argument("--kpi-cookie")
    parser.add_argument("--google-credentials")
    parser.add_argument("--google-token")
    parser.add_argument("--date-preset")
    parser.add_argument("--status-marker")
    parser.add_argument("--ad-group-id")
    parser.add_argument("--ad-group-ids", nargs="+")
    parser.add_argument("--write-fields", nargs="+")
    parser.add_argument("--backfill-missing-cost-trend", action="store_true")
    parser.add_argument("--repair-original-summary", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report")
    return parser.parse_args()


def normalize_write_fields(raw_fields: Optional[Sequence[str]]) -> List[str]:
    fields = list(raw_fields) if raw_fields else list(LEGACY_FIELDS)
    normalized: List[str] = []
    for field in fields:
        field = FIELD_ALIASES.get(field, field)
        if field not in ALLOWED_FIELDS:
            raise ValueError(
                f"Unsupported write field: {field}. Allowed values: "
                f"{', '.join(sorted(ALLOWED_FIELDS))}."
            )
        if field not in normalized:
            normalized.append(field)
    return normalized


def load_skill_config(readme_path: Optional[Path] = None) -> SkillConfig:
    path = readme_path or SKILL_README_PATH
    config = SkillConfig()
    if not path.exists():
        return config

    text = path.read_text(encoding="utf-8")

    date_match = re.search(r"^\s*数据查看日期\s*:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if date_match:
        config.date_preset = date_match.group(1).strip()

    status_match = re.search(r"^\s*状态标记\s*:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if status_match:
        config.status_marker = status_match.group(1).strip()

    report_match = re.search(r"^\s*默认报告路径\s*:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if report_match:
        config.report_path = report_match.group(1).strip()

    fields_section = extract_markdown_section(text, "需要登记的字段")
    selected_fields: List[str] = []
    for line in fields_section.splitlines():
        match = re.match(r"^\s*-\s*(.+?)\s*:\s*(是|否)\s*$", line.strip())
        if not match:
            continue
        field_name, enabled = match.groups()
        if parse_bool_flag(enabled):
            selected_fields.append(field_name.strip())
    if selected_fields:
        config.write_fields = selected_fields

    alias_section = extract_markdown_section(text, "字段名映射")
    for line in alias_section.splitlines():
        stripped = line.strip().strip("`")
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, raw_values = stripped.split(":", 1)
        key = key.strip()
        if key not in DEFAULT_HEADER_ALIASES:
            continue
        aliases = [item.strip() for item in re.split(r"[，,]", raw_values) if item.strip()]
        if aliases:
            config.header_aliases[key] = unique_preserve_order([key, *aliases])

    return config


def apply_runtime_header_aliases(config: SkillConfig) -> None:
    RUNTIME_HEADER_ALIASES.clear()
    for key, aliases in DEFAULT_HEADER_ALIASES.items():
        configured = config.header_aliases.get(key, aliases)
        RUNTIME_HEADER_ALIASES[key] = unique_preserve_order([key, *configured])


def resolve_today() -> date:
    raw = os.getenv("CODEX_TODAY")
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid CODEX_TODAY value: {raw!r}. Expected YYYY-MM-DD."
        ) from exc


def canonicalize_date_preset(date_preset: str) -> str:
    normalized = DATE_PRESET_ALIASES.get((date_preset or "").strip())
    if normalized:
        return normalized
    raise ValueError(
        "Unsupported date preset: "
        f"{date_preset}. Allowed values: {', '.join(sorted(set(DATE_PRESET_ALIASES.values())))}."
    )


def month_bounds(year: int, month: int) -> Tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def build_date_window(date_preset: str) -> DateWindow:
    preset = canonicalize_date_preset(date_preset)
    today = resolve_today()
    end = today - timedelta(days=1)

    if preset == "近7天":
        start = end - timedelta(days=6)
    elif preset == "近14天":
        start = end - timedelta(days=13)
    elif preset == "近30天":
        start = end - timedelta(days=29)
    elif preset == "本月":
        start = end.replace(day=1)
    elif preset == "上月":
        previous_month = today.month - 1 or 12
        previous_year = today.year - 1 if today.month == 1 else today.year
        start, end = month_bounds(previous_year, previous_month)
    else:
        raise ValueError(f"Unsupported date preset: {date_preset}")

    return DateWindow(
        preset=preset,
        start=start.isoformat(),
        end=end.isoformat(),
    )


def extract_ctr_value(row: dict) -> str:
    ctr_candidates = [
        row.get("ctr"),
        row.get("CTR"),
        row.get("ctr_label"),
        row.get("ctr_value"),
    ]
    value = first_nonempty(
        *(str(candidate).strip() for candidate in ctr_candidates if candidate is not None)
    )
    return value or ""


def fetch_kpi_assets(
    session: requests.Session, ad_group_id: str, date_window: DateWindow, app_id: int
) -> List[KPIAsset]:
    url = (
        "https://kpi.drojian.dev/google/asset/index-ajax"
        f"?page=1&limit=50&ad_group_id={ad_group_id}"
        f"&date={date_window.start}+-+{date_window.end}"
        f"&start_date={date_window.start}&end_date={date_window.end}"
        "&sort_field=cost&sort_type=desc&sort_value=3"
        f"&asset=&search_type=0&app_id={app_id}&tag="
    )
    response = session.get(url, timeout=30)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data", [])
    text_rows = [
        row
        for row in data
        if str(
            first_nonempty(row.get("asset_type"), row.get("field_type"), "")
        ).strip().lower()
        == "text"
    ]
    return [
        KPIAsset(
            asset=str(row.get("asset", "")).strip(),
            asset_status=str(row.get("status", "")).strip(),
            performance=str(row.get("performance_label", "")).strip(),
            ctr=extract_ctr_value(row),
            rank=idx,
            field_type=str(row.get("field_type", "")).strip(),
        )
        for idx, row in enumerate(text_rows, start=1)
        if str(row.get("asset", "")).strip()
    ]


def col_letter(idx: int) -> str:
    result = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        result = chr(65 + rem) + result
    return result


def find_header_index(row: Sequence[str], target: str) -> Optional[int]:
    for i, cell in enumerate(row):
        if cell_matches_target(str(cell).strip(), target):
            return i
    return None


def find_all_header_indices(row: Sequence[str], target: str) -> List[int]:
    return [i for i, cell in enumerate(row) if cell_matches_target(str(cell).strip(), target)]


def parse_left_rank(value: str) -> Optional[int]:
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else None


def build_rank_chain(existing_value: str, new_rank: int) -> str:
    left_rank = parse_left_rank(existing_value)
    return f"{left_rank}>{new_rank}" if left_rank is not None else str(new_rank)


def build_rank_trend(existing_rank_chain: str, new_rank: int) -> str:
    left_rank = parse_left_rank(existing_rank_chain)
    if left_rank is None:
        return ""
    if new_rank < left_rank:
        return "上升"
    if new_rank > left_rank:
        return "下降"
    return "不变"


def append_transition_value(existing_value: str, new_value: str) -> str:
    existing_value = (existing_value or "").strip()
    new_value = (new_value or "").strip()
    if not new_value:
        return existing_value
    if not existing_value:
        return new_value
    if existing_value.endswith(">"):
        return f"{existing_value}{new_value}"
    if ">" in existing_value:
        parts = [part.strip() for part in existing_value.split(">") if part.strip()]
        if parts and parts[-1] == new_value:
            return existing_value
        return f"{existing_value}>{new_value}"
    if existing_value == new_value:
        return existing_value
    return f"{existing_value}>{new_value}"


def build_period_value(existing_value: str, date_window: DateWindow) -> str:
    existing_value = (existing_value or "").strip()
    if not existing_value:
        return f"{date_window.start} - {date_window.end}"
    if existing_value.endswith("-"):
        update_date = date.today()
        return f"{existing_value}{update_date.month}/{update_date.day}"
    return existing_value


def add_cell_update(
    updates: List[dict],
    change_log: List[dict],
    row_idx: int,
    col_idx_zero_based: int,
    field: str,
    old_value: str,
    new_value: str,
    change_kind: str,
) -> None:
    if old_value == new_value:
        return
    updates.append(
        {
            "range": f"{col_letter(col_idx_zero_based + 1)}{row_idx}",
            "values": [[new_value]],
        }
    )
    change_log.append(
        {
            "row": row_idx,
            "col": col_letter(col_idx_zero_based + 1),
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "change_kind": change_kind,
        }
    )


def compute_bg_rate(kpi_assets: Sequence[KPIAsset]) -> str:
    total = len(kpi_assets)
    if total == 0:
        return "0%"
    good_count = sum(1 for asset in kpi_assets if asset.performance in {"Best", "Good"})
    return f"{round(good_count * 100 / total)}%"


def get_row(values: List[List[str]], row_idx: int) -> List[str]:
    if 1 <= row_idx <= len(values):
        return values[row_idx - 1]
    return []


def get_cell(values: List[List[str]], row_idx: int, col_idx_zero_based: int) -> str:
    row = get_row(values, row_idx)
    if 0 <= col_idx_zero_based < len(row):
        return str(row[col_idx_zero_based]).strip()
    return ""


def row_has_any_text(row: Sequence[str]) -> bool:
    return any(str(cell).strip() for cell in row)


def row_is_blank(values: List[List[str]], row_idx: int) -> bool:
    return not row_has_any_text(get_row(values, row_idx))


def row_matches_prefix(row: Sequence[str], prefix: Sequence[str]) -> bool:
    for idx, expected in enumerate(prefix):
        if idx >= len(row) or not cell_matches_target(str(row[idx]).strip(), expected):
            return False
    return True


def is_optimization_header_row(row: Sequence[str]) -> bool:
    return any(cell_matches_target(cell, "Asset") for cell in row) and any(
        cell_matches_target(cell, "Performance") for cell in row
    ) and (
        any(cell_matches_target(cell, "优化轮次") for cell in row)
        or any(cell_matches_target(cell, "cost排序") for cell in row)
    )


def parse_original_date_range(values: List[List[str]], opt_header_idx: int) -> str:
    for row_idx in range(1, opt_header_idx):
        first_cell = get_cell(values, row_idx, 0)
        match = re.search(r"原始数据（(.+?)）", first_cell)
        if match:
            return match.group(1).strip()
    return ""


def compute_original_bg_rate(values: List[List[str]], opt_header_idx: int) -> str:
    raw_header_idx = None
    for row_idx in range(1, opt_header_idx):
        row = get_row(values, row_idx)
        if row_matches_prefix(row, BLOCK_HEADER_PREFIX):
            raw_header_idx = row_idx
            break
    if raw_header_idx is None:
        return ""

    header = get_row(values, raw_header_idx)
    perf_col = find_header_index(header, "Performance")
    asset_type_col = find_header_index(header, "Asset Type")
    if perf_col is None:
        return ""

    total = 0
    good_count = 0
    for row_idx in range(raw_header_idx + 1, opt_header_idx):
        row = get_row(values, row_idx)
        if not row_has_any_text(row):
            break
        asset_type = get_cell(values, row_idx, asset_type_col) if asset_type_col is not None else ""
        if asset_type.strip().lower() in {"youtube", "image", "video"}:
            continue
        total += 1
        if get_cell(values, row_idx, perf_col) in {"Best", "Good"}:
            good_count += 1
    if total == 0:
        return ""
    return f"{round(good_count * 100 / total)}%"


def find_current_summary_row(
    values: List[List[str]],
    summary_header_idx: Optional[int],
    period_col: Optional[int],
    bg_col: Optional[int],
) -> Optional[int]:
    if summary_header_idx is None:
        return None

    fallback_row = None
    for row_idx in range(summary_header_idx + 1, len(values) + 1):
        row = get_row(values, row_idx)
        if not row_has_any_text(row):
            if fallback_row is not None:
                break
            continue
        round_label = get_cell(values, row_idx, 1)
        if round_label == "原方案":
            continue
        fallback_row = row_idx
        period_value = get_cell(values, row_idx, period_col) if period_col is not None else ""
        bg_value = get_cell(values, row_idx, bg_col) if bg_col is not None else ""
        if period_value.endswith("-") or bg_value.endswith(">"):
            return row_idx
    return fallback_row


def _get_visible_sheet_titles(spreadsheet: gspread.Spreadsheet) -> List[str]:
    """Return ordered list of non-hidden sheet titles via Sheets API.

    Uses the Sheets API directly (not gspread._properties) so hidden status
    is always authoritative regardless of gspread version.
    """
    service = build("sheets", "v4", credentials=spreadsheet.client.auth)
    resp = service.spreadsheets().get(
        spreadsheetId=spreadsheet.id,
        fields="sheets(properties(title,hidden))",
    ).execute()
    return [
        s["properties"]["title"]
        for s in resp.get("sheets", [])
        if not s["properties"].get("hidden", False)
    ]


def find_target_sheets(
    spreadsheet: gspread.Spreadsheet, ad_group_ids: Optional[Sequence[str]] = None
) -> List[gspread.Worksheet]:
    wanted = set(ad_group_ids or [])
    visible_titles = _get_visible_sheet_titles(spreadsheet)
    targets = []
    for title in visible_titles:
        ad_group_id = extract_ad_group_id_from_title(title)
        if not ad_group_id:
            continue
        if wanted and ad_group_id not in wanted:
            continue
        targets.append(spreadsheet.worksheet(title))
    return targets


def find_target_sheets_in_visible_range(
    spreadsheet: gspread.Spreadsheet,
    start_worksheet: str,
    end_worksheet: str,
    ad_group_ids: Optional[Sequence[str]] = None,
) -> List[gspread.Worksheet]:
    # Step 1: get all visible titles (anchors may not have ad_group_id pattern)
    visible_titles = _get_visible_sheet_titles(spreadsheet)

    if start_worksheet not in visible_titles:
        raise ValueError(f"Start worksheet not found or not visible: {start_worksheet}")
    if end_worksheet not in visible_titles:
        raise ValueError(f"End worksheet not found or not visible: {end_worksheet}")

    start_index = visible_titles.index(start_worksheet)
    end_index = visible_titles.index(end_worksheet)
    if start_index > end_index:
        raise ValueError(
            "Start worksheet appears after end worksheet among visible sheets: "
            f"{start_worksheet} > {end_worksheet}"
        )

    # Step 2: within the range, keep only sheets with a valid ad_group_id
    wanted = set(ad_group_ids or [])
    ranged_titles = visible_titles[start_index : end_index + 1]
    targets = []
    for title in ranged_titles:
        ad_group_id = extract_ad_group_id_from_title(title)
        if not ad_group_id:
            continue
        if wanted and ad_group_id not in wanted:
            continue
        targets.append(spreadsheet.worksheet(title))
    return targets


def detect_blocks_in_named_worksheet(
    values: List[List[str]], target_ad_group_ids: Sequence[str]
) -> Tuple[List[WorksheetBlock], List[str]]:
    wanted = set(target_ad_group_ids)
    found_blocks: List[WorksheetBlock] = []
    found_ids: Set[str] = set()

    for row_idx in range(1, len(values)):
        header_row = get_row(values, row_idx)
        if not cell_matches_target(get_cell(values, row_idx, 0), "Ad Group ID"):
            continue
        ad_group_id = get_cell(values, row_idx + 1, 0)
        if ad_group_id not in wanted or ad_group_id in found_ids:
            continue
        asset_header_row = None
        for probe_row in range(row_idx + 2, len(values) + 1):
            probe = get_row(values, probe_row)
            if row_matches_prefix(probe, BLOCK_HEADER_PREFIX):
                asset_header_row = probe_row
                break
            if probe_row > row_idx + 6 and cell_matches_target(
                get_cell(values, probe_row, 0), "Ad Group ID"
            ):
                break
        if asset_header_row is None:
            continue

        header = get_row(values, asset_header_row)
        asset_col = find_header_index(header, "Asset")
        status_col = find_header_index(header, "Status")
        perf_col = find_header_index(header, "Performance")
        columns: Dict[str, int] = {}
        if asset_col is not None:
            columns["Asset"] = asset_col
        if status_col is not None:
            columns["Status"] = status_col
        if perf_col is not None:
            columns["Performance"] = perf_col

        data_start_row = asset_header_row + 1
        data_end_row = len(values)
        for probe_row in range(data_start_row, len(values) + 1):
            first_cell = get_cell(values, probe_row, 0)
            if probe_row > data_start_row and cell_matches_target(first_cell, "Ad Group ID"):
                data_end_row = probe_row - 1
                break
            if probe_row >= data_start_row and row_is_blank(values, probe_row):
                data_end_row = probe_row - 1
                break

        found_blocks.append(
            WorksheetBlock(
                ad_group_id=ad_group_id,
                meta_header_row=row_idx,
                meta_value_row=row_idx + 1,
                asset_header_row=asset_header_row,
                data_start_row=data_start_row,
                data_end_row=data_end_row,
                columns=columns,
            )
        )
        found_ids.add(ad_group_id)

    missing_ids = [ad_group_id for ad_group_id in target_ad_group_ids if ad_group_id not in found_ids]
    return found_blocks, missing_ids


def build_kpi_asset_maps(
    kpi_assets: Sequence[KPIAsset],
) -> Tuple[Dict[str, KPIAsset], Set[str]]:
    asset_map: Dict[str, KPIAsset] = {}
    duplicate_assets: Set[str] = set()
    for asset in kpi_assets:
        if asset.asset in asset_map:
            duplicate_assets.add(asset.asset)
            continue
        asset_map[asset.asset] = asset
    for asset_text in duplicate_assets:
        asset_map.pop(asset_text, None)
    return asset_map, duplicate_assets


def make_entry_status(apply: bool, meta_status: str, update_count: int) -> str:
    if meta_status == "skipped":
        return "skipped"
    if update_count == 0:
        return "dry_run"
    return "updated" if apply else "dry_run"


def qualify_updates(ws_title: str, updates: Sequence[dict]) -> List[dict]:
    qualified = []
    for update in updates:
        update_range = update["range"]
        if "!" not in update_range:
            update_range = f"'{ws_title}'!{update_range}"
        qualified.append({"range": update_range, "values": update["values"]})
    return qualified


def build_updates_for_sheet(
    ws: gspread.Worksheet,
    kpi_assets: Sequence[KPIAsset],
    status_marker: str,
    date_window: DateWindow,
    write_fields: Sequence[str],
    backfill_missing_cost_trend: bool = False,
    repair_original_summary: bool = False,
) -> Tuple[List[dict], dict]:
    values = ws.get_all_values()
    if not kpi_assets:
        reason, explanation = translate_reason("no_text_assets")
        return [], {
            "status": "skipped",
            "reason": reason,
            "explanation": explanation,
            "missing_fields": [],
            "updated_fields": [],
            "skipped_fields": list(write_fields),
            "matched_assets": [],
            "unmatched_assets": [],
            "duplicate_assets": [],
            "text_asset_count": 0,
        }

    opt_header_idx = None
    for row_idx, row in enumerate(values, start=1):
        if is_optimization_header_row(row):
            opt_header_idx = row_idx
            break
    if not opt_header_idx:
        for row_idx, row in enumerate(values, start=1):
            if find_header_index(row, "Asset") is not None and find_header_index(row, "Performance") is not None:
                opt_header_idx = row_idx
                break
    if not opt_header_idx:
        reason, explanation = translate_reason("missing_opt_header")
        return [], {
            "status": "skipped",
            "reason": reason,
            "explanation": explanation,
            "missing_fields": [],
            "updated_fields": [],
            "skipped_fields": list(write_fields),
            "matched_assets": [],
            "unmatched_assets": [],
            "duplicate_assets": [],
            "text_asset_count": len(kpi_assets),
        }

    summary_header_idx = None
    for row_idx, row in enumerate(values, start=1):
        if row and cell_matches_target(str(row[0]).strip(), "数据周期"):
            summary_header_idx = row_idx
            break

    opt_header = values[opt_header_idx - 1]
    perf_col = find_header_index(opt_header, "Performance")
    cost_cols = find_all_header_indices(opt_header, "Cost排序") or find_all_header_indices(opt_header, "消耗排名")
    cost_trend_col = find_header_index(opt_header, "Cost趋势")
    ctr_col = find_header_index(opt_header, "Ctr")
    opt_period_col = find_header_index(opt_header, "数据周期")
    asset_col = find_header_index(opt_header, "Asset")
    if asset_col is None:
        reason, explanation = translate_reason("missing_asset_column")
        return [], {
            "status": "skipped",
            "reason": reason,
            "explanation": explanation,
            "missing_fields": [],
            "updated_fields": [],
            "skipped_fields": list(write_fields),
            "matched_assets": [],
            "unmatched_assets": [],
            "duplicate_assets": [],
            "text_asset_count": len(kpi_assets),
        }
    if perf_col is None:
        reason, explanation = translate_reason("missing_marker_column")
        return [], {
            "status": "skipped",
            "reason": reason,
            "explanation": explanation,
            "missing_fields": [],
            "updated_fields": [],
            "skipped_fields": list(write_fields),
            "matched_assets": [],
            "unmatched_assets": [],
            "duplicate_assets": [],
            "text_asset_count": len(kpi_assets),
        }

    summary_header = values[summary_header_idx - 1] if summary_header_idx else []
    period_col = find_header_index(summary_header, "数据周期") if summary_header else None
    bg_col = find_header_index(summary_header, "Best/Good率") if summary_header else None
    current_summary_row_idx = find_current_summary_row(
        values, summary_header_idx, period_col, bg_col
    )
    original_summary_row_idx = summary_header_idx + 1 if summary_header_idx is not None else None
    original_period_value = parse_original_date_range(values, opt_header_idx)
    original_bg_rate = compute_original_bg_rate(values, opt_header_idx)

    asset_map, duplicate_assets = build_kpi_asset_maps(kpi_assets)
    updates: List[dict] = []
    detailed_updates: List[dict] = []
    matched_assets = []
    unmatched_assets = []
    updated_fields = set()
    skipped_fields = set()
    missing_fields = set()
    marker_rows_found = 0

    data_end_row = summary_header_idx - 1 if summary_header_idx else len(values)
    for row_idx in range(opt_header_idx + 1, data_end_row + 1):
        performance_value = get_cell(values, row_idx, perf_col)
        asset_text = get_cell(values, row_idx, asset_col)
        existing_cost = get_cell(values, row_idx, cost_cols[0]) if cost_cols else ""
        existing_trend = (
            get_cell(values, row_idx, cost_trend_col)
            if cost_trend_col is not None
            else (get_cell(values, row_idx, cost_cols[1]) if len(cost_cols) > 1 else "")
        )

        is_marker_row = performance_value == status_marker
        is_trend_backfill_row = (
            backfill_missing_cost_trend
            and not is_marker_row
            and bool(asset_text)
            and bool(existing_cost)
            and ">" in existing_cost
            and not existing_trend
        )
        if not is_marker_row and not is_trend_backfill_row:
            continue
        marker_rows_found += 1
        if not asset_text:
            continue
        if asset_text in duplicate_assets:
            unmatched_assets.append({"row": row_idx, "asset": asset_text, "reason": "duplicate_in_kpi"})
            continue
        matched = asset_map.get(asset_text)
        if not matched:
            unmatched_assets.append({"row": row_idx, "asset": asset_text, "reason": "missing_in_kpi"})
            continue
        matched_assets.append(asset_text)

        if "Performance" in write_fields:
            existing_performance = get_cell(values, row_idx, perf_col)
            add_cell_update(
                updates,
                detailed_updates,
                row_idx,
                perf_col,
                "Performance",
                existing_performance,
                matched.performance,
                "replace",
            )
            updated_fields.add("Performance")

        if "cost排序" in write_fields or "Cost趋势" in write_fields:
            if cost_cols:
                primary_cost_col = cost_cols[0]
                existing_cost = get_cell(values, row_idx, primary_cost_col)
                new_cost = build_rank_chain(existing_cost, matched.rank)
                if "cost排序" in write_fields:
                    add_cell_update(
                        updates,
                        detailed_updates,
                        row_idx,
                        primary_cost_col,
                        "Cost排序",
                        existing_cost,
                        new_cost,
                        "replace",
                    )
                    updated_fields.add("cost排序")

                if "Cost趋势" in write_fields:
                    trend_col = cost_trend_col
                    if trend_col is None and len(cost_cols) > 1:
                        trend_col = cost_cols[1]
                    if trend_col is not None:
                        existing_trend = get_cell(values, row_idx, trend_col)
                        trend_value = build_rank_trend(existing_cost, matched.rank)
                        add_cell_update(
                            updates,
                            detailed_updates,
                            row_idx,
                            trend_col,
                            "Cost趋势",
                            existing_trend,
                            trend_value,
                            "replace",
                        )
                        updated_fields.add("Cost趋势")
                    else:
                        skipped_fields.add("Cost趋势")
                        missing_fields.add("Cost趋势")
            else:
                skipped_fields.add("cost排序")
                missing_fields.add("cost排序")
                if "Cost趋势" in write_fields:
                    skipped_fields.add("Cost趋势")
                    missing_fields.add("Cost趋势")

        if "Ctr" in write_fields:
            if ctr_col is not None:
                existing_ctr = get_cell(values, row_idx, ctr_col)
                new_ctr = append_transition_value(existing_ctr, matched.ctr)
                add_cell_update(
                    updates,
                    detailed_updates,
                    row_idx,
                    ctr_col,
                    "Ctr",
                    existing_ctr,
                    new_ctr,
                    "append",
                )
                updated_fields.add("Ctr")
            else:
                skipped_fields.add("Ctr")
                missing_fields.add("Ctr")

        if "数据周期" in write_fields and opt_period_col is not None:
            existing_period = get_cell(values, row_idx, opt_period_col)
            new_period = build_period_value(existing_period, date_window)
            add_cell_update(
                updates,
                detailed_updates,
                row_idx,
                opt_period_col,
                "行数据周期",
                existing_period,
                new_period,
                "append",
            )
            updated_fields.add("数据周期")

    bg_rate = compute_bg_rate(kpi_assets)
    if "Best/Good率" in write_fields:
        if bg_col is not None and current_summary_row_idx is not None:
            existing_bg = get_cell(values, current_summary_row_idx, bg_col)
            new_bg = append_transition_value(existing_bg, bg_rate)
            add_cell_update(
                updates,
                detailed_updates,
                current_summary_row_idx,
                bg_col,
                "Best/Good率",
                existing_bg,
                new_bg,
                "append",
            )
            updated_fields.add("Best/Good率")
        else:
            skipped_fields.add("Best/Good率")
            missing_fields.add("Best/Good率")

    if "数据周期" in write_fields:
        if period_col is not None and current_summary_row_idx is not None:
            existing_period = get_cell(values, current_summary_row_idx, period_col)
            new_period = build_period_value(existing_period, date_window)
            add_cell_update(
                updates,
                detailed_updates,
                current_summary_row_idx,
                period_col,
                "数据周期",
                existing_period,
                new_period,
                "append",
            )
            updated_fields.add("数据周期")
        else:
            skipped_fields.add("数据周期")
            missing_fields.add("数据周期")

    if (
        repair_original_summary
        and original_summary_row_idx is not None
        and period_col is not None
        and original_period_value
        and get_cell(values, original_summary_row_idx, 1) == "原方案"
    ):
        existing_period = get_cell(values, original_summary_row_idx, period_col)
        if existing_period != original_period_value:
            add_cell_update(
                updates,
                detailed_updates,
                original_summary_row_idx,
                period_col,
                "原方案数据周期",
                existing_period,
                original_period_value,
                "repair",
            )

    if (
        repair_original_summary
        and original_summary_row_idx is not None
        and bg_col is not None
        and original_bg_rate
        and get_cell(values, original_summary_row_idx, 1) == "原方案"
    ):
        existing_bg = get_cell(values, original_summary_row_idx, bg_col)
        if existing_bg != original_bg_rate:
            add_cell_update(
                updates,
                detailed_updates,
                original_summary_row_idx,
                bg_col,
                "原方案Best/Good率",
                existing_bg,
                original_bg_rate,
                "repair",
            )

    if updates:
        reason = ""
        explanation = ""
        status = "ready"
    elif marker_rows_found == 0:
        reason, explanation = translate_reason("no_matching_marker_rows")
        status = "skipped"
    elif not updated_fields:
        reason, explanation = translate_reason("no_updatable_fields")
        status = "skipped"
    else:
        reason = ""
        explanation = ""
        status = "ready"

    return updates, {
        "status": status,
        "reason": reason,
        "explanation": explanation,
        "missing_fields": sorted(missing_fields),
        "matched_assets": matched_assets,
        "unmatched_assets": unmatched_assets,
        "duplicate_assets": sorted(duplicate_assets),
        "updated_fields": sorted(updated_fields),
        "skipped_fields": sorted(skipped_fields),
        "best_good_rate": bg_rate,
        "text_asset_count": len(kpi_assets),
        "detailed_updates": detailed_updates,
    }


def build_updates_for_named_worksheet(
    ws: gspread.Worksheet,
    ad_group_ids: Sequence[str],
    kpi_assets_by_group: Dict[str, List[KPIAsset]],
    status_marker: str,
    write_fields: Sequence[str],
    date_window: DateWindow,
    backfill_missing_cost_trend: bool = False,
    repair_original_summary: bool = False,
) -> Tuple[List[dict], List[dict]]:
    values = ws.get_all_values()
    sheet_ad_group_id = extract_ad_group_id_from_title(ws.title)
    if (
        len(ad_group_ids) == 1
        and sheet_ad_group_id
        and ad_group_ids[0] == sheet_ad_group_id
    ):
        kpi_assets = kpi_assets_by_group.get(ad_group_ids[0], [])
        updates, meta = build_updates_for_sheet(
            ws=ws,
            kpi_assets=kpi_assets,
            status_marker=status_marker,
            date_window=date_window,
            write_fields=write_fields,
            backfill_missing_cost_trend=backfill_missing_cost_trend,
            repair_original_summary=repair_original_summary,
        )
        entry = {
            "worksheet": ws.title,
            "ad_group_id": ad_group_ids[0],
            "planned_updates": qualify_updates(ws.title, updates),
            "applied": False,
            "status": "pending",
            "meta": meta,
        }
        return updates, [entry]

    blocks, missing_ids = detect_blocks_in_named_worksheet(values, ad_group_ids)
    entries: List[dict] = []
    all_updates: List[dict] = []

    for missing_id in missing_ids:
        reason, explanation = translate_reason("missing_block_for_ad_group")
        entries.append(
            {
                "worksheet": ws.title,
                "ad_group_id": missing_id,
                "planned_updates": [],
                "applied": False,
                "status": "skipped",
                "meta": {
                    "status": "skipped",
                    "reason": reason,
                    "explanation": explanation,
                    "missing_fields": [],
                    "matched_assets": [],
                    "unmatched_assets": [],
                    "duplicate_assets": [],
                    "updated_fields": [],
                    "skipped_fields": list(write_fields),
                    "marker_rows_found": 0,
                    "block_rows": None,
                    "text_asset_count": 0,
                },
            }
        )

    for block in blocks:
        kpi_assets = kpi_assets_by_group.get(block.ad_group_id, [])
        if not kpi_assets:
            reason, explanation = translate_reason("no_text_assets")
            entries.append(
                {
                    "worksheet": ws.title,
                    "ad_group_id": block.ad_group_id,
                    "planned_updates": [],
                    "applied": False,
                    "status": "skipped",
                "meta": {
                    "status": "skipped",
                    "reason": reason,
                    "explanation": explanation,
                    "missing_fields": [],
                    "matched_assets": [],
                    "unmatched_assets": [],
                    "duplicate_assets": [],
                        "updated_fields": [],
                        "skipped_fields": list(write_fields),
                        "marker_rows_found": 0,
                        "block_rows": [block.data_start_row, block.data_end_row],
                        "text_asset_count": 0,
                    },
                }
            )
            continue

        missing_required = []
        if "Asset" not in block.columns:
            missing_required.append("Asset")
        if "Performance" not in block.columns:
            missing_required.append("Performance")
        if "Status" in write_fields and "Status" not in block.columns:
            missing_required.append("Status")
        if "Performance" in write_fields and "Performance" not in block.columns:
            missing_required.append("Performance")
        if missing_required:
            missing_target_fields = [
                field for field in normalize_missing_field_key(missing_required) if field in write_fields
            ]
            if "Status" in missing_required:
                reason, explanation = translate_reason("missing_status_column")
            elif "Performance" in missing_required:
                reason, explanation = translate_reason("missing_performance_column")
            elif "Asset" in missing_required:
                reason, explanation = translate_reason("missing_asset_column")
            else:
                reason, explanation = translate_reason("missing_marker_column")
            entries.append(
                {
                    "worksheet": ws.title,
                    "ad_group_id": block.ad_group_id,
                    "planned_updates": [],
                    "applied": False,
                    "status": "skipped",
                    "meta": {
                        "status": "skipped",
                        "reason": reason,
                        "explanation": explanation,
                        "missing_fields": missing_target_fields,
                        "matched_assets": [],
                        "unmatched_assets": [],
                        "duplicate_assets": [],
                        "updated_fields": [],
                        "skipped_fields": list(write_fields),
                        "marker_rows_found": 0,
                        "block_rows": [block.data_start_row, block.data_end_row],
                        "text_asset_count": len(kpi_assets),
                    },
                }
            )
            continue

        asset_map, duplicate_assets = build_kpi_asset_maps(kpi_assets)
        updates: List[dict] = []
        matched_assets = []
        unmatched_assets = []
        updated_fields = set()
        marker_rows_found = 0

        asset_col = block.columns["Asset"]
        marker_col = block.columns["Performance"]
        status_col = block.columns.get("Status")
        perf_col = block.columns.get("Performance")

        for row_idx in range(block.data_start_row, block.data_end_row + 1):
            marker_value = get_cell(values, row_idx, marker_col)
            if marker_value != status_marker:
                continue
            marker_rows_found += 1
            asset_text = get_cell(values, row_idx, asset_col)
            if not asset_text:
                continue
            if asset_text in duplicate_assets:
                unmatched_assets.append(
                    {"row": row_idx, "asset": asset_text, "reason": "duplicate_in_kpi"}
                )
                continue
            matched = asset_map.get(asset_text)
            if not matched:
                unmatched_assets.append(
                    {"row": row_idx, "asset": asset_text, "reason": "missing_in_kpi"}
                )
                continue
            matched_assets.append(asset_text)

            if "Status" in write_fields and status_col is not None:
                status_cell = f"{col_letter(status_col + 1)}{row_idx}"
                updates.append({"range": status_cell, "values": [[matched.asset_status]]})
                updated_fields.add("Status")
            if "Performance" in write_fields and perf_col is not None:
                perf_cell = f"{col_letter(perf_col + 1)}{row_idx}"
                updates.append({"range": perf_cell, "values": [[matched.performance]]})
                updated_fields.add("Performance")

        if marker_rows_found == 0:
            reason, explanation = translate_reason("no_matching_marker_rows")
            meta_status = "skipped"
        elif not updated_fields:
            reason, explanation = translate_reason("no_updatable_fields")
            meta_status = "skipped"
        else:
            reason = ""
            explanation = ""
            meta_status = "ready"

        entries.append(
            {
                "worksheet": ws.title,
                "ad_group_id": block.ad_group_id,
                "planned_updates": qualify_updates(ws.title, updates),
                "applied": False,
                "status": "pending",
                "meta": {
                    "status": meta_status,
                    "reason": reason,
                    "explanation": explanation,
                    "missing_fields": [],
                    "matched_assets": matched_assets,
                    "unmatched_assets": unmatched_assets,
                    "duplicate_assets": sorted(duplicate_assets),
                    "updated_fields": sorted(updated_fields),
                    "skipped_fields": [field for field in write_fields if field not in updated_fields],
                    "marker_rows_found": marker_rows_found,
                    "block_rows": [block.data_start_row, block.data_end_row],
                    "text_asset_count": len(kpi_assets),
                },
            }
        )
        if meta_status == "ready":
            all_updates.extend(updates)

    ordered_entries = sorted(entries, key=lambda item: str(item["ad_group_id"]))
    return all_updates, ordered_entries


def main() -> None:
    args = parse_args()
    try:
        config = load_skill_config()
        apply_runtime_header_aliases(config)
        resolved_date_preset = args.date_preset or config.date_preset or DEFAULT_DATE_PRESET
        resolved_status_marker = (
            args.status_marker or config.status_marker or DEFAULT_STATUS_MARKER
        )
        resolved_report = args.report or config.report_path or DEFAULT_REPORT_PATH
        write_fields = normalize_write_fields(args.write_fields or config.write_fields)
        client = load_gspread_client(args.google_credentials, args.google_token)
        spreadsheet = client.open_by_url(args.sheet_url)
        session = load_kpi_session(
            args.kpi_cookie, args.kpi_cookie_file, args.kpi_session_json
        )
        validate_kpi_session(session)
        app_id = resolve_app_id(args.app_name, args.app_id, session)
        date_window = build_date_window(resolved_date_preset)
    except (
        FileNotFoundError,
        ValueError,
        requests.RequestException,
        gspread.GSpreadException,
    ) as exc:
        fail(str(exc))

    report_path = Path(resolved_report)
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path

    ad_group_ids: List[str] = []
    if args.ad_group_id:
        ad_group_ids.append(str(args.ad_group_id))
    if args.ad_group_ids:
        ad_group_ids.extend(str(item) for item in args.ad_group_ids)
    ad_group_ids = list(dict.fromkeys(ad_group_ids))

    report = {
        "mode": "named_worksheet" if args.worksheet else ("single" if ad_group_ids else "bulk_default"),
        "app_id": app_id,
        "date_preset": date_window.preset,
        "kpi_date_range": f"{date_window.start} - {date_window.end}",
        "status_marker": resolved_status_marker,
        "write_fields": write_fields,
        "worksheet_range": {
            "start": args.start_worksheet or "",
            "end": args.end_worksheet or "",
        },
        "targets": [],
    }
    approved_missing_sets: Set[Tuple[str, ...]] = set()

    try:
        if args.worksheet:
            if not ad_group_ids:
                raise ValueError("--worksheet mode requires --ad-group-id or --ad-group-ids.")
            ws = spreadsheet.worksheet(args.worksheet)
            kpi_assets_by_group = {
                ad_group_id: fetch_kpi_assets(session, ad_group_id, date_window, app_id)
                for ad_group_id in ad_group_ids
            }
            updates, entries = build_updates_for_named_worksheet(
                ws=ws,
                ad_group_ids=ad_group_ids,
                kpi_assets_by_group=kpi_assets_by_group,
                status_marker=resolved_status_marker,
                write_fields=write_fields,
                date_window=date_window,
                backfill_missing_cost_trend=args.backfill_missing_cost_trend,
                repair_original_summary=args.repair_original_summary,
            )
            for entry in entries:
                confirm_continue_for_missing_fields(
                    entry["meta"].get("missing_fields", []),
                    approved_missing_sets,
                )
            if args.apply and updates:
                ws.batch_update(updates)
            for entry in entries:
                entry["applied"] = bool(
                    args.apply
                    and entry["meta"].get("status") == "ready"
                    and entry["planned_updates"]
                )
                entry["status"] = make_entry_status(
                    args.apply,
                    entry["meta"].get("status", "skipped"),
                    len(entry["planned_updates"]),
                )
                report["targets"].append(entry)
        else:
            if bool(args.start_worksheet) != bool(args.end_worksheet):
                raise ValueError(
                    "--start-worksheet and --end-worksheet must be provided together."
                )
            if args.start_worksheet and args.end_worksheet:
                targets = find_target_sheets_in_visible_range(
                    spreadsheet,
                    args.start_worksheet,
                    args.end_worksheet,
                    ad_group_ids or None,
                )
                report["mode"] = "bulk_visible_range"
            else:
                targets = find_target_sheets(spreadsheet, ad_group_ids or None)
            for ws in targets:
                ad_group_id = extract_ad_group_id_from_title(ws.title)
                if not ad_group_id:
                    continue
                kpi_assets = fetch_kpi_assets(session, ad_group_id, date_window, app_id)
                updates, meta = build_updates_for_sheet(
                    ws=ws,
                    kpi_assets=kpi_assets,
                    status_marker=resolved_status_marker,
                    date_window=date_window,
                    write_fields=write_fields,
                    backfill_missing_cost_trend=args.backfill_missing_cost_trend,
                    repair_original_summary=args.repair_original_summary,
                )
                confirm_continue_for_missing_fields(
                    meta.get("missing_fields", []),
                    approved_missing_sets,
                )
                qualified_updates = qualify_updates(ws.title, updates)
                if args.apply and updates and meta.get("status") == "ready":
                    ws.batch_update(updates)
                report["targets"].append(
                    {
                        "worksheet": ws.title,
                        "ad_group_id": ad_group_id,
                        "planned_updates": qualified_updates,
                        "meta": meta,
                        "applied": bool(
                            args.apply and qualified_updates and meta.get("status") == "ready"
                        ),
                        "status": make_entry_status(
                            args.apply, meta.get("status", "skipped"), len(qualified_updates)
                        ),
                    }
                )
    except (ValueError, requests.RequestException, gspread.GSpreadException) as exc:
        fail(str(exc))

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
