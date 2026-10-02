# -*- coding: utf-8 -*-
"""残局研究的引擎适配层: 把 chess_engine.search() 包成"可以直接落库的分析结果"。

职责:
    * 配置   —— 三档预设 (quick / standard / precise) 与受控变着 (root_moves) 的
                参数解析 + 下限校验 (standard 就是 depth>=30 / movetime>=60000,
                不允许被悄悄调低, 否则两批结果没有可比性);
    * 调用   —— 拼 UCI position / go 命令 (含 searchmoves), 收集 info, 支持中途取消;
    * 判定   —— mate>0 写 win, mate<0 写 loss; cp 只有在"够大 **且** 主变稳定"时才
                敢写 win/loss, 否则一律 unknown; 引擎给的 0 分/重复只当"和棋候选",
                挂 needs_rule_review 交人工复核;
    * 校验   —— 每条引擎着法落库前都要过 rules.validate_move, 过不了的不进候选。

不做:
    * 不碰数据库 (endgame/store.py 负责);
    * 不判棋例 —— 长将/长捉/三次重复由 endgame/repetition.py 出信号 + 人工裁决;
    * 不自作主张改引擎选项 —— 规格要的 Repetition Rule / Mate Threat Depth 等
      在 Pikafish 2026-09-06 上并不存在, set_option() 认不出来就跳过并如实回报。

坐标一律用 ICCS (UCI-Cyclone): 文件 a~i, 横线 0~9, 0 是红方底线;
中文记谱 (san) 只用于展示。
"""

from core import rules
from core.chess_engine import START_FEN
from core.coord_utils import pv_to_chinese

from . import fen_rules
from . import repetition as repetition_mod

# ----------------------------------------------------------------------
# 分析档位
# ----------------------------------------------------------------------
# 下限的含义: standard/precise 是"精确分析", 结果要能互相比较, 所以不允许调低;
# quick 是给人边摆边看的, 故意允许更浅更快。
# root_moves 是"受控变着": 用 searchmoves 钉住根节点某一着, MultiPV 必须是 1。
PRESETS = {
    "quick": {
        "depth": 24, "movetime_ms": 10000, "multipv": 3, "confirm_bestmove": False,
        "min_depth": 1, "min_movetime_ms": 1, "min_multipv": 1, "exact_multipv": None,
    },
    "standard": {
        "depth": 30, "movetime_ms": 60000, "multipv": 3, "confirm_bestmove": True,
        "min_depth": 30, "min_movetime_ms": 60000, "min_multipv": 3, "exact_multipv": None,
    },
    "precise": {
        "depth": 40, "movetime_ms": 180000, "multipv": 3, "confirm_bestmove": True,
        "min_depth": 40, "min_movetime_ms": 180000, "min_multipv": 3, "exact_multipv": None,
    },
    "root_moves": {
        "depth": 30, "movetime_ms": 60000, "multipv": 1, "confirm_bestmove": False,
        "min_depth": 30, "min_movetime_ms": 60000, "min_multipv": 1, "exact_multipv": 1,
    },
}
MODES = tuple(PRESETS)

# 整体超时 = movetime 的 120% (规格要求), 再加 5s 兜底, 免得刚好卡在边界上误判超时
TIMEOUT_RATIO = 1.2
TIMEOUT_SLACK_MS = 5000

# ----------------------------------------------------------------------
# 结论判定参数
# ----------------------------------------------------------------------
# cp 要大到什么程度才敢说赢/输。250cp 约等于"多半子且形势好", 再低就说不清了。
CP_WIN_THRESHOLD = 250
CP_LOSS_THRESHOLD = -250
# 主变稳定的判定: 只看最深那几层, 最佳着法必须一致、分数波动不能超过这个容差
CP_STABILITY_TOLERANCE = 50
STABILITY_DEPTH_SLACK = 2
MIN_STABLE_SAMPLES = 3
# 引擎评估在这个带子里 + 有重复信号时, 才把"和棋"当候选提出来
CP_DRAW_BAND = 30
# 胜率换算的刻度 (Elo 意义上的 400 分 = 10 倍胜率)
WINRATE_SCALE = 400.0

# 校验变例时最多走几步 (PV 可能很长, 全验没必要还拖慢响应)
PV_VALIDATE_PLIES = 12

# ----------------------------------------------------------------------
# 规则开关
# ----------------------------------------------------------------------
RULE_OPTION_DEFAULTS = {
    # 分析用的规则集名字, 会写进 endgame_positions.repetition_rule
    "repetition_rule": "chinese_2020_analysis",
    # 杀棋威胁搜索深度: 默认 6, 复杂杀局用 8
    "mate_threat_depth": 6,
    # 是否启用规则层判定 (重复/长将长捉信号 + 人工复核流程)
    "rule_aware": True,
    # 三次重复在引擎侧保持默认, 不显式下发
    "strict_three_fold": True,
    # 60 回合自然限着只允许用户显式开启
    "sixty_move_rule": False,
}

# 规则开关 -> 引擎上的 UCI 选项名。
# 实测 Pikafish 2026-09-06 一个都没有(它的选项只有 Debug Log File / NumaPolicy /
# Threads / Hash / Clear Hash / Ponder / MultiPV / Move Overhead / nodestime /
# UCI_ShowWDL / EvalFile)。缺这些选项不影响正确性: 规则判定由本包承担,
# 引擎只负责"算得准"。
RULE_ENGINE_OPTIONS = {
    "repetition_rule": "Repetition Rule",
    "mate_threat_depth": "Mate Threat Depth",
    "strict_three_fold": "Strict Three Fold",
    "sixty_move_rule": "Sixty Move Rule",
}
# 默认关闭、只有用户显式开启时才下发的开关
ONLY_WHEN_ENABLED = ("sixty_move_rule",)

MATE_THREAT_DEPTHS = (6, 8)


class AnalyzerError(ValueError):
    """配置不成立 / 局面不能分析时抛出 (路由层转 400, 不写库)"""


# ----------------------------------------------------------------------
# 配置解析
# ----------------------------------------------------------------------
def default_timeout_ms(movetime_ms):
    """整体超时 = movetime 的 120% + 5s (规格: 超时必须大于 movetime 的 120%)"""
    return int(int(movetime_ms) * TIMEOUT_RATIO) + TIMEOUT_SLACK_MS


def _clean_moves(value):
    """把 searchmoves 参数整理成去重后的 ICCS 列表 (字符串 / 列表都收)

    坐标本身不合法就直接报错 —— 引擎收到坏着法会 CRITICAL ERROR 退出,
    与其让它崩, 不如在入口拦下来。
    """
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    out, seen = [], set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        try:
            rules.parse_iccs(text)
        except rules.RuleError as e:
            raise AnalyzerError(f"searchmoves 里有不合法的着法 {text!r}: {e}") from e
        move = text[:4]
        if move not in seen:
            seen.add(move)
            out.append(move)
    return out


def resolve_rule_options(rule_options=None):
    """合并规则层开关; 非法值直接报错

    静默纠正是最糟的选择: 事后没人说得清"这盘到底按哪套规则算的"。
    """
    merged = dict(RULE_OPTION_DEFAULTS)
    for key, value in (rule_options or {}).items():
        if key not in merged:
            raise AnalyzerError(f"未知的规则开关: {key!r}, 可选: "
                                f"{', '.join(sorted(merged))}")
        merged[key] = value

    for key in ("strict_three_fold", "sixty_move_rule", "rule_aware"):
        if not isinstance(merged[key], bool):
            raise AnalyzerError(f"规则开关 {key} 必须是布尔值, 实际 {merged[key]!r}")
    if merged["mate_threat_depth"] not in MATE_THREAT_DEPTHS:
        raise AnalyzerError(f"mate_threat_depth 只支持 {list(MATE_THREAT_DEPTHS)} "
                            f"(复杂杀局用 8), 实际 {merged['mate_threat_depth']!r}")
    if not str(merged["repetition_rule"] or "").strip():
        raise AnalyzerError("repetition_rule 不能为空")
    return merged


def resolve_config(mode="standard", *, depth=None, movetime_ms=None, multipv=None,
                   searchmoves=None, rule_options=None, confirm_bestmove=None,
                   timeout_ms=None):
    """解析一次分析的完整配置 (档位预设 + 用户覆盖 + 下限校验)

    :raise AnalyzerError: 未知档位 / 低于下限 / root_moves 没给 searchmoves
    :return: {mode, depth, movetime_ms, multipv, searchmoves, rule_options,
              confirm_bestmove, timeout_ms, preset}
    """
    key = str(mode or "standard").strip().lower()
    if key not in PRESETS:
        raise AnalyzerError(f"未知的分析档位 mode={mode!r}, 可选: {', '.join(MODES)}")
    preset = PRESETS[key]

    eff_depth = int(depth) if depth else preset["depth"]
    eff_time = int(movetime_ms) if movetime_ms else preset["movetime_ms"]
    eff_multi = int(multipv) if multipv else preset["multipv"]

    if eff_depth < preset["min_depth"]:
        raise AnalyzerError(f"{key} 档的 depth 不能低于 {preset['min_depth']}"
                            f"(实际 {eff_depth})")
    if eff_time < preset["min_movetime_ms"]:
        raise AnalyzerError(f"{key} 档的 movetimeMs 不能低于 {preset['min_movetime_ms']}"
                            f"(实际 {eff_time})")
    if eff_multi < preset["min_multipv"]:
        raise AnalyzerError(f"{key} 档的 multipv 不能低于 {preset['min_multipv']}"
                            f"(实际 {eff_multi})")
    if preset["exact_multipv"] is not None and eff_multi != preset["exact_multipv"]:
        raise AnalyzerError(f"{key} 档的 multipv 必须是 {preset['exact_multipv']}"
                            f"(MultiPV=1 只用来确定最佳着法)")

    moves = _clean_moves(searchmoves)
    if key == "root_moves" and not moves:
        raise AnalyzerError("root_moves 档必须给 searchmoves (受控变着要钉住某一着)")

    merged_rules = resolve_rule_options(rule_options)

    confirm = preset["confirm_bestmove"] if confirm_bestmove is None else bool(confirm_bestmove)
    if eff_multi <= 1:
        confirm = False  # 本来就是 MultiPV=1, 不需要再确认一遍

    return {
        "mode": key,
        "depth": eff_depth,
        "movetime_ms": eff_time,
        "multipv": eff_multi,
        "searchmoves": moves,
        "rule_options": merged_rules,
        "confirm_bestmove": confirm,
        "timeout_ms": int(timeout_ms) if timeout_ms else default_timeout_ms(eff_time),
        "preset": {k: preset[k] for k in ("depth", "movetime_ms", "multipv")},
    }


# ----------------------------------------------------------------------
# UCI 命令拼装 (同时用作结果里的"参数快照")
# ----------------------------------------------------------------------
def build_position(*, fen=None, start_fen=None, moves=None):
    """拼 UCI position 命令的参数部分

    规格要求"从起始局面到当前 ply 的完整 UCI moves": 引擎要靠整串走子历史
    才能正确判断重复局面, 所以只要给了起点和着法就走 moves 形式;
    起点就是标准开局时用更短的 "startpos"。

    :raise AnalyzerError: 三种参数组合都给不出来时
    """
    history = [str(mv).strip() for mv in (moves or []) if str(mv or "").strip()]
    base = (start_fen or "").strip()
    current = (fen or "").strip()

    if history:
        head = "startpos" if (not base or base == START_FEN) else f"fen {base}"
        return f"{head} moves {' '.join(history)}"
    if not current:
        raise AnalyzerError("构造 position 失败: fen 与 (start_fen + moves) 至少要给一个")
    return f"fen {current}"


def build_go_command(config, searchmoves=None):
    """拼 go 命令 (与 chess_engine.search() 内部的拼法保持一致)

    单独抽出来是为了留一份"这次到底用什么参数算的"快照, 存进结果里,
    方便事后核对两批结果为什么不一样。
    """
    moves = _clean_moves(searchmoves if searchmoves is not None else config.get("searchmoves"))
    go = ["go", "depth", str(int(config["depth"])),
          "movetime", str(int(config["movetime_ms"]))]
    if moves:
        go.append("searchmoves")
        go.extend(moves)
    return " ".join(go)


def build_move_records(*, fen=None, start_fen=None, moves=None):
    """把"起始局面 + 整串着法"重放成每步前后的局面, 供重复检测使用

    引擎只要 ICCS 着法串, 重复检测要的是每步的 fen_before/fen_after —— 两边需要
    的其实是同一份"从起始局面到当前 ply"的完整历史, 所以在这里重放一次共用。
    顺手核对重放结果和调用方给的 fen 是不是同一个局面: 对不上说明着法串和局面
    不匹配, 引擎会算错东西, 必须拦下来。

    :return: [{"ply", "side", "iccs", "fen_before", "fen_after"}, ...]
    :raise AnalyzerError: 着法串重放不下去, 或重放终点与 fen 不是同一个局面
    """
    history = [str(mv).strip() for mv in (moves or []) if str(mv or "").strip()]
    if not history:
        return []

    base = (start_fen or "").strip() or START_FEN
    grid, side = rules.fen_to_board(base)
    records = []
    for index, move in enumerate(history):
        before = rules.board_to_fen(grid, side)
        try:
            grid, info = rules.apply_iccs(grid, move, side=side)
        except rules.RuleError as e:
            raise AnalyzerError(f"走子历史第 {index + 1} 步 {move} 重放失败: {e}") from e
        records.append({"ply": index + 1, "side": "red" if side == "w" else "black",
                        "iccs": info["iccs"], "fen_before": before,
                        "fen_after": info["fen_after"]})
        side = "b" if side == "w" else "w"

    if fen:
        replayed = fen_rules.position_key(records[-1]["fen_after"])
        expected = fen_rules.position_key(fen)
        if replayed != expected:
            raise AnalyzerError("走子历史重放到终点后的局面与传入的 fen 不一致, "
                                "请检查 moves 与 fen 是不是同一盘棋")
    return records


def apply_rule_engine_options(engine, rule_options):
    """把规则开关翻译成引擎选项; 引擎不认识的跳过并如实回报

    :return: {"applied": {...}, "skipped": [选项名...], "notes": [...]}
    """
    applied, skipped = {}, []
    for key, option_name in RULE_ENGINE_OPTIONS.items():
        value = rule_options.get(key)
        if value is None:
            continue
        if key in ONLY_WHEN_ENABLED and not value:
            continue                      # 默认关闭的开关, 用户没开就不发
        if engine is not None and hasattr(engine, "supports") and engine.supports(option_name):
            engine.set_option(option_name, value)
            applied[option_name] = value
        else:
            skipped.append(option_name)

    notes = []
    if skipped:
        notes.append("引擎不支持这些规则选项, 已跳过(规则判定由规则层承担): "
                     + ", ".join(sorted(skipped)))
    return {"applied": applied, "skipped": sorted(skipped), "notes": notes}


# ----------------------------------------------------------------------
# info 收集 / 主变稳定性
# ----------------------------------------------------------------------
class InfoHistory:
    """收集本次搜索的所有 info 行, 用来判断"主变是否已经稳定"

    引擎每次刷新同一路都会再发一条 info, 单看最后一条无法区分
    "从头到尾都是这一着" 和 "刚刚才换的" —— 后者不能拿来下结论。
    """

    def __init__(self):
        self.snapshots = []
        self.partial_best = None

    def __call__(self, info):
        snapshot = {
            "multipv": info.get("multipv") or 1,
            "depth": info.get("depth"),
            "seldepth": info.get("seldepth"),
            "score": info.get("score"),
            "mate": info.get("mate"),
            "bound": info.get("bound"),
            "pv": list(info.get("pv") or []),
            "nodes": info.get("nodes"),
            "nps": info.get("nps"),
            "time_ms": info.get("time_ms"),
        }
        self.snapshots.append(snapshot)
        if snapshot["multipv"] == 1 and snapshot["pv"]:
            self.partial_best = snapshot["pv"][0]
        return snapshot

    def primary_snapshots(self):
        """只看第 1 路(引擎首选)的快照"""
        return [s for s in self.snapshots if s["multipv"] == 1]


def evaluate_stability(snapshots, *, slack=STABILITY_DEPTH_SLACK,
                       tolerance=CP_STABILITY_TOLERANCE, min_samples=MIN_STABLE_SAMPLES):
    """判断主变是否已经稳定 (决定敢不敢把 cp 写成 win/loss)

    只取第 1 路、不带上下界(lowerbound/upperbound 不是确切分数)的快照,
    再看"最深的那几层": 最佳着法一致 + 分数波动在容差内 = 稳定。
    引擎给出 mate 是确定值, 直接算稳定。

    :return: {"samples", "depth", "bestmove", "score_min", "score_max",
              "score_range", "same_bestmove", "mate_proven", "stable", "reason"}
    """
    out = {"samples": 0, "depth": None, "bestmove": None, "score_min": None,
           "score_max": None, "score_range": None, "same_bestmove": False,
           "mate_proven": False, "stable": False, "reason": ""}

    usable = [s for s in (snapshots or [])
              if (s.get("multipv") or 1) == 1 and not s.get("bound")
              and (s.get("score") is not None or s.get("mate") is not None)]
    out["samples"] = len(usable)
    if not usable:
        out["reason"] = "没有可用的主变快照"
        return out

    deepest = max(int(s.get("depth") or 0) for s in usable)
    out["depth"] = deepest
    near = [s for s in usable if int(s.get("depth") or 0) >= deepest - int(slack)]
    last = usable[-1]
    out["bestmove"] = (last.get("pv") or [None])[0]

    cps = [s["score"] for s in near if s.get("score") is not None]
    if cps:
        out["score_min"], out["score_max"] = min(cps), max(cps)
        out["score_range"] = out["score_max"] - out["score_min"]

    best_moves = {(s.get("pv") or [None])[0] for s in near}
    out["same_bestmove"] = len(best_moves) == 1 and None not in best_moves

    if last.get("mate") is not None:
        out["mate_proven"] = True
        out["stable"] = True
        out["reason"] = "引擎给出确定的杀棋结论"
        return out

    if len(near) < int(min_samples):
        out["reason"] = f"深层样本不足({len(near)} < {min_samples})"
        return out
    if not out["same_bestmove"]:
        out["reason"] = "最佳着法在深层之间发生过改变"
        return out
    if out["score_range"] is None or out["score_range"] > int(tolerance):
        out["reason"] = f"分数波动过大({out['score_range']}cp > {tolerance}cp)"
        return out

    out["stable"] = True
    out["reason"] = "最佳着法与分数在深层保持稳定"
    return out


# ----------------------------------------------------------------------
# 结论判定
# ----------------------------------------------------------------------
def mate_plies(mate):
    """UCI 的 "mate N" = 走子方再走 N 步将死, 折算成半着数 = 2N-1"""
    count = abs(int(mate))
    return 0 if count == 0 else count * 2 - 1


def normalized_win_from_cp(cp, scale=WINRATE_SCALE):
    """cp -> 0~1 的胜率(走子方视角); 只用于展示与排序, 不是裁决"""
    return round(1.0 / (1.0 + 10 ** (-float(cp) / scale)), 4)


def to_move_column(side):
    """本包内部 'w'/'b' -> 落库列 'r'/'b'"""
    return "r" if str(side).lower()[:1] == "w" else "b"


def decide_conclusion(primary, *, to_move, config=None, stability=None,
                      repetition_result=None, bestmove=None):
    """把一路主变 + 规则信号 + 稳定性, 判定成可以落库的结论

    判定优先级 (规格):
        mate > 0        -> win  + mate_plies
        mate < 0        -> loss
        cp 够大且主变稳 -> win / loss
        其余            -> unknown (宁可说不知道, 也不瞎下结论)
    引擎给的和棋信号 (0 分 / 三次重复) 只作候选: result 写 needs_rule_review,
    score_type 写 draw, 并挂上 repeat/natural 原因, 绝不自动判和。

    :return: {score_type, score_value, normalized_win, result, result_for,
              mate_plies, draw_type, rule_review_status, confidence,
              needs_rule_review, notes}
    """
    out = {
        "score_type": "unknown",
        "score_value": None,
        "normalized_win": None,
        "result": "unknown",
        # 结果必须写清是"谁"的胜负: 引擎分数是走子方视角, 所以 result_for 就是走子方
        "result_for": to_move_column(to_move),
        "mate_plies": None,
        "draw_type": None,
        "rule_review_status": "none",
        "confidence": "preliminary",
        "needs_rule_review": False,
        "notes": [],
    }

    rep = repetition_result or {}
    if rep.get("needs_rule_review"):
        out["needs_rule_review"] = True
        out["rule_review_status"] = rep.get("rule_review_status") or "pending"
        out["notes"].append("检测到重复局面/长将长捉信号, 必须人工棋例复核, "
                            "不能按三次重复自动判和")

    line = primary or {}
    mate = line.get("mate")
    cp = line.get("score")
    bound = line.get("bound")
    stable = bool((stability or {}).get("stable"))

    if bound:
        out["notes"].append(f"分数带 {bound}bound, 不是确切值, 不能据此判定胜负")

    if mate is not None:
        out["score_type"] = "mate"
        out["score_value"] = int(mate)
        out["mate_plies"] = mate_plies(mate)
        if int(mate) > 0:
            out["result"] = "win"
            out["normalized_win"] = 1.0
            out["notes"].append(f"引擎给出 {abs(int(mate))} 步杀的确定结论")
        else:
            out["result"] = "loss"
            out["normalized_win"] = 0.0
            out["notes"].append(f"引擎给出 {abs(int(mate))} 步被杀的确定结论")

    elif cp is not None:
        cp = int(cp)
        out["score_value"] = cp
        out["normalized_win"] = normalized_win_from_cp(cp)
        in_draw_band = abs(cp) <= CP_DRAW_BAND
        draw_candidate = in_draw_band and (cp == 0 or bool(rep.get("threefold_candidate")))

        if draw_candidate:
            out["score_type"] = "draw"
            out["draw_type"] = "repeat" if rep.get("threefold_candidate") else "natural"
            out["result"] = "needs_rule_review"
            out["needs_rule_review"] = True
            if out["rule_review_status"] == "none":
                out["rule_review_status"] = "pending"
            out["notes"].append(
                f"引擎评估接近均势({cp}cp): 只能作为和棋候选"
                f"({out['draw_type']}), 是否成和必须人工棋例确认")
        elif bound:
            out["score_type"] = "cp"
            out["result"] = "unknown"
            out["notes"].append("分数带上下界, 只能当参考, 不足以判定胜负")
        elif cp >= CP_WIN_THRESHOLD and stable:
            out["score_type"] = "cp"
            out["result"] = "win"
            out["notes"].append(f"主变稳定且分数 {cp}cp >= {CP_WIN_THRESHOLD}cp, 判为胜势")
        elif cp <= CP_LOSS_THRESHOLD and stable:
            out["score_type"] = "cp"
            out["result"] = "loss"
            out["notes"].append(f"主变稳定且分数 {cp}cp <= {CP_LOSS_THRESHOLD}cp, 判为败势")
        else:
            out["score_type"] = "cp"
            out["result"] = "unknown"
            if not stable:
                out["notes"].append("主变尚未稳定, 只给初步结论")
            else:
                out["notes"].append(f"分数 {cp}cp 落在判定阈值 "
                                    f"({CP_LOSS_THRESHOLD}~{CP_WIN_THRESHOLD}) 内, "
                                    "不足以判定胜负")

    else:
        out["notes"].append("引擎没有给出可用分数, 结论只能是 unknown")

    # 有棋例待复核时, 置信度统一降级: 引擎的数值再漂亮也不能替裁判下结论
    if out["needs_rule_review"]:
        out["confidence"] = "rule_review_needed"
    elif out["result"] in ("win", "loss"):
        out["confidence"] = "stable"
    else:
        out["confidence"] = "preliminary"
    return out


# ----------------------------------------------------------------------
# 候选着法
# ----------------------------------------------------------------------
def validate_pv(pv, fen, *, side=None, limit=PV_VALIDATE_PLIES):
    """逐着走一遍变例, 返回 {"plies_ok", "checked", "error", "error_ply"}

    低深度时引擎偶尔会吐出接不上的变例, 落库前必须过一遍 rules.validate_move;
    limit 是校验步数上限 (变例可能几十步, 全验没必要)。
    """
    grid, current = rules.fen_to_board(fen)
    if side:
        current = str(side).lower()[:1]
    moves = [str(mv).strip() for mv in (pv or []) if str(mv or "").strip()]

    checked = 0
    for index, move in enumerate(moves[:int(limit)]):
        try:
            info = rules.validate_move(grid, move, side=current)
        except rules.RuleError as e:
            return {"plies_ok": checked, "checked": checked,
                    "error": str(e), "error_ply": index + 1}
        grid, _captured = rules.apply_move(grid, info["from"][0], info["from"][1],
                                          info["to"][0], info["to"][1])
        current = "b" if current == "w" else "w"
        checked += 1
    return {"plies_ok": checked, "checked": checked, "error": None, "error_ply": None}


def extract_candidates(lines, fen, *, side=None, key_move=None, user_moves=None,
                       pv_limit=PV_VALIDATE_PLIES):
    """引擎各路候选 -> endgame_candidates 行 (落库前逐着过规则校验)

    :param lines: chess_engine.search() 返回的 lines
    :param fen: 当前局面 (用于校验与生成中文记谱)
    :param key_move: 关键步的 ICCS 着法, 命中的候选标 is_key_move=1
    :param user_moves: 用户自己摆出来的变着(ICCS 集合), 命中标 is_user_variation=1
    :return: {"candidates": [...], "rejected": [{move, reason}], "notes": [...]}
    """
    grid, board_side = rules.fen_to_board(fen)
    current = str(side).lower()[:1] if side else board_side
    key_set = set(_clean_moves(key_move)) if key_move else set()
    user_set = set(_clean_moves(user_moves))

    candidates, rejected, notes = [], [], []
    for index, line in enumerate(lines or []):
        pv = [str(mv).strip() for mv in (line.get("pv") or []) if str(mv or "").strip()]
        if not pv:
            continue
        raw_move = pv[0]
        try:
            info = rules.validate_move(grid, raw_move, side=current)
        except rules.RuleError as e:
            # 引擎给了个不合法的着法: 记下来但不写进候选(禁止绕过 validator 入库)
            rejected.append({"move": raw_move, "reason": str(e)})
            continue

        move_uci = info["iccs"]
        check = validate_pv(pv, fen, side=current, limit=pv_limit)
        if check["error"]:
            notes.append(f"候选 {move_uci} 的变例第 {check['error_ply']} 步校验不过: "
                         f"{check['error']} (前 {check['plies_ok']} 步合法)")

        mate = line.get("mate")
        score = line.get("score")
        if mate is not None:
            score_type, score_value = "mate", int(mate)
        elif score is not None:
            score_type, score_value = "cp", int(score)
        else:
            score_type, score_value = "unknown", None

        candidates.append({
            "rank": len(candidates) + 1,
            "multipv_index": int(line.get("multipv") or index + 1),
            "move_uci": move_uci,
            "from_square": move_uci[:2],
            "to_square": move_uci[2:4],
            "promotion": None,          # 象棋没有升变
            "score_type": score_type,
            "score_value": score_value,
            "mate_plies": mate_plies(mate) if mate is not None else None,
            "pv_uci": " ".join(pv),
            "pv_san": " ".join(pv_to_chinese(pv, fen)),
            "visited_nodes": line.get("nodes"),
            "depth": line.get("depth"),
            "seldepth": line.get("seldepth"),
            "bound": line.get("bound"),
            "pv_plies_ok": check["plies_ok"],
            "is_key_move": 1 if move_uci in key_set else 0,
            "is_user_variation": 1 if move_uci in user_set else 0,
        })

    if rejected:
        notes.append("引擎给出的这些着法在当前局面不合法, 已丢弃: "
                     + ", ".join(f"{item['move']}({item['reason']})" for item in rejected))
    return {"candidates": candidates, "rejected": rejected, "notes": notes}


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
def analyze_position(engine, *, fen, to_move=None, start_fen=None, moves=None,
                     mode="standard", depth=None, movetime_ms=None, multipv=None,
                     searchmoves=None, rule_options=None, key_move=None,
                     user_moves=None, move_records=None, repetition_result=None,
                     cancel=None, on_progress=None, timeout_ms=None):
    """对一个局面做一轮残局研究分析, 返回可直接落库的结果

    :param engine: ChessEngine 实例 (或同接口的对象)
    :param fen: 待分析的局面 (必须先过 fen_rules.validate_position)
    :param to_move: 显式指定走子方 ('w'/'b'); 不给就用 FEN 里的
    :param start_fen: 这盘棋的起始局面; 与 moves 一起构成完整走子历史
    :param moves: 从起始局面到当前 ply 的全部着法 (ICCS 字符串列表)
    :param move_records: storage 风格的着法行(带 fen_before/fen_after); 给了就直接
        用它做重复检测, 不给就用 start_fen + moves 重放出来
    :param repetition_result: 已有的重复检测结果; 不给就现算
    :param cancel: threading.Event, 置位后立刻让引擎 stop
    :param on_progress: 每收到一条 info 回调一次(上报进度用)
    :raise AnalyzerError: 局面不合法 / 配置不成立 / searchmoves 或走子历史对不上
    :return: 见模块文档; status 为 completed 或 cancelled
    """
    check = fen_rules.validate_position(fen, to_move=to_move)
    if not check["legal"]:
        detail = "; ".join(item["message"] for item in check["errors"])
        raise AnalyzerError(f"局面不合法, 不能分析: {detail}")

    config = resolve_config(mode, depth=depth, movetime_ms=movetime_ms, multipv=multipv,
                            searchmoves=searchmoves, rule_options=rule_options,
                            timeout_ms=timeout_ms)

    # 受控变着必须真的是这个局面下的合法着法, 否则引擎会直接报错退出
    grid, board_side = rules.fen_to_board(check["fen"])
    for move in config["searchmoves"]:
        try:
            rules.validate_move(grid, move, side=board_side)
        except rules.RuleError as e:
            raise AnalyzerError(f"searchmoves 里的 {move} 在该局面下不合法: {e}") from e

    # 重复检测用的局面序列: 优先用调用方给的对局记录, 否则用 起始局面 + 着法串 重放。
    # 必须在开搜之前算 —— 重放能顺便发现"moves 和 fen 不是同一盘棋", 这种错误不该
    # 花掉一次 60s 的搜索才发现。
    records = move_records
    if records is None:
        records = build_move_records(fen=check["fen"], start_fen=start_fen, moves=moves)

    history = InfoHistory()
    notes = []

    engine_options = apply_rule_engine_options(engine, config["rule_options"])
    notes.extend(engine_options["notes"])

    position_cmd = build_position(fen=check["fen"], start_fen=start_fen, moves=moves)
    go_cmd = build_go_command(config)

    def _on_info(info):
        history(info)
        if on_progress is not None:
            on_progress(info)

    search = engine.search(position_cmd,
                           depth=config["depth"], movetime=config["movetime_ms"],
                           multipv=config["multipv"],
                           searchmoves=config["searchmoves"] or None,
                           cancel=cancel, on_info=_on_info,
                           timeout_ms=config["timeout_ms"])

    lines = list(search.get("lines") or [])
    primary = lines[0] if lines else None
    bestmove = search.get("bestmove") or ((primary or {}).get("pv") or [None])[0]

    # 规格要求最佳着法额外用 MultiPV=1 确认一遍 (MultiPV>1 时才有意义)
    confirmed_bestmove, confirm_info = None, None
    if config["confirm_bestmove"] and not search.get("stopped"):
        confirm = engine.search(position_cmd,
                                depth=config["depth"], movetime=config["movetime_ms"],
                                multipv=1, searchmoves=config["searchmoves"] or None,
                                cancel=cancel, timeout_ms=config["timeout_ms"])
        confirm_lines = list(confirm.get("lines") or [])
        confirmed_bestmove = confirm.get("bestmove") or (
            (confirm_lines[0].get("pv") or [None])[0] if confirm_lines else None)
        confirm_info = {"bestmove": confirmed_bestmove,
                        "stopped": confirm.get("stopped"),
                        "duration_ms": confirm.get("duration_ms")}
        if confirmed_bestmove and confirmed_bestmove != bestmove:
            notes.append(f"MultiPV={config['multipv']} 首选与 MultiPV=1 确认结果不一致"
                         f"({bestmove} vs {confirmed_bestmove}), 已采用确认结果")
            bestmove = confirmed_bestmove

    stability = evaluate_stability(history.snapshots)
    if not stability["stable"]:
        notes.append(f"主变未稳定: {stability['reason']}")

    # 重复检测: 长将/长捉只出信号, 一律挂 needs_rule_review 交人工棋例复核
    repetition_result = (repetition_result if repetition_result is not None
                         else repetition_mod.analyze_repetition(records))

    conclusion = decide_conclusion(primary, to_move=check["side_to_move"],
                                   config=config, stability=stability,
                                   repetition_result=repetition_result,
                                   bestmove=bestmove)
    notes.extend(conclusion["notes"])

    extracted = extract_candidates(lines, check["fen"], key_move=key_move,
                                   user_moves=user_moves)
    notes.extend(extracted["notes"])

    engine_meta = search.get("engine") or {}
    stopped = bool(search.get("stopped"))
    status = "cancelled" if stopped else "completed"
    if stopped:
        notes.append("分析被取消, 结果是中途快照, 不能当作最终结论")

    best_pv = (primary or {}).get("pv") or []

    return {
        "ok": True,
        "status": status,
        # ---- 与 endgame_positions 列一一对应的部分 ----
        "fen": check["fen"],
        "position_hash": check["position_hash"],
        "to_move": to_move_column(check["side_to_move"]),
        "to_move_side": check["side_to_move"],
        "repetition_rule": config["rule_options"]["repetition_rule"],
        "engine_version": engine_meta.get("engine") or "",
        "nnue_file": engine_meta.get("nnue_file") or "",
        "depth": config["depth"],
        "seldepth": (primary or {}).get("seldepth"),
        "movetime_ms": config["movetime_ms"],
        "multipv": config["multipv"],
        "rule_aware": 1 if config["rule_options"]["rule_aware"] else 0,
        "bestmove": bestmove,
        "best_pv_uci": " ".join(best_pv),
        "score_type": conclusion["score_type"],
        "score_value": conclusion["score_value"],
        "normalized_win": conclusion["normalized_win"],
        "result": conclusion["result"],
        "result_for": conclusion["result_for"],
        "mate_plies": conclusion["mate_plies"],
        "draw_type": conclusion["draw_type"],
        "rule_review_status": conclusion["rule_review_status"],
        "confidence": conclusion["confidence"],
        "analysis_duration_ms": search.get("duration_ms"),
        # ---- 候选着法 / 规则信号 / 可复现的配置快照 ----
        "candidates": extracted["candidates"],
        "rejected_candidates": extracted["rejected"],
        "needs_rule_review": conclusion["needs_rule_review"],
        "notes": notes,
        "stability": stability,
        "repetition": repetition_result,
        "history_plies": len(records or []),
        "rule_options": config["rule_options"],
        "engine_options": engine_options,
        "confirmed_bestmove": confirmed_bestmove,
        "confirm": confirm_info,
        "config": {"mode": config["mode"], "depth": config["depth"],
                   "movetime_ms": config["movetime_ms"], "multipv": config["multipv"],
                   "searchmoves": config["searchmoves"],
                   "timeout_ms": config["timeout_ms"],
                   "confirm_bestmove": config["confirm_bestmove"]},
        "command_snapshot": {
            "position": position_cmd,
            "go": go_cmd,
            "go_confirm": (build_go_command({**config, "multipv": 1})
                           if config["confirm_bestmove"] else None),
        },
        "position_state": {"in_check": check["in_check"],
                           "has_legal_move": check["has_legal_move"],
                           "piece_count": check["piece_count"]},
        "progress": {"info_count": search.get("info_count"),
                     "partial_best": history.partial_best,
                     "stopped": stopped,
                     "duration_ms": search.get("duration_ms")},
        "lines": lines,
    }
