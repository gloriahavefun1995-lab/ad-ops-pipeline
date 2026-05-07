#!/usr/bin/env python3
import argparse
import base64
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BUSINESS_PATH_KEYWORDS = (
    "/google/asset/index-ajax",
    "/google/ad-group/index-ajax",
    "/work/app/index",
)

VERIFIABLE_PATH_KEYWORDS = (
    "/google/asset/index-ajax",
    "/google/ad-group/index-ajax",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--cookie-out")
    parser.add_argument("--cookie-header-out")
    parser.add_argument("--session-json-out")
    parser.add_argument("--summary-out")
    parser.add_argument("--domain", default="kpi.drojian.dev")
    parser.add_argument("--login-url", default="https://kpi.drojian.dev/work/app/index")
    parser.add_argument("--cdp-ready-timeout-ms", type=int, default=30 * 1000)
    parser.add_argument("--timeout-ms", type=int, default=15 * 60 * 1000)
    args = parser.parse_args()
    if not any(
        [
            args.cookie_out,
            args.cookie_header_out,
            args.session_json_out,
            args.summary_out,
        ]
    ):
        parser.error(
            "At least one output path is required: --cookie-out, "
            "--cookie-header-out, --session-json-out, or --summary-out."
        )
    return args


def http_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def http_request(url: str, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as response:
        content = response.read().decode("utf-8", errors="replace")
        return content, response.headers.get("content-type", "")


def ws_connect(ws_url: str):
    parsed = urllib.parse.urlparse(ws_url)
    host = parsed.hostname
    port = parsed.port or 80
    path = parsed.path
    if parsed.query:
        path += f"?{parsed.query}"
    sock = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(handshake.encode("utf-8"))
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    return sock


def ws_send(sock, payload: str):
    data = payload.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)
    mask_key = os.urandom(4)
    length = len(data)
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack("!Q", length))
    frame.extend(mask_key)
    frame.extend(b ^ mask_key[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(frame))


def ws_recv(sock):
    header = sock.recv(2)
    if len(header) < 2:
        return ""
    payload_len = header[1] & 0x7F
    if payload_len == 126:
        payload_len = struct.unpack("!H", sock.recv(2))[0]
    elif payload_len == 127:
        payload_len = struct.unpack("!Q", sock.recv(8))[0]
    masked = bool(header[1] & 0x80)
    mask_key = sock.recv(4) if masked else b""
    data = b""
    while len(data) < payload_len:
        chunk = sock.recv(payload_len - len(data))
        if not chunk:
            break
        data += chunk
    if masked:
        data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
    return data.decode("utf-8", errors="replace")


class CDPClient:
    def __init__(self, ws_url: str):
        self.sock = ws_connect(ws_url)
        self.sock.settimeout(1)
        self.next_id = 1

    def send(self, method: str, params=None):
        msg_id = self.next_id
        self.next_id += 1
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        ws_send(self.sock, json.dumps(payload))
        return msg_id

    def recv(self):
        try:
            raw = ws_recv(self.sock)
        except socket.timeout:
            return None
        if not raw:
            return None
        return json.loads(raw)

    def call(self, method: str, params=None, attempts: int = 100):
        msg_id = self.send(method, params)
        for _ in range(attempts):
            message = self.recv()
            if not message:
                continue
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"CDP error for {method}: {message['error']}")
                return message.get("result", {})
        raise RuntimeError(f"Timed out waiting for CDP response: {method}")


def choose_page_ws(cdp_url: str, ready_timeout_ms: int, preferred_domain: str):
    deadline = time.time() + ready_timeout_ms / 1000
    last_error = None
    while time.time() < deadline:
        try:
            targets = http_json(f"{cdp_url}/json")
            preferred_target = None
            for target in targets:
                if target.get("type") != "page":
                    continue
                ws_url = target.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                target_url = str(target.get("url", ""))
                if preferred_domain in target_url:
                    return ws_url, target_url
                if preferred_target is None:
                    preferred_target = (ws_url, target_url)
            if preferred_target is not None:
                return preferred_target
            version_info = http_json(f"{cdp_url}/json/version")
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    else:
        raise RuntimeError(f"Timed out waiting for CDP to become ready: {last_error}")

    browser_ws = version_info.get("webSocketDebuggerUrl")
    if not browser_ws:
        raise RuntimeError("No browser webSocketDebuggerUrl found.")
    browser_client = CDPClient(browser_ws)
    target = browser_client.call("Target.createTarget", {"url": "about:blank"})
    target_id = target.get("targetId")
    if not target_id:
        raise RuntimeError("Target.createTarget did not return a targetId.")
    for _ in range(50):
        targets = http_json(f"{cdp_url}/json")
        for item in targets:
            if item.get("id") == target_id and item.get("webSocketDebuggerUrl"):
                return item["webSocketDebuggerUrl"], item.get("url", "about:blank")
        time.sleep(0.2)
    raise RuntimeError("Created a page target, but it never exposed webSocketDebuggerUrl.")


def save_cookies(cookie_out: str, cookies):
    out_path = Path(cookie_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path_str: str, content: str):
    out_path = Path(path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")


def save_json(path_str: str, payload):
    out_path = Path(path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_cookie_header(cookies):
    return "; ".join(
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if cookie.get("name") and cookie.get("value") is not None
    )


def normalize_cookies_for_domain(cookies, domain):
    normalized = []
    for cookie in cookies:
        cookie_domain = str(cookie.get("domain", "")).lstrip(".")
        if domain not in cookie_domain:
            continue
        normalized.append(cookie)
    return normalized


def has_usable_login_cookies(cookies):
    names = {cookie.get("name", "") for cookie in cookies}
    return "_csrf-backend" in names and "PHPSESSID" in names and "_identity-backend" in names


def get_kpi_cookies(client, domain: str):
    cookies = client.call("Network.getCookies", {"urls": [f"https://{domain}"]}).get("cookies", [])
    return normalize_cookies_for_domain(cookies, domain)


def is_authenticated_kpi_url(url: str):
    if "kpi.drojian.dev" not in url:
        return False
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    if "/login" in path or "/site/login" in path:
        return False
    return True


def is_business_kpi_url(url: str):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    return any(keyword in path for keyword in BUSINESS_PATH_KEYWORDS)


def is_verifiable_kpi_url(url: str):
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or ""
    return any(keyword in path for keyword in VERIFIABLE_PATH_KEYWORDS)


def summarize_payload(payload):
    if isinstance(payload, dict):
        parts = []
        if payload.get("code") is not None:
            parts.append(f"code={payload.get('code')}")
        if payload.get("msg"):
            parts.append(f"msg={payload.get('msg')}")
        if payload.get("data") is not None:
            parts.append(f"data_type={type(payload.get('data')).__name__}")
        if not parts:
            parts.append(f"keys={','.join(sorted(str(key) for key in payload.keys())[:6])}")
        return ", ".join(parts)
    return f"payload_type={type(payload).__name__}"


def verify_kpi_session(cookies, verify_url: str):
    if not verify_url:
        return {
            "ok": False,
            "status": "cookies_ready_but_unverified",
            "verified_url": "",
            "verification_result": "No KPI business URL available for active verification.",
        }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": build_cookie_header(cookies),
    }
    try:
        content, content_type = http_request(verify_url, headers=headers)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:160].strip().replace("\n", " ")
        return {
            "ok": False,
            "status": "cookies_ready_but_unverified",
            "verified_url": verify_url,
            "verification_result": f"HTTP {exc.code}: {body or exc.reason}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "cookies_ready_but_unverified",
            "verified_url": verify_url,
            "verification_result": f"Request failed: {exc}",
        }

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        snippet = content[:160].strip().replace("\n", " ")
        if "<html" in content.lower() or "text/html" in content_type.lower():
            result = "KPI returned HTML instead of JSON."
        else:
            result = f"KPI returned non-JSON response ({content_type or 'unknown content-type'}): {snippet}"
        return {
            "ok": False,
            "status": "cookies_ready_but_unverified",
            "verified_url": verify_url,
            "verification_result": result,
        }

    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        data = payload["data"]
        result = f"Verified KPI JSON payload with data list ({len(data)} rows)."
        return {
            "ok": True,
            "status": "session_verified",
            "verified_url": verify_url,
            "verification_result": result,
        }
    if isinstance(payload, list):
        result = f"Verified KPI JSON list payload ({len(payload)} rows)."
        return {
            "ok": True,
            "status": "session_verified",
            "verified_url": verify_url,
            "verification_result": result,
        }
    return {
        "ok": False,
        "status": "cookies_ready_but_unverified",
        "verified_url": verify_url,
        "verification_result": f"Unexpected KPI payload shape ({summarize_payload(payload)}).",
    }


def export_session_outputs(args, cookies, status: str, selected_target_url: str, verification, seen_urls, captured_from: str):
    cookie_header = build_cookie_header(cookies)
    session_payload = {"cookies": cookies}
    summary_payload = {
        "status": status,
        "domain": args.domain,
        "cookie_count": len(cookies),
        "cookie_names": [cookie.get("name", "") for cookie in cookies if cookie.get("name")],
        "captured_from": captured_from,
        "selected_target_url": selected_target_url,
        "verified_url": verification.get("verified_url", ""),
        "verification_result": verification.get("verification_result", ""),
        "seen_kpi_urls": seen_urls[:20],
        "outputs": {
            "cookie_out": args.cookie_out or "",
            "cookie_header_out": args.cookie_header_out or "",
            "session_json_out": args.session_json_out or "",
            "summary_out": args.summary_out or "",
        },
    }
    if args.cookie_out:
        save_cookies(args.cookie_out, cookies)
    if args.cookie_header_out:
        save_text(args.cookie_header_out, cookie_header + "\n")
    if args.session_json_out:
        save_json(args.session_json_out, session_payload)
    if args.summary_out:
        save_json(args.summary_out, summary_payload)
    result = {
        "status": status,
        "cookie_out": args.cookie_out or "",
        "cookie_header_out": args.cookie_header_out or "",
        "session_json_out": args.session_json_out or "",
        "summary_out": args.summary_out or "",
        "cookie_count": len(cookies),
        "cookie_names": summary_payload["cookie_names"],
        "captured_from": captured_from,
        "selected_target_url": selected_target_url,
        "verified_url": verification.get("verified_url", ""),
        "verification_result": verification.get("verification_result", ""),
        "seen_kpi_urls": seen_urls[:20],
    }
    if args.cookie_out:
        print(f"[OK] Saved {len(cookies)} cookies JSON to {args.cookie_out}")
    if args.cookie_header_out:
        print(f"[OK] Saved cookie header to {args.cookie_header_out}")
    if args.session_json_out:
        print(f"[OK] Saved session JSON to {args.session_json_out}")
    if args.summary_out:
        print(f"[OK] Saved summary JSON to {args.summary_out}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def export_logged_out_summary(args, selected_target_url: str, seen_urls):
    summary_payload = {
        "status": "logged_out",
        "domain": args.domain,
        "cookie_count": 0,
        "cookie_names": [],
        "captured_from": "",
        "selected_target_url": selected_target_url,
        "verified_url": "",
        "verification_result": "Missing usable KPI login cookies.",
        "seen_kpi_urls": seen_urls[:20],
        "outputs": {
            "cookie_out": args.cookie_out or "",
            "cookie_header_out": args.cookie_header_out or "",
            "session_json_out": args.session_json_out or "",
            "summary_out": args.summary_out or "",
        },
    }
    if args.summary_out:
        save_json(args.summary_out, summary_payload)


def attempt_export_and_verify(args, client, selected_target_url: str, seen_urls, captured_from: str, verify_url: str):
    cookies = get_kpi_cookies(client, args.domain)
    if not has_usable_login_cookies(cookies):
        return False
    verification = verify_kpi_session(cookies, verify_url)
    status = "session_verified" if verification.get("ok") else "cookies_ready_but_unverified"
    export_session_outputs(args, cookies, status, selected_target_url, verification, seen_urls, captured_from)
    return True


def _load_cached_session(path_str: str):
    """Return cookies from session file if it exists and cookies are usable."""
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cookies = payload.get("cookies", [])
    except Exception:
        return None
    if has_usable_login_cookies(cookies):
        return cookies
    return None


def main():
    args = parse_args()

    if args.session_json_out:
        cached_cookies = _load_cached_session(args.session_json_out)
        if cached_cookies is not None:
            print(f"[INFO] Reusing existing session from {args.session_json_out} (written today, cookies valid). Skipping CDP flow.")
            verification = {"ok": False, "verified_url": "", "verification_result": "Loaded from existing session file; not re-verified."}
            export_session_outputs(args, cached_cookies, "cookies_ready_but_unverified", args.session_json_out, verification, [], "session_json_out_cache")
            return

    print(f"[INFO] Fetching page target from {args.cdp_url}")
    ws_url, selected_target_url = choose_page_ws(args.cdp_url, args.cdp_ready_timeout_ms, args.domain)
    client = CDPClient(ws_url)
    client.call("Network.enable")
    client.call("Page.enable")
    initial_verify_url = args.login_url if is_authenticated_kpi_url(args.login_url) and is_verifiable_kpi_url(args.login_url) else ""
    if attempt_export_and_verify(args, client, selected_target_url, [], "", initial_verify_url):
        return

    if args.login_url and args.login_url != selected_target_url:
        print(f"[INFO] Navigating page target to {args.login_url}")
        client.call("Page.navigate", {"url": args.login_url})
        selected_target_url = args.login_url
    print("[INFO] Please complete KPI login in Chrome. I will export cookies as soon as usable login cookies are available and verify them with a KPI business request when possible.")

    deadline = time.time() + args.timeout_ms / 1000
    seen_urls = []
    seen_url_set = set()
    while time.time() < deadline:
        message = client.recv()
        if not message:
            continue
        method = message.get("method")
        if method not in {"Network.requestWillBeSent", "Network.responseReceived"}:
            continue
        event = message.get("params", {})
        request_url = ""
        if method == "Network.requestWillBeSent":
            request_url = event.get("request", {}).get("url", "")
        else:
            request_url = event.get("response", {}).get("url", "")
        if "kpi.drojian.dev" not in request_url:
            continue
        if request_url not in seen_url_set:
            seen_url_set.add(request_url)
            seen_urls.append(request_url)
        if not is_authenticated_kpi_url(request_url):
            continue
        selected_target_url = request_url
        verify_url = request_url if is_verifiable_kpi_url(request_url) else ""
        if attempt_export_and_verify(args, client, selected_target_url, seen_urls, request_url, verify_url):
            return

    export_logged_out_summary(args, selected_target_url, seen_urls)
    raise RuntimeError(
        "Timed out waiting for usable KPI login cookies or a verifiable KPI business request."
    )


if __name__ == "__main__":
    main()
