#!/usr/bin/env bash
# 把 scripts/git-hooks/pre-commit 软链到 .git/hooks/pre-commit。
# 只需在每台机器克隆完仓库后跑一次。

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK_SRC="$PLUGIN_ROOT/scripts/git-hooks/pre-commit"
HOOK_DST="$PLUGIN_ROOT/.git/hooks/pre-commit"

if [[ ! -d "$PLUGIN_ROOT/.git" ]]; then
  echo "❌ 不是 git 仓库：$PLUGIN_ROOT" >&2
  exit 1
fi

if [[ ! -x "$HOOK_SRC" ]]; then
  chmod +x "$HOOK_SRC"
fi

mkdir -p "$PLUGIN_ROOT/.git/hooks"

if [[ -e "$HOOK_DST" || -L "$HOOK_DST" ]]; then
  echo "⚠ 已存在 pre-commit hook：$HOOK_DST"
  read -r -p "覆盖吗？[y/N] " yn
  case "$yn" in
    y|Y|yes|Yes) rm -f "$HOOK_DST" ;;
    *) echo "已取消。"; exit 1 ;;
  esac
fi

ln -s "$HOOK_SRC" "$HOOK_DST"
echo "✓ 已安装：$HOOK_DST → $HOOK_SRC"
echo "  下次 git commit 会检查 ~/.skill-man/skills 是否有新内容未同步。"
