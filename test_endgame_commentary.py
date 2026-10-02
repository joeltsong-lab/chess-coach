# -*- coding: utf-8 -*-
"""endgame.commentary 的单元测试 (不联网: LLM 调用一律用注入的假函数)

    python test_endgame_commentary.py
    python -m unittest test_endgame_commentary -v

守住规格里的三条:
  * 冻结 prompt —— 版本号与指纹钉死在这里, 模板被改动测试立刻红;
  * 只转述不计算 —— 解说能用到的数值/坐标, 边界就是库里那行分析结果;
  * 异常一律降级 —— 没 LLM / 报错 / 空回复 / 编造着法, 都退回模板解说, 不抛给调用方。
"""

import unittest

from chess_engine import START_FEN

from endgame import commentary

# 冻结: 改模板就必须改 PROMPT_VERSION, 同时更新这两个常量
PINNED_VERSION = "xq-endgame-commentary/v1"
PINNED_FINGERPRINT = "3e65ae99ed01f56b7c4efb5127f1fdd54df5ae19"

POSITION = {
    "id": 1, "game_id": 7, "ply": 0, "fen": START_FEN, "position_hash": "hash-1",
    "to_move": "r", "repetition_rule": "chinese_2020_analysis",
    "engine_version": "Pikafish 2026-09-06", "nnue_file": "pikafish.nnue",
    "depth": 30, "seldepth": 34, "movetime_ms": 60000, "multipv": 3, "rule_aware": 1,
    "bestmove": "h2e2", "best_pv_uci": "h2e2 h9g7",
    "score_type": "cp", "score_value": 305, "normalized_win": 0.85,
    "result": "win", "result_for": "r", "mate_plies": None, "draw_type": None,
    "rule_review_status": "none", "confidence": "stable",
    "analysis_duration_ms": 42000, "created_at": "2026-09-22 10:00:00",
}

CANDIDATES = [
    {"rank": 1, "move_uci": "h2e2", "score_type": "cp", "score_value": 305,
     "mate_plies": None, "pv_uci": "h2e2 h9g7", "pv_san": "炮二平五 马8进7",
     "visited_nodes": 1000, "is_key_move": 0},
    {"rank": 2, "move_uci": "b2e2", "score_type": "cp", "score_value": 120,
     "mate_plies": None, "pv_uci": "b2e2 h9g7", "pv_san": "炮八平五 马8进7",
     "visited_nodes": 900, "is_key_move": 0},
]


def position(**over) -> dict:
    row = dict(POSITION)
    row.update(over)
    return row


def draw_position() -> dict:
    """引擎的 0 分/重复信号: 只能当"和棋候选", 必须挂着待人工复核"""
    return position(bestmove="d0d1", best_pv_uci="d0d1", score_type="draw", score_value=0,
                    normalized_win=0.5, result="needs_rule_review", result_for=None,
                    draw_type="repeat", rule_review_status="pending",
                    confidence="rule_review_needed")


# ----------------------------------------------------------------------
# 冻结 prompt
# ----------------------------------------------------------------------
class TestFrozenPrompt(unittest.TestCase):
    def test_version_is_pinned(self):
        self.assertEqual(commentary.PROMPT_VERSION, PINNED_VERSION)

    def test_template_fingerprint_is_pinned(self):
        self.assertEqual(commentary.prompt_fingerprint(), PINNED_FINGERPRINT)

    def test_both_templates_are_module_level_constants(self):
        self.assertIsInstance(commentary.COMMENTARY_SYSTEM_PROMPT, str)
        self.assertIsInstance(commentary.COMMENTARY_USER_TEMPLATE, str)
        self.assertTrue(commentary.COMMENTARY_SYSTEM_PROMPT.strip())
        self.assertIn("{data}", commentary.COMMENTARY_USER_TEMPLATE)

    def test_messages_are_template_plus_facts(self):
        facts = commentary.build_facts(POSITION, CANDIDATES)
        messages = commentary.build_messages(facts)
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[0]["content"], commentary.COMMENTARY_SYSTEM_PROMPT)
        # 送进 prompt 的只有事实表: 引擎数值原样在里面, 没有别的料
        self.assertIn(commentary.render_facts(facts), messages[1]["content"])
        for value in (facts["bestMove"], facts["engine"], str(facts["scoreValue"])):
            self.assertIn(value, messages[1]["content"])

    def test_prompt_forbids_inventing_and_verdicts(self):
        system = commentary.COMMENTARY_SYSTEM_PROMPT
        self.assertIn("绝不自己推演", system)
        self.assertIn("不能写成正式棋例裁决", system)


# ----------------------------------------------------------------------
# 事实表: 只改名取值, 不加推断
# ----------------------------------------------------------------------
class TestFacts(unittest.TestCase):
    def test_facts_mirror_the_engine_row(self):
        facts = commentary.build_facts(POSITION, CANDIDATES)
        self.assertEqual(facts["bestMove"], POSITION["bestmove"])
        self.assertEqual(facts["scoreType"], POSITION["score_type"])
        self.assertEqual(facts["scoreValue"], POSITION["score_value"])
        self.assertEqual(facts["result"], POSITION["result"])
        self.assertEqual(facts["sideToMove"], "红方")
        self.assertEqual(facts["engine"], POSITION["engine_version"])
        self.assertFalse(facts["needsRuleReview"])
        self.assertEqual(len(facts["candidates"]), 2)
        self.assertEqual(facts["candidates"][0]["move"], "h2e2")
        self.assertEqual(facts["candidates"][0]["chinese"], "炮二平五")

    def test_black_to_move_is_labelled_black(self):
        facts = commentary.build_facts(position(to_move="b", result_for="b"), [])
        self.assertEqual(facts["sideToMove"], "黑方")

    def test_allowed_moves_are_exactly_the_engine_ones(self):
        facts = commentary.build_facts(POSITION, CANDIDATES)
        self.assertEqual(commentary.allowed_moves(facts), {"h2e2", "h9g7", "b2e2"})
        self.assertEqual(commentary.allowed_moves(commentary.build_facts(POSITION, [])),
                         {"h2e2", "h9g7"})

    def test_rule_based_text_only_quotes_engine_numbers(self):
        facts = commentary.build_facts(POSITION, CANDIDATES)
        text = commentary.rule_based_text(facts)
        self.assertIn(POSITION["bestmove"], text)
        self.assertIn(str(POSITION["score_value"]), text)
        self.assertIn(POSITION["engine_version"], text)
        self.assertEqual(commentary._invented_moves(text, facts), [])

    def test_draw_signal_is_never_a_verdict(self):
        facts = commentary.build_facts(draw_position(), [])
        self.assertTrue(facts["needsRuleReview"])
        text = commentary.rule_based_text(facts)
        self.assertIn("人工棋例复核", text)
        self.assertIn("不等于正式棋例裁决", text)
        self.assertIn("三次重复局面", commentary.render_facts(facts))


# ----------------------------------------------------------------------
# 解说入口: 正常返回 + 四种降级
# ----------------------------------------------------------------------
class TestExplain(unittest.TestCase):
    def test_llm_answer_is_used_when_it_stays_inside_the_data(self):
        seen = {}

        def fake(messages):
            seen["messages"] = messages
            return "红方先手, 引擎首选 h2e2, 评分 +305 厘兵, 结论 win。"

        out = commentary.explain(POSITION, CANDIDATES, llm_call=fake)
        self.assertTrue(out["llmUsed"])
        self.assertIsNone(out["fallbackReason"])
        self.assertEqual(out["inventedMoves"], [])
        self.assertIn("h2e2", out["text"])
        self.assertEqual(seen["messages"][0]["content"], commentary.COMMENTARY_SYSTEM_PROMPT)
        self.assertEqual(out["promptFingerprint"], PINNED_FINGERPRINT)
        self.assertIn("人工复核", out["disclaimer"])
        # 事实表不受解说影响
        self.assertEqual(out["engineFacts"]["bestMove"], POSITION["bestmove"])

    def test_llm_disabled_uses_the_template(self):
        out = commentary.explain(POSITION, CANDIDATES, use_llm=False)
        self.assertFalse(out["llmUsed"])
        self.assertEqual(out["fallbackReason"], "llm_disabled")
        facts = commentary.build_facts(POSITION, CANDIDATES)
        self.assertEqual(out["text"], commentary.rule_based_text(facts))

    def test_llm_error_uses_the_template(self):
        def boom(messages):
            raise RuntimeError("没有配置 API key")

        out = commentary.explain(POSITION, CANDIDATES, llm_call=boom)
        self.assertFalse(out["llmUsed"])
        self.assertTrue(out["fallbackReason"].startswith("llm_error:"))
        self.assertIn("RuntimeError", out["fallbackReason"])
        self.assertEqual(out["text"], commentary.rule_based_text(
            commentary.build_facts(POSITION, CANDIDATES)))

    def test_empty_answer_uses_the_template(self):
        out = commentary.explain(POSITION, CANDIDATES, llm_call=lambda messages: "  \n ")
        self.assertFalse(out["llmUsed"])
        self.assertEqual(out["fallbackReason"], "llm_empty")
        self.assertTrue(out["text"].strip())

    def test_invented_move_discards_the_whole_answer(self):
        def invented(messages):
            return "红方可以先走 a9a8, 这样更好。"      # a9a8 引擎数据里没有

        out = commentary.explain(POSITION, CANDIDATES, llm_call=invented)
        self.assertFalse(out["llmUsed"])
        self.assertEqual(out["fallbackReason"], "llm_invented_move")
        self.assertEqual(out["inventedMoves"], ["a9a8"])
        self.assertNotIn("a9a8", out["text"])           # 编造内容一律不给用户看
        self.assertEqual(out["text"], commentary.rule_based_text(
            commentary.build_facts(POSITION, CANDIDATES)))

    def test_fen_board_segment_is_not_mistaken_for_a_move(self):
        """FEN 的棋盘段里有形如 "b1k1" 的片段, 不能当成编造着法"""
        def quotes_fen(messages):
            return f"这是当前局面: {START_FEN}, 引擎给的首选已写在上面。"

        out = commentary.explain(POSITION, CANDIDATES, llm_call=quotes_fen)
        self.assertTrue(out["llmUsed"], out["fallbackReason"])
        self.assertEqual(out["inventedMoves"], [])

    def test_explanation_is_not_written_back_to_the_row(self):
        before = dict(POSITION)
        commentary.explain(POSITION, CANDIDATES, use_llm=False)
        self.assertEqual(POSITION, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
