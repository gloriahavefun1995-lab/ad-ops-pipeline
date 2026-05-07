# 优化文案表现登记 — 用户配置文件

**功能说明：** 这个 skill 用于登记上一轮优化的表现数据。

> **使用说明（给用户）：** 这个 skill 用于登记上一轮优化的表现数据。使用前需要提供：1. 在线表格链接；2. App name；3. Sheet 范围。如果有缺失，skill 会引导你一步步提供。

这个 `README.md` 不是普通说明书，而是这个 skill 的**配置入口**。

开始使用前，Agent 应先读取这份文件，和用户确认里面的配置；确认后的结果会直接影响：

- 默认登记哪些字段
- 去 KPI 看哪个日期口径
- 默认筛选哪个状态标记
- 默认报告输出到哪里
- 表格里各字段实际叫什么表头

如果脚本或 skill 里原来写死了这些参数，应该优先改为读取本文件，再由本轮用户输入覆盖。

---

## 首次使用前必须完成的四项准备

### 1. `chrome-launcher-with-userdata` 已安装且可调用

用途：启动带登录态的 Chrome，用来准备 KPI 登录态。

如果还没装：

1. 在 SkillMan 搜索并安装 `chrome-launcher-with-userdata`
2. 点击「链接到 Codex」
3. 完成后重新触发当前任务

首次复制 Chrome User Data 时，需要先关闭所有 Chrome 窗口。

### 2. KPI 登录态已经准备好

默认检查文件：

- `<skill目录>/kpi_session.json`

如果文件不存在，或你不确定登录态是否还有效，让 Agent 先帮你准备：

```text
先帮我启动 chrome 并准备 KPI 登录态
```

标准流程是：

1. 调用 `/chrome-launcher-with-userdata` 启动 Chrome
2. 手动登录 `https://kpi.drojian.dev`
3. 运行 `scripts/capture_kpi_cookie_cdp.py`
4. 生成或更新 `<skill目录>/kpi_session.json`

脚本输出 `ok` 或 `cookies_ready_but_unverified` 都可以继续使用；如果输出 `logged_out`，说明还需要先登录 KPI。

### 3. `references/app-id-map.json` 已有你账号可用的 app 映射

这个文件就是这里说的 `app_id` 文档。后续只要说 `app name`，skill 就能优先从这里解析 `app_id`。

如果还没准备好，直接对 Agent 说：

```text
帮我把 KPI 后台我有权限的所有 app id 解析出来并更新到 references/app-id-map.json
```

它会调用 `scripts/refresh_app_id_map.py`，从 KPI 后台拉取当前账号有权限的 app 列表并写回本地映射文件。

### 4. Google 认证文件路径已配置

检测方式：读取本文件 `### Google 认证` 中的 `google-credentials` 值，判断是否还是 `<待配置>`。

如果还是 `<待配置>`：

1. 在用户本地搜索 `client_secret*.json`（优先查 ~/Desktop、~/Downloads、~/Documents）
2. 将找到的路径展示给用户确认
3. 用户确认后，将该路径写入本文件的 `google-credentials:` 配置项，替换 `<待配置>`

如果已配置：直接继续。

---

## 每次开始前 Agent 必须先确认

每次真正执行同步前，Agent 至少先问这两项：

1. `需要登记的字段`
2. `去 KPI 查看数据的日期选择`

同时建议一起确认：

3. `目标 app`
4. `sheet URL`
5. `处理范围`
6. `执行阶段`：`dry-run` 或 `apply`
7. `用于识别定位的字段`：`待确认` / `优化中` / 其他（可补充）

这些确认结果如果是稳定偏好，应写回本文件的配置区，供后续复用。

---

## 默认配置

下面这部分就是脚本应读取的默认配置。除非本轮用户明确给了新值，否则优先使用这里。

### 数据查询

数据查看日期: 近30天

可选值：

- `近7天`
- `近14天`
- `近30天`
- `本月`
- `上月`

### 同步参数

状态标记: 进行中
状态标记可选值:
- 进行中
- 待确认
- 其他（由用户本轮输入指定）

说明：状态标记用于在表格中识别和定位需要登记数据的素材行。每次任务开始时必须向用户确认本轮要登记哪个状态下的文案表现，不应直接沿用上次的值。

默认报告路径: ./output/kpi_bulk_sync_report.json

### Google 认证

以下路径每次构造 `sync_kpi_bulk.py` 命令时必须同时传入，无需向用户确认：

google-credentials: <待配置>
google-token: ~/.claude/credentials/google-workspace/authorized_user.json

说明：credentials 含 `"installed"` key，脚本要求同时提供 token；只传其中一个会报 `Missing Google credentials`。

### 需要登记的字段

登记字段:
- Performance: 是
- cost排序: 是
- 数据周期: 是
- Best/Good率: 是
- Ctr: 否
- Status: 否
- Cost趋势: 否

说明：

- 这里勾选的是“默认写入字段”
- 如果本轮用户回答了新的字段列表，应以本轮回答为准
- 脚本应将这里的结果转成 `--write-fields`

### 字段显示名

下面记录的是用户确认过的字段显示名。Agent 向用户输出时使用这里的名称；传给脚本时自动翻译回内部名称。

Performance 显示名: 表现数据
数据周期 显示名: 数据周期
附加字段:

说明：

- `Performance 显示名` 同时作为表格列标题的候选别名，写入下方「字段名映射」的 Performance 条目
- `数据周期 显示名` 同时作为表格列标题的候选别名，写入下方「字段名映射」的 数据周期 条目
- `附加字段` 为逗号分隔列表，追加到 `--write-fields`；留空表示不追加

### 字段名映射

下面配置的是你表格里的真实表头名。左侧是脚本内部字段名，右侧是你表格里可能出现的标题，多个候选用逗号分隔。

Asset: Asset
Status: Status
Asset Type: Asset Type
Performance: Performance, 表现数据
优化轮次: 优化轮次
cost排序: cost排序, Cost排序, 消耗排名
消耗排名: 消耗排名, cost排序, Cost排序
Cost趋势: Cost趋势
Ctr: Ctr, CTR, 点击率
数据周期: 数据周期
Best/Good率: Best/Good率, Best/Good
Ad Group ID: Ad Group ID

如果你的表格字段名和默认值不一样，就改这里，而不是去改脚本里的硬编码。

---

## 快速开始

推荐直接用对话触发，不需要自己手拼命令。

### 单张工作表

```text
请用 优化文案表现登记 同步这个表。
目标 app：PDF Reader2
sheet URL：https://docs.google.com/spreadsheets/d/xxx/edit
处理范围：worksheet=165723563697-en
需要登记的字段：Performance、cost排序、数据周期、Best/Good率
去 KPI 查看数据的日期选择：近30天
执行阶段：dry-run
```

### 批量广告组工作表

```text
请用 优化文案表现登记 批量同步这个表里所有广告组工作表。
目标 app：All Reader2
sheet URL：https://docs.google.com/spreadsheets/d/xxx/edit
需要登记的字段：Performance、cost排序
去 KPI 查看数据的日期选择：近14天
执行阶段：dry-run
```

### 指定可见范围

```text
请用 优化文案表现登记 处理从 168790594724-en 到 158509137482-en 之间的可见广告组工作表。
目标 app：Pilates
sheet URL：https://docs.google.com/spreadsheets/d/xxx/edit
需要登记的字段：Performance、Best/Good率
去 KPI 查看数据的日期选择：本月
执行阶段：dry-run
```

---

## 如何更新 app_id 文档

推荐直接对 Agent 说：

```text
帮我把 KPI 后台我有权限的所有 app id 解析出来并更新到 references/app-id-map.json
```

底层对应的脚本入口是：

```bash
python3 scripts/refresh_app_id_map.py \
  --kpi-session-json ./kpi_session.json
```

也可以改用：

- `--kpi-cookie`
- `--kpi-cookie-file`

脚本会覆盖写入 `references/app-id-map.json`，并输出本次更新了多少个 app。

---

## 常见问题与失败排查

### 1. 提示缺少 `chrome-launcher-with-userdata`

说明当前环境还没有安装这个 skill，先完成安装并链接到 Codex，再重新触发任务。

### 2. 提示缺少 KPI 登录态

说明没有可用的 `kpi_session.json`，或当前登录态已失效。先启动 Chrome，登录 KPI，再重新抓取 session。

### 3. 提示 `app-id-map.json` 为空或找不到

说明还没同步本账号可用 app 列表。先运行 app-id map 刷新流程。

> 如果你是从别人那里复制来的这个 skill，`app-id-map.json` 是**故意留空**的——原作者的 app 数据已被清除，你需要用自己的 KPI 登录态重新生成。

### 4. 提示 `Unsupported date preset`

说明你输入的日期预设不在支持列表内。当前只支持：

- `近7天`
- `近14天`
- `近30天`
- `本月`
- `上月`

### 5. 表里有字段缺失

这个 skill 默认不会因为单个可选字段缺失就终止整张表，而是先提示：

```text
有【字段1、字段2】缺失，是否继续。
```

你可以决定继续还是停止。本轮同一种缺失字段组合只会确认一次。

---

## 维护说明

如果你是维护者，这份文件的职责是：

1. 记录用户确认过的默认配置
2. 作为脚本读取配置的来源
3. 让 skill 在正式执行前有一个稳定的“配置阶段”

如果要新增可配置项，优先先加到这里，再改脚本去读取；不要先把新值写死到脚本里。
