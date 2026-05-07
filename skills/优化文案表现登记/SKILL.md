---
name: 优化文案表现登记
description: 将 KPI 后台广告组素材指标同步到 Google Sheets，支持单张工作表或批量同步多个广告组工作表。当用户提到 KPI后台、Ad Group、素材数据、近30天、在线表格、Google Sheets、Performance、cost排序、消耗排名、数据周期、Best/Good率、批量同步或所有广告组工作表时触发。
metadata:
  version: "1.5.0"
---

# 优化文案表现登记

这个 skill 用于登记上一轮优化的表现数据。当用户希望把 KPI 后台广告组文字素材的表现数据登记到 Google Sheets 时，使用此 skill。

此 skill 的目标不是生成新文案，而是安全同步 KPI 指标到已有表格结构中，包括但不限于：

- `Performance`
- `cost排序` / `消耗排名`
- `Ctr`
- `数据周期`
- `Best/Good率`

表格读写优先使用用户自带的 `google-workspace` skill（如已安装）。KPI 数据获取优先使用 HTTP / JSON 接口，只有在缺少登录态时才走 Chrome 登录态准备流程。

## 任务开始时必须输出

任务触发后，第一句话必须向用户输出：

> 这个skill用于登记上一轮优化的表现数据。

之后再进入后续流程。

## 优先阅读

**每次任务开始时必须先读取：**

- `README.md` — 用户配置文件，不只是说明文档

读取后按下面顺序执行：

1. 先完成 `README.md` 里的四项前置检测
2. 任意一项未就绪时，停止当前同步，转为引导用户补齐
3. 四项检测全部通过后，进入”配置阶段”
4. 配置阶段先和用户确认 README 里的默认字段、日期、状态标记、字段名映射是否需要调整
5. 将确认结果视为本轮真实配置；稳定偏好应写回 `README.md`
6. 执行阶段以“本轮用户回答”覆盖 README 默认值
7. 未明确要求 `apply` 时，默认先执行 `dry-run`

主运行脚本：

- `scripts/sync_kpi_bulk.py`

辅助脚本：

- `scripts/capture_kpi_cookie_cdp.py`
- `scripts/refresh_app_id_map.py`

参考文档：

- `references/field-rules.md`
- `references/bulk-sync.md`

## 前置检测（每次任务开始时自动执行）

### 检测一：`chrome-launcher-with-userdata` 是否可用

检测方式：

- 尝试调用 `/chrome-launcher-with-userdata`

如果不可用：

1. 停止当前同步
2. 告知用户需要先安装并链接 `chrome-launcher-with-userdata`
3. 引导用户到 SkillMan 安装后重新触发当前任务

### 检测二：KPI 登录态是否已准备好

检测方式：

- 检查 `<skill目录>/kpi_session.json` 是否存在
- 文件存在时优先复用
- 文件缺失或用户明确表示登录态不可用时，改走 Chrome 登录态准备链路

如果未准备好：

1. 停止当前同步
2. 调用 `/chrome-launcher-with-userdata`
3. 引导用户在 Chrome 中登录 `https://kpi.drojian.dev`
4. 运行：

```bash
python3 scripts/capture_kpi_cookie_cdp.py \
  --cdp-url "<CDP_URL>" \
  --session-json-out <skill目录>/kpi_session.json \
  --summary-out <skill目录>/kpi_summary.json
```

5. `ok` 或 `cookies_ready_but_unverified` 都视为可继续
6. 若输出 `logged_out`，提醒用户先完成 KPI 登录后重试

### 检测三：`references/app-id-map.json` 是否已配置

检测方式：

- 读取 `references/app-id-map.json`
- 文件不存在、为空文件或内容为空对象 `{}` 都视为未配置

如果未配置：

1. 停止当前同步
2. 告知用户需要先更新本地 `app_id` 文档
3. 引导用户执行：

```text
帮我把 KPI 后台我有权限的所有 app id 解析出来并更新到 references/app-id-map.json
```

4. 底层使用：

```bash
python3 scripts/refresh_app_id_map.py \
  --kpi-session-json <skill目录>/kpi_session.json
```

5. 完成后再回到当前同步任务

### 检测四：Google 认证文件路径是否已配置

检测方式：

- 读取 `README.md` 的 `### Google 认证` 中的 `google-credentials` 值
- 值为 `<待配置>` 则视为未配置

如果未配置：

1. 停止当前同步
2. 在用户本地搜索 `client_secret*.json`（优先查 ~/Desktop、~/Downloads、~/Documents）
3. 将找到的路径展示给用户确认
4. 用户确认后，将路径写入 `README.md` 的 `google-credentials:` 配置项，替换 `<待配置>`
5. 完成后继续后续流程

如果已配置：直接继续。

## 配置阶段职责

`README.md` 在这个 skill 里承担的是”配置文件”职责，而不是普通 readme。

Agent 在正式同步前必须先读取其中的配置区，并根据用户回答决定是否更新：

- `数据查看日期`
- `状态标记`
- `登记字段`
- `字段名映射`
- `字段显示名`
- `默认报告路径`

如果脚本里原本把这些值写死，应该优先改为读取 `README.md`，再由本轮用户输入覆盖。

### 字段显示名配置向导

四项前置检测通过后，在询问同步参数之前，先检查 README.md 的「字段显示名」配置区：

**已有配置时：**

直接输出当前配置让用户确认，例如：

> 当前字段配置：
> - 后台数据表现字段：**表现**（对应 Performance）
> - 数据周期字段：**优化时间**（对应 数据周期）
> - 附加字段：Best/Good率、cost排序
>
> 是否沿用以上配置，还是需要修改？

用户确认沿用则直接进入同步参数确认；用户要修改则触发下方向导。

**无配置或用户要修改时，逐题询问：**

**第 1 题（Q3）：** 你表格里用于表示后台数据表现的列标题是什么？
- 选项：`Performance` / `表现` / 用户自由输入

**第 2 题（Q8）：** 你表格里用于表示数据周期的列标题是什么？
- 选项：`数据周期` / `优化时间` / 用户自由输入

**第 3 题（Q9）：** 是否需要追加以下字段？（可多选，也可不选）
- `Best/Good率`
- `cost排序`
- `Cost趋势`

> 注：`优化组数` 暂不支持，脚本尚未实现该字段写入。

Q9 选了某字段后，继续逐一询问该字段在表格里的列标题是否需要自定义（默认与字段名相同）。

**配置结果的使用规则：**

1. Q3 / Q8 的用户输入同时承担两个作用：
   - **显示名**：所有面向用户的输出（报告、确认、进度）一律用用户输入的名称，不再用内部名称
   - **表头别名**：写入 README.md「字段名映射」对应字段的候选列表，脚本据此定位列
2. 传给脚本的 `--write-fields` 始终使用内部名称（`Performance`、`数据周期`），由 Agent 在构造命令时翻译，不透传用户输入
3. 每次向导结束后，将最终配置写回 README.md「字段显示名」区，供下次复用

## 正式同步前必须确认的参数

四项前置检测全部通过后，必须先向用户确认本轮真实参数。不要沿用旧任务值。

固定必问：

1. `需要登记的字段`
2. `KPI 日期选择`
3. `状态标记`：本轮要登记哪个状态下的文案表现？可选值：`进行中` / `待确认` / 其他（用户自填）

同时还要确认：

4. `目标 app`
5. `sheet URL`
6. `处理范围`
7. `执行阶段`

参数解释：

- `需要登记的字段`：先看 `README.md` 默认配置，再以本轮回答覆盖，最终对应 `--write-fields`
- `KPI 日期选择`：先看 `README.md` 默认配置，再以本轮回答覆盖，最终对应 `--date-preset`
- `状态标记`：每次必问，不沿用上次值；先展示 `README.md` 中的默认值供参考，但必须由用户本轮明确确认；最终对应 `--status-marker`；可选值：`进行中` / `待确认` / 其他
- `处理范围`：单 worksheet / 单 ad group / 多 ad group / 可见范围
- `执行阶段`：`dry-run` 或 `apply`

若用户只说“同步一下这个表”，仍然要先补齐以上关键信息，至少把：

- 字段
- 日期预设
- app
- 目标 sheet 范围

问清楚后再执行。

如果用户说“以后都按这个字段组合/这个日期口径来”，应同步更新 `README.md` 的配置区，而不是只在当前命令里临时传参。

## 当前脚本能力与默认值

`scripts/sync_kpi_bulk.py` 当前会先读取 `README.md` 配置，再用 CLI 或本轮用户输入覆盖。

如果 `README.md` 未配置，再回退到脚本内默认值。

脚本内兜底默认值：

- 默认状态标记：`进行中`
- 默认字段：`Performance`、`cost排序`、`数据周期`、`Best/Good率`
- 默认报告路径：`./output/kpi_bulk_sync_report.json`

当前支持的日期预设：

- `近7天`
- `近14天`
- `近30天`
- `本月`
- `上月`

不支持自由输入绝对日期范围；若用户提出绝对日期，本轮应明确说明当前脚本不支持，并建议先改配置方案或脚本再执行。

## 推荐执行流程

### 1. 先做 dry-run

默认先执行：

```bash
python3 scripts/sync_kpi_bulk.py \
  --sheet-url "<sheet-url>" \
  --app-name "<app-name>" \
  --kpi-session-json "<skill目录>/kpi_session.json" \
  --google-credentials "<README.md 中 google-credentials 路径>" \
  --google-token "<README.md 中 google-token 路径>" \
  --date-preset "<日期预设>" \
  --write-fields <字段列表> \
  --status-marker "<用户本轮确认的状态标记>"
```

按用户范围补充：

- `--worksheet`
- `--ad-group-id`
- `--ad-group-ids`
- `--start-worksheet`
- `--end-worksheet`

如果 CLI 未显式传入：

- `--date-preset` 从 `README.md` 读取
- `--write-fields` 从 `README.md` 读取
- `--report` 从 `README.md` 读取

注：`--status-marker` 必须来自用户本轮明确确认，不从 README.md 静默读取。

字段定位时优先使用 `README.md` 的“字段名映射”。

### 2. 检查 dry-run 结果

重点检查：

- 实际命中的工作表
- 实际绝对日期范围
- 实际写入字段
- 缺失字段
- 未匹配素材
- 跳过原因

### 3. 仅在用户明确要求后执行 apply

执行 `apply` 前确认：

1. 范围已经确认
2. 缺失字段已确认是否继续
3. 未匹配行已接受跳过

## 处理范围规则

### 单张命名 worksheet

当用户指定某个 worksheet，并且其中可能包含一个或多个 ad group 区块时：

- 必须同时提供 `--worksheet`
- 必须同时提供 `--ad-group-id` 或 `--ad-group-ids`

### 普通批量模式

当用户说“所有广告组工作表”或未指定 ad group 时：

- 枚举可见 sheet
- 仅保留名称中能识别出 ad group id 的 sheet

### 可见范围模式

当用户说“从 A 到 B 之间”时：

- 使用 `--start-worksheet` 和 `--end-worksheet`
- 在全部可见 sheet 中截取范围
- 再过滤出 ad group sheet 作为实际同步目标

## 字段与写入规则

- `Performance`：写入 KPI 文字素材状态
- `cost排序` / `消耗排名`：写入消耗排名链
- `Ctr`：写入 KPI CTR
- `数据周期`：写入本次同步使用的精确绝对日期范围，或按已有追加规则追加
- `Best/Good率`：仅根据当前 ad group 文字素材计算

规则保持不变：

- 缺少单个可选字段时，默认跳过该字段，不把整张表判为失败
- 对相同缺失字段组合，同次运行只确认一次
- 文本无法精确匹配时不猜测，不强写

## 报告要求

最终回复必须遵守工作区 AGENTS 规则，至少包含：

- 本次识别到的任务类型：`优化文案表现登记`
- 实际处理对象
- 实际绝对日期范围
- 当前阶段：`dry-run` 或 `apply`
- 成功 / 跳过 / 失败摘要
- 报告路径
- 未处理原因

写表类任务还要单列说明：

- 哪些是结构问题
- 哪些是业务跳过
- 如果只做了 `dry-run`，必须明确说明尚未写入

## 标准维护入口

### 更新 app-id map

```bash
python3 scripts/refresh_app_id_map.py \
  --kpi-session-json <skill目录>/kpi_session.json
```

### 准备 KPI 登录态

```bash
python3 scripts/capture_kpi_cookie_cdp.py \
  --cdp-url "<CDP_URL>" \
  --session-json-out <skill目录>/kpi_session.json \
  --summary-out <skill目录>/kpi_summary.json
```

### 主同步脚本

```bash
python3 scripts/sync_kpi_bulk.py \
  --sheet-url "<sheet-url>" \
  --app-name "<app-name>" \
  --kpi-session-json <skill目录>/kpi_session.json \
  --google-credentials "<README.md 中 google-credentials 路径>" \
  --google-token "<README.md 中 google-token 路径>" \
  --date-preset "<日期预设>" \
  --write-fields <字段列表>
```

## 流水线衔接

完成本 skill 主流程（`scripts/sync_kpi_bulk.py` 执行结束、汇报报告写出）后，**主流程完成 ≠ 会话终止**，按以下顺序继续：

1. 把这次产出的关键交接信息以一段简短列表打印给用户：
   - 操作的 Sheet URL
   - 处理对象（app + 广告组范围）
   - 实际处理 vs 跳过的对象数（来自 `kpi_bulk_sync_report.json`）
   - 当前阶段（dry-run 还是 apply）
2. 询问用户："本步已完成。是否继续调用 `优化组筛选补充` 来检查同一个 sheet 里有没有缺语言、需补充的广告组？"
3. 等用户**明确**回复"继续 / 好 / yes / 调用下一步"或同义词，再调用同 plugin 内的 `优化组筛选补充` skill（按 description 自动匹配，**不要**硬编码 `/<plugin>:<skill>` 命名空间形式）。把刚才的 Sheet URL + app 名作为上下文传过去。
4. 若用户回复"暂停 / 不用 / 停 / 我自己看看"或同义词，则只汇报本步结果，不主动调下一个 skill，等待新指令。

