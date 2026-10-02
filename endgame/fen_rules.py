# -*- coding: utf-8 -*-
"""残局研究的严格 FEN 校验、局面规范化与摆盘操作重放。

跟 rules.validate_fen() 的分工:
    rules.validate_fen() 是"整局棋"用的宽松结构校验 —— 10 行 / 每行 9 条纵线 /
    字符认不认识, 缺将、士象站错位都放行 (因为整局复盘只要 FEN 能读出来就行)。
    本模块是"摆盘"用的严校验 —— 摆出来的局面必须真的能当成一局棋继续走。

为什么返回问题清单而不是抛异常:
    前端要分三档展示 (legal / errors / warnings), 抛异常表达不了 warnings,
    也没法一次把所有问题都报出来 (用户不想改一个报一个)。

三档含义:
    errors   该局面不可能出现在任何合法对局里 —— 不能保存为 published
    warnings 能摆能走, 但值得提醒 (当前方正被将军、已被将死、FEN 缺回合字段)
    legal    errors 为空

坐标一律用 ICCS 单格 (a0~i9), 行列换算只调 coord_utils, 本模块不自己算。
"""

import hashlib

import rules
from coord_utils import GRID_COLS, GRID_ROWS, grid_to_square, square_to_grid

# ----------------------------------------------------------------------
# 常量表
# ----------------------------------------------------------------------
SIDES = ("w", "b")

# 各子最多能有多少枚 (帅/将单独按"必须恰好 1 枚"处理)
PIECE_LIMITS = {"A": 2, "B": 2, "N": 2, "R": 2, "C": 2, "P": 5}

# 九宫: 红方在下三行(行 7~9), 黑方在上三行(行 0~2), 都只占中间三条纵线
PALACE_ROWS = {"w": (7, 8, 9), "b": (0, 1, 2)}

# 仕/士只可能落在这 5 个点 (九宫的四个角 + 中心)
ADVISOR_POINTS = {
    "w": {(7, 3), (7, 5), (8, 4), (9, 3), (9, 5)},
    "b": {(0, 3), (0, 5), (1, 4), (2, 3), (2, 5)},
}

# 相/象只可能落在这 7 个点 (不过河, 且走"田"字), 红方在下半, 黑方在上半
ELEPHANT_POINTS = {
    "w": {(5, 2), (5, 6), (7, 0), (7, 4), (7, 8), (9, 2), (9, 6)},
    "b": {(0, 2), (0, 6), (2, 0), (2, 4), (2, 8), (4, 2), (4, 6)},
}

# 兵/卒不可能出现的位置: 兵只进不退, 红兵起步于行 6 往上走, 卒起步于行 3 往下走
FORBIDDEN_PAWN_ROWS = {"w": (7, 8, 9), "b": (0, 1, 2)}

PIECE_NAMES = {"K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵",
               "k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒"}

# 摆盘操作支持的 op 类型
OPERATION_KINDS = ("clear", "set", "remove", "move", "load")


def _issue(code, message, **detail):
    """一条问题: 错误码 + 中文说明 + 结构化细节 (前端要按 code 做分支)"""
    return {"code": code, "message": message, "detail": detail}


def _piece_side(piece):
    """棋子字符 -> 'w'/'b' (大写为红)"""
    return "w" if piece.isupper() else "b"


# ----------------------------------------------------------------------
# 结构扫描 (语义检查之前先跑, 结构不过就没法建棋盘)
# ----------------------------------------------------------------------
def _scan_structure(board_text):
    """扫 FEN 的棋盘部分, 返回 (rows, errors); rows 为每行的展开后长度校验结果

    只做"能不能解析出 10x9 的盘面"这一层: 行数、每行展开是否 9 列、字符是否合法。
    """
    errors = []
    rows = (board_text or "").split("/")
    if len(rows) != 10:
        errors.append(_issue("FEN_ROW_COUNT",
                             f"FEN 棋盘部分应为 10 行(层), 实际 {len(rows)} 行",
                             rows=len(rows)))
        return rows, errors

    for i, row in enumerate(rows):
        width = 0
        for ch in row:
            if ch == "0":
                errors.append(_issue("FEN_BAD_CHAR",
                                     f"FEN 第 {i + 1} 行出现了非法字符 '0'"
                                     "(空格要用 1~9 的数字表示, 不能用 0)",
                                     row=i + 1))
            elif ch.isdigit():
                width += int(ch)
            elif ch in rules.PIECE_CHARS:
                width += 1
            else:
                errors.append(_issue("FEN_BAD_CHAR",
                                     f"FEN 第 {i + 1} 行有无法识别的字符: {ch!r}",
                                     row=i + 1, char=ch))
        if width != GRID_COLS:
            errors.append(_issue("FEN_COLUMN_COUNT",
                                 f"FEN 第 {i + 1} 行展开后应为 {GRID_COLS} 条纵线, "
                                 f"实际 {width} 条",
                                 row=i + 1, columns=width))
    return rows, errors


def _count_pieces(grid):
    """按棋子字符统计数量, 返回 {piece_char: n} (只含出现过的)"""
    counts = {}
    for row in grid:
        for piece in row:
            if piece:
                counts[piece] = counts.get(piece, 0) + 1
    return counts


def _check_pieces(grid, counts, errors):
    """帅将各 1 枚 + 士象马炮车兵卒数量上限 + 各子在不在它可能出现的点上"""
    for king, who in (("K", "红方"), ("k", "黑方")):
        n = counts.get(king, 0)
        if n == 0:
            errors.append(_issue("KING_MISSING", f"{who}没有将/帅", piece=king))
        elif n > 1:
            errors.append(_issue("KING_DUPLICATE",
                                 f"{who}有 {n} 枚将/帅, 应该只有 1 枚", piece=king, count=n))

    for upper, limit in PIECE_LIMITS.items():
        for piece in (upper, upper.lower()):
            n = counts.get(piece, 0)
            if n > limit:
                errors.append(_issue("PIECE_TOO_MANY",
                                     f"{PIECE_NAMES[piece]}最多 {limit} 枚, 实际 {n} 枚",
                                     piece=piece, count=n, limit=limit))

    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            piece = grid[r][c]
            if not piece:
                continue
            side = _piece_side(piece)
            kind = piece.upper()
            square = grid_to_square(r, c)

            if kind == "K":
                if c not in (3, 4, 5) or r not in PALACE_ROWS[side]:
                    errors.append(_issue("KING_OUTSIDE_PALACE",
                                         f"{PIECE_NAMES[piece]}在 {square}, 不在九宫内",
                                         piece=piece, square=square))
            elif kind == "A":
                if (r, c) not in ADVISOR_POINTS[side]:
                    errors.append(_issue("ADVISOR_OUTSIDE_PALACE",
                                         f"{PIECE_NAMES[piece]}在 {square}, "
                                         "不在九宫的士位上", piece=piece, square=square))
            elif kind == "B":
                if (r, c) not in ELEPHANT_POINTS[side]:
                    errors.append(_issue("ELEPHANT_OUTSIDE_POINTS",
                                         f"{PIECE_NAMES[piece]}在 {square}, "
                                         "不是它可能落到的象位(不能过河)",
                                         piece=piece, square=square))
            elif kind == "P":
                if r in FORBIDDEN_PAWN_ROWS[side]:
                    errors.append(_issue("PAWN_ON_OWN_BACK_RANKS",
                                         f"{PIECE_NAMES[piece]}在 {square}, "
                                         "但兵/卒只进不退, 不可能退到本方底线",
                                         piece=piece, square=square))


def _kings_face_each_other(grid):
    """白脸将: 两个将/帅在同一条纵线上, 中间没有任何棋子"""
    red = rules.find_king(grid, True)
    black = rules.find_king(grid, False)
    if not red or not black or red[1] != black[1]:
        return False
    col = red[1]
    lo, hi = sorted((red[0], black[0]))
    return all(not grid[r][col] for r in range(lo + 1, hi))


# ----------------------------------------------------------------------
# 宽松解析 (摆盘过程中间态用)
# ----------------------------------------------------------------------
def parse_board(fen, to_move=None):
    """只把 FEN 读成盘面, 不做棋规检查

    摆盘是一步步改的, 中间态(比如刚清空还没摆将)本来就不合法, 所以"重放操作的
    起点"和"最终能不能存"必须用两套标准: 这里管前者。
    返回 {"ok": bool, "grid": 10x9, "side": "w"/"b", "errors": [...]}
    """
    text = (fen or "").strip()
    if not text:
        return {"ok": False, "grid": None, "side": None,
                "errors": [_issue("FEN_EMPTY", "FEN 不能为空")]}

    parts = text.split()
    _rows, errs = _scan_structure(parts[0])
    if errs:
        return {"ok": False, "grid": None, "side": None, "errors": errs}

    if to_move is not None:
        side = str(to_move).strip().lower()[:1]
    elif len(parts) > 1:
        side = parts[1].strip().lower()[:1]
    else:
        side = "w"
    if side not in SIDES:
        return {"ok": False, "grid": None, "side": None,
                "errors": [_issue("SIDE_INVALID", f"走子方只能是 w/b, 实际 {side!r}")]}

    try:
        grid, _ = rules.fen_to_board(parts[0])
    except rules.RuleError as e:
        return {"ok": False, "grid": None, "side": None,
                "errors": [_issue("FEN_UNPARSABLE", str(e))]}
    return {"ok": True, "grid": grid, "side": side, "errors": []}


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def validate_position(fen, to_move=None):
    """严格校验一个摆设局面

    :param fen: 待校验的 FEN (可以只有棋盘部分, 走子方缺省视为红先)
    :param to_move: 显式指定走子方 ('w'/'b'), 给了就以它为准 —— 用户在界面上
                    手动"设置轮次"时走这条路径, 不需要去改 FEN 字符串
    :return: {
        "legal": bool,                  # errors 为空
        "errors": [{code, message, detail}],
        "warnings": [{code, message, detail}],
        "fen": str|None,                # 规范化后的 FEN(errors 非空时为 None)
        "side_to_move": "w"|"b"|None,
        "piece_count": int|None,
        "position_key": str|None,       # 局面规范键(棋盘 + 走子方), 不含回合数
        "position_hash": str|None,      # position_key 的 sha1, 落库用
        "in_check": bool,
        "has_legal_move": bool,
    }
    """
    errors, warnings = [], []
    out = {"legal": False, "errors": errors, "warnings": warnings, "fen": None,
           "side_to_move": None, "piece_count": None, "position_key": None,
           "position_hash": None, "in_check": False, "has_legal_move": False}

    text = (fen or "").strip()
    if not text:
        errors.append(_issue("FEN_EMPTY", "FEN 不能为空"))
        return out

    parts = text.split()
    rows_seen, errors_structure = _scan_structure(parts[0])
    errors.extend(errors_structure)
    if errors_structure:
        return out

    # ---- 走子方: 显式参数 > FEN 第二个字段 > 缺省红先 ----
    if to_move is not None:
        side = str(to_move).strip().lower()[:1]
        if side not in SIDES:
            errors.append(_issue("SIDE_INVALID",
                                 f"走子方只能是 w(红) 或 b(黑), 实际 {to_move!r}",
                                 side=to_move))
            return out
    elif len(parts) > 1:
        side = parts[1].strip().lower()[:1]
        if side not in SIDES:
            errors.append(_issue("SIDE_INVALID",
                                 f"FEN 的走子方字段只能是 w/b, 实际 {parts[1]!r}",
                                 side=parts[1]))
            return out
    else:
        side = "w"
        warnings.append(_issue("SIDE_FIELD_MISSING",
                               "FEN 没有走子方字段, 已按红先处理"))
    out["side_to_move"] = side

    if len(parts) < 6:
        warnings.append(_issue("FEN_FIELDS_TRUNCATED",
                               "FEN 缺少回合计数字段, 已按 0/1 补齐"))

    # ---- 建棋盘 (结构已过, 这里只可能因为内部不一致而失败) ----
    try:
        grid, _fen_side = rules.fen_to_board(parts[0])
    except rules.RuleError as e:
        errors.append(_issue("FEN_UNPARSABLE", str(e)))
        return out

    counts = _count_pieces(grid)
    out["piece_count"] = sum(counts.values())
    _check_pieces(grid, counts, errors)

    kings_ok = counts.get("K", 0) == 1 and counts.get("k", 0) == 1

    if kings_ok:
        if _kings_face_each_other(grid):
            errors.append(_issue("KINGS_FACING",
                                 "白脸将: 两个将/帅照面且中间没有棋子, 这个局面不合法"))
        # 轮到走子的一方可以被将军; 但"没轮到走的那一方"被将军是走不出来的
        other_red = side == "b"
        if rules.king_attacked(grid, other_red):
            errors.append(_issue("SIDE_NOT_TO_MOVE_IN_CHECK",
                                 f"没轮到的{'红' if other_red else '黑'}方正被将军, "
                                 "这个局面走不出来"))

    out["in_check"] = rules.in_check(grid, side) if kings_ok else False
    if out["in_check"]:
        warnings.append(_issue("SIDE_TO_MOVE_IN_CHECK", "当前走子方正被将军"))

    if kings_ok:
        has_move = _has_legal_move(grid, side == "w")
        out["has_legal_move"] = has_move
        if not has_move:
            if out["in_check"]:
                warnings.append(_issue("CHECKMATED", "当前走子方已被将死, 无法继续走棋"))
            else:
                errors.append(_issue("NO_LEGAL_MOVE",
                                     "当前走子方无着可走(困毙), 这个局面不能作为研究起点"))

    if errors:
        return out

    out["fen"] = rules.board_to_fen(grid, side)
    key = _position_key(grid, side)
    out["position_key"] = key
    out["position_hash"] = _hash_key(key)
    out["legal"] = True
    return out


def _has_legal_move(grid, red):
    """red 方是否至少有一走着法 (找到第一着就返回, 不必生成全部)

    大子局面下生成全部着法要几百毫秒, 而校验只关心"有没有", 所以这里不调
    rules.legal_moves()。
    """
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            piece = grid[r][c]
            if not piece or rules.is_red(piece) != red:
                continue
            for tr, tc in rules.pseudo_moves(grid, r, c):
                if rules.is_legal_move(grid, r, c, tr, tc):
                    return True
    return False


# ----------------------------------------------------------------------
# 局面规范化 / 哈希
# ----------------------------------------------------------------------
def _position_key(grid, side):
    """局面规范键: 棋盘 + 走子方, 不含"走了几回合"—— 重复局面判定只看这两项"""
    return f"{rules.board_to_fen(grid, side).split()[0]} {side}"


def _hash_key(key):
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def position_key(fen, to_move=None):
    """FEN -> 局面规范键; FEN 结构都读不出来时返回 None (不抛异常, 便于在循环里用)

    只按"盘面 + 走子方"算, 不做棋规校验 —— 重复检测要连着算几十个局面,
    这里必须便宜。真实对局里的每个局面本身都是合法的, 不需要再验一遍。
    """
    parsed = parse_board(fen, to_move=to_move)
    if not parsed["ok"]:
        return None
    return _position_key(parsed["grid"], parsed["side"])


def position_hash(fen, to_move=None):
    """FEN -> 局面哈希 (sha1 十六进制, 落 endgame_positions.position_hash)"""
    key = position_key(fen, to_move=to_move)
    return _hash_key(key) if key else None


# ----------------------------------------------------------------------
# 摆盘操作重放
# ----------------------------------------------------------------------
def _op_issue(code, message, index, **detail):
    return _issue(code, message, operation=index, **detail)


def apply_operations(fen, operations, to_move=None, clear=False):
    """在 fen 的基础上依次执行 operations, 返回 {"fen":..., "errors":[...], "applied": n}

    支持的操作 (square 一律 ICCS 单格, 如 "h2"):
        {"op": "clear"}                              清空棋盘
        {"op": "set",    "square": "a0", "piece": "R"}  放一枚棋子
        {"op": "remove", "square": "a0"}              拿掉一枚棋子
        {"op": "move",   "from": "a0", "to": "a1"}    拖放: 把 from 的棋子挪到 to
        {"op": "load",   "fen": "..."}                整盘替换(连走子方一起换)

    这里只做"操作本身能不能执行"的检查(格子名对不对、棋符认不认识、起点有没有子),
    不做棋规校验 —— 摆盘过程中间态本来就不一定合法。最终能不能保存由
    validate_position() 决定, 那一道才是硬门槛。

    拖放落在已有棋子上时会覆盖, 并用 warnings 提示"被顶掉的是什么"。
    """
    errors, warnings = [], []
    result = {"fen": None, "errors": errors, "warnings": warnings, "applied": 0,
              "side_to_move": None}

    base = parse_board(fen, to_move=to_move)
    if not base["ok"]:
        first = base["errors"][0]
        errors.append(_issue("BASE_FEN_INVALID",
                             f"起始局面不合法: {first['message']}",
                             errors=base["errors"]))
        return result
    grid, side = base["grid"], base["side"]

    if operations is None:
        operations = []
    if not isinstance(operations, (list, tuple)):
        errors.append(_issue("OPERATIONS_NOT_LIST", "operations 必须是数组"))
        return result

    if clear:
        grid = [["" for _ in range(GRID_COLS)] for _ in range(GRID_ROWS)]

    def row_col(square, index, field):
        try:
            return square_to_grid(square)
        except ValueError as e:
            errors.append(_op_issue("OP_BAD_SQUARE", f"第 {index} 个操作: {e}", index,
                                    field=field, square=square))
            return None

    for index, op in enumerate(operations, 1):
        if not isinstance(op, dict):
            errors.append(_op_issue("OP_NOT_OBJECT", f"第 {index} 个操作不是对象", index))
            continue
        kind = str(op.get("op") or "").strip().lower()
        if kind not in OPERATION_KINDS:
            errors.append(_op_issue("OP_UNKNOWN",
                                    f"第 {index} 个操作的 op 只能是 "
                                    f"{'/'.join(OPERATION_KINDS)}, 实际 {op.get('op')!r}",
                                    index))
            continue

        if kind == "clear":
            grid = [["" for _ in range(GRID_COLS)] for _ in range(GRID_ROWS)]

        elif kind == "load":
            loaded = parse_board(op.get("fen"))
            if not loaded["ok"]:
                errors.append(_op_issue("OP_BAD_FEN",
                                        f"第 {index} 个操作的 fen 不合法: "
                                        f"{loaded['errors'][0]['message']}",
                                        index, errors=loaded["errors"]))
                continue
            grid, side = loaded["grid"], loaded["side"]

        elif kind == "set":
            at = row_col(op.get("square"), index, "square")
            piece = op.get("piece")
            if at is None:
                continue
            if not isinstance(piece, str) or len(piece) != 1 or piece not in rules.PIECE_CHARS:
                errors.append(_op_issue("OP_BAD_PIECE",
                                        f"第 {index} 个操作的 piece 必须是 "
                                        "KABNRCP/kabnrcp 之一, 实际 "
                                        f"{op.get('piece')!r}", index, piece=op.get("piece")))
                continue
            grid[at[0]][at[1]] = piece

        elif kind == "remove":
            at = row_col(op.get("square"), index, "square")
            if at is None:
                continue
            grid[at[0]][at[1]] = ""

        else:  # move
            src = row_col(op.get("from"), index, "from")
            dst = row_col(op.get("to"), index, "to")
            if src is None or dst is None:
                continue
            if src == dst:
                continue
            piece = grid[src[0]][src[1]]
            if not piece:
                errors.append(_op_issue("OP_EMPTY_SOURCE",
                                        f"第 {index} 个操作: 起点 "
                                        f"{grid_to_square(*src)} 上没有棋子", index,
                                        square=grid_to_square(*src)))
                continue
            displaced = grid[dst[0]][dst[1]]
            if displaced:
                warnings.append(_op_issue("OP_OVERWRITE",
                                          f"第 {index} 个操作: 目标点 "
                                          f"{grid_to_square(*dst)} 上的"
                                          f"{PIECE_NAMES.get(displaced, displaced)}被覆盖",
                                          index, piece=displaced))
            grid[dst[0]][dst[1]] = piece
            grid[src[0]][src[1]] = ""
        result["applied"] += 1

    if errors:
        return result

    result["fen"] = rules.board_to_fen(grid, side)
    result["side_to_move"] = side
    return result
