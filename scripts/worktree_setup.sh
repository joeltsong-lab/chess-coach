#!/usr/bin/env bash
# =============================================================================
# worktree_setup.sh —— 多分支并行工作区（git worktree）
#
#   scripts/worktree_setup.sh init                     建 endgame / mobile 两个工作区
#   scripts/worktree_setup.sh list                     列出所有工作区与分支、是否有改动
#   scripts/worktree_setup.sh remove <endgame|mobile|路径> [--force]
#
# 工作区固定放在主仓库的上一级：
#   ../chess-coach-endgame   -> feature/endgame
#   ../chess-coach-mobile    -> feature/mobile
# 主仓库自己留 main（这也是 run.sh 里 stable 的目录）。
#
# 为什么需要 worktree:
#   一个工作区只能检出一个分支，而「稳定版 + 残局版同时跑」需要两份代码同时存在。
#   worktree 让两个分支共享同一个 .git，来回切换不必反复 stash/checkout。
#
# 注意:
#   * 同一个分支不能同时出现在两个工作区里 —— 要建 endgame 工作区，主仓库就得先回到
#     main；本脚本遇到这种情况会直接说明原因，不会偷偷动你的工作区。
#   * remove 只移除工作区，**不会删分支**；删分支用 branch_manager.sh delete。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  printf '\033[1;31m[错误]\033[0m 本脚本必须放在 git 仓库的 scripts/ 下\n' >&2
  exit 1
fi

_common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir)"
case "$_common" in
  /*|[A-Za-z]:/*) : ;;
  *) _common="$REPO_ROOT/$_common" ;;
esac
MAIN_REPO="$(cd "$_common/.." && pwd)"
MAIN_PARENT="$(dirname "$MAIN_REPO")"

WT_ENDGAME="$MAIN_PARENT/chess-coach-endgame"
WT_MOBILE="$MAIN_PARENT/chess-coach-mobile"

bold() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[中止]\033[0m %s\n' "$*" >&2; exit 1; }

git_() { git -C "$REPO_ROOT" "$@"; }

usage() {
  cat <<'EOF'
chess-coach worktree 管理

  worktree_setup.sh init      建 ../chess-coach-endgame(feature/endgame)
                              与 ../chess-coach-mobile(feature/mobile)
  worktree_setup.sh list      列出工作区 / 分支 / 是否有未提交改动
  worktree_setup.sh remove <endgame|mobile|路径> [--force]

  --force 仅用于 remove：工作区还有未提交改动时强制移除。
EOF
}

# endgame / mobile / feature/xxx 之类的别名换成路径
resolve_target() {
  case "$1" in
    endgame|feature/endgame) printf '%s' "$WT_ENDGAME" ;;
    mobile|feature/mobile)   printf '%s' "$WT_MOBILE" ;;
    *)                       printf '%s' "$1" ;;
  esac
}

add_worktree() {
  local path="$1" branch="$2" out
  if [ -e "$path" ]; then
    if git -C "$path" rev-parse --git-dir >/dev/null 2>&1; then
      warn "$path 已存在且可用，跳过"
      return 0
    fi
    die "$path 已存在但不是本仓库的工作区，请先自行处理（不覆盖你的目录）"
  fi

  git_ show-ref --verify --quiet "refs/heads/$branch" || die "本地没有分支 $branch，先建好再跑 init"

  if ! out="$(git_ worktree add "$path" "$branch" 2>&1)"; then
    case "$out" in
      *"already checked out"*|*"already used by worktree"*)
        die "$branch 已经在别的工作区里检出了（一个分支只能在一个工作区）。
        先把那个工作区切走，例如: git -C \"$MAIN_REPO\" switch main" ;;
    esac
    die "git worktree add 失败: $out"
  fi
  ok "已建工作区: $path（$branch）"
}

cmd_init() {
  bold "建立多分支工作区"
  info "主仓库: $MAIN_REPO（保留 main，也是 scripts/run.sh stable 的目录）"
  add_worktree "$WT_ENDGAME" "feature/endgame"
  add_worktree "$WT_MOBILE"  "feature/mobile"
  cmd_list
  info "启动全部: scripts/run.sh all"
}

cmd_list() {
  bold "工作区 / 分支"
  local line path="" br dirty
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) path="${line#worktree }" ;;
      "branch refs/heads/"*)
        br="${line#branch refs/heads/}"
        if [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]; then
          dirty="有未提交改动"
        else
          dirty="干净"
        fi
        printf '  %-18s %-8s %s\n' "$br" "$dirty" "$path"
        ;;
      "detached") printf '  %-18s %-8s %s\n' "(脱离 HEAD)" "-" "$path" ;;
    esac
  done < <(git_ worktree list --porcelain)
  printf '\n'
  git_ worktree list
}

cmd_remove() {
  local arg="" force=0 path
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=1 ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（可用 --force）" ;;
      *) if [ -z "$arg" ]; then arg="$1"; else die "只接受一个目标"; fi ;;
    esac
    shift
  done
  [ -n "$arg" ] || die "用法: worktree_setup.sh remove <endgame|mobile|路径> [--force]"

  path="$(resolve_target "$arg")"
  [ -e "$path" ] || die "找不到工作区: $path"
  [ "$path" = "$MAIN_REPO" ] && die "拒绝移除主仓库的工作区"

  if [ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]; then
    if [ "$force" -ne 1 ]; then
      warn "$path 里还有未提交改动:"
      git -C "$path" status --short | sed 's/^/        /' >&2
      die "确认不要了就加 --force 重跑；否则先提交或备份"
    fi
    warn "--force 移除 $path（里面的未提交改动会一起丢掉）"
  fi

  if [ "$force" -eq 1 ]; then
    git_ worktree remove --force "$path"
  else
    git_ worktree remove "$path"
  fi
  git_ worktree prune
  ok "已移除工作区: $path（分支还在，要删分支用 branch_manager.sh delete）"
}

# ---------------------------------------------------------------------------
main() {
  local action="${1:-}"
  [ -n "$action" ] || { usage; exit 1; }
  shift
  case "$action" in
    init)                 cmd_init "$@" ;;
    list)                 cmd_list "$@" ;;
    remove|rm)            cmd_remove "$@" ;;
    -h|--help|help)       usage ;;
    *)                    usage; die "未知子命令: $action" ;;
  esac
}

main "$@"