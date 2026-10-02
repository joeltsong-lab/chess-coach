# -*- coding: utf-8 -*-
"""中国象棋基本棋规: FEN 结构校验、着法生成、走子后自将/照面检测。

前端 templates/index.html 里有一份等价的 JS 实现 (pseudoMoves / kingAttacked /
isLegalMove), 这里是后端侧的同一套规则, 供保存与导入时校验着法合法性,
两边判罚必须一致, 否则"前端让走、后端拒收"会很难查。
改动任何一条走子规则时, 记得同步改 templates/index.html 里对应的那段。

只判基本走子规则: 各子走法、蹩马腿、塞象眼、炮架、九宫、相不过河、兵过河横走、
不能吃自己的子、不能吃将、走后不能自将、将帅不能照面。
**不判**长将/长捉/重复局面/60 回合无吃子等对局层面的判定。

棋盘表示: grid[row][col], row 0 = 黑方底线(棋盘顶部), col 0 = a 列, 空格为 ''。
与 coord_utils.fen_to_grid() 的输出、以及前端 grid 完全一致。
"""

from .coord_utils import FILES, GRID_COLS, GRID_ROWS, fen_to_grid, grid_to_iccs, iccs_to_grid

PIECE_CHARS = set("KABNRCPkabnrcp")

# 斜行走子: 记谱时写目标纵线而不是步数 (跟 coord_utils 保持一致)
DIAGONAL_PIECES = {"N", "B", "A"}


class RuleError(ValueError):
    """着法或局面不合法"""


# ----------------------------------------------------------------------
# 盘面基本操作
# ----------------------------------------------------------------------
def is_red(piece):
    """棋子是不是红方 (大写为红)"""
    return bool(piece) and piece.isupper()


def inside(r, c):
    return 0 <= r < GRID_ROWS and 0 <= c < GRID_COLS


def in_palace(r, c, red):
    """(r, c) 是否在九宫内"""
    if c < 3 or c > 5:
        return False
    return (7 <= r <= 9) if red else (0 <= r <= 2)


def fen_to_board(fen):
    """FEN -> (grid, side); 结构不对抛 RuleError

    :return: (10x9 的二维数组, 'w'/'b')
    """
    text = (fen or "").strip()
    if not text:
        raise RuleError("FEN 不能为空")
    parts = text.split()
    rows = parts[0].split("/")
    if len(rows) != 10:
        raise RuleError(f"FEN 棋盘部分应为 10 行, 实际 {len(rows)} 行")
    for i, row in enumerate(rows):
        for ch in row:
            if not ch.isdigit() and ch not in PIECE_CHARS:
                raise RuleError(f"FEN 第 {i + 1} 行有无法识别的字符: {ch!r}")
    try:
        grid, side = fen_to_grid(text)
    except ValueError as e:   # coord_utils 的列数/纵线数校验
        raise RuleError(f"FEN 不合法: {e}") from e
    return grid, side


def board_to_fen(grid, side="w", halfmove=0, fullmove=1):
    """grid + 走子方 -> FEN (与前端 gridToFen() 等价)"""
    rows = []
    for row in grid:
        text, empty = "", 0
        for piece in row:
            if not piece:
                empty += 1
                continue
            if empty:
                text += str(empty)
                empty = 0
            text += piece
        if empty:
            text += str(empty)
        rows.append(text)
    return f"{'/'.join(rows)} {side} - - {int(halfmove)} {int(fullmove)}"


def validate_fen(fen, require_kings=False):
    """结构校验, 通过返回规范化后的 FEN, 不通过抛 RuleError

    :param require_kings: True 时额外要求红黑双方各有且只有一枚将/帅。
        残局研究常见"缺将"的摆设局面, 所以默认不查。
    """
    grid, side = fen_to_board(fen)
    if require_kings:
        for king, who in (("K", "红方"), ("k", "黑方")):
            n = sum(row.count(king) for row in grid)
            if n != 1:
                raise RuleError(f"{who}应有且只有一枚将/帅, 实际 {n} 枚")
    parts = fen.strip().split()
    return board_to_fen(grid, side,
                        parts[4] if len(parts) > 4 else 0,
                        parts[5] if len(parts) > 5 else 1)


def copy_grid(grid):
    return [row[:] for row in grid]


def find_king(grid, red):
    """找将/帅, 返回 (row, col); 没有返回 None"""
    king = "K" if red else "k"
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            if grid[r][c] == king:
                return (r, c)
    return None


# ----------------------------------------------------------------------
# 着法生成
# ----------------------------------------------------------------------
def pseudo_moves(grid, r, c):
    """(r, c) 上那枚棋子的伪合法着法: 只管走子形状, 不管走完会不会自将

    :return: [(row, col), ...]
    """
    piece = grid[r][c]
    if not piece:
        return []
    red = is_red(piece)
    kind = piece.lower()
    out = []

    def push(rr, cc):
        if not inside(rr, cc):
            return
        target = grid[rr][cc]
        if target and is_red(target) == red:
            return          # 不能吃自己的子
        out.append((rr, cc))

    if kind in ("r", "c"):  # 车 / 炮
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc, screen = r + dr, c + dc, False
            while inside(rr, cc):
                target = grid[rr][cc]
                if not target:
                    if kind == "r" or not screen:
                        out.append((rr, cc))     # 炮被炮架挡住后不能再走空格
                elif kind == "r":
                    push(rr, cc)                 # 车: 吃掉挡路的第一个子后停住
                    break
                else:
                    if not screen:
                        screen = True            # 遇到第一个子 = 炮架
                    else:
                        push(rr, cc)             # 炮架后的第一个子可吃
                        break
                rr += dr
                cc += dc
    elif kind == "n":  # 马
        for dr, dc in ((-2, -1), (-2, 1), (2, -1), (2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2)):
            lr = r + dr // 2 if abs(dr) == 2 else r
            lc = c + dc // 2 if abs(dc) == 2 else c
            if not inside(lr, lc) or grid[lr][lc]:
                continue                         # 蹩马腿
            push(r + dr, c + dc)
    elif kind == "b":  # 相 / 象
        for dr, dc in ((-2, -2), (-2, 2), (2, -2), (2, 2)):
            rr, cc = r + dr, c + dc
            if not inside(rr, cc):
                continue
            if (rr < 5) if red else (rr > 4):
                continue                         # 相不过河
            if grid[r + dr // 2][c + dc // 2]:
                continue                         # 塞象眼
            push(rr, cc)
    elif kind == "a":  # 仕 / 士
        for dr, dc in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
            if in_palace(r + dr, c + dc, red):
                push(r + dr, c + dc)
    elif kind == "k":  # 将 / 帅
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            if in_palace(r + dr, c + dc, red):
                push(r + dr, c + dc)
    elif kind == "p":  # 兵 / 卒
        forward = -1 if red else 1
        push(r + forward, c)
        if (r <= 4) if red else (r >= 5):
            push(r, c - 1)                       # 过河后可横走
            push(r, c + 1)
    return out


def king_attacked(grid, red):
    """red 方的将/帅是否正被攻击 (含将帅照面); 该方没有将/帅时返回 False

    没有将/帅时返回 False 是刻意的: 摆设局面允许缺将, 不因为缺将就判定"被攻击"。
    """
    king = find_king(grid, red)
    if not king:
        return False
    kr, kc = king
    enemy_king = "k" if red else "K"

    for r in range(kr - 1, -1, -1):          # 将帅照面
        piece = grid[r][kc]
        if piece:
            if piece == enemy_king:
                return True
            break
    for r in range(kr + 1, GRID_ROWS):
        piece = grid[r][kc]
        if piece:
            if piece == enemy_king:
                return True
            break

    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            piece = grid[r][c]
            if not piece or is_red(piece) == red:
                continue                     # 只看对方棋子
            if (kr, kc) in pseudo_moves(grid, r, c):
                return True
    return False


def is_legal_move(grid, fr, fc, tr, tc):
    """(fr,fc) -> (tr,tc) 是否合法 (含走后不能自将、不能照面)"""
    piece = grid[fr][fc]
    if not piece:
        return False
    if (tr, tc) not in pseudo_moves(grid, fr, fc):
        return False
    target = grid[tr][tc]
    if target and target.upper() == "K":
        return False                         # 不允许吃将
    after = copy_grid(grid)
    after[tr][tc] = piece
    after[fr][fc] = ""
    return not king_attacked(after, is_red(piece))


def legal_moves(grid, side=None, red=None):
    """某一方全部合法着法

    :param side: 'w'(红) / 'b'(黑), 与 red 参数二选一
    :return: ICCS 着法字符串列表, 如 ['b2e2', 'h2e2', ...], 已按着法排序
    """
    if red is None:
        red = (side or "w").lower().startswith("w")
    out = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            piece = grid[r][c]
            if not piece or is_red(piece) != red:
                continue
            for tr, tc in pseudo_moves(grid, r, c):
                if is_legal_move(grid, r, c, tr, tc):
                    out.append(grid_to_iccs(r, c, tr, tc))
    return sorted(out)


def legal_moves_from(grid, r, c):
    """指定某枚棋子的全部合法着法, 返回 ICCS 列表"""
    return sorted(grid_to_iccs(r, c, tr, tc)
                  for tr, tc in pseudo_moves(grid, r, c)
                  if is_legal_move(grid, r, c, tr, tc))


def in_check(grid, side):
    """side 方的将/帅是否被将军 ('w'/'b')"""
    return king_attacked(grid, (side or "w").lower().startswith("w"))


# ----------------------------------------------------------------------
# 走子
# ----------------------------------------------------------------------
def parse_iccs(iccs_move):
    """ICCS 着法 -> ((fr, fc), (tr, tc)); 解析不了抛 RuleError"""
    move = (iccs_move or "").strip()
    if len(move) < 4:
        raise RuleError(f"ICCS 着法至少需要 4 个字符: {iccs_move!r}")
    try:
        coord = iccs_to_grid(move[:4])
    except ValueError as e:
        raise RuleError(f"ICCS 着法不合法: {e}") from e
    return (coord["from"][0], coord["from"][1]), (coord["to"][0], coord["to"][1])


def validate_move(grid, iccs_move, side=None):
    """校验一步棋是否合法, 合法返回 ((fr,fc),(tr,tc)) 并附带规范化的 ICCS

    :param side: 传入时还要检查是不是轮到这一方走 ('w'/'b')
    :return: {"iccs": "h2e2", "from": [r,c], "to": [r,c], "piece": "C",
              "captured": None|"p", "chinese_side": 'w'}
    """
    (fr, fc), (tr, tc) = parse_iccs(iccs_move)
    piece = grid[fr][fc]
    if not piece:
        raise RuleError(f"{iccs_move} 的起点 {FILES[fc]}{9 - fr} 上没有棋子")
    mover = "w" if is_red(piece) else "b"
    if side and mover != (side or "w").lower()[:1]:
        raise RuleError(f"{iccs_move} 是{'红' if mover == 'w' else '黑'}方的着法, "
                        f"但现在轮到{'红' if side == 'w' else '黑'}方走")
    if not is_legal_move(grid, fr, fc, tr, tc):
        target = grid[tr][tc]
        hint = " 不能吃将" if target and target.upper() == "K" else ""
        raise RuleError(f"{iccs_move} 在该局面下不是合法着法"
                        f"（{FILES[fc]}{9 - fr} → {FILES[tc]}{9 - tr}）{hint}")
    return {
        "iccs": grid_to_iccs(fr, fc, tr, tc),
        "from": [fr, fc],
        "to": [tr, tc],
        "piece": piece,
        "captured": grid[tr][tc] or None,
        "side": mover,
    }


def apply_move(grid, fr, fc, tr, tc):
    """走一步, 返回 (新 grid, 被吃掉的棋子或 None); 不改动传入的 grid"""
    after = copy_grid(grid)
    captured = after[tr][tc] or None
    after[tr][tc] = after[fr][fc]
    after[fr][fc] = ""
    return after, captured


def apply_iccs(grid, iccs_move, side=None):
    """校验并走一步, 返回 (新 grid, 着法信息 dict); 不合法抛 RuleError"""
    info = validate_move(grid, iccs_move, side=side)
    (fr, fc), (tr, tc) = tuple(info["from"]), tuple(info["to"])
    after, captured = apply_move(grid, fr, fc, tr, tc)
    info["fen_after"] = board_to_fen(after, "b" if info["side"] == "w" else "w")
    return after, info
