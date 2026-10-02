# -*- coding: utf-8 -*-
"""对"引擎已经算完的残局分析结果"做中文解说 —— 冻结 prompt, 只转述不计算。

为什么要"冻结":
    解说是给人看的文字, 一旦随手改 prompt, 同一份分析结果会给出不同的说法,
    出了问题也没法复现。所以两个模板是模块级常量, 改动必须同步改 PROMPT_VERSION,
    并在测试里核对 prompt_fingerprint() —— 加了什么、什么时候加的, 有据可查。

边界 (与 explainer.py 同一条原则):
    1. 只做"解释", 不做"计算" —— 送进 prompt 的全部是引擎算好的数值;
    2. 不得编造候选之外的着法 —— 返回的文字里出现的 ICCS 坐标必须能在
       引擎数据里找到, 找不到就整条作废, 降级为模板解说 (llm_invented_move);
    3. 不得把引擎的和棋信号说成正式棋例裁决 —— 需要复核的一律原样带上"待人工复核";
    4. 任何异常 (没配 key / 超时 / 返回空 / 编造着法) 都降级, 不影响引擎结果返回。

降级不是报错: 没配 LLM 时返回一段完全由引擎数值拼出来的模板解说 (llmUsed=False),
照样能用。前端据此决定是否加"AI 解说"角标。
"""

import hashlib
import re

# ----------------------------------------------------------------------
# 冻结 prompt (改这里必须同时改 PROMPT_VERSION)
# ----------------------------------------------------------------------
PROMPT_VERSION = "xq-endgame-commentary/v1"

COMMENTARY_SYSTEM_PROMPT = (
    "你是中国象棋残局教练，负责把引擎已经算好的数据转述成人话。"
    "铁律："
    "①只能解释下面给出的数据，绝不自己推演、绝不计算新着法、绝不修改任何评分或结论；"
    "②可以提到的着法坐标必须原样照抄下面出现过的，不许写出数据里没有的坐标；"
    "③引擎给的和棋/重复信号只是候选，不能写成正式棋例裁决；"
    "④拿不准就直说数据不足，不要编。"
    "输出中文，3-6 句，不要 Markdown 语法、不要标题、不要代码块。"
)

COMMENTARY_USER_TEMPLATE = """【引擎分析数据】(全部来自引擎, 已落库, 不得改动)
{data}

请按下面的顺序讲清楚:
1) 现在轮到谁走、引擎的最优着法是什么、这条主变大致想干什么;
2) 候选着法之间差在哪里 (有评分就引用评分, 没有就说没有);
3) 这个局面的结论是什么、可信度如何;
4) 如果标了"待人工复核", 明确说这是引擎信号、需人工棋例复核, 不要下结论。

只准使用上面给出的坐标与数值, 不要补充你自己的着法或评分。"""

DISCLAIMER = ("解说由语言模型转述引擎数值, 只解释不改写; "
              "棋例结论一律以人工复核为准。")


def prompt_fingerprint() -> str:
    """两个冻结模板的 sha1, 测试拿它守住 prompt 没被悄悄改过"""
    blob = (PROMPT_VERSION + "\x00" + COMMENTARY_SYSTEM_PROMPT + "\x00"
            + COMMENTARY_USER_TEMPLATE).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()


# ----------------------------------------------------------------------
# 数据整形
# ----------------------------------------------------------------------
RESULT_WORDS = {
    "win": "走子方胜势",
    "loss": "走子方劣势(可能被杀)",
    "needs_rule_review": "和棋信号, 但需人工棋例复核",
    "unknown": "结论不足(引擎没给出可用结论)",
}
CONFIDENCE_WORDS = {
    "preliminary": "初步结果",
    "rule_review_needed": "需人工复核",
    "stable": "主变已稳定",
    "confirmed": "MultiPV=1 已确认",
}
DRAW_WORDS = {"repeat": "三次重复局面(候选)", "natural": "自然限着/零分(候选)"}


def _split(text) -> list:
    """'a b c'/'a,b' -> ['a','b','c'] (库里 pv 是空格或逗号分隔的字符串)"""
    return [item for item in str(text or "").replace(",", " ").split() if item]


def _score_text(score_type, score_value) -> str:
    """引擎分数 -> 中文说法 (走子方视角, 与 decide_conclusion 的口径一致)"""
    if score_type == "mate" and score_value is not None:
        n = abs(int(score_value))
        return f"{n} 步内将死" if int(score_value) > 0 else f"{n} 步内被杀"
    if score_type == "draw":
        return "接近和棋(0 分档)"
    if score_type == "cp" and score_value is not None:
        return f"{int(score_value):+d} 厘兵"
    if score_type == "unknown":
        return "未知"
    return "未知"


def build_facts(position: dict, candidates=None) -> dict:
    """endgame_positions 一行 + 候选行 -> 只含引擎数值的事实表

    这里只做改名/取值, **不加任何推断** —— 解说能说的内容, 边界就是这张表。
    """
    side = "红方" if position.get("to_move") == "r" else "黑方"
    facts = {
        "fen": position.get("fen"),
        "sideToMove": side,
        "engine": position.get("engine_version") or "未知",
        "nnue": position.get("nnue_file") or "",
        "depth": position.get("depth"),
        "seldepth": position.get("seldepth"),
        "movetimeMs": position.get("movetime_ms"),
        "multipv": position.get("multipv"),
        "ruleSet": position.get("repetition_rule"),
        "ruleAware": bool(position.get("rule_aware")),
        "bestMove": position.get("bestmove"),
        "bestPv": _split(position.get("best_pv_uci")),
        "scoreType": position.get("score_type"),
        "scoreValue": position.get("score_value"),
        "normalizedWin": position.get("normalized_win"),
        "result": position.get("result"),
        "resultFor": position.get("result_for"),
        "matePlies": position.get("mate_plies"),
        "drawType": position.get("draw_type"),
        "confidence": position.get("confidence"),
        "ruleReviewStatus": position.get("rule_review_status"),
        "needsRuleReview": position.get("rule_review_status") == "pending",
        "analysisDurationMs": position.get("analysis_duration_ms"),
        "candidates": [],
    }
    for row in candidates or []:
        pv_san = _split(row.get("pv_san"))
        facts["candidates"].append({
            "rank": row.get("rank"),
            "move": row.get("move_uci"),
            "chinese": pv_san[0] if pv_san else "",
            "scoreType": row.get("score_type"),
            "scoreValue": row.get("score_value"),
            "matePlies": row.get("mate_plies"),
            "pv": _split(row.get("pv_uci")),
            "pvSan": pv_san,
            "visitedNodes": row.get("visited_nodes"),
            "isKey": bool(row.get("is_key_move")),
        })
    return facts


def allowed_moves(facts: dict) -> set:
    """解说里允许出现的全部 ICCS 坐标 (引擎数据里有的)"""
    allowed = set()
    if facts.get("bestMove"):
        allowed.add(facts["bestMove"])
    allowed.update(facts.get("bestPv") or [])
    for item in facts.get("candidates") or []:
        if item.get("move"):
            allowed.add(item["move"])
        allowed.update(item.get("pv") or [])
    return allowed


def render_facts(facts: dict) -> str:
    """事实表 -> 给 LLM 看的纯文本块 (同时也是模板解说的事实来源)"""
    lines = [
        f"局面 FEN：{facts.get('fen')}",
        f"轮到走棋：{facts.get('sideToMove')}",
        f"引擎：{facts.get('engine')}"
        + (f"（NNUE: {facts['nnue']}）" if facts.get("nnue") else ""),
        f"搜索配置：深度 {facts.get('depth')}"
        f"（实际选深 {facts.get('seldepth')}）/ 限时 {facts.get('movetimeMs')} 毫秒"
        f" / MultiPV {facts.get('multipv')} / 规则集 {facts.get('ruleSet')}"
        f"（规则感知: {'是' if facts.get('ruleAware') else '否'}）",
        f"引擎首选着法：{facts.get('bestMove') or '无'}",
        f"首选主变：{' '.join(facts.get('bestPv') or []) or '无'}",
        f"局面评分：{_score_text(facts.get('scoreType'), facts.get('scoreValue'))}"
        f"（score_type={facts.get('scoreType')}, score_value={facts.get('scoreValue')}）",
        f"引擎结论：{facts.get('result')}"
        f"（{RESULT_WORDS.get(facts.get('result'), '未知取值')}）"
        f"，结论归属：{facts.get('resultFor')}，"
        f"可信度：{facts.get('confidence')}"
        f"({CONFIDENCE_WORDS.get(facts.get('confidence'), '未知取值')})",
    ]
    if facts.get("drawType"):
        lines.append(f"和棋信号类型：{facts['drawType']}"
                     f"({DRAW_WORDS.get(facts['drawType'], '未知取值')})")
    if facts.get("needsRuleReview"):
        lines.append(f"棋例状态：{facts.get('ruleReviewStatus')} —— "
                     "待人工复核, 引擎信号不等于正式棋例裁决")
    if facts.get("analysisDurationMs") is not None:
        lines.append(f"引擎用时：{facts.get('analysisDurationMs')} 毫秒")

    rows = []
    for item in facts.get("candidates") or []:
        pv = " ".join(item.get("pvSan") or item.get("pv") or []) or "无"
        rows.append(
            f"  {item.get('rank')}. {item.get('move')}"
            + (f"（{item['chinese']}）" if item.get("chinese") else "")
            + f" 评分 {_score_text(item.get('scoreType'), item.get('scoreValue'))}"
            + f"（score_type={item.get('scoreType')}, score_value={item.get('scoreValue')}）"
            + f" 后续变化 {pv}"
            + (f" 节点数 {item['visitedNodes']}" if item.get("visitedNodes") is not None else "")
            + ("（关键步）" if item.get("isKey") else "")
        )
    lines.append("候选着法：")
    lines.extend(rows or ["  （暂无候选着法）"])
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 模板解说 (降级用, 不含任何引擎之外的信息)
# ----------------------------------------------------------------------
def rule_based_text(facts: dict) -> str:
    """完全由引擎数值拼出的说明 —— 没配 LLM 或 LLM 不可用时的兜底"""
    parts = [
        f"当前轮到{facts.get('sideToMove')}走棋。",
        f"引擎 {facts.get('engine')} 在深度 {facts.get('depth')}"
        f"（实际选深 {facts.get('seldepth')}）、限时 {facts.get('movetimeMs')} 毫秒、"
        f"MultiPV {facts.get('multipv')} 下算出：",
    ]
    best = facts.get("bestMove")
    if best:
        pv = " ".join(facts.get("bestPv") or []) or "无"
        parts.append(f"首选 {best}，评分 {_score_text(facts.get('scoreType'), facts.get('scoreValue'))}，"
                     f"主变 {pv}。")
    else:
        parts.append("这一轮没有算出可用首选着法。")

    ranked = [c for c in (facts.get("candidates") or []) if c.get("move")][:3]
    if ranked:
        detail = "；".join(
            f"第 {c['rank']} 选 {c['move']}"
            + (f"（{c['chinese']}）" if c.get("chinese") else "")
            + f" {_score_text(c.get('scoreType'), c.get('scoreValue'))}"
            for c in ranked)
        parts.append(f"候选依次为：{detail}。")

    parts.append(f"引擎结论是 {facts.get('result')}"
                 f"（{RESULT_WORDS.get(facts.get('result'), '未知取值')}），"
                 f"结论归属 {facts.get('resultFor')}，可信度 {facts.get('confidence')}。")
    if facts.get("needsRuleReview"):
        parts.append("检测到重复局面/长将长捉信号，已挂起等待人工棋例复核 —— "
                     "引擎的和棋信号不等于正式棋例裁决，不能直接判和。")
    parts.append("（这段说明由引擎数值直接拼出，未经语言模型加工。）")
    return "".join(parts)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
# 独立的 ICCS 坐标 token: 前后不能再接字母/数字/'/', 免得把 FEN 的棋盘部分
# (比如 b1k1b2 里的 b1k1) 当成着法
_INVENTED_MOVE_RE = re.compile(r"(?<![A-Za-z0-9/])[a-i][0-9][a-i][0-9](?![A-Za-z0-9/])")


def _invented_moves(text: str, facts: dict) -> list:
    """文字里出现的、引擎数据中没有的 ICCS 坐标 (编造着法的检查)

    只看独立的坐标 token: 前后不能再接字母/数字/'/', 免得把 FEN 的棋盘部分
    （比如 b1k1b2 里的 b1k1）当成着法误判。
    """
    allowed = allowed_moves(facts)
    return sorted({token for token in _INVENTED_MOVE_RE.findall(text or "")
                   if token not in allowed})


def build_messages(facts: dict) -> list:
    """冻结模板 + 事实表 -> chat messages"""
    return [
        {"role": "system", "content": COMMENTARY_SYSTEM_PROMPT},
        {"role": "user", "content": COMMENTARY_USER_TEMPLATE.format(data=render_facts(facts))},
    ]


def _default_llm_call(messages):
    """默认走 explainer.call_llm (OpenAI 兼容接口), 没配 key 会抛异常 -> 降级"""
    from explainer import call_llm
    return call_llm(messages)


def explain(position: dict, candidates=None, *, use_llm=True, llm_call=None) -> dict:
    """对一条已落库的分析结果做解说

    :param position: endgame_positions 的行(dict)
    :param candidates: endgame_candidates 的行列表
    :param use_llm: 显式关掉就只出模板解说
    :param llm_call: 测试注入用的调用函数 (messages -> str); 默认 explainer.call_llm
    :return: {promptVersion, promptFingerprint, llmUsed, text, fallbackReason,
              engineFacts, disclaimer, inventedMoves}
    """
    facts = build_facts(position, candidates)
    out = {
        "promptVersion": PROMPT_VERSION,
        "promptFingerprint": prompt_fingerprint(),
        "llmUsed": False,
        "text": "",
        "fallbackReason": None,
        "engineFacts": facts,
        "disclaimer": DISCLAIMER,
        "inventedMoves": [],
    }

    if not use_llm:
        out["fallbackReason"] = "llm_disabled"
        out["text"] = rule_based_text(facts)
        return out

    call = llm_call or _default_llm_call
    try:
        text = (call(build_messages(facts)) or "").strip()
    except Exception as e:                       # 没配 key / 网络 / 超时 / 解析失败
        out["fallbackReason"] = f"llm_error: {type(e).__name__}: {e}"
        out["text"] = rule_based_text(facts)
        return out

    if not text:
        out["fallbackReason"] = "llm_empty"
        out["text"] = rule_based_text(facts)
        return out

    invented = _invented_moves(text, facts)
    if invented:
        # 编造了引擎数据里没有的着法 -> 整条作废, 不能把编造的内容给人看
        out["fallbackReason"] = "llm_invented_move"
        out["inventedMoves"] = invented
        out["text"] = rule_based_text(facts)
        return out

    out["llmUsed"] = True
    out["text"] = text
    return out
