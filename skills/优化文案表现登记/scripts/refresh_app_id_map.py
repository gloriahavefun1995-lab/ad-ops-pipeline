#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

from sync_kpi_bulk import (
    _skill_app_map_path,
    fetch_app_list,
    load_kpi_session,
    validate_kpi_session,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kpi-session-json")
    parser.add_argument("--kpi-cookie-file")
    parser.add_argument("--kpi-cookie")
    parser.add_argument(
        "--output",
        default=str(_skill_app_map_path()),
        help="输出的 app-id map 路径，默认写入 references/app-id-map.json。",
    )
    return parser.parse_args()


def build_app_map(apps: list[dict]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for app in apps:
        name = str(app.get("app", "")).strip()
        app_id = app.get("id")
        if not name or app_id in (None, ""):
            continue
        mapping[name] = int(app_id)
    return dict(sorted(mapping.items(), key=lambda item: item[0].lower()))


def main() -> None:
    args = parse_args()
    session = load_kpi_session(
        args.kpi_cookie,
        args.kpi_cookie_file,
        args.kpi_session_json,
    )
    validate_kpi_session(session)
    apps = fetch_app_list(session)
    mapping = build_app_map(apps)
    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "app_count": len(mapping),
                "output": str(output_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
