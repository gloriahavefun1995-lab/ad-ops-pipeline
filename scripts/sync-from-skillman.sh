#!/usr/bin/env bash
# Pull updates from ~/.skill-man/skills/<...>/ into this plugin's skills/.
#
# skill-man 是上游，本 plugin 是下游。本脚本只做单向 skill-man → plugin。
# 不会删除 plugin-only 文件（例如 write_creative.py、refresh_app_id_map.py
# 这些没在 skill-man 里出现的脚本），所以反复跑安全。
#
# 用法：
#   scripts/sync-from-skillman.sh             # 先 preview，再交互确认
#   scripts/sync-from-skillman.sh --yes       # 跳过确认（CI / 自动化）
#   scripts/sync-from-skillman.sh --dry-run   # 只 preview，永不写入

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SKILLMAN_ROOT="$HOME/.skill-man/skills"
TIMESTAMP_FILE="$PLUGIN_ROOT/.last-skillman-sync"

SKILLS=(
  "ad-creative"
  "chrome-launcher-with-userdata"
  "优化文案表现登记"
  "优化组筛选补充"
  "低表现文案定位"
)

EXCLUDES=(
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude='.DS_Store'
  --exclude='chrome_user_data'
  --exclude='kpi_session.json'
  --exclude='kpi_summary.json'
  --exclude='output'
  --exclude='evals/evals.json'
  --exclude='*.swp'
  --exclude='/优化组筛选补充'  # skill-man 那边有个自嵌套同名子目录，跳过
)

DRY_RUN=0
SKIP_PROMPT=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y) SKIP_PROMPT=1 ;;
    -h|--help)
      sed -n '2,/^$/{s/^# \{0,1\}//;p;}' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -d "$SKILLMAN_ROOT" ]]; then
  echo "❌ 找不到 skill-man 目录：$SKILLMAN_ROOT" >&2
  echo "   这台机器上没有 skill-man 副本，无法同步。" >&2
  exit 1
fi

echo "▶ skill-man → $SKILLMAN_ROOT"
echo "▶ plugin    → $PLUGIN_ROOT/skills"
echo

PREVIEW_HAS_CHANGES=0
PREVIEW_TMPFILES=()
trap 'rm -f "${PREVIEW_TMPFILES[@]}"' EXIT

for skill in "${SKILLS[@]}"; do
  src="$SKILLMAN_ROOT/$skill"
  dst="$PLUGIN_ROOT/skills/$skill"
  echo "── $skill ──"
  if [[ ! -d "$src" ]]; then
    echo "  ⚠ skill-man 没有这个 skill（plugin-only），跳过"
    echo
    continue
  fi
  if [[ ! -d "$dst" ]]; then
    echo "  ! plugin 这边目录缺失，会整体新建"
  fi
  tmp="$(mktemp)"
  PREVIEW_TMPFILES+=("$tmp")
  rsync -av --dry-run --itemize-changes "${EXCLUDES[@]}" "$src/" "$dst/" 2>/dev/null \
    | grep -E '^[<>ch.*][a-zA-Z.+]' \
    | grep -v -E '^\.[a-zA-Z]+\.{6,} \./' \
    > "$tmp" || true
  if [[ ! -s "$tmp" ]]; then
    echo "  ✓ 一致，无变更"
  else
    sed 's/^/  /' "$tmp"
    PREVIEW_HAS_CHANGES=1
  fi
  echo
done

if [[ $PREVIEW_HAS_CHANGES -eq 0 ]]; then
  echo "✓ 全部 skill 与 skill-man 一致，无事可做。"
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$TIMESTAMP_FILE"
  exit 0
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "(--dry-run，未写入)"
  exit 0
fi

echo "提示符说明：>f 新文件 / 改文件； cd 新目录； .f 仅元信息变化。"
echo "skill-man → plugin 单向同步，不会删除 plugin-only 文件。"
echo

if [[ $SKIP_PROMPT -eq 0 ]]; then
  read -r -p "继续吗？[y/N] " yn
  case "$yn" in
    y|Y|yes|Yes) ;;
    *) echo "已取消。"; exit 1 ;;
  esac
fi

for skill in "${SKILLS[@]}"; do
  src="$SKILLMAN_ROOT/$skill"
  dst="$PLUGIN_ROOT/skills/$skill"
  [[ -d "$src" ]] || continue
  mkdir -p "$dst"
  rsync -a "${EXCLUDES[@]}" "$src/" "$dst/"
done

date -u +"%Y-%m-%dT%H:%M:%SZ" > "$TIMESTAMP_FILE"
echo
echo "✓ 同步完成；时间戳写入 $TIMESTAMP_FILE"
echo "下一步：git diff 检查变更，再 commit & push。"
