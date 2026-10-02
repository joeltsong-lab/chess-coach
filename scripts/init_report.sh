#!/usr/bin/env bash
# =============================================================================
# init_report.sh —— 生成 scripts/init_report.txt
#
# 内容: 当前分支 / 最新 commit / 各分支一览 / 与 main 的新增-修改-删除文件数 /
#       未跟踪文件 / 被忽略的运行产物 / 暂存目录处理结果 / 远程状态 / 源目录保护
#
# 用法:
#   scripts/init_report.sh
#   可选环境变量（给了才校验，用于证明导入没有改动源目录）:
#     STABLE_DIR=... ENDGAME_DIR=... scripts/init_report.sh
#
# 报告是本地运行产物，已在 .gitignore 里（scripts/init_report.txt），不会进版本库。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  printf '\033[1;31m[错误]\033[0m 本脚本必须放在 git 仓库的 scripts/ 下\n' >&2
  exit 1
fi

MAIN_BRANCH="main"
BRANCH_ENDGAME="feature/endgame"
BRANCH_MOBILE="feature/mobile"

STABLE_DIR="${STABLE_DIR:-}"
ENDGAME_DIR="${ENDGAME_DIR:-}"

OUT="$REPO_ROOT/scripts/init_report.txt"
mkdir -p "$REPO_ROOT/scripts"

git_() { git -C "$REPO_ROOT" "$@"; }

# 可用的 sha256 命令名（含参数）；$(sha_cmd) 会被当作命令名用，不能直接执行它
sha_cmd() {
  if command -v sha256sum >/dev/null 2>&1; then printf 'sha256sum'; else printf 'shasum -a 256'; fi
}

# 目录内容摘要（跳过 __pycache__），用来证明源目录没被动过
digest_dir() {
  find "$1" -type f -not -path '*/__pycache__/*' -not -name '*.pyc' -not -name '*.pyo' \
       -exec $(sha_cmd) {} + | LC_ALL=C sort -k2 | $(sha_cmd) | awk '{print $1}'
}

# 统计 A/M/D/R 数量
count_status() {
  local base="$1" branch="$2" kind="$3"
  git_ diff --name-status "$base...$branch" 2>/dev/null \
    | awk -v k="$kind" 'NF { if (k=="R") { if ($1 ~ /^R/) n++ } else if ($1==k) n++ } END { print n+0 }'
}

# 输出 base...branch 的差异块
diff_block() {
  local base="$1" branch="$2" ns
  printf '\n  与 %s 的差异: %s...%s\n' "$base" "$base" "$branch"
  if ! git_ rev-parse --verify --quiet "refs/heads/$branch" >/dev/null; then
    printf '    （分支 %s 不存在，跳过）\n' "$branch"
    return 0
  fi
  printf '    新增 %s / 修改 %s / 删除 %s / 重命名 %s\n' \
    "$(count_status "$base" "$branch" A)" "$(count_status "$base" "$branch" M)" \
    "$(count_status "$base" "$branch" D)" "$(count_status "$base" "$branch" R)"
  ns="$(git_ diff --name-status "$base...$branch" 2>/dev/null || true)"
  if [ -z "$ns" ]; then
    printf '    （无差异）\n'
  else
    printf '%s\n' "$ns" | sed 's/^/      /'
  fi
}

# 运行产物: 存在就报大小，不存在就报缺失（不擅自加进 .gitignore，只提示）
artifact_line() {
  local f="$1" n
  if [ -f "$REPO_ROOT/$f" ]; then
    n="$(wc -c <"$REPO_ROOT/$f" 2>/dev/null || echo 0)"
    printf '  %-26s 存在   %s MB\n' "$f" "$(awk -v n="$n" 'BEGIN { printf "%.2f", n/1048576 }')"
  else
    printf '  %-26s 缺失\n' "$f"
  fi
}

_common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir)"
case "$_common" in
  /*|[A-Za-z]:/*) : ;;
  *) _common="$REPO_ROOT/$_common" ;;
esac
MAIN_REPO="$(cd "$_common/.." && pwd)"

{
  echo "chess-coach 初始化报告"
  echo "================================================================================"
  echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "仓库目录: $REPO_ROOT"
  echo "主仓库:   $MAIN_REPO"
  echo "git:      $(git --version)"
  echo
  echo "[1] 当前分支与最新提交"
  echo "  当前分支: $(git_ symbolic-ref --quiet --short HEAD 2>/dev/null || echo '(游离 HEAD)')"
  echo "  最新提交: $(git_ log -1 --pretty='%h %s')"
  echo "  提交时间: $(git_ log -1 --pretty='%ci')"
  echo "  作者:     $(git_ log -1 --pretty='%an <%ae>')"
  echo
  echo "[2] 分支一览（分支 / 最新提交 / 上游）"
  while IFS= read -r b; do
    hb="$(git_ log -1 --pretty='%h %s' "$b" 2>/dev/null || true)"
    up="$(git_ for-each-ref --format='%(upstream:short)' "refs/heads/$b" 2>/dev/null || true)"
    if [ -n "$up" ]; then
      lr="$(git_ rev-list --left-right --count "$up...$b" 2>/dev/null | awk '{ printf "落后 %s / 领先 %s", $1, $2 }' || true)"
      extra="   上游 $up（$lr）"
    else
      extra="   上游 无"
    fi
    printf '  %-18s %s%s\n' "$b" "$hb" "$extra"
  done < <(git_ for-each-ref --format='%(refname:short)' refs/heads/)
  echo
  echo "[3] 与 $MAIN_BRANCH 的文件差异（main 是唯一稳定线）"
  diff_block "$MAIN_BRANCH" "$BRANCH_ENDGAME"
  diff_block "$MAIN_BRANCH" "$BRANCH_MOBILE"
  echo
  echo "[4] 未跟踪文件（不含 .gitignore 忽略的项）"
  untracked="$(git_ status --porcelain --untracked-files=all 2>/dev/null | awk '$1=="??"{ print substr($0, 4) }' || true)"
  if [ -z "$untracked" ]; then
    echo "  （无）"
  else
    printf '%s\n' "$untracked" | sed 's/^/  /'
  fi
  echo
  echo "[5] 运行产物（按约定不入库，只在本机存在；没有自动加进 .gitignore）"
  artifact_line "engine/Pikafish.exe"
  artifact_line "engine/pikafish.nnue"
  artifact_line "data/chess.db"
  echo "  如需随仓库分发引擎/权重，自行删除 .gitignore 末节对应行再提交。"
  echo
  echo "[6] 暂存目录处理结果"
  for d in stable .staging-endgame .staging-stable; do
    if [ -e "$REPO_ROOT/$d" ]; then
      echo "  $d/ 仍存在 —— 需要人工确认"
    else
      echo "  $d/ 不存在（导入时用的暂存内容已提升到仓库根并清理）"
    fi
  done
  echo
  echo "[7] 远程状态"
  if [ -n "$(git_ remote)" ]; then
    git_ remote -v | sed 's/^/  /'
    git_ branch -vv | sed 's/^/  /'
  else
    echo "  未配置远程（git remote -v 为空）"
    echo "  要推送: git remote add origin <url> && git push -u origin main feature/endgame feature/mobile"
  fi
  echo
  echo "[8] 源目录保护（导入过程只读源目录，不删除、不覆盖）"
  if [ -n "$STABLE_DIR" ] && [ -d "$STABLE_DIR" ]; then
    echo "  稳定版源目录: $STABLE_DIR"
    echo "    内容摘要(sha256): $(digest_dir "$STABLE_DIR")"
  else
    echo "  稳定版源目录: 未提供（设 STABLE_DIR=... 可一并记录摘要）"
  fi
  if [ -n "$ENDGAME_DIR" ] && [ -d "$ENDGAME_DIR" ]; then
    echo "  残局版源目录: $ENDGAME_DIR"
    echo "    内容摘要(sha256): $(digest_dir "$ENDGAME_DIR")"
  else
    echo "  残局版源目录: 未提供（设 ENDGAME_DIR=... 可一并记录摘要）"
  fi
  echo
  echo "[9] 下一步"
  echo "  git log --oneline --all --graph"
  echo "  scripts/branch_manager.sh list"
  echo "  scripts/branch_manager.sh diff main feature/endgame"
  echo "  scripts/worktree_setup.sh init && scripts/run.sh all"
  echo "  scripts/run.sh stop"
} > "$OUT"

echo "报告已生成: $OUT"