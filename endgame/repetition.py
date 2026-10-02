# -*- coding: utf-8 -*-
"""重复局面 / 长将 / 长捉信号检测。

这一模块的输出**只是信号**, 不是裁决。原因是中国象棋的棋例判定本身需要人来定:
"长将"要看是不是每着都在将、"长捉"要看被捉的子是不是真无根、一将一杀要判断
杀势是否成立, 还要区分"允许的"和"禁止的"。三次重复在象棋规则里也**不是**自动判和
(跟国际象棋不同), 必须由裁判按"棋例"裁决。所以这里:
    * 绝不返回 win/loss/draw 的胜负结论;
    * 只回答"有没有重复""这一轮里双方都在干什么", 并统一把
      needs_rule_review / rule_review_status='pending' 挂上, 交给人工复核。

已实现的信号 (都有明确简化, 见各函数注释):
    threefold_candidate  最近 window 半着内, 当前局面出现过 3 次
    repeat_segment       这一轮重复覆盖的 ply 区间
    perpetual_check      某一方在这一轮里的每一着都是"将"
    perpetual_chase      某一方在这一轮里的每一着都是"捉"
    check_chase_mix      某一方在这一轮里"将"和"捉"混着来 (一将一捉 / 一将一杀候选)

长将/长捉按"循环区间内该方所有着法"统计, 而不是只看最后一着 ——
只看最后一着会把"兑子后随便将一下"误判成长将。
"""

from core import rules
from core.coord_utils import GRID_COLS, GRID_ROWS, grid_to_square

from . import fen_rules

# 重复检测保留的半着窗口: 三次重复最多需要 8 个半着 (每重复一次要多走 4 个半着),
# 留 16 半着 = 8 个整回合, 足够看清"是不是在来回倒腾"。
DEFAULT_WINDOW = 16

# 判断"捉"时算数的被捉子: 车/马/炮 与过了河的兵卒。
# 未过河的兵/卒价值太低, 规则里也不作为被捉的对象。
CHASE_KINDS = ("R", "N", "C", "P")

# storage 里 moves.side 存的是 'red'/'black', 本包内部一律 'w'/'b'
SIDE_ALIASES = {"red": "w", "w": "w", "black": "b", "b": "b"}


def _other(side):
    return "b" if side == "w" else "w"


def _side_from_fen(fen):
    """FEN 第二个字段 = 下一步该谁走"""
    parts = (fen or "").split()
    return "b" if len(parts) > 1 and parts[1][:1].lower() == "b" else "w"


def _crossed_river(row, red):
    """红方过河 = 行号 <= 4; 黑方过河 = 行号 >= 5"""
    return row <= 4 if red else row >= 5


def _attacked_by(grid, tr, tc, red):
    """(tr,tc) 是否被 red 方的棋子攻击, 返回攻击者的 (row, col) 列表

    先把目标格清空再看着法 —— 否则 pseudo_moves 会因为"不能吃自己的子"
    而看不见"己方棋子保护己方棋子", 有根子会被误判成无根。
    """
    probe = rules.copy_grid(grid)
    probe[tr][tc] = ""
    out = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            piece = probe[r][c]
            if not piece or rules.is_red(piece) != red:
                continue
            if (tr, tc) in rules.pseudo_moves(probe, r, c):
                out.append((r, c))
    return out


def chase_targets(grid, side):
    """side 方"正在威胁吃"的对方棋子 (捉的候选)

    判定: 对方某枚车/马/炮(或已过河的兵卒)被己方攻击, 且它没有被对方保护。
    简化说明: 真实棋例还要求"被捉的子跑不掉""捉子不为得子"等, 这里不判,
    所以结果一律当候选, 交人工复核。
    """
    red = side == "w"
    enemy_red = not red
    out = []
    for r in range(GRID_ROWS):
        for c in range(GRID_COLS):
            piece = grid[r][c]
            if not piece or rules.is_red(piece) != enemy_red:
                continue
            kind = piece.upper()
            if kind not in CHASE_KINDS:
                continue                      # 将/帅被攻击叫"将", 不叫"捉"
            if kind == "P" and not _crossed_river(r, enemy_red):
                continue                      # 未过河的兵卒不算被捉的子
            if not _attacked_by(grid, r, c, red):
                continue
            if _attacked_by(grid, r, c, enemy_red):
                continue                      # 有根子不算捉
            out.append({"square": grid_to_square(r, c), "piece": piece})
    return out


def move_signals(fen_before, iccs):
    """一步棋的将/捉信号

    :return: {"check": bool, "chase": bool, "chase_targets": [...]}
             着法不合法抛 rules.RuleError
    将优先于捉: 一着同时将军又捉子的按"将"归类 (规则里将的效力更高),
    否则"一将一捉"这种形态会被算成"捉了两次"。
    """
    grid, side = rules.fen_to_board(fen_before)
    info = rules.validate_move(grid, iccs, side=side)
    after, _captured = rules.apply_move(grid, info["from"][0], info["from"][1],
                                        info["to"][0], info["to"][1])

    if rules.in_check(after, _other(side)):
        return {"check": True, "chase": False, "chase_targets": []}

    before = {t["square"] for t in chase_targets(grid, side)}
    fresh = [t for t in chase_targets(after, side) if t["square"] not in before]
    return {"check": False, "chase": bool(fresh), "chase_targets": fresh}


def normalize_records(moves):
    """把 storage 里的 moves 行整理成检测需要的形状

    :param moves: [{ply, side, iccs, fen_before, fen_after}, ...] 按 ply 升序
    :return: [{"ply", "side", "iccs", "fen_before", "fen_after"}, ...]
             缺 iccs / fen_before / fen_after 的行直接跳过 (读不出局面就无从判断)
    """
    out = []
    for index, move in enumerate(moves or []):
        iccs = (move.get("iccs") or "").strip()
        fen_before = (move.get("fen_before") or "").strip()
        fen_after = (move.get("fen_after") or "").strip()
        if not iccs or not fen_before or not fen_after:
            continue
        try:
            ply = int(move.get("ply"))
        except (TypeError, ValueError):
            ply = index + 1
        # 走子方优先从 fen_after 反推(fen_after 里的走子方是"下一步该谁", 取反即可),
        # 反推不出来再用 side 列, 两处都对不上时跳过
        side = SIDE_ALIASES.get((move.get("side") or "").strip().lower())
        if fen_after.split():
            side = _other(_side_from_fen(fen_after))
        if side not in ("w", "b"):
            continue
        out.append({"ply": ply, "side": side, "iccs": iccs,
                    "fen_before": fen_before, "fen_after": fen_after})
    return out


def analyze_repetition(moves, window=DEFAULT_WINDOW):
    """检测重复局面与长将/长捉信号

    :param moves: storage 风格的着法列表 (见 normalize_records)
    :param window: 回看的半着数, 默认 16
    :return: {
        "window": int,
        "positions_seen": int,
        "threefold_candidate": bool,
        "repeat_segment": {"from_ply", "to_ply", "occurrences"} | None,
        "perpetual_check": bool,
        "perpetual_chase": bool,
        "check_chase_mix": bool,
        "sides": {"w": {"moves","checks","chases"}, "b": {...}},
        "needs_rule_review": bool,
        "rule_review_status": "none"|"pending",
        "draw_type": "repeat"|"none",
        "notes": [str, ...],
    }
    """
    records = normalize_records(moves)
    out = {
        "window": int(window),
        "positions_seen": 0,
        "threefold_candidate": False,
        "repeat_segment": None,
        "perpetual_check": False,
        "perpetual_chase": False,
        "check_chase_mix": False,
        "sides": {"w": {"moves": 0, "checks": 0, "chases": 0},
                  "b": {"moves": 0, "checks": 0, "chases": 0}},
        "needs_rule_review": False,
        "rule_review_status": "none",
        "draw_type": "none",
        "notes": [],
    }
    if not records:
        return out

    # 局面序列: 第 k 项 = 走完前 k 个半着之后的局面, 第 0 项是首着的起始局面
    keys = [fen_rules.position_key(records[0]["fen_before"])]
    keys += [fen_rules.position_key(record["fen_after"]) for record in records]

    start = max(0, len(keys) - 1 - int(window))
    seen = keys[start:]
    out["positions_seen"] = len(seen)
    if not seen or seen[-1] is None:
        return out

    current = seen[-1]
    hits = [i for i, key in enumerate(seen) if key == current]
    if len(hits) < 3:
        return out

    # 局面序列下标 i 对应"半着 i 走完之后"; 半着编号从 1 开始, 所以 i == ply
    from_ply = start + hits[0]
    to_ply = start + hits[-1]
    out["threefold_candidate"] = True
    out["draw_type"] = "repeat"
    out["repeat_segment"] = {"from_ply": from_ply, "to_ply": to_ply,
                             "occurrences": len(hits)}

    # 只统计循环区间内的着法 (半着 from_ply+1 .. to_ply, 即 records 下标 from_ply..to_ply-1)
    for index in range(from_ply, to_ply):
        record = records[index]
        try:
            signals = move_signals(record["fen_before"], record["iccs"])
        except rules.RuleError as e:
            out["notes"].append(f"第 {record['ply']} 着无法判定将/捉: {e}")
            continue
        bucket = out["sides"][record["side"]]
        bucket["moves"] += 1
        if signals["check"]:
            bucket["checks"] += 1
        elif signals["chase"]:
            bucket["chases"] += 1

    for side in ("w", "b"):
        bucket = out["sides"][side]
        if bucket["moves"] < 2:
            continue
        if bucket["checks"] == bucket["moves"]:
            out["perpetual_check"] = True
        if bucket["chases"] == bucket["moves"]:
            out["perpetual_chase"] = True
        if bucket["checks"] and bucket["chases"]:
            out["check_chase_mix"] = True

    if out["perpetual_check"] or out["perpetual_chase"] or out["check_chase_mix"]:
        out["notes"].append("检测到长将/长捉/一将一捉形态: 三次重复不能自动判和或判负, "
                            "必须按棋例人工裁决")
    else:
        out["notes"].append("出现三次重复局面, 但没检测到长将/长捉形态; "
                            "是否成和仍需人工确认")

    # 只要出现重复就进人工复核: 象棋的重复局面从来不是自动判和的
    out["needs_rule_review"] = True
    out["rule_review_status"] = "pending"
    return out
