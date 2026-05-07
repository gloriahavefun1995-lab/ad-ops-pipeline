---
name: 低表现文案定位
description: 查询 KPI 数据，筛选低表现广告素材，并在 Google Sheets 中插入格式正确的空白优化行。内容字段（Asset、翻译、优化思路）留空供后续填写。当用户提到低表现素材同步、插入优化行、sheet sync、筛选低表现素材，或需要在 Google Sheets 中准备优化行时触发此 skill。
---

# 低表现文案定位

当用户希望 Claude Code 从 KPI 数据中筛选低表现广告素材，并在 Google Sheets 中插入空白、格式正确的优化行时，使用此 skill。

此 skill **不**生成优化文案，也不填写内容字段。它只创建行结构（含格式、验证和合并对齐），以便后续流程或手动填写内容。

此 skill 的脚本路径相对于本 SKILL.md 所在目录；执行 `scripts/...` 之前请先 `cd` 到本 skill 目录。

## 优先阅读

**每次任务开始时必须先读取：**
- `README.md` — 用户配置文件，包含前置检测规则、字段名映射、查询日期、筛选规则等个性化设置。读取后：
  1. 先按「前置检测」章节逐项检查 chrome-launcher-with-userdata、KPI 登陆态、app-id-map.json，任意一项未就绪则停止并引导用户完成，全部通过后再继续。**特别注意**：app-id-map.json 为空时，Agent 无需等待用户指令，直接自动解析 KPI 后台所有有权限的 app 并写入文件，完成后继续。三项检测全部通过后，向用户询问「本次任务的 App name 是什么」，展示已有选项，用户确认后再执行筛选
  2. **字段名映射** 由 `sync_low_assets.py` 启动时自动解析（`apply_user_field_aliases_from_readme`），无需 Agent 介入
  3. 其它配置块（数据查询、登记字段、筛选规则、写入行为、其他偏好等）当前**仅供 Agent 阅读参考**，脚本未自动读取——Agent 据此调整调用参数（如 `--query-window`）或运行决策

存在时优先读取：
- 最新的 `optimization_task*.md`（仅用于提取表格 URL、应用名、工作表、查询窗口）

内置辅助脚本：
- `scripts/align_sheet_format.py`
- `scripts/capture_kpi_cookie_cdp.py` — 从已登录的 Chrome 导出 KPI session JSON 的辅助脚本
- `scripts/list_visible_sheets.py` — 查询两个锚点之间的**可见** sheet 列表（自动过滤隐藏 sheet）
- `scripts/detect_unknown_headers.py` — 启动 sync 前的**强制前置**：检测目标 sheet 表头与已知字段别名的对应关系，输出哪些"用途"无匹配、哪些表头未识别（详见工作流程 1.7）

主运行脚本：
- `scripts/sync_low_assets.py` — 此 skill 唯一的用户入口

## 工作流程

0. **前置检测 + 询问 App name**
   - 按 README「前置检测」章节完成三项检测（chrome-launcher、KPI 登陆态、app-id-map.json）
   - 若 app-id-map.json 为空：**自动**解析 KPI 后台所有有权限的 app 并写入文件，无需等待用户指令
   - 三项检测全部通过后，向用户询问本次任务的 App name（展示 app-id-map.json 中已有选项）
   - 用户确认 App name 后继续
1. 如有 task 文件，先读取（仅提取表格 URL、应用名、工作表、查询窗口、app_id）。
1.5. **确定目标工作表列表（可见 sheet 优先）**
   - 用户若指定两个锚点 sheet（如「从 A 到 B 之间的所有可见 sheet」），必须用 `scripts/list_visible_sheets.py` 获取范围内的可见 sheet 列表：
     ```bash
     python3 scripts/list_visible_sheets.py \
       --sheet-url "SHEETS_URL" \
       --from-sheet "锚点A" \
       --to-sheet "锚点B"
     ```
   - 脚本通过 Google Sheets API 查询每个 sheet 的 `hidden` 属性，只返回未隐藏的 tab（按 tab 顺序，含两端锚点）。
   - **不得**用 gspread 的 `worksheets()` 方法替代，该方法返回全部 sheet 含隐藏 sheet。
   - **用户说「所有 sheet」但未给锚点时**：`list_visible_sheets.py` 必须提供 `--from-sheet` / `--to-sheet`，无法直接调用。此时改用内联 Python 脚本通过 Sheets API 枚举所有可见 tab：
     ```python
     import os, json
     from pathlib import Path
     from google.oauth2.credentials import Credentials
     from googleapiclient.discovery import build

     SHEET_ID = "SPREADSHEET_ID"
     cred_candidates = [
         Path.home() / ".codex/credentials/google-workspace/authorized_user.json",
         Path.home() / ".claude/credentials/google-workspace/authorized_user.json",
     ]
     cred_file = next(p for p in cred_candidates if p.exists())
     creds = Credentials.from_authorized_user_file(str(cred_file))
     service = build("sheets", "v4", credentials=creds)
     meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
     visible = [s["properties"]["title"] for s in meta["sheets"]
                if not s["properties"].get("hidden", False)]
     print(json.dumps(visible))
     ```
     获取完整列表后，再按命名规则（如 `ad_group_id-lang` 格式）过滤出目标 sheet。
   - 若用户明确指定了完整的 sheet 名列表（非范围描述），可直接使用，无需调用此脚本。
1.7. **表头识别与询问（启动 sync 前的强制前置）**
   - 拿到目标 sheet 列表后，**必须**先跑：
     ```bash
     python3 scripts/detect_unknown_headers.py \
       --sheet-url "SHEETS_URL" \
       sheet1 sheet2 ...
     ```
   - 解读输出 JSON 的 `consolidated` 块：
     - `has_required_field_gap: true` → 分两种情况处理：
       - 若某 sheet 报告 `"error": "Missing optimization section header"`（表格尚无优化区域，属正常空白表）：**仅跳过该 sheet**，继续处理其余 sheet，不终止整个任务
       - 若某 sheet 有优化区域但缺少必填字段（`asset` / `strategy`）：**停止整个任务**并报告（脚本本身在 sync 阶段也会拒绝继续）
     - `needs_user_input: true` → 同时存在「未匹配的"用途"」与「无法归类的表头」，需要让用户做映射决策
     - 否则（即使 `unmapped_keys_any_sheet` 非空）：未匹配的都是可选字段（如 `rank` / `ctr`），表头里也确实没有对应列，直接继续即可
   - 当需要询问用户时（`needs_user_input: true`）：
     - 对每个 `unmapped_keys_any_sheet` 中的 key，配合 `unknown_in_any_sheet` 的候选表头，向用户提问：
       > "你的表里 `{用途中文名}` 对应哪一列？候选：{未识别表头列表}（或回答"无此列"跳过）"
     - 收到答复后，用 Edit 工具把映射写进 `README.md` 的 `## 字段名映射` 表对应行的"你的表格标题"列
     - 重新跑一次 `detect_unknown_headers.py` 确认 `needs_user_input: false` 后再继续
   - 已写进 README 的映射在 `sync_low_assets.py` 启动时由 `apply_user_field_aliases_from_readme()` 自动加载，无需额外参数
2. 通过 `scripts/sync_low_assets.py` 执行任务。KPI 数据通过以下两种路径之一流入，不引入第二个 KPI 获取入口：
   - **主路径**：用 `scripts/capture_kpi_cookie_cdp.py` 从已登录的 Chrome 导出 session JSON，然后传 `--kpi-session-file` 给 `sync_low_assets.py`，由脚本内部自动请求 KPI API。
   - **备用路径（Chrome MCP 可用时）**：用 Chrome MCP 预取 KPI JSON 文件，传 `--kpi-data-dir` 给 `sync_low_assets.py`。
3. 获取 KPI 数据：

   **主路径 — session file（推荐，Chrome MCP 不可用时也适用）：**
   a. 确认 Chrome 已通过 Chrome Launcher skill 启动并获得 `cdp_url`。
   b. 运行 `scripts/capture_kpi_cookie_cdp.py --cdp-url {cdp_url} --session-json-out {session_file}`。
      - 脚本检测到三个必需 cookie（`_csrf-backend`、`PHPSESSID`、`_identity-backend`）即立刻导出，无需等待业务请求。
      - 状态为 `cookies_ready_but_unverified` 属正常，不阻塞后续流程。
      - 状态为 `logged_out` 表示用户尚未登录，需在 Chrome 中完成登录后重试。
   c. 将 session 文件路径传给 `sync_low_assets.py --kpi-session-file {session_file}`，脚本将自动请求 KPI API（`index-ajax`）。
   d. 验证 KPI 响应 `data` 为 JSON list；若为 HTML、`{"code":...,"msg":...}` 或其他非标准格式，停止并报告错误。

   **备用路径 — kpi-data-dir（仅 Chrome MCP 可用时）：**
   a. 提取所有目标工作表标题中的 `ad_group_id`（`-` 前的数字前缀）。
   b. 用 Chrome MCP 导航到 KPI API URL，读取 JSON 并保存为 `{kpi_data_dir}/{ad_group_id}.json`。
   c. 验证 payload（`data` 必须为 JSON list），通过后传 `--kpi-data-dir {kpi_data_dir}` 给 `sync_low_assets.py`。

4. 对每个目标工作表：
   - 从预取的 KPI 数据中加载文字素材
   - 仅检查排名末三位的文字素材
   - 仅筛选 `Low` 或 `Learning` 表现的行
   - 若末三位均不符合条件，跳过该工作表
5. 从标题行动态检测优化区域；摘要区域视为可选。
5.5. **语言检测与翻译预处理（新增）**
   - 当目标 sheet 列表中**存在非英语/非中文** tab 时，先用 `--dry-run` 拿到低表现素材清单（不写表）：
     ```bash
     python3 scripts/sync_low_assets.py --dry-run ...其它参数 sheet1 sheet2 ...
     ```
     dry-run 报告每个候选的 `asset` / `mode`（`no_history` / `with_history` / `with_history_skipped_duplicate`）和 `needs_translation` 标志。
   - 从每个目标工作表标题提取语言后缀（ad_group_id `-` 后的部分，如 `ar`、`fr`、`es`、`西班牙语`、`阿拉伯语` 等中文标签也支持）
   - 若后缀**不属于**英语（`en`、`us`、`gb`、`英语` 等）也**不属于**中文（`zh`、`cn`、`tw`、`hk`、`中文`），视为需翻译语言
   - **只对 `mode=no_history` 的素材翻译**（它们的 `原方案` 行需要写翻译）；`mode=with_history` 的素材不需要翻译，对应空白优化行的 `翻译` 留空
   - 若 dry-run 结果中所有候选均为 `with_history`，跳过翻译步骤，直接正式运行（不传 `--translations-json`）
   - 若存在 `no_history` 素材，将其原文逐条翻译为英语，整理为 JSON，通过 `--translations-json '{"原文": "English translation", ...}'` 传给 `sync_low_assets.py` 正式运行
6. 按以下安全插入规则插入行：
   - **有历史**（素材已存在于表格中）：在该素材最后一次出现位置的正下方插入一个空白行
   - **无历史**（表格中未找到该素材）：在优化区域底部追加 `原方案` 行（含原始素材文本）+ 一个空白优化行
   - 优先复用已有空白行，再插入新行
   - 不留新的空隙
7. 字段写入规则（**有历史** / 无历史的**空白优化行**）：
   - `优化轮次`：有历史时为下一轮次编号，无历史时为 `一轮优化`
   - `Performance`：`进行中`
   - `Cost排序` / `消耗排名`：KPI 排名（两种表头名称，指同一字段）
   - `Ctr`：KPI CTR
   - `数据周期`：今日日期
   - `Asset type`：从参考行继承
8. 内容字段留空（**有历史** / 无历史的**空白优化行**均留空）：
   - `Asset`（新文案）
   - `翻译`
   - `优化思路`
   - `字符数`
8.5. **无历史的 `原方案` 行**字段规则（与空白优化行严格区分）：
   - **写入**：`Asset type`（从参考行继承）、`优化轮次`（固定为 `原方案`）、`Asset`（原始素材文本）、`翻译`（非英/中语言时的英文翻译）
   - **留空**：`Performance`、`Cost排序` / `消耗排名`、`Ctr`、`数据周期`、`优化思路`、`字符数`
   - 不得从格式参考行继承这些字段的值（防止旧 `原方案` 行中已有的 `Low` 表现或优化思路内容被带入新行）
9. 保留格式与验证：
   - 从最近的匹配参考行复制行格式
   - 随格式一并复制数据验证
   - 保留 `Performance` 验证
   - 保留 `Cost排序` / `消耗排名` 相邻验证列
   - 保持 `Asset type` 格式与相邻行对齐
   - 仅在摘要区域存在时复制摘要行格式
   - 若 `Asset type` 单元格已合并且优化行该列为空，向上追溯到块标题再判断参考行是否匹配
   - 若无历史插入时无法解析有效的优化风格参考行，停止而非写入空白风格行
   - **格式回退**：若优化区域内找不到与目标 `asset_type`（如 Description）相同类型的参考行，脚本自动回退到优化标题行上方的原始数据区扫描同类型行借用格式，避免借用错误类型（如用 Headline 格式给 Description 行）
   - 若可选字段缺失，每遇到新的缺失字段组合提示用户一次，本次任务内复用该答复
10. 每次写入后验证：
    - 原始素材仍存在（历史行数未减少）
    - 无历史情况：`原方案` 行包含正确的原始素材文本
    - 未引入意外空白行
11. 仅在摘要区域存在且有可写摘要字段时，追加一行摘要行。
12. 报告内容：
    - 已优化的工作表
    - 每个工作表的插入行位置
    - 跳过的工作表及原因
    - 使用的精确日期范围
    - 格式对齐辅助脚本是否运行
    - KPI 数据状态 / 错误类型（payload 不可用时）

## 规则

- 不以固定行号作为主要定位策略。
- 始终以素材文本内容为锚点。
- 将应用名、表格 URL、工作表名、日期窗口视为每次运行的输入参数。
- 不假定固定应用或固定表格。
- 若查询窗口为 `近30日`，计算方式：
  - start = 今日 - 30 天
  - end = 今日 - 1 天
- 最终报告中使用精确日期。
- 若素材语言非英语且非中文，仅在 `原方案` 行（`mode=no_history`）中填写原素材的英语翻译（`翻译` 字段）；有历史的空白优化行（`mode=with_history`）和无历史的空白优化行中 `翻译` 均留空。
- 优化区域必须包含 `Asset` 和 `优化思路`；任一缺失则停止整个任务。
- `优化轮次` 为可选项，不得因其缺失而阻塞任务。
- 摘要区域缺失时静默跳过。
- 内容字段不由此 skill 填写。

## 实现说明

- 优先使用此 skill 的 `scripts/sync_low_assets.py` 作为主运行脚本。
- 优先使用此 skill 的 `scripts/align_sheet_format.py` 进行写入后格式对齐。
- **多段优化区域**：部分 sheet 有多轮优化，每轮各有自己的标题行（同时包含 `asset_type`、`round`、`perf` 字段）。`detect_sheet_layout` 始终选取**行号最大（最后出现）的标题行**作为优化区域边界，新的优化行插入到最后一段优化区域底部。摘要区域的检测在最后一个优化标题行之后进行，不会将第二段（或更后的）优化标题行误判为摘要。
- `scripts/capture_kpi_cookie_cdp.py` 仅作为从已登录 Chrome 导出 KPI session JSON 的辅助脚本，不是第二个任务入口。
- `capture_kpi_cookie_cdp.py` 在 `_csrf-backend`、`PHPSESSID`、`_identity-backend` 三个 cookie 均存在时立即导出——**不需要**业务请求。导出后若有可用的 KPI 业务 URL，会尝试验证；`cookies_ready_but_unverified` 是无 KPI 页面访问时的正常结果，**不阻塞**工作流程。只有在必需 cookie 缺失（用户尚未登录）时，脚本才进入网络监听循环。
- `--query-window` 接受字符串值，不是数字。从 README 读取 `数据查看日期` 后，原样传给脚本：`--query-window "近30日"` / `--query-window "近7日"`。传数字（如 `30`）会报 `Unsupported query window` 错误。
- 非交互式运行（Agent 环境）必须加 `--auto-continue`，否则遇到可选字段缺失时脚本会等待 stdin 输入而挂起。
- 如需指定日期，设置 `CODEX_TODAY=YYYY-MM-DD` 使日期窗口与任务日一致。
- `app_id` 解析优先级：
  - `--app-id`
  - task md 中的 `- KPI app_id：...`
  - 显式指定的 `--app-map-file`
  - 此 skill 的 `references/app-id-map.json`
  - task 目录中的 `app_id_map.json`（兼容性后备）
- KPI 数据访问优先级：
  1. `--kpi-session-file` / `KPI_SESSION_FILE` / `KPI_COOKIE_HEADER` / `KPI_COOKIES_JSON` — **推荐**，由 `capture_kpi_cookie_cdp.py` 导出 session JSON 后传入；`sync_low_assets.py` 内部自动请求 `google/asset/index-ajax`，无需手动预取。请求时包含 `Accept: application/json, text/plain, */*` 和 `X-Requested-With: XMLHttpRequest`。`cookies_ready_but_unverified` 状态可直接使用，API 返回 `data` list 即视为隐式验证。
  2. `--kpi-data-dir` — 备用，仅当 Chrome MCP 可用时使用。需在外部预取每个 `{ad_group_id}.json` 并放入该目录，再传给 `sync_low_assets.py`。
- Google Workspace 凭证解析优先级：
  - 显式指定的 `--credentials` / `--authorized-user`
  - `GOOGLE_WORKSPACE_CREDENTIALS` / `GOOGLE_WORKSPACE_TOKEN`
  - `$CODEX_HOME/credentials/google-workspace/...`
  - `~/.codex/credentials/google-workspace/...`
  - `~/.claude/credentials/google-workspace/...`（兼容性后备）

## 何时停止并询问

以下情况停止并询问：
- 浏览器重定向到 Google 登录页（用户需登录一次后重试）
- 目标工作表的 KPI 数据文件缺失
- KPI API 返回业务错误 payload、无效 JSON、HTML 或 `data` 不为 list 的任何 payload
- session 捕获报告 `logged_out`（用户尚未登录 KPI）
- `detect_unknown_headers.py` 报告 `has_required_field_gap: true`（必填字段 `asset` / `strategy` 在某 sheet 无匹配，**停止整个任务**）
- `detect_unknown_headers.py` 报告 `needs_user_input: true`（询问用户表头映射并写入 README 后重跑 detect 确认，再继续 sync）
- KPI 素材无法安全匹配到表格历史
- 写入操作会覆盖或删除已有历史
- 请求的任务与之前已修正的表格状态冲突

## 典型用户提示

`帮我筛选低表现素材并在在线表格中插入空白优化行`

有 task 文件时：

`帮我查看 /.../广告素材优化 中的 task 文件，筛选低表现素材并插入空白行`

## 标准三步串联命令

```bash
# Step 1：通过 Chrome Launcher skill 获取 cdp_url（示意，实际由 skill 输出）
CDP_URL="http://localhost:11897"

# Step 2：捕获 KPI session（--session-json-out 同时作为复用缓存，文件存在且 Cookie 有效时自动跳过 CDP）
python3 scripts/capture_kpi_cookie_cdp.py \
  --cdp-url "$CDP_URL" \
  --session-json-out /path/to/kpi_session.json \
  --summary-out /path/to/kpi_summary.json

# Step 3：筛选低表现素材并插入优化行（sync_low_assets.py 内部自动请求 KPI）
# 默认只输出摘要（每个 sheet 一行），完整 JSON 报告写入 output/low-asset-sheet-sync/sync_report.json
python3 scripts/sync_low_assets.py \
  --sheet-url "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit" \
  --app-name "AppName" \
  --kpi-session-file /path/to/kpi_session.json \
  --query-window "近30日" \
  --auto-continue \
  "AD_GROUP_ID-lang"

# 调试时加 --verbose 可打印完整 JSON 报告到终端
python3 scripts/sync_low_assets.py ... --verbose

# Plan-only：列出每个 sheet 的候选素材和 mode（with_history / no_history /
# with_history_skipped_duplicate），并标注是否 needs_translation；不写入表格。
# 处理非英语/非中文 sheet 时**必须先跑 dry-run**，据此翻译后再用
# --translations-json 正式运行（见工作流程 5.5）。
python3 scripts/sync_low_assets.py ... --dry-run
```

说明：
- Step 2 的 `--session-json-out` 路径即为缓存路径，重复运行时若文件存在且 Cookie 有效，自动跳过 CDP 捕获直接复用（不受文件写入日期限制）。路径由调用方指定，脚本本身不硬编码任何默认路径。
- Step 3 使用 `--app-name` 自动从 `references/app-id-map.json` 解析 app_id，无需手动指定 `--app-id`。
- 不需要中间的手动 KPI 预取步骤（`--kpi-data-dir` 模式）。

## 按范围查询可见 sheet（list_visible_sheets.py）

用于在两个锚点之间自动获取可见 sheet 列表（过滤隐藏 tab）：

```bash
# JSON 格式（默认）
python3 scripts/list_visible_sheets.py \
  --sheet-url "https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit" \
  --from-sheet "168790594724-en" \
  --to-sheet "158509137482-en"

# Shell 参数格式（可直接展开到命令行）
python3 scripts/list_visible_sheets.py \
  --sheet-url "SHEETS_URL" \
  --from-sheet "A-en" \
  --to-sheet "B-en" \
  --format args
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `--sheet-url` | 是 | Google Sheets URL 或 Spreadsheet ID |
| `--from-sheet` | 是 | 起始锚点 sheet 名（含） |
| `--to-sheet` | 是 | 结束锚点 sheet 名（含） |
| `--format` | 否 | `json`（默认）或 `args`（shell 引号格式，可直接展开） |
| `--credentials` | 否 | OAuth credentials.json 路径 |
| `--authorized-user` | 否 | authorized_user.json 路径 |

**注意**：两个锚点的顺序（from/to）可互换，脚本会自动按 tab 视觉顺序截取范围。
