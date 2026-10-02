#!/usr/bin/env bash
# =============================================================================
# branch_manager.sh —— chess-coach 分支管理
#
# 子命令:
#   list                                    列出本地/远程分支
#   status                                  当前分支、上游、领先/落后、工作区
#   switch <分支> [--stash|--abort]         切换分支（有未提交改动会先问怎么处理）
#   new <新分支> [--from main] [--stash]    从 main（或指定基线）派生并切过去
#   diff [base] [branch] [--name-only|--name-status]   默认 main...HEAD 的统计
#   sync [--confirm] <目标分支> [来源=main]  把来源合并到目标（默认只允许 main -> 特性分支）
#   delete <分支> [--force] [--remote]      删分支；未合并的必须 --force，且要再输入一次分支名
#
# 模型约定:
#   * main 是唯一长期稳定线；特性分支（feature/*）都从 main 派生；
#   * 特性分支之间不互相合并；
#   * 反向同步（任何以 main 为目标的合并）必须显式加 --confirm。
#
# 安全约定:
#   任何会动工作区的操作，先检查 git status；有未提交改动 -> 提示 stash / 手动提交 / 中止，
#   绝不静默覆盖。（非交互环境下读不到输入时一律按“中止”处理。）
#
# 用法示例:
#   scripts/branch_manager.sh list
#   scripts/branch_manager.sh switch feature/endgame
#   scripts/branch_manager.sh new feature/foo --from main
#   scripts/branch_manager.sh diff main feature/endgame
#   scripts/branch_manager.sh sync feature/endgame
#   scripts/branch_manager.sh sync --confirm main feature/endgame
#   scripts/branch_manager.sh delete feature/foo --force
# =============================================================================
set -euo pipefail

MAIN_BRANCH="main"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  printf '\033[1;31m[错误]\033[0m 本脚本必须放在 git 仓库的 scripts/ 下（当前找不到仓库）\n' >&2
  exit 1
fi

bold() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
sub()  { printf '  \033[1;34m->\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[中止]\033[0m %s\n' "$*" >&2; exit 1; }

git_() { git -C "$REPO_ROOT" "$@"; }

# 交互读取：优先当前终端，其次 /dev/tty；都读不到就返回空串（调用方按中止处理）
ask() {
  local prompt="$1" ans=""
  if [ -t 0 ]; then
    read -r -p "$prompt" ans || true
  elif [ -r /dev/tty ]; then
    read -r -p "$prompt" ans < /dev/tty || true
  else
    printf '(非交互环境，读不到输入，按中止处理)\n' >&2
  fi
  printf '%s' "$ans"
}

current_branch() { git_ symbolic-ref --quiet --short HEAD 2>/dev/null || printf 'HEAD(游离)'; }
is_dirty()       { [ -n "$(git_ status --porcelain)" ]; }
branch_exists()  { git_ show-ref --verify --quiet "refs/heads/$1"; }
remote_exists()  { git_ show-ref --verify --quiet "refs/remotes/origin/$1"; }

# 找出某个分支所在的 worktree 路径（没有就返回非 0）
worktree_for_branch() {
  local want="$1" line path=""
  while IFS= read -r line; do
    case "$line" in
      "worktree "*) path="${line#worktree }" ;;
      "branch refs/heads/"*)
        if [ "${line#branch refs/heads/}" = "$want" ]; then printf '%s' "$path"; return 0; fi ;;
    esac
  done < <(git_ worktree list --porcelain)
  return 1
}

usage() {
  cat <<'EOF'
chess-coach 分支管理（main = 唯一稳定线）

  branch_manager.sh list                                    列出分支
  branch_manager.sh status                                  当前状态（分支/上游/领先落后/工作区）
  branch_manager.sh switch <分支> [--stash|--abort]          切换分支
  branch_manager.sh new <新分支> [--from main] [--stash]     派生新特性分支
  branch_manager.sh diff [base] [branch] [--name-only|--name-status]
                                                            默认 main...HEAD 的统计
  branch_manager.sh sync [--confirm] <目标> [来源=main]      合并来源到目标
  branch_manager.sh delete <分支> [--force] [--remote]       删除分支

约定:
  * sync 默认只允许 main -> 特性分支；任何以 main 为目标的合并都要加 --confirm；
  * switch/new/sync 遇到未提交改动会提示 [s]tash / [c]手动提交后重跑 / [a]中止；
  * delete 删未合并分支必须 --force，并且还要再输入一次分支名确认。
EOF
}

# 工作区有未提交改动时的统一处理；返回 0 表示可以继续
require_clean() {
  local action="$1" mode="${2:-ask}"
  is_dirty || return 0

  printf '\n\033[1;33m[注意]\033[0m %s：工作区有未提交改动\n' "$action" >&2
  git_ status --short | sed 's/^/        /' >&2

  case "$mode" in
    stash) do_stash "$action"; return 0 ;;
    abort) die "已按 --abort 中止（工作区原样未动）" ;;
  esac

  printf '\n  怎么处理？\n' >&2
  printf '    s) 先 git stash 存起来（之后 git stash pop 恢复），继续本次操作\n' >&2
  printf '    c) 我手动提交/撤销，本次先中止\n' >&2
  printf '    a) 直接中止\n' >&2
  local ans
  ans="$(ask '  输入 s / c / a: ')"
  case "$ans" in
    s|S) do_stash "$action" ;;
    c|C) die "已中止：请先提交或撤销改动，再重跑本命令" ;;
    *)   die "已中止（未做任何改动）" ;;
  esac
}

do_stash() {
  local tag="branch_manager.sh: $1 @ $(date '+%Y-%m-%d %H:%M:%S')"
  git_ stash push -u -m "$tag" >/dev/null
  ok "已 stash：$tag（git stash list / git stash pop 恢复）"
}

# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
cmd_list() {
  bold "仓库: $REPO_ROOT"
  bold "本地分支（* 为当前）"
  git_ branch -vv
  if [ -n "$(git_ remote)" ]; then
    bold "远程分支"
    git_ branch -r
  else
    bold "远程分支"
    info "未配置远程（git remote -v 为空）"
  fi
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
cmd_status() {
  bold "仓库: $REPO_ROOT"
  info "当前分支: $(current_branch)"
  info "HEAD: $(git_ log -1 --pretty='%h %s')"

  local up
  up="$(git_ rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
  if [ -n "$up" ]; then
    info "上游: $up"
    local counts
    counts="$(git_ rev-list --left-right --count "$up...HEAD" 2>/dev/null || printf '? ?')"
    info "落后/领先: $(printf '%s' "$counts" | awk '{printf "%s / %s", $1, $2}')"
  else
    info "上游: 无（未推送或未设置跟踪）"
  fi

  if [ -n "$(git_ stash list)" ]; then
    info "stash: $(git_ stash list | wc -l | tr -d ' ') 条"
  fi

  bold "工作区"
  if is_dirty; then git_ status --short; else ok "干净"; fi

  if [ -n "$(git_ worktree list | tail -n +2)" ]; then
    bold "worktree"
    git_ worktree list
  fi
}

# ---------------------------------------------------------------------------
# switch
# ---------------------------------------------------------------------------
cmd_switch() {
  local pos1="" mode="ask"
  while [ $# -gt 0 ]; do
    case "$1" in
      --stash) mode="stash" ;;
      --abort) mode="abort" ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（可用 --stash / --abort）" ;;
      *) if [ -z "$pos1" ]; then pos1="$1"; else die "只接受一个分支名"; fi ;;
    esac
    shift
  done
  [ -n "$pos1" ] || die "用法: branch_manager.sh switch <分支> [--stash|--abort]"

  local cur; cur="$(current_branch)"
  [ "$pos1" = "$cur" ] && { ok "已经在 $pos1 上"; return 0; }

  local from_remote=0
  if branch_exists "$pos1"; then
    :
  elif remote_exists "$pos1"; then
    from_remote=1
  else
    die "本地和 origin 都没有分支 $pos1（branch_manager.sh list 看看有哪些）"
  fi

  require_clean "切换分支前" "$mode"

  if [ "$from_remote" -eq 1 ]; then
    warn "本地没有 $pos1，从 origin/$pos1 建跟踪分支"
    git_ switch -c "$pos1" --track "origin/$pos1"
  else
    git_ switch "$pos1"
  fi
  ok "已切换: $cur -> $(current_branch)"
}

# ---------------------------------------------------------------------------
# new
# ---------------------------------------------------------------------------
cmd_new() {
  local pos1="" base="$MAIN_BRANCH" mode="ask"
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) base="${2:-}"; shift || die "--from 需要跟一个分支名" ;;
      --stash) mode="stash" ;;
      --abort) mode="abort" ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（可用 --from <基线> / --stash）" ;;
      *) if [ -z "$pos1" ]; then pos1="$1"; else die "只接受一个新分支名"; fi ;;
    esac
    shift
  done
  [ -n "$pos1" ] || die "用法: branch_manager.sh new <新分支> [--from main] [--stash]"

  branch_exists "$pos1" && die "分支 $pos1 已存在（用 switch 切过去）"
  branch_exists "$base" || die "基线分支 $base 不存在"

  if [ "$base" != "$MAIN_BRANCH" ]; then
    warn "基线不是 $MAIN_BRANCH 而是 $base；模型约定特性分支只从 $MAIN_BRANCH 派生"
  fi

  require_clean "新建分支前" "$mode"
  git_ switch -c "$pos1" "$base"
  ok "已从 $base 创建并切到 $pos1"
}

# ---------------------------------------------------------------------------
# diff —— 默认看 main...当前分支
# ---------------------------------------------------------------------------
cmd_diff() {
  local fmt="--stat" pos1="" pos2=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --stat|--name-only|--name-status) fmt="$1" ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（可用 --stat / --name-only / --name-status）" ;;
      *) if [ -z "$pos1" ]; then pos1="$1"; elif [ -z "$pos2" ]; then pos2="$1"; else die "最多两个位置参数"; fi ;;
    esac
    shift
  done

  local base="${pos1:-$MAIN_BRANCH}"
  local branch="${pos2:-$(current_branch)}"
  [ "$base" = "$branch" ] && die "$base 和 $branch 是同一个分支，没有可比内容"

  bold "git diff $base...$branch $fmt"
  git_ diff "$fmt" "$base...$branch"
  printf '\n'
  printf '  新增: %s  修改: %s  删除: %s  重命名: %s\n' \
    "$(git_ diff --name-status "$base...$branch" | awk '$1=="A"' | wc -l | tr -d ' ')" \
    "$(git_ diff --name-status "$base...$branch" | awk '$1=="M"' | wc -l | tr -d ' ')" \
    "$(git_ diff --name-status "$base...$branch" | awk '$1=="D"' | wc -l | tr -d ' ')" \
    "$(git_ diff --name-status "$base...$branch" | awk '$1 ~ /^R/' | wc -l | tr -d ' ')"
}

# ---------------------------------------------------------------------------
# sync —— 默认只允许 main -> 特性分支
# ---------------------------------------------------------------------------
cmd_sync() {
  local confirm=0 pos1="" pos2=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --confirm) confirm=1 ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（反向同步请用 --confirm）" ;;
      *) if [ -z "$pos1" ]; then pos1="$1"; elif [ -z "$pos2" ]; then pos2="$1"; else die "最多两个位置参数"; fi ;;
    esac
    shift
  done

  local target="${pos1:-}" source="${pos2:-$MAIN_BRANCH}"
  [ -n "$target" ] || die "用法: branch_manager.sh sync <目标分支> [来源分支=$MAIN_BRANCH] [--confirm]"
  branch_exists "$target" || die "本地没有分支 $target"
  branch_exists "$source" || die "本地没有分支 $source"
  [ "$target" = "$source" ] && die "目标分支和来源分支不能相同"

  if [ "$source" != "$MAIN_BRANCH" ] || [ "$target" = "$MAIN_BRANCH" ]; then
    if [ "$confirm" -ne 1 ]; then
      die "默认只允许 $MAIN_BRANCH -> 特性分支（本次是 $source -> $target）。确实要做请加 --confirm"
    fi
    warn "反向/非主源同步：$source -> $target（已用 --confirm 确认）"
  fi

  # 优先在已有 worktree 里做，避免动当前工作区
  local run_dir="$REPO_ROOT" wt=""
  if wt="$(worktree_for_branch "$target")"; then
    [ "$wt" = "$REPO_ROOT" ] && wt=""
  fi
  if [ -n "$wt" ] && [ -d "$wt" ]; then
    run_dir="$wt"
    sub "在 worktree 里合并: $run_dir（当前工作区不动）"
    if [ -n "$(git -C "$run_dir" status --porcelain)" ]; then
      die "$run_dir 有未提交改动，请先处理（不静默覆盖）"
    fi
  else
    local cur; cur="$(current_branch)"
    if [ "$cur" != "$target" ]; then
      require_clean "同步 $target 前"
      sub "切换 $cur -> $target"
      if ! git_ switch "$target" 2>/dev/null; then
        if remote_exists "$target"; then
          git_ switch -c "$target" --track "origin/$target"
        else
          die "切换到 $target 失败，请手动检查"
        fi
      fi
    fi
  fi

  sub "git -C $run_dir merge $source"
  if ! GIT_MERGE_AUTOEDIT=no git -C "$run_dir" merge --no-edit "$source"; then
    warn "合并有冲突，已停在冲突状态（目录: $run_dir）"
    git -C "$run_dir" status --short | sed 's/^/        /' >&2
    die "解决冲突后: git -C \"$run_dir\" commit；想放弃: git -C \"$run_dir\" merge --abort"
  fi
  ok "$source 已合并进 $target: $(git -C "$run_dir" log -1 --pretty='%h %s')"
}

# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
cmd_delete() {
  local branch="" force=0 remote=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) force=1 ;;
      --remote) remote=1 ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（可用 --force / --remote）" ;;
      *) if [ -z "$branch" ]; then branch="$1"; else die "只接受一个分支名"; fi ;;
    esac
    shift
  done
  [ -n "$branch" ] || die "用法: branch_manager.sh delete <分支> [--force] [--remote]"

  [ "$branch" = "$MAIN_BRANCH" ] && die "拒绝删除稳定线 $MAIN_BRANCH"
  [ "$branch" = "$(current_branch)" ] && die "不能删除当前所在分支，先切到别的分支"
  branch_exists "$branch" || die "本地没有分支 $branch"

  local merged=0
  if git_ branch --merged "$MAIN_BRANCH" --format='%(refname:short)' | grep -Fxq "$branch"; then
    merged=1
  fi

  if [ "$merged" -eq 0 ]; then
    if [ "$force" -ne 1 ]; then
      die "分支 $branch 还没有合并进 $MAIN_BRANCH，删除会丢掉上面的提交。确实要删请加 --force"
    fi
    warn "分支 $branch 未合并进 $MAIN_BRANCH，上面可能有没保存的工作"
    local ans
    ans="$(ask "  二次确认：请输入分支名 $branch 以继续删除: ")"
    [ "$ans" = "$branch" ] || die "确认串不匹配，已取消（什么都没删）"
  fi

  local flag="-d"
  [ "$force" -eq 1 ] && flag="-D"
  git_ branch "$flag" "$branch"
  ok "已删除本地分支 $branch"

  if [ "$remote" -eq 1 ]; then
    if remote_exists "$branch"; then
      git_ push origin --delete "$branch"
      ok "已删除 origin/$branch"
    else
      warn "origin 上没有 $branch，跳过远程删除"
    fi
  fi
}

# ---------------------------------------------------------------------------
main() {
  [ "$#" -gt 0 ] || { usage; exit 1; }
  local cmd="$1"
  shift
  case "$cmd" in
    list)                 cmd_list "$@" ;;
    status)               cmd_status "$@" ;;
    switch)               cmd_switch "$@" ;;
    new)                  cmd_new "$@" ;;
    diff)                 cmd_diff "$@" ;;
    sync)                 cmd_sync "$@" ;;
    delete)               cmd_delete "$@" ;;
    -h|--help|help)       usage ;;
    *)                    usage; die "未知子命令: $cmd" ;;
  esac
}

main "$@"