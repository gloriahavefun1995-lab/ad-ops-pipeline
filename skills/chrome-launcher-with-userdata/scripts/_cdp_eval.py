import socket, json, base64, os, struct, urllib.parse, sys, time

def ws_connect(url):
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    ws_port = parsed.port or 80
    ws_path = parsed.path
    sock = socket.create_connection((host, ws_port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        "GET " + ws_path + " HTTP/1.1\r\n"
        "Host: " + host + ":" + str(ws_port) + "\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: " + key + "\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(handshake.encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    return sock

def ws_send(sock, message):
    payload = message.encode("utf-8")
    frame = bytearray()
    frame.append(0x81)
    mask_key = os.urandom(4)
    length = len(payload)
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack("!H", length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack("!Q", length))
    frame.extend(mask_key)
    masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    frame.extend(masked)
    sock.sendall(bytes(frame))

def ws_recv(sock):
    # 简单读取一个 WebSocket 帧
    try:
        header = sock.recv(2)
        if len(header) < 2:
            return ""
        payload_len = header[1] & 0x7F
        if payload_len == 126:
            ext = sock.recv(2)
            payload_len = struct.unpack("!H", ext)[0]
        elif payload_len == 127:
            ext = sock.recv(8)
            payload_len = struct.unpack("!Q", ext)[0]
        data = b""
        while len(data) < payload_len:
            chunk = sock.recv(payload_len - len(data))
            if not chunk:
                break
            data += chunk
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""

def ws_send_and_recv(sock, msg_id, method, params=None):
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    ws_send(sock, json.dumps(msg))
    # 读取响应（可能需要跳过事件消息）
    for _ in range(20):
        raw = ws_recv(sock)
        if not raw:
            continue
        try:
            resp = json.loads(raw)
            if resp.get("id") == msg_id:
                return resp
        except json.JSONDecodeError:
            continue
    return None

params_file = sys.argv[1]
with open(params_file, "r", encoding="utf-8") as f:
    params = json.load(f)

ws_url = params["ws_url"]
html = params["html"]

sock = ws_connect(ws_url)

# 1. 启用 Page 域
ws_send_and_recv(sock, 1, "Page.enable")

# 2. 导航到 about:blank（离开 chrome://newtab）
ws_send_and_recv(sock, 2, "Page.navigate", {"url": "about:blank"})
time.sleep(0.5)

# 3. 获取 frameId
resp = ws_send_and_recv(sock, 3, "Page.getFrameTree")
frame_id = None
if resp and "result" in resp:
    frame_id = resp["result"].get("frameTree", {}).get("frame", {}).get("id")

# 4. 用 Page.setDocumentContent 注入完整 HTML
if frame_id:
    ws_send_and_recv(sock, 4, "Page.setDocumentContent", {
        "frameId": frame_id,
        "html": html
    })
else:
    # fallback: 用 Runtime.evaluate 设置
    js = "document.open(); document.write(" + json.dumps(html) + "); document.close();"
    ws_send_and_recv(sock, 4, "Runtime.evaluate", {"expression": js})

sock.close()
