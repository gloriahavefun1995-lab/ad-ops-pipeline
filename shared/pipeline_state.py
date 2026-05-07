"""Shared pipeline state helper for ad-ops-pipeline plugin.

Each app has one state file at:
    ~/.claude/ad-ops-pipeline-state/<app_name>_pipeline.json

The state tracks each ad-group sheet tab's current stage so any skill (筛选 →
低表现定位 → ad-creative) can pick up the previous stage's output without
re-querying APIs or asking the user to re-paste context.

State schema:
{
  "app_name": "ChatGPT",
  "app_id": 43,
  "sheet_url": "https://docs.google.com/spreadsheets/d/.../edit",
  "updated_at": "2026-05-07T10:30:00",
  "tabs": {
    "<tab_name>": {
      "ad_group_id": "159746413129",
      "lang_key": "en",
      "stage": "filled_creative" | "low_assets_synced" | "raw_data_filled" | "skeleton_only" | "created",
      "stage_history": [
        {"stage": "created", "at": "..."},
        ...
      ],
      "pending_rows": [22, 33, 49],   # optimization rows awaiting copy
      "filled_rows": [],              # rows where copy is filled
      "notes": ""
    },
    ...
  }
}

Stages (in order):
  created          — sheet exists but only header row, no 原始数据 yet
  skeleton_only    — opt area headers pre-placed by update_sheets.py, no 原始数据
  raw_data_filled  — user added 原始数据 + (optionally) opt area
  low_assets_synced — sync_low_assets.py ran; 原方案 / 一轮 placeholder rows present
  filled_creative  — ad-creative wrote new copy into pending rows

Helpers are intentionally minimal — JSON read/write + a few convenience methods.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


def state_dir() -> Path:
    """Return the directory where pipeline state JSON files live."""
    base = Path(os.environ.get("AD_OPS_PIPELINE_STATE_DIR", "")).expanduser()
    if not base or str(base) == ".":
        base = Path.home() / ".claude" / "ad-ops-pipeline-state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def state_path(app_name: str) -> Path:
    """Return the state file path for a given app name."""
    safe = app_name.replace("/", "_").replace(" ", "_")
    return state_dir() / f"{safe}_pipeline.json"


def load_state(app_name: str) -> Dict:
    """Load existing state, or return a fresh skeleton."""
    path = state_path(app_name)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "app_name": app_name,
        "app_id": None,
        "sheet_url": None,
        "updated_at": None,
        "tabs": {},
    }


def save_state(state: Dict) -> Path:
    """Persist state to disk and return the path."""
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = state_path(state["app_name"])
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_tab(
    state: Dict,
    tab_name: str,
    *,
    stage: Optional[str] = None,
    ad_group_id: Optional[str] = None,
    lang_key: Optional[str] = None,
    pending_rows: Optional[List[int]] = None,
    filled_rows: Optional[List[int]] = None,
    notes: Optional[str] = None,
) -> Dict:
    """Update one tab's state in-place and append to its stage_history."""
    tab = state["tabs"].setdefault(tab_name, {
        "ad_group_id": None,
        "lang_key": None,
        "stage": None,
        "stage_history": [],
        "pending_rows": [],
        "filled_rows": [],
        "notes": "",
    })
    if ad_group_id is not None:
        tab["ad_group_id"] = ad_group_id
    if lang_key is not None:
        tab["lang_key"] = lang_key
    if stage is not None and tab.get("stage") != stage:
        tab["stage"] = stage
        tab.setdefault("stage_history", []).append({
            "stage": stage,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
    if pending_rows is not None:
        tab["pending_rows"] = list(pending_rows)
    if filled_rows is not None:
        tab["filled_rows"] = list(filled_rows)
    if notes is not None:
        tab["notes"] = notes
    return tab


def tabs_at_stage(state: Dict, stage: str) -> List[str]:
    """Return list of tab names currently at the given stage."""
    return [t for t, info in state.get("tabs", {}).items() if info.get("stage") == stage]


def summary(state: Dict) -> Dict:
    """Return a counts-by-stage summary, useful for human-readable reports."""
    counts: Dict[str, int] = {}
    for tab, info in state.get("tabs", {}).items():
        counts[info.get("stage") or "unknown"] = counts.get(info.get("stage") or "unknown", 0) + 1
    return {
        "app_name": state.get("app_name"),
        "sheet_url": state.get("sheet_url"),
        "updated_at": state.get("updated_at"),
        "total_tabs": len(state.get("tabs", {})),
        "by_stage": counts,
    }
