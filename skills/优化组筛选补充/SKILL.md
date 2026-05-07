---
name: 优化组筛选补充
description: |
  从本地 ad group export Excel 中筛选前20条里的 top 语言广告组，识别缺失语言，并仅从第21条数据之后按顺序补充每个缺失语言的首条命中组。用户确认后再同步到 Google Sheets，并分类汇报新建、取消隐藏、保持不动和隐藏结果。用户提到“优化组筛选”“筛选补充广告组”“ad group筛选”“优化组补充”“筛选top广告组”“广告组同步”“ad group同步”“筛选语言广告组”“top语言广告组”“补充缺失语言”“广告组导出筛选”时必须使用本 skill。
---

# 优化组筛选补充

从本地 ad group export 文件中筛选 top 语言广告组，补缺失语言，并同步到 Google Sheets。

> 本 skill 中所有 `scripts/...` 路径都相对于本 SKILL.md 所在目录；运行脚本前请先 `cd` 到本 skill 目录。

默认原则：

- 只处理本轮用户提供的 Excel、top 语言和 Google Sheets
- 任务开始先说明 skill 功能，并先检查 3 项必需输入是否齐全
- 先分析、再确认、最后同步
- 只保留前20条中的 top 语言组
- 对缺失语言，只取第21条数据之后按顺序命中的首条广告组
- 最终名单只能来自 `top_selected + supplements(found=true)`

## 必要输入

每次执行至少确认这三项；用户消息里已经给出时直接使用：

1. `Google Sheets 链接`
2. `本地 Excel 文件路径`（获取方式：① 前往新中台，选择目标 App 和时间范围，导出全部数据到本地；② 按住 Option，两指点击文件，即可看到「将 xx 拷贝为路径名称」）
3. `top 语言列表`

缺少输入时，用 AskUserQuestion 分三条独立问题依次询问，每项单独一格，不要合并成一个问题框。

Excel 列结构应包含：

```text
Ad Group Id | Campaign Id | Ad Group Name | Good&Best数量 | 总数量 | 占比
```

## 前置依赖

```bash
pip install openpyxl google-auth google-api-python-client google-auth-oauthlib
```

Google 凭证默认路径：

```text
~/.claude/credentials/google-workspace/authorized_user.json
```

若凭证不在默认位置，执行时通过 `--credentials <路径>` 指定。

## 执行流程

### Step 1：语言预处理与分析

#### Step 1a：补全 LANG_SPECS（运行脚本前必做）

**在运行脚本之前**，先用自身语言知识逐一分析用户输入的每种语言：

- 识别标准语言名称与 ISO 639-1 语言代码（如 马来语 → `ms`）
- 判断该语言是否已在 `scripts/analyze_excel.py` 的 `LANG_SPECS` 中
- 若不在：直接编辑脚本，按如下结构补充完整定义，再运行

```python
"ms": {
    "display_name": "马来语",
    "input_aliases": ["马来语", "马来文", "ms", "malay", "bahasa melayu", "bahasa malaysia", "bm"],
    "match_tokens": ["ms", "malay", "bahasa", "bm"],
},
```

**不允许**因为语言不在 `LANG_SPECS` 中就报"不支持"或跳过——只要 agent 能识别该语言，就必须先补进脚本再运行。

#### Step 1b：运行脚本

调用 `scripts/analyze_excel.py`：

```bash
python3 scripts/analyze_excel.py \
  --file "<Excel文件路径>" \
  --top-langs <语言列表，空格分隔>
```

脚本职责：

- 先扫描分析 `top 语言列表`，识别主语言，并忽略地区修饰信息
- 再归一化为标准语言键，支持中文、英文、语言代码和 `Language (Region)` 结构混合输入
- 输出本轮用于本地表格识别的 `confirmed_languages`
- 在本地表格中按 token 级匹配识别语言，短代码只允许独立 token 命中
- 扫描前20条数据，生成 `top_selected` 与 `top_excluded`
- 计算 `present_langs` / `missing_langs`
- 对每个缺失语言，只从第21条数据起按顺序取首条命中组，生成 `supplements`

若脚本输出仍有 `unknown_inputs`，说明 Step 1a 有遗漏，应补全后重跑，不得带着 unknown_inputs 进入 Step 2。

重点输出字段：

- `requested_top_languages`
- `confirmed_languages`
- `unknown_inputs`
- `top_selected`
- `top_excluded`
- `present_langs`
- `missing_langs`
- `supplements`

### Step 2：先给用户确认

先回显 `confirmed_languages`，明确说明这是“先扫描分析输入、只保留主语言后生成的本轮识别方案”，并说明本轮会按哪些 `match_tokens` 在本地表格中识别 top 语言。允许用户：

- 修改候选标记
- 删除不想纳入的标记
- 补充命名变体

建议先展示这张确认表：

| 用户输入 | 标准语言键 | 识别语言 | 候选识别标记 |
|----------|------------|----------|--------------|
| 葡萄牙语 | pt | 葡语 | pt, portuguese, pt_br, pt-br, ptbr, brazilian portuguese |
| English (India) | en | 英语 | en, english |

如果用户输入类似：

- `English (India)`
- `Portuguese (Brazil)`
- `Spanish (United States)`
- `Arabic (Egypt)`

不要机械地把它们当作未知输入；应先分析主语言，再生成候选识别标记给用户确认。
如果输入带地区，例如 `英语（英国）`、`English (India)`、`Spanish (United States)`，默认只识别主语言，不按地区拆分识别范围。

然后再汇报 4 组结果：

1. `前20条保留的 top 语言组`
2. `前20条剔除的非 top 语言组`
3. `top语言覆盖分析`
4. `缺失语言补充组`

缺失语言补充组表头建议：

| 语言 | 真实Excel行号 | 数据序号 | Ad Group Id | Ad Group Name | 占比 |
|------|----------------|----------|-------------|---------------|------|

确认时必须明确说明：

- `supplements` 的规则固定为：每个缺失语言只取前20条数据之后的首条命中
- 禁止按 `Good&Best数量`、`占比`、素材形式或命名偏好择优
- 对语言代码的搜索采用 token 级匹配，不按普通单词子串误判；例如 `Personal Plan` 不能因为包含 `pl` 就识别成波兰语

**用户未确认前，不要继续同步。**

### Step 3：整理最终名单

用户确认后，只能合并：

- `top_selected`
- `supplements` 中 `found: true` 的组

输出统一表格：

| # | Ad Group Id | Ad Group Name | Good&Best数量 | best&good率 |
|---|-------------|---------------|----------------|-------------|
| top保留组 1~ | ... | ... | ... | ... |
| 补充组 补1~ | ... | ... | ... | ... |

约束：

- 严格使用 analyze 脚本 JSON 输出
- 不包含 `top_excluded`
- 若最终名单出现不在 `top_selected + supplements(found=true)` 内的广告组，应直接报错

### Step 4：同步到 Google Sheets

调用 `scripts/update_sheets.py`：

```bash
python3 scripts/update_sheets.py \
  --sheet-url "<Google Sheets URL>" \
  --groups '<JSON数组>' \
  [--credentials "<凭证文件路径>"]
```

`groups` 参数必须严格来自 `top_selected + supplements(found=true)`，每条格式为 `{"id": "<ad_group_id>", "lang_key": "<语言键>"}`。

脚本对比 sheet 名称中的 ad group id，执行：

- 不存在：新建 sheet，命名为 `{id}-{lang_key}`
- 存在且可见：保持不动
- 存在但隐藏：取消隐藏
- 可见但不在本次收集组中：隐藏

只针对名称中含 10 位以上数字 ID 的广告组 sheet 执行隐藏，不影响说明性 sheet。

### Step 5：汇报结果

最终按 4 类表格汇报：

1. `保持不动`
2. `取消隐藏`
3. `新建 Sheet`
4. `已隐藏`

## 禁区

- 不要在用户确认前直接同步
- 不要在脚本输出之外手工追加广告组
- 不要把后面表现更好的同语种组替换成补充组
- 不要混用真实 Excel 行号和数据序号
- 不要扩大到用户未指定的其他文件或其他 sheet

## 汇报要求

最终回复至少包含：

- 本次处理的 Excel 文件
- 本次确认后的 top 语言 / 标准语言键
- 当前阶段：分析确认 / 已同步
- 成功项摘要
- 跳过项或未处理原因
- 若已同步，按 `保持不动 / 取消隐藏 / 新建 / 已隐藏` 分类汇报

## 流水线衔接（含未优化组人工 gate）

完成 `update_sheets.py` 同步、Step 5 四类汇报输出后，**主流程完成 ≠ 会话终止**，按以下顺序继续：

1. 检查"新建 Sheet"那一栏的数量 **N**——这就是"之前没做过素材优化的广告组"数。
2. **如果 N > 0**（hook 2，人工 gate）：
   - 把所有新建的 sheet tab 名（带 ad group ID）以列表形式打印给用户。
   - 告知："这 N 个广告组之前没做过素材优化，sheet tab 已经建好但内容是空的。请打开 sheet 手工填入素材数据（标题 / 描述 / 跑量参数等），填好后回复'已填好'或'done'我再继续。"
   - **停下等用户回复**，不要主动跑下一个 skill，也不要假设用户已经填好。
3. **如果 N == 0**：跳过 hook 2，直接进入下一步。
4. 用户回复"已填好"/"done" 或 N==0 后（hook 3）：询问"是否继续调用 `低表现文案定位` 来挑出本次同步进 sheet 的低表现素材，准备改写？"，等用户**明确**确认后再调用同 plugin 内的 `低表现文案定位` skill（按 description 自动匹配，**不要**硬编码 `/<plugin>:<skill>` 命名空间形式）。把当前 Sheet URL + 用户填好的 tab 范围作为上下文传过去。
5. 若用户回复"暂停 / 不用 / 停"，则只汇报本步结果，等待新指令。

