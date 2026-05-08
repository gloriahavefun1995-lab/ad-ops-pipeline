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

### 方式 C：Codex / OpenCode（手动软链）

`/plugin marketplace` 是 Claude Code 专属机制，Codex / OpenCode 不识别，需要手动把 skills 软链到自家路径：

```bash
# 1. 克隆本仓库到任意位置
git clone https://github.com/gloriahavefun1995-lab/ad-ops-pipeline.git
cd ad-ops-pipeline

# 2. 软链 5 个 skill 到 Codex skills 目录
mkdir -p ~/.codex/skills
for s in ad-creative chrome-launcher-with-userdata 优化文案表现登记 优化组筛选补充 低表现文案定位; do
  ln -sf "$(pwd)/skills/$s" ~/.codex/skills/"$s"
done

# 3. 后续更新
git pull   # 仓库一更新，软链自动跟上，无需重装
```

OpenCode 用户把目标目录换成 `~/.config/opencode/skills/` 即可（OpenCode 也会扫描 `~/.codex/skills/` 和 `~/.claude/skills/`，所以装在任一路径下都能用）。

skill 脚本跨平台，凭证路径自动按 `$CODEX_HOME/credentials/google-workspace/...` → `~/.codex/credentials/google-workspace/...` → `~/.claude/credentials/google-workspace/...` 顺序回退，无需改脚本。

## 外部依赖（团队成员需自备）

| 依赖 | 用途 | 怎么准备 |
|---|---|---|
| **Google Workspace 凭证** | 读写 Google Sheets | 默认路径 `~/.claude/credentials/google-workspace/authorized_user.json`；或运行时通过 `--credentials <path>` 指定 |
| **KPI 后台访问权限 + Chrome 登录态** | 拉广告组素材数据 | 第一次运行时由 `chrome-launcher-with-userdata` 启动带 CDP 的 Chrome，手动登录一次后 cookies 持久化 |
| **Chrome 浏览器** | CDP 自动化 | macOS / Windows 自动检测安装路径，无需配置 |
| **Python 3 + 依赖** | 脚本运行环境 | `pip install gspread google-auth google-api-python-client google-auth-oauthlib openpyxl requests` |

## 本地开发

修改任意 SKILL.md / 脚本后，在已加载本 plugin 的 Claude Code 会话里 `/reload-plugins` 即可热更，不必重启。

### 从 `~/.skill-man/skills/` 拉上游更新

本 plugin 的 5 个 skill 是从 `~/.skill-man/skills/<name>/` **复制**来的（不是软链），所以 skill-man 那边的更新**不会自动**进入本仓库。需要手动同步：

```bash
# 1. 预览将带过来的变更（永远先跑这个）
scripts/sync-from-skillman.sh --dry-run

# 2. 确认无误后正式同步（交互式确认）
scripts/sync-from-skillman.sh

# 3. 检查 git diff、commit、push
git diff
git add -A && git commit -m "Sync from skill-man" && git push
```

约定：

- skill-man 是**上游**，单向同步 skill-man → plugin。脚本不会删除 plugin-only 文件（如 `write_creative.py`、`refresh_app_id_map.py`），所以反复跑是安全的。
- 如果你**直接在 plugin 里**改了文件（像 commit 872a4bc 那次），要先把改动**手动倒灌回 skill-man**，否则下次同步会把 plugin 的新版盖掉。预览输出会清楚显示哪些 plugin 文件会被覆盖——看到不对就 abort。
- runtime 杂物（`output/`、`kpi_session.json`、`chrome_user_data/`、`__pycache__/` 等）已在脚本里 exclude，不会被搬过来。

#### Pre-commit 提醒（可选但推荐）

每台机器克隆完仓库后跑一次：

```bash
scripts/install-git-hooks.sh
```

之后每次 `git commit` 时，hook 会扫一遍 `~/.skill-man/skills/` 是否有比上次同步更新的文件，有就在 stderr 提醒——但不会阻塞 commit，只是告诉你"或许该先跑一下 sync"。`.last-skillman-sync` 是每台机器自己的时间戳文件，已在 `.gitignore` 里。

## 内部约定

- SKILL.md 里所有 `scripts/...` 路径都相对于该 SKILL.md 所在目录；脚本调用前 Claude 会先 `cd` 到 skill 目录。
- `${CLAUDE_PLUGIN_ROOT}` 仅在 hook / MCP / LSP / monitor 配置的 `command` 字段做 shell 替换，**不**在 SKILL.md 正文里替换；本插件目前不依赖这一变量。
- 跨 skill 协作（如 `低表现文案定位` 需要 Chrome 登录态）通过模型按 description 自动调用同 plugin 内的另一 skill 完成，不需要硬编码命名空间。

## License

内部使用。
