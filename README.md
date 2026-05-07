# ad-ops-pipeline

投放素材文案优化流水线 Claude Code plugin。打包了 5 个相关 skill，覆盖从 Chrome 登录态准备、KPI 后台数据登记、低表现素材筛选、新文案创意，到广告组语言补充的完整闭环。

## 包含的 Skill

| 阶段 | Skill | 干什么 |
|---|---|---|
| 0. 基建 | `chrome-launcher-with-userdata` | 起一个保留登录态的 Chrome（CDP 端口），供后续 skill 抓 KPI 后台数据 |
| 1. 拉数 + 登记 | `优化文案表现登记` | 从 KPI 后台批量拉广告组素材指标，写进 Google Sheets |
| 2. 筛低表现 | `低表现文案定位` | 在表里挑出低表现素材，插入待填写的优化行 |
| 3. 写新文案 | `ad-creative` | 提供平台规格 + 写作参考，辅助生成新文案 |
| 4. 广告组补充 | `优化组筛选补充` | 从本地 Excel 筛选 top 语言广告组，补缺失语言，同步回 Google Sheets |

5 个 skill 全部按 description 自动触发——你直接用自然语言描述意图（"帮我把 KPI 后台某广告组数据同步到表格"），Claude Code 会自动选中合适的 skill；也可以手动用 `/ad-ops-pipeline:<skill-name>` 命名空间形式调用。

## 安装

### 方式 A：本地试用（不持久安装）

```bash
claude --plugin-dir <本仓库本地路径>
```

进入会话后输入 `/help` 应能看到 5 个 skill 都列在 `ad-ops-pipeline:` 命名空间下。

### 方式 B：通过 GitHub marketplace 持久安装（推荐团队场景）

仓库推到 GitHub 后，团队成员一次性执行：

```
/plugin marketplace add <github-owner>/<repo-name>
/plugin install ad-ops-pipeline@zhaobo-ad-tools
```

之后无论是 CLI、Desktop app、IDE 扩展都能用。`/plugin marketplace update` 可以拉取后续更新。

## 外部依赖（团队成员需自备）

| 依赖 | 用途 | 怎么准备 |
|---|---|---|
| **Google Workspace 凭证** | 读写 Google Sheets | 默认路径 `~/.claude/credentials/google-workspace/authorized_user.json`；或运行时通过 `--credentials <path>` 指定 |
| **KPI 后台访问权限 + Chrome 登录态** | 拉广告组素材数据 | 第一次运行时由 `chrome-launcher-with-userdata` 启动带 CDP 的 Chrome，手动登录一次后 cookies 持久化 |
| **Chrome 浏览器** | CDP 自动化 | macOS / Windows 自动检测安装路径，无需配置 |
| **Python 3 + 依赖** | 脚本运行环境 | `pip install gspread google-auth google-api-python-client google-auth-oauthlib openpyxl requests` |

## 本地开发

修改任意 SKILL.md / 脚本后，在已加载本 plugin 的 Claude Code 会话里 `/reload-plugins` 即可热更，不必重启。

## 内部约定

- SKILL.md 里所有 `scripts/...` 路径都相对于该 SKILL.md 所在目录；脚本调用前 Claude 会先 `cd` 到 skill 目录。
- `${CLAUDE_PLUGIN_ROOT}` 仅在 hook / MCP / LSP / monitor 配置的 `command` 字段做 shell 替换，**不**在 SKILL.md 正文里替换；本插件目前不依赖这一变量。
- 跨 skill 协作（如 `低表现文案定位` 需要 Chrome 登录态）通过模型按 description 自动调用同 plugin 内的另一 skill 完成，不需要硬编码命名空间。

## License

内部使用。
