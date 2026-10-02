# -*- coding: utf-8 -*-
"""坐标与记谱工具。

两套坐标系的对照:

  ICCS (引擎输出, 形如 "h2e2")
      纵线 a~i: 从红方视角由左到右, a 在最左, i 在最右
      横线 0~9: 0 是红方底线, 9 是黑方底线

  前端 Grid (templates/index.html 里的 grid[row][col])
      row 0~9: row 0 在最上方 = 黑方底线, row 9 在最下方 = 红方底线
      col 0~8: col 0 在最左, 对应 a

两者只差一个上下翻转:  row = 9 - rank,  col = 纵线字母索引

!! 需要校准的地方 !!
如果前端把棋盘改成红方在上(或别的朝向), 只需要改这一处:
  GRID_ORIENTATION / iccs_to_grid()  —— 基准映射只在本文件维护这一份
后端两个接口都把算好的棋盘行列(from/to)直接发给前端, 前端不再自己解析 ICCS,
只保留"换边"用的那层 180° 显示旋转(与这里的朝向无关)。
"""

FILES = "abcdefghi"
GRID_ROWS = 10
GRID_COLS = 9

# 与前端棋盘朝向一致: 行 0 在黑方底线(上方), 行 9 在红方底线(下方)
GRID_ORIENTATION = "red_bottom"

RED_PIECE_NAMES = {"K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵"}
BLACK_PIECE_NAMES = {"k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒"}

RED_DIGITS = "一二三四五六七八九"
BLACK_DIGITS = "123456789"

# 斜行走子, 记谱时写目标纵线而不是步数
DIAGONAL_PIECES = {"N", "B", "A"}


# ----------------------------------------------------------------------
# ICCS <-> 前端 Grid
# ----------------------------------------------------------------------
def iccs_to_grid(iccs_move, grid_orientation=GRID_ORIENTATION):
    """ICCS 着法 -> 前端 Grid 坐标

    :param iccs_move: 形如 "h2e2"; 引擎偶尔会带附加信息, 只取前 4 位
    :param grid_orientation: 'red_bottom' 表示前端行 0 在上=黑方底线(本项目的朝向),
                             'black_bottom' 表示行 0 在上=红方底线
    :return: {"from": [row, col], "to": [row, col]}
    """
    if grid_orientation not in ("red_bottom", "black_bottom"):
        raise ValueError(f"未知的 grid_orientation: {grid_orientation!r}")

    move = (iccs_move or "").strip()
    if len(move) < 4:
        raise ValueError(f"ICCS 着法至少需要 4 个字符: {iccs_move!r}")
    head = move[:4]

    def parse(file_ch, rank_ch):
        if file_ch not in FILES:
            raise ValueError(f"无法解析 ICCS 着法 (纵线必须是 a~i): {iccs_move!r}")
        if rank_ch not in "0123456789":
            raise ValueError(f"无法解析 ICCS 着法 (横线必须是 0~9): {iccs_move!r}")
        col = FILES.index(file_ch)
        rank = int(rank_ch)
        row = 9 - rank if grid_orientation == "red_bottom" else rank
        return [row, col]

    return {"from": parse(head[0], head[1]), "to": parse(head[2], head[3])}


def grid_to_iccs(row_from, col_from, row_to, col_to, grid_orientation=GRID_ORIENTATION):
    """前端 Grid 坐标 -> ICCS 着法 (iccs_to_grid 的逆运算)"""
    if grid_orientation not in ("red_bottom", "black_bottom"):
        raise ValueError(f"未知的 grid_orientation: {grid_orientation!r}")

    def to_iccs(row, col):
        if not (0 <= row < GRID_ROWS) or not (0 <= col < GRID_COLS):
            raise ValueError(f"Grid 坐标越界: row={row}, col={col}")
        rank = 9 - row if grid_orientation == "red_bottom" else row
        return f"{FILES[col]}{rank}"

    return to_iccs(row_from, col_from) + to_iccs(row_to, col_to)


# ----------------------------------------------------------------------
# FEN / 记谱
# ----------------------------------------------------------------------
def parse_fen(fen):
    """解析 FEN, 返回 (棋盘, 走子方)

    棋盘为 dict: {(纵线索引, 横线索引): 棋子字符}
    纵线索引 0-8 对应 a-i; 横线索引 0-9 对应红方底线(0)到黑方底线(9)。
    """
    parts = fen.split()
    if not parts:
        raise ValueError("FEN 为空")
    rows = parts[0].split("/")
    if len(rows) != 10:
        raise ValueError(f"FEN 棋盘部分应为 10 行, 实际 {len(rows)} 行: {parts[0]}")

    board = {}
    for i, row in enumerate(rows):
        rank = 9 - i  # FEN 第一行是黑方底线, 即横线 9
        file_idx = 0
        for ch in row:
            if ch.isdigit():
                file_idx += int(ch)
            else:
                if file_idx > 8:
                    raise ValueError(f"FEN 第 {i + 1} 行超出 9 条纵线: {row}")
                board[(file_idx, rank)] = ch
                file_idx += 1
        if file_idx != 9:
            raise ValueError(f"FEN 第 {i + 1} 行纵线数不是 9: {row}")

    side = "b" if len(parts) > 1 and parts[1].lower().startswith("b") else "w"
    return board, side


def fen_to_grid(fen, grid_orientation=GRID_ORIENTATION):
    """FEN -> 前端 Grid 二维数组 (grid[row][col]), 空格为 ''

    用于自测: 确认把 ICCS 转出来的行列号取到的棋子, 与前端渲染的是同一枚。
    """
    board, side = parse_fen(fen)
    grid = [["" for _ in range(GRID_COLS)] for _ in range(GRID_ROWS)]
    for (file_idx, rank), piece in board.items():
        row = 9 - rank if grid_orientation == "red_bottom" else rank
        grid[row][file_idx] = piece
    return grid, side


def _file_name(file_idx, is_red):
    """纵线索引 -> 记谱用的纵线编号

    红方: 从红方视角由右向左为一~九, 即 i=一, h=二, ..., a=九
    黑方: 从黑方视角由右向左为 1~9, 即 a=1, b=2, ..., i=9
    """
    if is_red:
        return RED_DIGITS[8 - file_idx]
    return BLACK_DIGITS[file_idx]


def _step_name(num, is_red):
    """步数 -> 记谱数字"""
    return (RED_DIGITS if is_red else BLACK_DIGITS)[num - 1]


def _piece_name(piece):
    """棋子字符 -> 中文名"""
    if piece.isupper():
        return RED_PIECE_NAMES.get(piece, "?")
    return BLACK_PIECE_NAMES.get(piece, "?")


def move_to_chinese(board, move):
    """ICCS 坐标着法 -> 中文着法, 例如 h2e2 -> 炮二平五

    :param board: 该着法走出之前的棋盘 dict, 函数会原地更新棋盘,
                  以便连续转换整条 PV 变例。
    :param move: 形如 "h2e2" 的四字符坐标着法
    """
    if len(move) < 4 or move[0] not in FILES or move[2] not in FILES:
        return move
    try:
        f_from, r_from = FILES.index(move[0]), int(move[1])
        f_to, r_to = FILES.index(move[2]), int(move[3])
    except ValueError:
        return move

    piece = board.get((f_from, r_from))
    if piece is None:
        return move

    is_red = piece.isupper()
    name = _piece_name(piece)

    # 同一纵线上有两枚同种棋子时, 用 前/后 代替纵线编号
    same_file = sorted(
        (r for (f, r), p in board.items() if f == f_from and p == piece),
        reverse=is_red,  # 红方横线号大的是"前", 黑方横线号小的是"前"
    )
    if len(same_file) == 2:
        # 前/后 写在棋子名之前, 例如 "前车进一"
        head = ("前" if same_file[0] == r_from else "后") + name
    else:
        head = name + _file_name(f_from, is_red)

    if r_from == r_to:
        action = "平" + _file_name(f_to, is_red)
    else:
        advance = r_to > r_from if is_red else r_to < r_from
        action = "进" if advance else "退"
        if piece.upper() in DIAGONAL_PIECES:
            action += _file_name(f_to, is_red)   # 马/相(象)/仕(士) 记目标纵线
        else:
            action += _step_name(abs(r_to - r_from), is_red)  # 车/炮/兵/将 记步数

    # 应用着法, 供后续着法继续转换
    del board[(f_from, r_from)]
    board[(f_to, r_to)] = piece
    return f"{head}{action}"


def iccs_to_chinese(iccs_move, fen):
    """ICCS 着法 -> 传统中文着法 (基于当前 FEN 里的局面)

    >>> iccs_to_chinese("h2e2", "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1")
    '炮二平五'
    """
    board, _ = parse_fen(fen)
    move = (iccs_move or "").strip()
    if len(move) < 4:
        return move
    return move_to_chinese(board, move[:4])


def pv_to_chinese(pv_iccs, fen):
    """整条 PV 变例 -> 中文着法列表 (依次落子, 所以能正确命名后续着法)"""
    board, _ = parse_fen(fen)
    out = []
    for mv in pv_iccs or []:
        mv = (mv or "").strip()
        out.append(move_to_chinese(board, mv[:4]) if len(mv) >= 4 else mv)
    return out
