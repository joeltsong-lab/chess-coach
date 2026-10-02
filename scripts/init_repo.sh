#!/usr/bin/env bash
# =============================================================================
# init_repo.sh —— 把「稳定版 Web」与「残局研究版」导入同一个 Git 仓库，并建立分支模型
#
# 用法（Git Bash / macOS / Linux / WSL）:
#   REPO_DIR=/d/Traeprojects/chess-coach \
#   STABLE_DIR=/d/Traeprojects/xiangqi_ai_web_save \
#   ENDGAME_DIR=/d/Traeprojects/xiangqi_ai_endgame \
#   bash scripts/init_repo.sh
#
# 可覆盖的变量:
#   REPO_DIR      新仓库位置（默认 $HOME/chess-coach）
#   STABLE_DIR    稳定版源目录（默认 $HOME/xiangqi_ai_web_save）
#   ENDGAME_DIR   残局版源目录（默认 $HOME/xiangqi_ai_endgame）
#   REMOTE_URL    远程地址；留空则跳过推送
#   GIT_NAME / GIT_EMAIL   提交者身份（默认仓库级占位身份 xiangqi-dev <xiangqi-dev@localhost>）
#
# 承诺:
#   * 不删除、不覆盖、不重写历史：只在新建的仓库里提交；
#   * 两个源目录只读：执行前后各算一次内容摘要，不一致就报错；
#   * 分阶段执行，每阶段打印状态；任一步失败立即中止并指明阶段；
#   * 目标目录已有 .git、或与源目录相同、或非空 → 直接拒绝，不静默覆盖。
#
# 阶段:
#   1 环境与前置检查   2 main 基线（稳定版）   3 main 基建（文档/脚本/.gitignore）
#   4 feature/endgame 残局增量   5 feature/mobile 占位   6 推送（若配置 REMOTE_URL）
#   7 初始化报告       8 手动验收清单
# =============================================================================
set -euo pipefail

STEP="0"
say()  { printf '\n\033[1;36m==== 阶段 %s ====\033[0m\n' "$*"; }
sub()  { printf '  \033[1;34m->\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m[OK]\033[0m %s\n' "$*"; }
warn() { printf '  \033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m[中止] %s\033[0m\n' "$*" >&2; exit 1; }
trap 'rc=$?; if [ "$rc" -ne 0 ]; then printf "\n\033[1;31m[中止] 阶段 %s 执行失败（退出码 %s）\033[0m\n" "$STEP" "$rc" >&2; fi' EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAYLOAD_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

REPO_DIR="${REPO_DIR:-$HOME/chess-coach}"
STABLE_DIR="${STABLE_DIR:-$HOME/xiangqi_ai_web_save}"
ENDGAME_DIR="${ENDGAME_DIR:-$HOME/xiangqi_ai_endgame}"
REMOTE_URL="${REMOTE_URL:-}"
GIT_NAME="${GIT_NAME:-xiangqi-dev}"
GIT_EMAIL="${GIT_EMAIL:-xiangqi-dev@localhost}"

BRANCH_STABLE="main"
BRANCH_ENDGAME="feature/endgame"
BRANCH_MOBILE="feature/mobile"

# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
# 可用的 sha256 命令名（含参数），供 find -exec 与管道使用
#   注意：这里只能 printf 出命令，不能直接执行 —— $(sha_cmd) 的结果会被当作命令名
sha_cmd() {
  if command -v sha256sum >/dev/null 2>&1; then printf 'sha256sum'; else printf 'shasum -a 256'; fi
}

# 目录内容摘要（跳过 __pycache__），用于证明源目录没被改动
digest_dir() {
  find "$1" -type f -not -path '*/__pycache__/*' -not -name '*.pyc' -not -name '*.pyo' \
       -exec $(sha_cmd) {} + | LC_ALL=C sort -k2 | $(sha_cmd) | awk '{print $1}'
}

# 复制目录（含隐藏文件；排除 __pycache__），跨平台用 tar 保权限与时间戳
copy_tree() {
  mkdir -p "$2"
  ( cd "$1" && tar cf - --exclude='__pycache__' --exclude='*.pyc' . ) | ( cd "$2" && tar xf - )
}

# 逐字节校验源目录里的每个文件都在目标目录有一份相同内容；输出不一致条数
#   第 3 个参数起是「跳过」的 glob，可以给多个，例如:
#     verify_tree "$SRC" "$DST" 'data/*' 'README.md'
verify_tree() {
  local src="$1" dst="$2"
  shift 2
  local bad=0 rel skip
  while IFS= read -r -d '' f; do
    rel="${f#"$src"/}"
    for skip in "$@"; do
      case "$rel" in $skip) continue 2 ;; esac
    done
    if [ ! -f "$dst/$rel" ]; then printf '      缺失: %s\n' "$rel" >&2; bad=$((bad + 1)); continue; fi
    cmp -s "$f" "$dst/$rel" || { printf '      内容不同: %s\n' "$rel" >&2; bad=$((bad + 1)); }
  done < <(find "$src" \( -name '__pycache__' -o -name '.git' \) -prune -o -type f -print0)
  printf '%s' "$bad"
}

# 二进制/运行时产物：既不进版本库，也不参与“增量搬迁”
is_artifact() {
  case "$1" in
    data/*|engine/*.exe|engine/*.nnue|*.pyc|*.pyo) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
STEP="1 环境与前置检查"
say "$STEP"
command -v git >/dev/null 2>&1 || die "找不到 git，请先安装 Git（https://git-scm.com/downloads）"
GIT_VERSION="$(git --version)"
sub "git: $GIT_VERSION"
sub "bash: $BASH_VERSION"

[ -d "$STABLE_DIR" ]   || die "稳定版源目录不存在: $STABLE_DIR"
[ -d "$ENDGAME_DIR" ]  || die "残局版源目录不存在: $ENDGAME_DIR"
[ -f "$STABLE_DIR/app.py" ]      || die "$STABLE_DIR 不像稳定版（缺 app.py）"
[ -d "$ENDGAME_DIR/endgame" ]    || die "$ENDGAME_DIR 不像残局版（缺 endgame/）"
ok "源目录检查通过"

REPO_PARENT="$(dirname "$REPO_DIR")"
mkdir -p "$REPO_PARENT" || die "无法创建 $REPO_PARENT"
REPO_DIR="$(cd "$REPO_PARENT" && pwd)/$(basename "$REPO_DIR")"
[ "$REPO_DIR" = "$STABLE_DIR" ]  && die "REPO_DIR 不能等于稳定版源目录"
[ "$REPO_DIR" = "$ENDGAME_DIR" ] && die "REPO_DIR 不能等于残局版源目录"
[ -e "$REPO_DIR/.git" ] && die "$REPO_DIR 已经是 git 仓库，本脚本不覆盖已有仓库"
if [ -d "$REPO_DIR" ] && [ -n "$(ls -A "$REPO_DIR" 2>/dev/null)" ]; then
  die "$REPO_DIR 非空且没有 .git，为避免覆盖请先清空或换 REPO_DIR"
fi
mkdir -p "$REPO_DIR" || die "无法创建 $REPO_DIR"
probe="$REPO_DIR/.write_probe"
( : >"$probe" ) 2>/dev/null || die "$REPO_DIR 不可写"
rm -f "$probe"
sub "REPO_DIR   = $REPO_DIR"
sub "STABLE_DIR = $STABLE_DIR"
sub "ENDGAME_DIR= $ENDGAME_DIR"
sub "PAYLOAD_DIR= $PAYLOAD_DIR"
[ -f "$PAYLOAD_DIR/.gitignore" ] || die "缺少 $PAYLOAD_DIR/.gitignore（payload 不完整）"
[ -d "$PAYLOAD_DIR/scripts" ]    || die "缺少 $PAYLOAD_DIR/scripts/（payload 不完整）"
ok "目标目录可用"

DIGEST_STABLE_BEFORE="$(digest_dir "$STABLE_DIR")"
DIGEST_ENDGAME_BEFORE="$(digest_dir "$ENDGAME_DIR")"
sub "源目录摘要已记录（结束时会再校验一次）"

if [ -z "$(git config --global user.name 2>/dev/null || true)" ]; then
  warn "全局 git 身份未配置，本次使用仓库级身份: $GIT_NAME <$GIT_EMAIL>"
fi

# ---------------------------------------------------------------------------
STEP="2 main 基线（稳定版）"
say "$STEP"
git init -q "$REPO_DIR"
git -C "$REPO_DIR" symbolic-ref HEAD "refs/heads/$BRANCH_STABLE"
git -C "$REPO_DIR" config user.name "$GIT_NAME"
git -C "$REPO_DIR" config user.email "$GIT_EMAIL"
# 逐字节导入：关掉换行转换，避免 CRLF/LF 被改写导致内容与源目录不一致
git -C "$REPO_DIR" config core.autocrlf false
sub "git init 完成，初始分支 $BRANCH_STABLE，core.autocrlf=false"

STAGING_STABLE="$REPO_DIR/stable"
sub "复制稳定版内容 -> $STAGING_STABLE（暂存）"
copy_tree "$STABLE_DIR" "$STAGING_STABLE"
ok "复制完成（$(find "$STAGING_STABLE" -type f -not -path '*/__pycache__/*' | wc -l | tr -d ' ') 个文件）"

sub "把暂存内容提升到仓库根目录（保持原目录结构，根目录 = 可启动的稳定版）"
shopt -s dotglob nullglob
for item in "$STAGING_STABLE"/*; do mv "$item" "$REPO_DIR/"; done
shopt -u dotglob nullglob
rmdir "$STAGING_STABLE"
ok "暂存目录 stable/ 已提升并删除"

cp "$PAYLOAD_DIR/.gitignore" "$REPO_DIR/.gitignore"
ok "已生成 .gitignore"

bad="$(verify_tree "$STABLE_DIR" "$REPO_DIR")"
if [ "$bad" != "0" ]; then
  die "仓库根与稳定版源目录有 $bad 处不一致，已中止（未提交）"
fi
ok "仓库根 = 稳定版源目录（逐字节一致，含 engine/ 与 data/ 下的被忽略文件）"

git -C "$REPO_DIR" add -A
git -C "$REPO_DIR" commit -q -F - <<EOF
chore: 导入稳定版基线

- 来源: $STABLE_DIR（逐文件复制，未修改原目录）
- 内容: 基础对局 / AI 提示 / 保存与复盘，仓库根目录即可 python app.py 启动
- 排除: engine/ 下的 Pikafish.exe 与 *.nnue（约 55MB）、data/*.db（运行时库），见 .gitignore
EOF
MAIN_COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
ok "$BRANCH_STABLE 首次提交 = $MAIN_COMMIT（$(git -C "$REPO_DIR" log -1 --pretty=%s)）"

# ---------------------------------------------------------------------------
STEP="3 main 基建（分支模型文档 + 管理脚本）"
say "$STEP"
if ! grep -q '## 版本管理与分支模型' "$REPO_DIR/README.md" 2>/dev/null; then
  cat "$PAYLOAD_DIR/README.versioning.md" >>"$REPO_DIR/README.md"
  ok "README.md 追加「版本管理与分支模型」章节"
else
  warn "README.md 已有版本管理章节，跳过追加"
fi
mkdir -p "$REPO_DIR/scripts"
if [ "$PAYLOAD_DIR" != "$REPO_DIR" ]; then
  for f in "$PAYLOAD_DIR/scripts"/*; do cp "$f" "$REPO_DIR/scripts/"; done
  ok "脚本已复制到 scripts/：$(cd "$REPO_DIR/scripts" && ls | tr '\n' ' ')"
else
  warn "payload 就在仓库里，跳过脚本复制"
fi
chmod +x "$REPO_DIR/scripts/"*.sh 2>/dev/null || true

git -C "$REPO_DIR" add -A
git -C "$REPO_DIR" commit -q -F - <<'EOF'
chore: 仓库基建（分支模型 + 管理脚本）

- README: 新增「版本管理与分支模型」章节（分支用途、端口、引擎/NNUE 与数据库约定）
- scripts/: branch_manager.sh、run.sh、worktree_setup.sh、init_report.sh、init_repo.sh
EOF
ok "基建提交 = $(git -C "$REPO_DIR" rev-parse --short HEAD)"

# ---------------------------------------------------------------------------
STEP="4 $BRANCH_ENDGAME（稳定版 + 残局增量）"
say "$STEP"
git -C "$REPO_DIR" checkout -q -b "$BRANCH_ENDGAME"
sub "已从 $BRANCH_STABLE 创建 $BRANCH_ENDGAME"

# 暂存目录故意不叫 endgame/ —— 那个路径正是残局模块要落地的地方，同名会自相冲突
STAGING_ENDGAME="$REPO_DIR/.staging-endgame"
sub "复制残局版内容 -> $STAGING_ENDGAME（暂存，稍后删除）"
copy_tree "$ENDGAME_DIR" "$STAGING_ENDGAME"
ok "复制完成（$(find "$STAGING_ENDGAME" -type f -not -path '*/__pycache__/*' | wc -l | tr -d ' ') 个文件）"

sub "计算残局版相对稳定版的增量（逐文件 sha/cmp 比较两个源目录）"
ADDED=()
MODIFIED=()
SKIPPED=()
while IFS= read -r -d '' f; do
  rel="${f#"$STAGING_ENDGAME"/}"
  if is_artifact "$rel"; then SKIPPED+=("$rel"); continue; fi
  if [ ! -e "$STABLE_DIR/$rel" ]; then
    ADDED+=("$rel")
  elif ! cmp -s "$f" "$STABLE_DIR/$rel"; then
    MODIFIED+=("$rel")
  fi
done < <(find "$STAGING_ENDGAME" \( -name '__pycache__' -o -name '.git' \) -prune -o -type f -print0)

sub "新增 ${#ADDED[@]} 个，修改 ${#MODIFIED[@]} 个，跳过二进制/运行时 ${#SKIPPED[@]} 个"
printf '       新增: %s\n' "${ADDED[@]:-（无）}"
printf '       修改: %s\n' "${MODIFIED[@]:-（无）}"
printf '       跳过: %s\n' "${SKIPPED[@]:-（无）}"

sub "只把增量文件搬到仓库根目录"
for rel in "${ADDED[@]:-}" "${MODIFIED[@]:-}"; do
  [ -n "$rel" ] || continue
  mkdir -p "$REPO_DIR/$(dirname "$rel")"
  cp -p "$STAGING_ENDGAME/$rel" "$REPO_DIR/$rel"
done
ok "增量已落地"

sub "删除暂存目录 .staging-endgame/"
rm -rf "$STAGING_ENDGAME"
[ -e "$STAGING_ENDGAME" ] && die "暂存目录删除失败"
ok "暂存目录已删除"

# 校验：根部应该等于残局版（data/*.db 是有意不带过来的运行时库；
# README.md 在阶段 3 被有意追加了「版本管理与分支模型」章节，所以也不再与源目录一致）
bad="$(verify_tree "$ENDGAME_DIR" "$REPO_DIR" 'data/*' 'README.md')"
if [ "$bad" != "0" ]; then
  die "仓库根与残局版源目录有 $bad 处不一致，已中止（未提交）"
fi
ok "仓库根 = 残局版源目录（data/*.db 除外；引擎二进制与 NNUE 两版本就相同）"

git -C "$REPO_DIR" add -A
git -C "$REPO_DIR" commit -q -F - <<EOF
feat(endgame): 引入残局研究模块

- 新增 ${#ADDED[@]} 个: endgame/ 包（摆盘/候选/主变/备注、引擎深度分析、定式库、LLM 冻结 prompt 解说、
  研究导入导出 xq-endgame-study v1）、migrations/0001_endgame_study_*、templates/study.html、
  test_endgame_*.py、test_migrate.py、migrate.py
- 修改 ${#MODIFIED[@]} 个: app.py、chess_engine.py、storage.py、coord_utils.py、templates/index.html、
  templates/studies.html、test_games_routes.py、test_storage.py
- 来源: $ENDGAME_DIR（相对稳定版的逐文件增量，未修改原目录）
- 未携带: data/*.db（运行时库，各分支各自 migrate）
EOF
ENDGAME_COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
ok "$BRANCH_ENDGAME 提交 = $ENDGAME_COMMIT"

# ---------------------------------------------------------------------------
STEP="5 $BRANCH_MOBILE（占位）"
say "$STEP"
git -C "$REPO_DIR" checkout -q "$BRANCH_STABLE"
git -C "$REPO_DIR" checkout -q -b "$BRANCH_MOBILE"
mkdir -p "$REPO_DIR/mobile"
cat >"$REPO_DIR/mobile/README.md" <<EOF
# 手机端（占位）

本分支从 \`$BRANCH_STABLE\` 派生，**不含残局研究代码**，只放安卓 / iOS / WASM 的落地实现。

## 计划

1. 先定公共引擎接口：局面表示、搜索参数（深度 / 限时 / MultiPV）、结果结构（bestmove / pv / 评分）；
2. 引擎复用：优先把 C++ 引擎按平台编译（Android NDK、iOS 静态库），Web/小程序侧考虑 WASM；
3. 再分平台实现 UI 与对局流程，公共逻辑尽量下沉到 \`core/\`（见根 README 的「后续演进」）。

## 本地端口

预留 5002（\`scripts/run.sh mobile\`）：目前只有本文件，脚本会提示“尚无启动入口”，不会真的起服务。

## 起步

\`\`\`bash
git merge main                        # 把 main 的最新改动并进当前分支
scripts/branch_manager.sh sync $BRANCH_MOBILE   # 同上，脚本会先检查工作区是否干净
\`\`\`
EOF
git -C "$REPO_DIR" add mobile/README.md
git -C "$REPO_DIR" commit -q -m "chore(mobile): 建 $BRANCH_MOBILE 占位分支（仅 README，不含残局代码）"
MOBILE_COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
ok "$BRANCH_MOBILE 提交 = $MOBILE_COMMIT"

# ---------------------------------------------------------------------------
STEP="6 推送远程（可选）"
say "$STEP"
if [ -n "$REMOTE_URL" ]; then
  if git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
    git -C "$REPO_DIR" remote set-url origin "$REMOTE_URL"
  else
    git -C "$REPO_DIR" remote add origin "$REMOTE_URL"
  fi
  sub "origin = $REMOTE_URL"
  for b in "$BRANCH_STABLE" "$BRANCH_ENDGAME" "$BRANCH_MOBILE"; do
    sub "push -u origin $b"
    git -C "$REPO_DIR" push -u origin "$b"
  done
  ok "三个分支已推送"
else
  warn "未设置 REMOTE_URL，跳过推送（本仓库仍是完整的本地仓库）"
  sub '以后要推: REMOTE_URL=git@github.com:you/chess-coach.git bash scripts/init_repo.sh（会拒绝重跑）'
  sub '或手动: git -C "<repo>" remote add origin <url> && git -C "<repo>" push -u origin main feature/endgame feature/mobile'
fi

# ---------------------------------------------------------------------------
STEP="7 初始化报告 + 源目录保护校验"
say "$STEP"
git -C "$REPO_DIR" checkout -q "$BRANCH_STABLE"
STABLE_DIR="$STABLE_DIR" ENDGAME_DIR="$ENDGAME_DIR" bash "$REPO_DIR/scripts/init_report.sh"
ok "报告: $REPO_DIR/scripts/init_report.txt"

DIGEST_STABLE_AFTER="$(digest_dir "$STABLE_DIR")"
DIGEST_ENDGAME_AFTER="$(digest_dir "$ENDGAME_DIR")"
[ "$DIGEST_STABLE_BEFORE" = "$DIGEST_STABLE_AFTER" ]   || die "稳定版源目录被改动了！"
[ "$DIGEST_ENDGAME_BEFORE" = "$DIGEST_ENDGAME_AFTER" ] || die "残局版源目录被改动了！"
ok "两个源目录未被修改（摘要一致: stable=${DIGEST_STABLE_AFTER:0:12} endgame=${DIGEST_ENDGAME_AFTER:0:12}）"

# ---------------------------------------------------------------------------
STEP="8 手动验收清单"
say "$STEP"
cat <<EOF
  仓库: $REPO_DIR
  main=$MAIN_COMMIT  $BRANCH_ENDGAME=$ENDGAME_COMMIT  $BRANCH_MOBILE=$MOBILE_COMMIT

  1) 历史与分支
       git -C "$REPO_DIR" log --oneline --all --graph
       git -C "$REPO_DIR" branch -a -v
       git -C "$REPO_DIR" diff main...feature/endgame --stat     # 只应列出残局模块范围内的文件

  2) 稳定版（main）
       git -C "$REPO_DIR" checkout main
       scripts/run.sh stable          -> http://127.0.0.1:5000
       走子 / 悔棋 / AI 提示 / 保存到“我的棋局” / 导出棋谱

  3) 残局版（feature/endgame）
       scripts/worktree_setup.sh init          # 建 ../chess-coach-endgame
       scripts/run.sh endgame                  -> http://127.0.0.1:5001
       （第一次先建表: 在 endgame 工作区里跑 python migrate.py up，幂等）
       定式新建 -> 摆盘（预检/应用）-> 深度分析 -> 候选/主变 -> 备注 -> LLM 解说 -> 导出/导入研究

  4) 合并演练（不改变历史结论，只验证可合并）
       git checkout main && 改一个小文件 && commit
       scripts/branch_manager.sh sync feature/endgame
       scripts/run.sh endgame 再看残局功能是否正常

  5) 收尾
       scripts/run.sh stop
       确认两个源目录未被改动：
         $STABLE_DIR
         $ENDGAME_DIR
       确认通过后再自行删除原目录（本脚本不删）
EOF
printf '\n\033[1;32m全部阶段完成。\033[0m\n'
