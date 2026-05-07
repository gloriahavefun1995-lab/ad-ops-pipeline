#!/usr/bin/env python3
"""Refresh references/app-id-map.json by querying the KPI backend with a captured session.

The KPI backend at kpi.drojian.dev exposes an asset-detail page whose URL contains
`app_id=<int>` and whose page or `app/list` API enumerates all apps the user has
permission to see.  This script:

  1. loads a session JSON file (produced by capture_kpi_cookie_cdp.py),
  2. probes a few well-known endpoints to retrieve the full app catalog,
  3. writes {<app_name>: <app_id>, ...} to references/app-id-map.json
     (merge with existing entries; never delete unknown ones).

Usage:
  python3 scripts/refresh_app_id_map.py --kpi-session-file /tmp/kpi_session.json [--out references/app-id-map.json]

If automatic discovery fails, the script prints a clear hint about what to do
manually (open KPI backend in Chrome, find app dropdown, copy entries) rather
than crashing.
"""
import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "references" / "app-id-map.json"

KPI_BASE_URLS = [
    # Probe order: most likely → least likely.
    # Each must accept GET with cookie auth and return either HTML containing
    # an app dropdown / list, or JSON.
    "https://kpi.drojian.dev/google/asset/index",
    "https://kpi.drojian.dev/admin/app/list",
    "https://kpi.drojian.dev/app/list",
]


def load_session_cookies(session_file: Path) -> str:
    with session_file.open("r", encoding="utf-8") as fh:
        sess = json.load(fh)
    cookies = sess.get("cookies") or []
    if isinstance(cookies, dict):
        # Some shapes store it as a flat dict {name: value}
        return "; ".join(f"{n}={v}" for n, v in cookies.items())
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if "name" in c and "value" in c)


def fetch_html(url: str, cookie_header: str, timeout: int = 20) -> Optional[str]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={
        "Cookie": cookie_header,
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html, application/json, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"[probe] {url} → HTTP {e.code}\n")
    except Exception as e:
        sys.stderr.write(f"[probe] {url} → {e}\n")
    return None


def parse_app_dropdown(html: str) -> Dict[str, int]:
    """Look for a <select name="app_id"> ... <option value="N">Name</option> ... </select> block."""
    found: Dict[str, int] = {}
    block = re.search(r'<select[^>]*name=(?:"app_id"|app_id)[^>]*>(.*?)</select>', html, re.S)
    if block:
        for m in re.finditer(r'<option[^>]*value="(\d+)"[^>]*>([^<]+)</option>', block.group(1)):
            aid = int(m.group(1))
            name = m.group(2).strip()
            if name and aid > 0:
                found[name] = aid
    # Some backends embed an inline JSON array of apps in script tags.
    if not found:
        for m in re.finditer(r'\{\s*"id"\s*:\s*(\d+)\s*,\s*"name"\s*:\s*"([^"]+)"', html):
            aid = int(m.group(1))
            name = m.group(2).strip()
            if name and aid > 0:
                found[name] = aid
    return found


def discover_app_ids(session_file: Path) -> Tuple[Dict[str, int], List[str]]:
    cookie_header = load_session_cookies(session_file)
    if not cookie_header:
        return {}, ["session file has no cookies"]
    diagnostics: List[str] = []
    for url in KPI_BASE_URLS:
        html = fetch_html(url, cookie_header)
        if html is None:
            diagnostics.append(f"could not fetch {url}")
            continue
        apps = parse_app_dropdown(html)
        if apps:
            diagnostics.append(f"discovered {len(apps)} apps from {url}")
            return apps, diagnostics
        diagnostics.append(f"no app dropdown found in {url}")
    return {}, diagnostics


def merge_into_existing(existing: Dict[str, int], discovered: Dict[str, int]) -> Dict[str, int]:
    merged = dict(existing)
    for name, aid in discovered.items():
        merged[name] = aid
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kpi-session-file", required=True, type=Path,
                        help="JSON file produced by capture_kpi_cookie_cdp.py")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"Output path (default: {DEFAULT_OUT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print discovered apps but do not write the file")
    args = parser.parse_args()

    if not args.kpi_session_file.exists():
        print(f"❌ session file not found: {args.kpi_session_file}", file=sys.stderr)
        return 2

    discovered, diagnostics = discover_app_ids(args.kpi_session_file)
    for line in diagnostics:
        print(f"[refresh_app_id_map] {line}", file=sys.stderr)

    if not discovered:
        print(json.dumps({
            "status": "no_apps_discovered",
            "diagnostics": diagnostics,
            "manual_hint": (
                "自动发现失败。请在 Chrome 打开 KPI 后台，找到顶部 App 下拉，"
                "把每个 App 的 (name, app_id) 手动追加到 references/app-id-map.json，例如："
                '{"ChatGPT": 43, "Open Chat": 17}'
            ),
        }, ensure_ascii=False, indent=2))
        return 1

    existing: Dict[str, int] = {}
    if args.out.exists():
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"[warn] existing {args.out} is not valid JSON ({exc}); will overwrite", file=sys.stderr)
            existing = {}

    merged = merge_into_existing(existing, discovered)

    if args.dry_run:
        print(json.dumps({
            "status": "dry_run",
            "would_write_to": str(args.out),
            "existing_count": len(existing),
            "discovered_count": len(discovered),
            "merged_count": len(merged),
            "merged": dict(sorted(merged.items())),
        }, ensure_ascii=False, indent=2))
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "out": str(args.out),
        "existing_count": len(existing),
        "discovered_count": len(discovered),
        "merged_count": len(merged),
        "added": sorted(set(discovered) - set(existing)),
        "updated": sorted(name for name in discovered if name in existing and existing[name] != discovered[name]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
