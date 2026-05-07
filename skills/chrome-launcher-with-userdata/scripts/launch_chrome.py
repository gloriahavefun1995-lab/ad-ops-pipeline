#!/usr/bin/env python3
"""
Chrome Launcher - 启动带远程调试端口的Chrome浏览器
自动检测Chrome路径、复制User Data、分配空闲端口、启动浏览器
兼容 Windows 和 macOS
"""

import argparse
import ctypes
import json
import os
import platform
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse


# ─── Chrome 路径检测 ───

def find_chrome_path():
    """自动检测Chrome安装路径，返回可执行文件路径或None"""
    system = platform.system()

    if system == "Windows":
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"),
                         "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"),
                         "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path

        # 尝试从注册表查询
        try:
            import winreg
            reg_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
            for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    key = winreg.OpenKey(hive, reg_path)
                    value, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    if os.path.isfile(value):
                        return value
                except OSError:
                    continue
        except ImportError:
            pass

    elif system == "Darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path

    return None


# ─── User Data 路径检测 ───

def get_default_user_data_dir():
    """获取Chrome默认User Data目录"""
    system = platform.system()
    if system == "Windows":
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    elif system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    return None


# ─── Chrome 进程检测 ───

def is_chrome_running():
    """检测用户的Chrome是否正在运行"""
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
                capture_output=True, text=True, timeout=5
            )
            return "chrome.exe" in result.stdout.lower()
        elif system == "Darwin":
            result = subprocess.run(
                ["pgrep", "-x", "Google Chrome"],
                capture_output=True, text=True, timeout=5
            )
            return result.returncode == 0
    except Exception:
        pass
    return False


def normalize_path(path):
    """归一化路径，便于跨进程比对。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _split_command_line(command):
    """按平台尽量准确地拆分命令行参数。"""
    system = platform.system()

    if system == "Windows":
        try:
            argc = ctypes.c_int()
            argv = ctypes.windll.shell32.CommandLineToArgvW(command, ctypes.byref(argc))
            if argv:
                args = [argv[i] for i in range(argc.value)]
                ctypes.windll.kernel32.LocalFree(argv)
                return args
        except Exception:
            pass

        # 退化路径：尽量保留 Windows 风格引号处理
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()

    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _clean_command_value(value):
    """清理参数值外层引号。"""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _extract_command_arg(command, arg_name):
    """从命令行中提取参数值，支持 --arg=value 或 --arg value。"""
    args = _split_command_line(command)

    for idx, arg in enumerate(args):
        if arg == arg_name and idx + 1 < len(args):
            return _clean_command_value(args[idx + 1])
        if arg.startswith(f"{arg_name}="):
            return _clean_command_value(arg.split("=", 1)[1])
    return None


def list_chrome_processes():
    """列出 Chrome 主进程及其命令行。"""
    system = platform.system()

    try:
        if system == "Darwin":
            result = subprocess.run(
                ["ps", "-axww", "-o", "pid=,command="],
                capture_output=True, text=True, timeout=5
            )
            processes = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or "Google Chrome" not in line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                pid_text, command = parts
                if "--type=" in command:
                    continue
                try:
                    pid = int(pid_text)
                except ValueError:
                    continue
                processes.append({"pid": pid, "command": command})
            return processes

        if system == "Windows":
            script = (
                "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
                "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0 or not result.stdout.strip():
                return []

            data = json.loads(result.stdout)
            if isinstance(data, dict):
                data = [data]

            processes = []
            for item in data:
                command = item.get("CommandLine") or ""
                if "--type=" in command:
                    continue
                pid = item.get("ProcessId")
                if isinstance(pid, int):
                    processes.append({"pid": pid, "command": command})
            return processes
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

    return []


def read_devtools_active_port(user_data_dir):
    """尝试从 User Data 目录读取当前 CDP 端口。"""
    port_file = os.path.join(user_data_dir, "DevToolsActivePort")
    try:
        with open(port_file, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line.isdigit():
            return int(first_line)
    except OSError:
        pass
    return None


def find_running_chrome_for_user_data(user_data_dir):
    """查找是否已有 Chrome 正在使用指定的 User Data 目录。"""
    normalized_target = normalize_path(user_data_dir)

    for process in list_chrome_processes():
        proc_user_data_dir = _extract_command_arg(process["command"], "--user-data-dir")
        if not proc_user_data_dir:
            continue
        if normalize_path(proc_user_data_dir) != normalized_target:
            continue

        port_text = _extract_command_arg(process["command"], "--remote-debugging-port")
        port = int(port_text) if port_text and port_text.isdigit() else None
        return {
            "pid": process["pid"],
            "command": process["command"],
            "port": port,
        }

    return None


def find_all_chrome_pids_for_user_data(user_data_dir):
    """查找所有（主进程 + 渲染/GPU 等辅助进程）使用指定 User Data 目录的 Chrome PID。"""
    normalized_target = normalize_path(user_data_dir)
    system = platform.system()
    pids = []

    try:
        if system == "Darwin":
            result = subprocess.run(
                ["ps", "-axww", "-o", "pid=,command="],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line or "Google Chrome" not in line:
                    continue
                parts = line.split(None, 1)
                if len(parts) != 2:
                    continue
                pid_text, command = parts
                try:
                    pid = int(pid_text)
                except ValueError:
                    continue
                proc_udd = _extract_command_arg(command, "--user-data-dir")
                if proc_udd and normalize_path(proc_udd) == normalized_target:
                    pids.append(pid)

        elif system == "Windows":
            script = (
                "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
                "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
                for item in data:
                    command = item.get("CommandLine") or ""
                    pid = item.get("ProcessId")
                    if not isinstance(pid, int):
                        continue
                    proc_udd = _extract_command_arg(command, "--user-data-dir")
                    if proc_udd and normalize_path(proc_udd) == normalized_target:
                        pids.append(pid)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

    return pids


def kill_chrome_instances_for_user_data(user_data_dir, graceful_timeout=6):
    """终止所有使用指定 User Data 目录的 Chrome 进程（主进程 + 子进程）。

    策略：
    - Windows：对每个 PID 使用 `taskkill /PID <pid> /T /F`（强制终止进程树）
    - macOS：先 SIGTERM 让 Chrome 优雅退出（能落盘 SQLite 等状态），
             超过 graceful_timeout 仍存活的进程再 SIGKILL。
    返回本次终止的 PID 数量。
    """
    pids = find_all_chrome_pids_for_user_data(user_data_dir)
    if not pids:
        return 0

    system = platform.system()
    print(f"[INFO] 检测到 {len(pids)} 个 Chrome 进程正在使用该 User Data 目录，准备终止后重新启动...")

    if system == "Windows":
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=5
                )
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError:
                pass

    # 等待进程实际退出
    deadline = time.time() + graceful_timeout
    while time.time() < deadline:
        if not find_all_chrome_pids_for_user_data(user_data_dir):
            print(f"[OK] 已终止所有相关 Chrome 进程")
            return len(pids)
        time.sleep(0.3)

    # 兜底：仍未退出则强制 KILL（macOS），Windows 上 /F 已是强制终止，这里再重试一轮
    remaining = find_all_chrome_pids_for_user_data(user_data_dir)
    if remaining:
        print(f"[WARN] {len(remaining)} 个进程未在 {graceful_timeout}s 内退出，强制终止...")
        for pid in remaining:
            try:
                if system == "Windows":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=5
                    )
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, subprocess.SubprocessError):
                pass

        deadline = time.time() + 3
        while time.time() < deadline:
            if not find_all_chrome_pids_for_user_data(user_data_dir):
                break
            time.sleep(0.3)

    still_alive = find_all_chrome_pids_for_user_data(user_data_dir)
    if still_alive:
        print(f"[ERROR] 仍有 {len(still_alive)} 个 Chrome 进程未能终止（PID: {still_alive}）")
    else:
        print(f"[OK] 已终止所有相关 Chrome 进程")

    return len(pids)



# ─── 复制 User Data ───

def should_copy_user_data(dst, force=False):
    """判断这次是否需要执行 User Data 复制。"""
    local_state = os.path.join(dst, "Local State")
    return force or not (os.path.isdir(dst) and os.path.isfile(local_state))


def copy_user_data(src, dst, force=False):
    """完整复制User Data目录到目标位置（使用 shutil.copytree）

    重要：复制前必须确保Chrome已关闭，否则关键文件（Cookies、Login Data等）
    可能被锁定或处于SQLite WAL不一致状态，导致登录态丢失。
    """
    # 检查是否已复制过（目标目录存在且包含Local State文件）
    if not should_copy_user_data(dst, force=force):
        print(f"[INFO] User Data 已存在于 {dst}，跳过复制。使用 --force-copy 强制重新复制。")
        return True

    if not os.path.isdir(src):
        print(f"[ERROR] 源 User Data 目录不存在: {src}")
        return False

    # 检查Chrome是否在运行
    if is_chrome_running():
        print(f"[ERROR] 检测到Chrome正在运行！Chrome运行时关键文件（Cookies等）被锁定，无法完整复制。")
        print(f"  请先关闭所有Chrome窗口，然后重新运行此脚本。")
        sys.exit(1)

    # 如果强制复制，先删除旧目录
    if force and os.path.isdir(dst):
        print(f"[INFO] 强制模式：删除旧目录 {dst}")
        shutil.rmtree(dst, ignore_errors=True)

    print(f"[INFO] 正在完整复制 User Data...")
    print(f"  源目录: {src}")
    print(f"  目标目录: {dst}")
    print(f"  (完整复制所有文件，可能需要较长时间，请耐心等待...)")

    try:
        shutil.copytree(src, dst, dirs_exist_ok=True, copy_function=shutil.copy2,
                        ignore_dangling_symlinks=True)
        print(f"[OK] User Data 完整复制完成")
    except shutil.Error as e:
        # copytree 会收集所有错误后一次性抛出
        error_count = len(e.args[0]) if e.args else 0
        print(f"[WARN] 复制完成，但有 {error_count} 个文件因权限问题跳过。")
        print(f"       建议关闭所有Chrome后使用 --force-copy 重新复制。")
    cleanup_chrome_runtime_artifacts(dst)
    return True

def cleanup_chrome_runtime_artifacts(user_data_dir):
    """清理会干扰新实例启动的运行时锁文件和端口文件。"""
    names = [
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
        "DevToolsActivePort",
    ]

    removed = []
    for name in names:
        path = os.path.join(user_data_dir, name)
        if not os.path.lexists(path):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
            removed.append(name)
        except OSError:
            pass

    if removed:
        print(f"[INFO] 已清理运行时痕迹: {', '.join(removed)}")


# ─── 端口管理 ───

def find_free_port():
    """在 10000-65535 范围内找一个空闲端口，避开常用端口区间。

    避开的区间：
    - 0-1023: 系统保留端口
    - 1024-9999: 常见服务端口（MySQL 3306, Redis 6379, Postgres 5432 等）
    - 8080/8443/8888 等常见开发端口也在此范围内
    """
    import random
    # 优先在高位端口范围内随机尝试
    tried = set()
    for _ in range(50):
        port = random.randint(10000, 65535)
        if port in tried:
            continue
        tried.add(port)
        if is_port_available(port):
            return port
    # 兜底：让系统分配
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_port_available(port):
    """检查指定端口是否可用"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _mac_app_bundle_from_binary(chrome_path):
    """从 macOS 可执行文件路径推导 .app 包路径。"""
    suffix = "/Contents/MacOS/Google Chrome"
    if chrome_path.endswith(suffix):
        return chrome_path[:-len(suffix)]
    return None


def resolve_browser_pid(port, fallback_pid=None):
    """尽量解析监听 CDP 端口的浏览器进程 PID。"""
    system = platform.system()

    if system == "Darwin":
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=5
            )
            pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if pids:
                return int(pids[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    return fallback_pid


# ─── 启动 Chrome ───

def launch_chrome(chrome_path, port, user_data_dir, headless=False):
    """启动Chrome并返回进程对象"""
    chrome_args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--profile-directory=Default",
        "--no-first-run",
    ]
    if headless:
        chrome_args.append("--headless=new")

    system = platform.system()
    cmd = chrome_args

    # 在 macOS 的有界面模式下使用 open -na 拉起独立 GUI 实例。
    if system == "Darwin" and not headless:
        bundle_path = _mac_app_bundle_from_binary(chrome_path)
        if bundle_path and os.path.isdir(bundle_path):
            cmd = [
                "open",
                "-na",
                bundle_path,
                "--args",
                *chrome_args[1:],
            ]

    print(f"[INFO] 启动Chrome...")
    print(f"  命令: {' '.join(cmd)}")

    # 使用非阻塞方式启动，方便后续轮询 CDP 端口。
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return process


def wait_for_cdp(port, timeout=15):
    """等待CDP端口就绪"""
    url = f"http://localhost:{port}/json/version"
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.urlopen(url, timeout=2)
            data = json.loads(req.read().decode())
            return data
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    return None


def _build_port_html(port):
    """构建显示端口信息的 HTML 页面"""
    return """<!DOCTYPE html>
<html>
<head><title>Chrome Debug - Port {port}</title></head>
<body style="margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;
             background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);font-family:system-ui,sans-serif;">
  <div style="background:#fff;border-radius:16px;padding:48px 64px;box-shadow:0 20px 60px rgba(0,0,0,.3);text-align:center;">
    <div style="font-size:20px;color:#666;margin-bottom:8px;">Chrome Remote Debugging</div>
    <div style="font-size:72px;font-weight:bold;color:#333;margin:16px 0;">{port}</div>
    <div style="font-size:16px;color:#999;margin-bottom:24px;">CDP Address</div>
    <code style="display:inline-block;background:#f4f4f8;padding:12px 24px;border-radius:8px;
                 font-size:18px;color:#4a5568;letter-spacing:.5px;">http://localhost:{port}</code>
    <div style="margin-top:32px;padding-top:24px;border-top:1px solid #eee;">
      <div style="font-size:14px;color:#aaa;margin-bottom:12px;">Playwright Connect Example</div>
      <code style="display:block;background:#1e1e2e;color:#a6e3a1;padding:16px 20px;border-radius:8px;
                   font-size:13px;text-align:left;white-space:pre;line-height:1.6;">browser = p.chromium.connect_over_cdp("http://localhost:{port}")</code>
    </div>
  </div>
</body>
</html>""".replace("{port}", str(port))


def show_port_on_page(port):
    """通过CDP在浏览器页面上显示端口信息，方便用户查看

    策略：先用 Page.navigate 导航到 about:blank（避免 chrome://newtab 覆盖），
    然后用 Page.setDocumentContent 注入完整 HTML。
    """
    try:
        # 获取页面列表，找到第一个 page 类型的页面
        req = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=5)
        pages = json.loads(req.read().decode())
        target_page = None
        for p in pages:
            if p.get("type") == "page":
                target_page = p
                break
        if not target_page:
            print(f"[INFO] 浏览器无可用页面")
            return

        ws_url = target_page.get("webSocketDebuggerUrl", "")
        if not ws_url:
            print(f"[INFO] 无法获取WebSocket地址")
            return

        html = _build_port_html(port)

        # 将参数写入临时文件
        skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        params_file = os.path.join(skill_dir, "_cdp_params.json")
        with open(params_file, "w", encoding="utf-8") as f:
            json.dump({"ws_url": ws_url, "html": html}, f, ensure_ascii=False)

        helper_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_cdp_eval.py")
        # 总是重写 helper（保持最新逻辑）
        _write_cdp_helper(helper_script)

        result = subprocess.run(
            [sys.executable, helper_script, params_file],
            capture_output=True, text=True, timeout=15
        )
        # 清理临时参数文件
        try:
            os.remove(params_file)
        except OSError:
            pass

        if result.returncode == 0:
            print(f"[OK] 已在浏览器页面显示端口信息")
        else:
            print(f"[INFO] 无法自动在页面显示端口信息: {result.stderr.strip()}")
    except Exception as e:
        print(f"[INFO] 无法自动在页面显示端口信息: {e}")


def _write_cdp_helper(path):
    """写入 CDP WebSocket helper 脚本

    通过原始 socket 实现 WebSocket 握手和消息发送（纯标准库），
    先发 Page.navigate 到 about:blank，再发 Page.setDocumentContent 注入 HTML。
    """
    code = r'''import socket, json, base64, os, struct, urllib.parse, sys, time

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
'''
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)


# ─── 主流程 ───

def main():
    parser = argparse.ArgumentParser(description="启动带远程调试端口的Chrome浏览器")
    parser.add_argument("--port", type=int, default=None, help="指定调试端口（默认自动分配）")
    # User Data 固定复制到本脚本所在的 skill 目录下
    skill_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_data_dir = os.path.join(skill_dir, "chrome_user_data")
    parser.add_argument("--user-data-dir", default=default_data_dir, help="复制后的User Data存放目录")
    parser.add_argument("--chrome-path", default=None, help="手动指定Chrome可执行文件路径")
    parser.add_argument("--force-copy", action="store_true", help="强制重新复制User Data")
    parser.add_argument("--no-copy", action="store_true", help="不复制User Data，使用全新配置")
    parser.add_argument("--headless", action="store_true", help="以无头模式启动Chrome（无界面）")
    args = parser.parse_args()

    # 1. 检测Chrome路径
    chrome_path = args.chrome_path or find_chrome_path()
    if not chrome_path:
        print("[ERROR] 未检测到Chrome安装路径。请使用 --chrome-path 手动指定。")
        sys.exit(1)
    print(f"[OK] Chrome路径: {chrome_path}")

    # 2. 处理固定的 User Data 目录
    user_data_dir = os.path.abspath(args.user_data_dir)
    if args.no_copy:
        print(f"[INFO] 跳过User Data复制，使用全新配置: {user_data_dir}")
        os.makedirs(user_data_dir, exist_ok=True)
    else:
        src_data = get_default_user_data_dir()
        if not src_data or not os.path.isdir(src_data):
            print(f"[WARN] 未找到Chrome User Data目录，将使用全新配置")
            os.makedirs(user_data_dir, exist_ok=True)
        else:
            print(f"[OK] 源User Data: {src_data}")
            if should_copy_user_data(user_data_dir, force=args.force_copy) and is_chrome_running():
                print("[ERROR] 检测到Chrome正在运行，当前需要复制 User Data。")
                print("  请先关闭所有Chrome窗口，然后重新运行此脚本。")
                sys.exit(1)
            if not copy_user_data(src_data, user_data_dir, force=args.force_copy):
                print("[ERROR] User Data复制失败")
                sys.exit(1)

    # 3. 如果该 User Data 已被一个本技能启动过但未正常关闭的 Chrome 使用，
    #    先把它（及其所有子进程）终止掉，再重新启动，确保是干净的新实例。
    if find_running_chrome_for_user_data(user_data_dir):
        kill_chrome_instances_for_user_data(user_data_dir)

    cleanup_chrome_runtime_artifacts(user_data_dir)

    # 4. 获取端口
    if args.port:
        if not is_port_available(args.port):
            print(f"[ERROR] 端口 {args.port} 已被占用，请更换端口或不指定端口让系统自动分配")
            sys.exit(1)
        port = args.port
    else:
        port = find_free_port()
    print(f"[OK] 调试端口: {port}")

    # 5. 启动Chrome
    process = launch_chrome(chrome_path, port, user_data_dir, headless=args.headless)

    # 6. 等待CDP就绪
    print(f"[INFO] 等待CDP就绪 (http://localhost:{port})...")
    cdp_info = wait_for_cdp(port)

    mode = "headless" if args.headless else "headed"

    # 7. 在页面上显示端口信息
    if cdp_info:
        show_port_on_page(port)

    if cdp_info:
        browser_pid = resolve_browser_pid(port, fallback_pid=process.pid)
        result = {
            "chrome_path": chrome_path,
            "port": port,
            "user_data_dir": user_data_dir,
            "cdp_url": f"http://localhost:{port}",
            "pid": browser_pid,
            "status": "running",
            "mode": mode,
            "browser_version": cdp_info.get("Browser", "unknown"),
        }
        print(f"\n[OK] Chrome已启动! (模式: {mode})")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"\n[WARN] Chrome已启动(PID: {process.pid})，但CDP端口未在超时内就绪。")
        print(f"  请手动检查: http://localhost:{port}/json/version")
        result = {
            "chrome_path": chrome_path,
            "port": port,
            "user_data_dir": user_data_dir,
            "cdp_url": f"http://localhost:{port}",
            "pid": process.pid,
            "status": "started_but_cdp_not_ready",
            "mode": mode,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))

    return result


if __name__ == "__main__":
    main()
