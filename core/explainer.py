# -*- coding: utf-8 -*-
"""让 LLM 对引擎候选做二次筛选: 从几条候选里挑一条并给出理由。

只用标准库 (urllib) 调 OpenAI 兼容的 /chat/completions 接口, 不引入第三方依赖。
配置全部走环境变量, 没配就自动降级为"引擎首选":

    LLM_API_KEY   必填, 没有就跳过二次筛选
    LLM_BASE_URL  可选, 默认 https://api.openai.com/v1
    LLM_MODEL     可选, 默认 gpt-4o-mini
    LLM_TIMEOUT   可选, 秒, 默认 20

设计原则: 只做"挑选"不做"计算"。给 LLM 的全部是引擎已经算好的候选,
并明确要求它不得自行推演走法、不得挑候选之外的着法; 挑出来的坐标还会再校验一次,
不在候选里就当失败处理。任何异常都降级, 不影响引擎结果返回。
"""

import json
import os
import re
import sys
import urllib.request

LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "20"))

PICK_SYSTEM_PROMPT = (
    "你是中国象棋教练。用户会给你几条引擎已经算好的候选着法，"
    "你只能从里面挑一条，绝不自行计算走法、绝不编造候选之外的着法或评分数据；"
    "候选数据缺失就直接挑第一条。"
    "输出必须是严格的 JSON，不要输出 JSON 以外的任何文字、解释或代码块标记。"
)

PICK_USER_TEMPLATE = """【引擎给出的候选着法】(来自 MultiPV，已按引擎评分排序)
棋面 FEN：{fen}
轮到走棋：{side}
对局阶段：{phase}

{candidates}

请只根据上面的数据，从候选里挑一条最适合推荐给人看/给人走的着法
（几条评分接近时，优先挑思路清晰、目的明确、不容易被反击的那条），
输出下面结构的严格 JSON：
{{"pick":"候选里的 ICCS 坐标，必须与上面完全一致","reason":"1-2 句中文理由，说清这步棋的战术目的","compare":"为什么它比其他候选更合适"}}
注意：pick 只能照抄上面出现过的坐标，评分、深度、变例也只能照抄，不得自己计算或编造。"""


def llm_configured() -> bool:
    """是否配置了 LLM（没配就自动 use_llm=false）"""
    return bool(LLM_API_KEY and LLM_BASE_URL and LLM_MODEL)


def detect_phase(fen: str) -> str:
    """按回合数和剩余子力粗略判断开局/中局/残局, 判断不了就返回空串"""
    parts = (fen or "").split()
    if not parts:
        return ""
    fullmove = 1
    if len(parts) >= 6:
        try:
            fullmove = int(parts[5])
        except ValueError:
            fullmove = 1
    # 棋盘里除将/帅以外的子力数
    material = sum(1 for ch in parts[0] if ch.isalpha() and ch.upper() != "K")
    if fullmove <= 12:
        return "开局"
    if material <= 12:
        return "残局"
    return "中局"


def build_pick_prompt(fen, candidates):
    """构造「从候选里挑一个」的 messages

    :param candidates: [{"rank", "iccs", "chinese", "score", "mate", "depth",
                         "pv_chinese"/"pv_iccs"}, ...]
    """
    rows = []
    for i, c in enumerate(candidates, 1):
        mate = c.get("mate")
        if mate is not None:
            score = ("走子方" if mate > 0 else "对方") + f" {abs(mate)} 步内将死"
        elif c.get("score") is not None:
            score = f"{c['score']:+d}"
        else:
            score = "未知"
        pv = c.get("pv_chinese") or c.get("pv_iccs") or []
        depth = c.get("depth") if c.get("depth") is not None else "未知"
        rows.append(
            f"{i}. {c.get('chinese') or c.get('iccs')} ({c.get('iccs')})\n"
            f"   评分：{score}　深度：{depth}\n"
            f"   后续变化：{' '.join(list(pv)[:4]) or '无'}"
        )

    parts = fen.split()
    side = "红方" if len(parts) > 1 and parts[1].lower().startswith("w") else "黑方"
    user = PICK_USER_TEMPLATE.format(
        fen=fen,
        side=side,
        phase=detect_phase(fen) or "未知",
        candidates="\n".join(rows) or "（无候选数据）",
    )
    return [
        {"role": "system", "content": PICK_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def call_llm(messages, timeout=None):
    """调用 OpenAI 兼容的 chat/completions 接口, 失败抛异常"""
    if not llm_configured():
        raise RuntimeError("未配置 LLM（需要环境变量 LLM_API_KEY）")

    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.2,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout or LLM_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _parse_llm_json(text):
    """从 LLM 回复里抠出 JSON（容忍 ```json 包裹和前后废话）"""
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("回复里没有 JSON 对象")
    return json.loads(text[start:end + 1])


def pick_best_move(fen, candidates):
    """让 LLM 对引擎候选做二次筛选, 挑一条并给出理由。

    :param candidates: 同 build_pick_prompt(), 至少要含 iccs / chinese / score / mate / depth
    :return: (pick_iccs, reason, llm_used)
        未配置 LLM、调用失败、或挑的着法不在候选里 —— 一律退回第一条, llm_used=False
    """
    if not candidates:
        return None, "", False
    first = candidates[0].get("iccs")
    if not llm_configured():
        return first, "引擎首选（未配置 LLM，没做二次筛选）", False

    try:
        data = _parse_llm_json(call_llm(build_pick_prompt(fen, candidates)))
        pick = str(data.get("pick") or "").strip()
        # LLM 只能照抄候选, 自己编出来的着法一律不认
        if pick not in {c.get("iccs") for c in candidates}:
            raise ValueError(f"LLM 挑的着法不在候选里: {pick!r}")
        reason = "\n".join(
            x for x in (str(data.get("reason") or "").strip(),
                        str(data.get("compare") or "").strip()) if x
        )
        return pick, reason or "引擎候选之一", True
    except Exception as e:
        print(f"[explainer] LLM 二次筛选失败, 退回引擎首选: {type(e).__name__}: {e}",
              file=sys.stderr)
        return first, "引擎首选（LLM 二次筛选失败，已降级）", False


# ----------------------------------------------------------------------
# 教练解说: 对"刚走的那一步"给一段中文点评
# ----------------------------------------------------------------------
COACH_SYSTEM_PROMPT = (
    "你是中国象棋教练。用户会给你一个局面、刚走的一步棋、引擎首选着法、"
    "两者评分差，以及一个已经定好的档位（最佳/可更好/疑手/失误）。"
    "你的任务是把这个结论讲清楚，绝不自行推演具体着法、绝不编造评分或变例；"
    "只解释已有数据，档位以用户给的为准。"
    "输出必须是严格的 JSON，不要输出 JSON 以外的任何文字、解释或代码块标记。"
)

COACH_USER_TEMPLATE = """【局面】FEN：{fen}
轮到走棋：{side}
对局阶段：{phase}

【刚走的这一步】{played}
【引擎首选】{best}
【评分差】{gap} 厘兵（0 = 与引擎首选相当，正值越大越吃亏）
【教练定档】{category}

请用 1-3 句中文，说明这步棋的问题或优点、为什么算这个档位、以及更好的思路。
只依据上面给出的数据，不要编造具体着法或评分。
输出严格 JSON：{{"reason":"..."}}"""

# 档位 -> 模板点评（没配 LLM / LLM 失败时用）
_COACH_FALLBACK = {
    "最佳": "这步 {played} 正是引擎首选，没有什么可挑剔的。",
    "可更好": "{played} 可以考虑，但引擎更推荐 {best}（评分差约 {gap} 厘兵）。",
    "疑手": "{played} 略有疑问，引擎更推荐 {best}（评分差约 {gap} 厘兵）。",
    "失误": "{played} 是明显失误，应该走 {best}（评分差约 {gap} 厘兵）。",
}


def _coach_fallback(played, best, gap_cp, category):
    """LLM 不可用时的模板点评"""
    template = _COACH_FALLBACK.get(category)
    if not template:
        return f"引擎更推荐 {best}。" if best else "暂无点评。"
    gap = gap_cp if gap_cp is not None else "?"
    return template.format(played=played or "这步棋", best=best or "?", gap=gap)


def coach(fen, played, best, gap_cp, category, phase=None):
    """对"刚走的那一步"给一段中文点评。

    :param fen: 走这一步**之前**的局面 FEN
    :param played: 刚走的那一步（中文着法或 ICCS，用于展示）
    :param best: 引擎首选着法（中文着法或 ICCS）
    :param gap_cp: played 相对 best 的评分差（厘兵，正数表示 played 更吃亏）
    :param category: 已经定好的档位: 最佳 / 可更好 / 疑手 / 失误
    :param phase: 可选，对局阶段（不给就按 FEN 自己判断）
    :return: (reason, llm_used)；未配置 LLM / 失败都退回模板点评, llm_used=False
    """
    fallback = _coach_fallback(played, best, gap_cp, category)
    if not llm_configured():
        return fallback, False

    parts = (fen or "").split()
    side = "红方" if len(parts) > 1 and parts[1].lower().startswith("w") else "黑方"
    user = COACH_USER_TEMPLATE.format(
        fen=fen,
        side=side,
        phase=phase or detect_phase(fen) or "未知",
        played=played or "未知",
        best=best or "未知",
        gap=gap_cp if gap_cp is not None else "未知",
        category=category or "未知",
    )
    try:
        data = _parse_llm_json(call_llm([
            {"role": "system", "content": COACH_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]))
        reason = str(data.get("reason") or "").strip()
        return (reason or fallback), bool(reason)
    except Exception as e:
        print(f"[explainer] LLM 教练解说失败, 退回模板点评: {type(e).__name__}: {e}",
              file=sys.stderr)
        return fallback, False
