---
name: chrome-launcher-with-userdata
description: 启动带远程调试端口的Chrome浏览器，自动检测Chrome安装路径，复制用户数据目录以保留登录状态，分配空闲端口，兼容Windows和Mac。专为Playwright自动化测试设计，通过CDP接管已登录的浏览器实例。当用户需要启动Chrome调试模式、连接CDP、使用Playwright接管浏览器、需要保留登录态的自动化测试、或提到remote-debugging-port时，使用本技能。即使用户只说"帮我启动浏览器"或"打开Chrome调试模式"，也应触发本技能。
---

# Chrome Launcher — 为Playwright启动带远程调试端口的Chrome

本技能自动完成以下工作，让Playwright可以通过CDP接管一个保留了用户登录态的Chrome实例：

1. 检测本机Chrome安装路径
2. 将Chrome User Data完整复制到技能目录下（保留登录态，避免影响原始数据）
3. 自动分配一个空闲端口（10000-65535，避开常用服务端口）
4. 检测复制后的 User Data 是否已经被一个 Chrome 实例占用；若已存在则先**终止旧实例**（含所有子进程）再重新启动
5. 以 `--remote-debugging-port` 模式启动Chrome
6. 验证CDP端口就绪，并在浏览器页面上直观显示端口信息
7. 输出连接信息（JSON）

## 使用方式

直接运行技能目录下的脚本：

```bash
python <skill-path>/scripts/launch_chrome.py
```

User Data 默认复制到 `<skill-path>/chrome_user_data/` 目录下。如果该目录已存在且包含 `Local State` 文件，则跳过复制。

> **重要前置条件**：只有在“首次复制 User Data”或使用 `--force-copy` 重新复制时，才需要先**完全关闭本机 Chrome**。这是因为复制期间 Cookies、Login Data 等关键文件可能被锁定，继续复制会导致登录态丢失。

复制完成后，后续再次运行时脚本会优先检查这份复制后的 `chrome_user_data` 是否已经被某个 Chrome 实例使用：
- 若已在运行（上次启动后未完全关闭）：**先终止旧实例**（含所有子进程），再使用同一份目录重新启动一个干净的新 Chrome
- 若未在运行：直接使用同一份目录启动新的 Chrome

> 终止旧实例时 Windows 使用 `taskkill /T /F` 终止整个进程树；macOS 先 `SIGTERM` 让 Chrome 落盘退出，超时未退再 `SIGKILL` 兜底。

### Playwright 连接示例

脚本启动后输出的 `cdp_url` 可直接用于 Playwright：

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    # 使用脚本输出的CDP地址连接
    browser = p.chromium.connect_over_cdp("http://localhost:<port>")
    context = browser.contexts[0]  # 获取已有的浏览器上下文（包含登录态）
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://your-app.com")
```

### 可选参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--port` | 指定调试端口（不指定则自动分配空闲端口） | 自动分配 |
| `--user-data-dir` | 复制后的 User Data 存放目录 | `<skill-path>/chrome_user_data` |
| `--chrome-path` | 手动指定Chrome可执行文件路径 | 自动检测 |
| `--force-copy` | 强制重新复制 User Data（先删除旧目录） | `false` |
| `--headless` | 以无头模式启动Chrome（无界面窗口） | `false` |

### 示例

```bash
# 自动检测一切，使用默认配置
python <skill-path>/scripts/launch_chrome.py

# 指定端口（如Playwright脚本固定使用9222）
python <skill-path>/scripts/launch_chrome.py --port 9222

# 强制重新复制User Data（登录态过期后刷新）
python <skill-path>/scripts/launch_chrome.py --force-copy

# 无头模式启动（不弹出浏览器窗口）
python <skill-path>/scripts/launch_chrome.py --headless

# 指定 User Data 目录
python <skill-path>/scripts/launch_chrome.py --user-data-dir /path/to/data
```

当用户提到"无头模式"、"headless"、"不要弹窗"、"后台运行浏览器"时，使用 `--headless` 参数启动。无头模式使用 Chrome 的 `--headless=new`（新版无头模式），功能与有界面模式完全一致，适合 CI/CD 环境或不需要观察浏览器的场景。

## 执行流程细节

### 第1步：检测Chrome安装路径

**Windows：**
1. `%ProgramFiles%\Google\Chrome\Application\chrome.exe`
2. `%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe`
3. `%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe`
4. 注册表 `HKLM/HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe`

**macOS：**
1. `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
2. `~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`

检测失败时提示用户通过 `--chrome-path` 手动指定。

### 第2步：复制User Data到技能目录

**源路径（自动检测）：**
- Windows: `%LOCALAPPDATA%\Google\Chrome\User Data`
- macOS: `~/Library/Application Support/Google/Chrome`

**目标路径：** `<skill-path>/chrome_user_data/`（可用 `--user-data-dir` 覆盖）

**复制策略：**
- 目标目录已存在且包含 `Local State` 文件 → 跳过复制，直接进入“检测是否已有实例在运行”
- 目标目录不存在或为空 → 执行完整复制（`shutil.copytree`）
- 指定 `--force-copy` → 删除旧目录后重新完整复制
- 源目录不存在（未安装过 Chrome 或路径异常） → 警告并创建空目录继续启动

**复制方式**：使用 `shutil.copytree` **完整**复制所有文件（包括缓存），以确保登录态相关文件完整。脚本不会选择性排除任何目录。首次复制时间取决于 User Data 大小，请耐心等待。

**Chrome 运行检测**：
- Windows：`tasklist /FI "IMAGENAME eq chrome.exe"`
- macOS：`pgrep -x "Google Chrome"`

若检测到 Chrome 正在运行，脚本只会在“当前确实需要重新复制 User Data”时**直接退出并报错**，要求用户先关闭所有 Chrome 窗口。这是为了防止关键文件被锁定或 SQLite 数据库处于不一致状态。

若当前不需要复制，而只是复用已经复制好的 `chrome_user_data`：
- 脚本会尝试检测是否已有 Chrome 正在使用这份目录
- 若已存在，则返回 `already_running`
- 若不存在，则继续启动新的实例

### 第3步：检测是否已经有实例在运行（若有则先终止）

脚本会枚举当前系统中的所有 Chrome 进程（主进程 + 渲染/GPU 等辅助进程），并检查其命令行参数中是否包含：

```text
--user-data-dir=<skill-path>/chrome_user_data
```

若匹配到同一份复制后的 User Data 目录，说明存在一个本技能上次启动后未完全关闭的 Chrome 实例。脚本会：

1. **终止所有匹配到的 Chrome 进程**（包含子进程）
   - Windows：对每个 PID 执行 `taskkill /PID <pid> /T /F`（强制终止进程树）
   - macOS：先 `SIGTERM` 让 Chrome 优雅落盘退出（约 6 秒超时），仍存活的进程用 `SIGKILL` 强制终止
2. 轮询确认所有进程退出后，清理 `SingletonLock` / `DevToolsActivePort` 等运行时痕迹
3. 继续后续的端口分配与启动流程，启动一个全新的 Chrome 实例

### 第4步：获取空闲端口

在 `10000-65535` 范围内随机挑端口并验证可用性（最多尝试 50 次），避开以下常用区间：
- `0-1023`：系统保留端口
- `1024-9999`：MySQL 3306、Redis 6379、Postgres 5432、常见开发端口 8080/8443/8888 等

兜底策略：若随机尝试都失败，则交由操作系统通过 `socket.bind(("127.0.0.1", 0))` 分配。

指定 `--port` 时先验证端口可用性，若被占用则报错退出。

### 第5步：启动Chrome并验证CDP

启动命令（有界面模式）：
```
chrome --remote-debugging-port=<port> \
       --user-data-dir=<skill-path>/chrome_user_data \
       --profile-directory=Default \
       --no-first-run
```

无头模式会追加 `--headless=new`。

Chrome 以非阻塞方式（`subprocess.Popen`）启动，stdout/stderr 被丢弃。启动后轮询 `http://localhost:<port>/json/version` 验证 CDP 就绪，超时 15 秒。

### 第6步：在页面上显示端口信息

CDP 就绪后，脚本会通过 WebSocket 连接 CDP，依次：
1. `Page.enable` 启用 Page 域
2. `Page.navigate` 导航到 `about:blank`（避开 `chrome://newtab` 限制）
3. `Page.getFrameTree` 获取当前 frameId
4. `Page.setDocumentContent` 注入一个展示端口号和 Playwright 连接示例的 HTML 页面

该功能通过同目录下的 `_cdp_eval.py` helper 实现（纯标准库 socket 手写 WebSocket 握手和帧，无第三方依赖）。即使页面注入失败也不影响 Chrome 启动本身。

### 输出

首次启动成功时输出（`status: "running"`）：

```json
{
  "chrome_path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "port": 54321,
  "user_data_dir": "C:\\Users\\11524\\.claude\\skills\\chrome-launcher\\chrome_user_data",
  "cdp_url": "http://localhost:54321",
  "pid": 12345,
  "status": "running",
  "mode": "headed",
  "browser_version": "Chrome/125.0.6422.76"
}
```

若检测到上次启动的 Chrome 未完全关闭，脚本会在日志中输出：

```text
[INFO] 检测到 N 个 Chrome 进程正在使用该 User Data 目录，准备终止后重新启动...
[OK] 已终止所有相关 Chrome 进程
```

然后继续上面的正常启动流程，最终输出仍然是 `status: "running"`。

若 Chrome 已启动但 CDP 未在 15 秒内就绪，`status` 会变为 `"started_but_cdp_not_ready"`，此时可手动访问 `http://localhost:<port>/json/version` 检查。

## 注意事项

- **只有在首次复制或 `--force-copy` 重新复制 User Data 时，才必须先关闭所有 Chrome 窗口**。
- 如果未能检测到 Chrome 安装路径或 User Data 源目录，脚本会提示用户通过 `--chrome-path` 手动指定（或自动降级为全新配置）。
- 后续再次运行时，脚本会优先检测是否已有 Chrome 正在使用同一份复制后的 User Data；若已存在则**自动终止旧实例（含子进程）后重新启动**一个干净的新 Chrome，无需手动清理。
- 该终止操作只针对使用本技能 `chrome_user_data` 目录的 Chrome 进程，不会影响用户日常使用的 Chrome 窗口。
- 首次完整复制 User Data 可能需要较长时间（取决于数据量），后续再次启动会直接跳过复制。
- `chrome_user_data` 目录建议加入 `.gitignore`，避免被提交到版本控制。
- `_cdp_eval.py` 是页面注入使用的 helper，主脚本每次运行时会重写它以保持最新逻辑。
