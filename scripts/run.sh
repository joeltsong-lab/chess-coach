#!/usr/bin/env bash
# =============================================================================
# run.sh —— 按分支/端口启动与停止 chess-coach
#
#   scripts/run.sh stable        稳定版（main）            http://127.0.0.1:5000
#   scripts/run.sh endgame       残局版（feature/endgame）  http://127.0.0.1:5001
#   scripts/run.sh mobile        手机端（feature/mobile）   http://127.0.0.1:5002
#   scripts/run.sh all           三个一起（后台运行）
#   scripts/run.sh stop [模块]   按 PID 停止（默认停全部）
#   scripts/run.sh status        各模块的目录/分支/PID/端口/存活情况
#
# 选项:
#   --bg   单模块也放到后台（日志 + PID 文件）
#   --fg   all 也留在前台（默认 all 是后台）
#
# 说明:
#   1) 一个工作区只能检出一个分支，所以运行目录这样选：
#        stable  -> 主仓库（须检出 main）
#        endgame -> ../chess-coach-endgame（scripts/worktree_setup.sh init 建的），
#                   没有 worktree 就退回主仓库（须检出 feature/endgame）
#        mobile  -> ../chess-coach-mobile，同上
#      目录与分支对不上时会明确指出该怎么办，不会跑到另一个分支的代码上。
#   2) 端口用 `python -c` 注入，**不改动仓库里的 app.py**，
#      这样 main 与 feature/* 的同源文件能保持逐字节一致（合并时更省事）。
#   3) 日志 scripts/.logs/<模块>.log、PID scripts/.pids/<模块>.pid，
#      统一放在**主仓库**下（跨 worktree 共享，所以 worktree 里也能 stop）。
#   4) mobile 目前只有 README，没有 app.py -> 提示“尚无启动入口”并跳过，不算失败。
# =============================================================================
set -euo pipefail

PORT_STABLE=5000
PORT_ENDGAME=5001
PORT_MOBILE=5002

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  printf '\033[1;31m[错误]\033[0m 本脚本必须放在 git 仓库的 scripts/ 下\n' >&2
  exit 1
fi

# 主仓库（worktree 的 --git-common-dir 指向主仓库的 .git）
_common="$(git -C "$REPO_ROOT" rev-parse --git-common-dir)"
case "$_common" in
  /*|[A-Za-z]:/*) : ;;
  *) _common="$REPO_ROOT/$_common" ;;
esac
MAIN_REPO="$(cd "$_common/.." && pwd)"
MAIN_PARENT="$(dirname "$MAIN_REPO")"

WT_ENDGAME="$MAIN_PARENT/chess-coach-endgame"
WT_MOBILE="$MAIN_PARENT/chess-coach-mobile"

PID_DIR="$MAIN_REPO/scripts/.pids"
LOG_DIR="$MAIN_REPO/scripts/.logs"

bold() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
sub()  { printf '  \033[1;34m->\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[注意]\033[0m %s\n' "$*" >&2; }
die()  { printf '\n\033[1;31m[中止]\033[0m %s\n' "$*" >&2; exit 1; }

branch_of() { git -C "$1" symbolic-ref --quiet --short HEAD 2>/dev/null || printf '(非 git 目录或无分支)'; }
port_of() {
  case "$1" in
    stable)  printf '%s' "$PORT_STABLE" ;;
    endgame) printf '%s' "$PORT_ENDGAME" ;;
    mobile)  printf '%s' "$PORT_MOBILE" ;;
    *)       printf '?' ;;
  esac
}

branch_of_target() {
  case "$1" in
    stable)  printf 'main' ;;
    endgame) printf 'feature/endgame' ;;
    mobile)  printf 'feature/mobile' ;;
    *)       printf '?' ;;
  esac
}

# 找出该模块应该运行在哪个目录；失败时把原因打到 stderr
resolve_dir() {
  local target="$1" want cands c
  want="$(branch_of_target "$target")"
  case "$target" in
    stable)  cands="$MAIN_REPO" ;;
    endgame) cands="$WT_ENDGAME
$MAIN_REPO" ;;
    mobile)  cands="$WT_MOBILE
$MAIN_REPO" ;;
    *)       return 1 ;;
  esac

  while IFS= read -r c; do
    [ -n "$c" ] || continue
    [ -d "$c" ] || continue
    if [ "$(branch_of "$c")" = "$want" ]; then printf '%s' "$c"; return 0; fi
  done <<EOF
$cands
EOF

  {
    printf '  \033[1;33m[注意]\033[0m %s 找不到检出 %s 的目录：\n' "$target" "$want" >&2
    printf '        试过: %s\n' "$(printf '%s' "$cands" | tr '\n' ' ')" >&2
    if [ "$target" = "stable" ]; then
      printf '        解决: git -C "%s" switch main\n' "$MAIN_REPO" >&2
    else
      printf '        解决: scripts/worktree_setup.sh init（建 %s）\n' \
        "$([ "$target" = "endgame" ] && printf '%s' "$WT_ENDGAME" || printf '%s' "$WT_MOBILE")" >&2
    fi
  } >&2
  return 1
}

# 优先用仓库里的虚拟环境，其次 PYTHON，最后系统 python3/python
pick_python() {
  local dir="$1"
  if [ -x "$dir/.venv/bin/python" ]; then printf '%s' "$dir/.venv/bin/python"; return 0; fi
  if [ -x "$dir/.venv/Scripts/python.exe" ]; then printf '%s' "$dir/.venv/Scripts/python.exe"; return 0; fi
  if [ -n "${PYTHON:-}" ]; then printf '%s' "$PYTHON"; return 0; fi
  if command -v python3 >/dev/null 2>&1; then command -v python3; return 0; fi
  if command -v python  >/dev/null 2>&1; then command -v python;  return 0; fi
  return 1
}

pid_of() { cat "$PID_DIR/$1.pid" 2>/dev/null || true; }
is_alive() { local p="$1"; [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

# 等端口起来（同时监控进程是否已经挂了，挂了就把日志尾巴打出来）
wait_http() {
  local name="$1" port="$2" pid="$3" i=0
  if ! command -v curl >/dev/null 2>&1; then
    warn "没有 curl，跳过 http://127.0.0.1:$port 可达性探测"
    return 0
  fi
  while [ "$i" -lt 40 ]; do
    if curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null; then
      ok "已就绪: http://127.0.0.1:$port"
      return 0
    fi
    if ! is_alive "$pid"; then
      warn "$name 进程提前退出（PID $pid），日志末尾:"
      [ -f "$LOG_DIR/$name.log" ] && tail -n 25 "$LOG_DIR/$name.log" >&2 || true
      return 1
    fi
    sleep 0.5
    i=$((i + 1))
  done
  warn "$name 启动 20 秒还没响应 http://127.0.0.1:$port，看日志: $LOG_DIR/$name.log"
  return 1
}

usage() {
  cat <<'EOF'
chess-coach 启动/停止

  run.sh stable | endgame | mobile     前台启动单个模块（Ctrl-C 停止）
  run.sh all                           三个模块一起后台启动
  run.sh stop [stable|endgame|mobile]  按 PID 停止（不带参数=停全部）
  run.sh status                        查看各模块目录/分支/PID/端口

  选项: --bg 单模块也后台运行 / --fg 仅对 all 无效（all 固定后台）

端口: stable 5000 | endgame 5001 | mobile 5002
日志: scripts/.logs/<模块>.log     PID: scripts/.pids/<模块>.pid
EOF
}

# 启动单个模块
start_one() {
  local name="$1" bg="$2"
  local port dir py code pid cur_pid
  port="$(port_of "$name")"

  dir="$(resolve_dir "$name")" || return 1

  cur_pid="$(pid_of "$name")"
  if is_alive "$cur_pid"; then
    warn "$name 已经在跑（PID $cur_pid，端口 $port），先 scripts/run.sh stop $name"
    return 0
  fi

  if [ ! -f "$dir/app.py" ]; then
    warn "$name 尚无启动入口（$dir 里没有 app.py），跳过"
    return 0
  fi

  py="$(pick_python "$dir")" || die "找不到 python，先装 Python 3，或 PYTHON=/path/to/python scripts/run.sh $name"
  if ! "$py" -c 'import flask' >/dev/null 2>&1; then
    die "$py 里没有 flask，先装依赖: \"$py\" -m pip install -r \"$dir/requirements.txt\""
  fi

  mkdir -p "$PID_DIR" "$LOG_DIR"
  sub "$name: 分支 $(branch_of_target "$name")，目录 $dir"
  sub "python: $py"
  sub "日志: $LOG_DIR/$name.log"

  # 不改动 app.py（里面把端口写死成 5000）：导入模块后用指定端口起服务
  code='import sys; sys.path.insert(0, "."); import app as m; m.app.run(host="127.0.0.1", port=int(sys.argv[1]), debug=False, threaded=True)'

  ( cd "$dir" && exec "$py" -u -c "$code" "$port" ) >>"$LOG_DIR/$name.log" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" >"$PID_DIR/$name.pid"
  sub "PID $pid -> http://127.0.0.1:$port"

  wait_http "$name" "$port" "$pid" || true

  if [ "$bg" -eq 0 ]; then
    ok "$name 前台运行中，Ctrl-C 停止"
    trap 'kill "$pid" 2>/dev/null || true; rm -f "$PID_DIR/$name.pid"' INT TERM
    wait "$pid" 2>/dev/null || true
    rm -f "$PID_DIR/$name.pid"
    ok "$name 已停止"
  else
    ok "$name 已后台启动"
  fi
}

# 按 PID 文件停进程
cmd_stop() {
  if [ "$#" -eq 0 ]; then set -- stable endgame mobile; fi
  bold "停止模块"
  local name pidf pid i
  for name in "$@"; do
    pidf="$PID_DIR/$name.pid"
    if [ ! -f "$pidf" ]; then
      info "$name: 没有 PID 记录，跳过"
      continue
    fi
    pid="$(cat "$pidf")"
    if is_alive "$pid"; then
      kill "$pid" 2>/dev/null || true
      i=0
      while [ "$i" -lt 20 ] && is_alive "$pid"; do
        sleep 0.3
        i=$((i + 1))
      done
      if is_alive "$pid"; then
        warn "$name(PID $pid) 没退出，改用 SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
      fi
      ok "$name 已停止（PID $pid）"
    else
      warn "$name: PID $pid 已经不存在（陈旧记录，清掉）"
    fi
    rm -f "$pidf"
  done
}

cmd_status() {
  bold "模块状态（主仓库: $MAIN_REPO）"
  printf '  %-8s %-16s %-8s %-8s %-6s %s\n' "模块" "分支" "PID" "状态" "端口" "目录"
  local name dir pid stat pidtxt port
  for name in stable endgame mobile; do
    port="$(port_of "$name")"
    pid="$(pid_of "$name")"
    dir="$(resolve_dir "$name" 2>/dev/null || true)"
    [ -n "$dir" ] || dir="(找不到检出 $(branch_of_target "$name") 的目录)"
    if [ -n "$pid" ]; then pidtxt="$pid"; else pidtxt="-"; fi
    if is_alive "$pid"; then
      stat="运行中"
    elif [ -n "$pid" ]; then
      stat="已退出"
      rm -f "$PID_DIR/$name.pid"
    else
      stat="未启动"
    fi
    printf '  %-8s %-16s %-8s %-8s %-6s %s\n' "$name" "$(branch_of_target "$name")" "$pidtxt" "$stat" "$port" "$dir"
  done
  info "日志目录: $LOG_DIR"
}

# ---------------------------------------------------------------------------
main() {
  local action="" target="" bgmode=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --bg) bgmode="bg" ;;
      --fg) bgmode="fg" ;;
      -h|--help) usage; return 0 ;;
      -*) die "未知参数: $1（见 run.sh --help）" ;;
      *) if [ -z "$action" ]; then action="$1"; else target="$1"; fi ;;
    esac
    shift
  done
  [ -n "$action" ] || { usage; exit 1; }

  case "$action" in
    stable|endgame|mobile)
      local bg=0
      [ "$bgmode" = "bg" ] && bg=1
      start_one "$action" "$bg"
      ;;
    all)
      [ "$bgmode" = "fg" ] && warn "all 固定后台运行，忽略 --fg"
      bold "启动全部模块（后台）"
      for t in stable endgame mobile; do
        start_one "$t" 1 || true
      done
      cmd_status
      info "停止: scripts/run.sh stop"
      ;;
    stop)
      if [ -n "$target" ]; then cmd_stop "$target"; else cmd_stop; fi
      ;;
    status)
      cmd_status
      ;;
    *)
      usage
      die "未知动作: $action"
      ;;
  esac
}

main "$@"

