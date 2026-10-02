# -*- coding: utf-8 -*-
"""endgame.repetition 的单元测试 (标准库 unittest, 不依赖引擎、不碰数据库)

    python test_endgame_repetition.py
    python -m unittest test_endgame_repetition -v
"""

import unittest

from core import rules

from endgame import repetition


def build(fens, moves, sides):
    """按 (局面序列, 着法, 走子方) 拼出 storage 风格的 moves 列表"""
    out = []
    for i, iccs in enumerate(moves):
        out.append({"ply": i + 1, "side": sides[i], "iccs": iccs,
                    "fen_before": fens[i], "fen_after": fens[i + 1]})
    return out


# ----------------------------------------------------------------------
# 长将: 红车在 d 线与 e 线之间来回将, 黑将被迫在 d9/e9 之间来回走
#   红帅 f0, 黑将 d9, 红车 e0 起步(d 线此时不通, 不算已将军)
# ----------------------------------------------------------------------
PERPETUAL_CHECK_FENS = [
    "3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1",     # P0 红车 e0
    "3k5/9/9/9/9/9/9/9/9/3R1K3 b - - 0 1",     # 车 e0d0 -> 将着 d9
    "4k4/9/9/9/9/9/9/9/9/3R1K3 w - - 0 1",     # 将 d9e9
    "4k4/9/9/9/9/9/9/9/9/4RK3 b - - 0 1",      # 车 d0e0 -> 将着 e9
    "3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1",      # 将 e9d9  (回到 P0)
    "3k5/9/9/9/9/9/9/9/9/3R1K3 b - - 0 1",
    "4k4/9/9/9/9/9/9/9/9/3R1K3 w - - 0 1",
    "4k4/9/9/9/9/9/9/9/9/4RK3 b - - 0 1",
    "3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1",      # 第三次出现 P0
]
PERPETUAL_CHECK_MOVES = ["e0d0", "d9e9", "d0e0", "e9d9",
                         "e0d0", "d9e9", "d0e0", "e9d9"]
PERPETUAL_CHECK_SIDES = ["red", "black"] * 4

# ----------------------------------------------------------------------
# 普通重复: 双方各挪自己的车, 谁也没将谁
# ----------------------------------------------------------------------
SHUFFLE_FENS = [
    "3k4r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",     # P0
    "3k4r/9/9/9/9/9/9/9/R8/4K4 b - - 0 1",     # 车 a0a1
    "3k5/8r/9/9/9/9/9/9/R8/4K4 w - - 0 1",     # 车 i9i8
    "3k5/8r/9/9/9/9/9/9/9/R3K4 b - - 0 1",     # 车 a1a0
    "3k4r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",     # 车 i8i9 (回到 P0)
    "3k4r/9/9/9/9/9/9/9/R8/4K4 b - - 0 1",
    "3k5/8r/9/9/9/9/9/9/R8/4K4 w - - 0 1",
    "3k5/8r/9/9/9/9/9/9/9/R3K4 b - - 0 1",
    "3k4r/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",     # 第三次出现 P0
]
SHUFFLE_MOVES = ["a0a1", "i9i8", "a1a0", "i8i9",
                 "a0a1", "i9i8", "a1a0", "i8i9"]
SHUFFLE_SIDES = ["red", "black"] * 4


class TestMoveSignals(unittest.TestCase):
    def test_checking_move(self):
        fen = "3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1"
        signals = repetition.move_signals(fen, "e0d0")
        self.assertTrue(signals["check"])
        self.assertFalse(signals["chase"])

    def test_creating_a_chase(self):
        """红车挪到 a 线才形成对黑马(无根)的威胁, 这一着才算捉"""
        fen = "n4k3/9/9/9/9/9/9/9/9/1R2K4 w - - 0 1"
        grid, _ = rules.fen_to_board(fen)
        self.assertEqual(repetition.chase_targets(grid, "w"), [])
        signals = repetition.move_signals(fen, "b0a0")
        self.assertFalse(signals["check"])
        self.assertTrue(signals["chase"])
        self.assertEqual([t["square"] for t in signals["chase_targets"]], ["a9"])

    def test_pre_existing_threat_is_not_a_chase(self):
        """本来就在威胁着, 这一着没新增威胁 -> 不算捉"""
        fen = "n2k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1"
        signals = repetition.move_signals(fen, "a0a1")
        self.assertFalse(signals["chase"])

    def test_defended_piece_is_not_a_chase_target(self):
        """黑马被自己的车保着(有根), 不算被捉"""
        grid, _ = rules.fen_to_board("nr1k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1")
        self.assertEqual(repetition.chase_targets(grid, "w"), [])

    def test_undefended_horse_is_a_chase_target(self):
        grid, _ = rules.fen_to_board("n2k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1")
        self.assertEqual(repetition.chase_targets(grid, "w"),
                         [{"square": "a9", "piece": "n"}])

    def test_illegal_move_raises(self):
        with self.assertRaises(rules.RuleError):
            repetition.move_signals("3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1", "a0a1")


class TestNormalizeRecords(unittest.TestCase):
    def test_side_inferred_from_fen(self):
        rows = [{"ply": 1, "side": "black", "iccs": "a0a1",
                 "fen_before": "3k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",
                 "fen_after": "3k5/9/9/9/9/9/9/9/R8/4K4 b - - 0 1"}]
        records = repetition.normalize_records(rows)
        self.assertEqual(records[0]["side"], "w")     # 从 fen_after 反推, 胜过 side 列

    def test_incomplete_rows_skipped(self):
        rows = [{"ply": 1, "iccs": "a0a1", "fen_before": "", "fen_after": "x"},
                {"ply": 2, "iccs": "", "fen_before": "x", "fen_after": "y"}]
        self.assertEqual(repetition.normalize_records(rows), [])

    def test_ply_falls_back_to_index(self):
        rows = [{"iccs": "e0d0",
                 "fen_before": "3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1",
                 "fen_after": "3k5/9/9/9/9/9/9/9/9/3R1K3 b - - 0 1"}]
        self.assertEqual(repetition.normalize_records(rows)[0]["ply"], 1)


class TestAnalyzeRepetition(unittest.TestCase):
    def test_no_moves(self):
        out = repetition.analyze_repetition([])
        self.assertFalse(out["threefold_candidate"])
        self.assertFalse(out["needs_rule_review"])
        self.assertEqual(out["rule_review_status"], "none")
        self.assertEqual(out["draw_type"], "none")

    def test_two_occurrences_are_not_threefold(self):
        """只重复了两次不算三次重复"""
        moves = build(PERPETUAL_CHECK_FENS[:5], PERPETUAL_CHECK_MOVES[:4],
                      PERPETUAL_CHECK_SIDES[:4])
        out = repetition.analyze_repetition(moves)
        self.assertFalse(out["threefold_candidate"])

    def test_perpetual_check(self):
        moves = build(PERPETUAL_CHECK_FENS, PERPETUAL_CHECK_MOVES,
                      PERPETUAL_CHECK_SIDES)
        out = repetition.analyze_repetition(moves)
        self.assertTrue(out["threefold_candidate"])
        self.assertEqual(out["repeat_segment"]["occurrences"], 3)
        self.assertEqual(out["repeat_segment"]["from_ply"], 0)
        self.assertEqual(out["repeat_segment"]["to_ply"], 8)
        self.assertTrue(out["perpetual_check"])
        self.assertFalse(out["perpetual_chase"])
        self.assertEqual(out["sides"]["w"]["checks"], 4)
        self.assertEqual(out["sides"]["w"]["moves"], 4)
        self.assertEqual(out["sides"]["b"]["checks"], 0)
        self.assertEqual(out["draw_type"], "repeat")

    def test_perpetual_check_needs_human_review(self):
        """三次重复 + 长将 只能进人工复核, 绝不能被自动判成和棋或某一方负"""
        moves = build(PERPETUAL_CHECK_FENS, PERPETUAL_CHECK_MOVES,
                      PERPETUAL_CHECK_SIDES)
        out = repetition.analyze_repetition(moves)
        self.assertTrue(out["needs_rule_review"])
        self.assertEqual(out["rule_review_status"], "pending")

    def test_plain_repetition_still_needs_review(self):
        """没有长将/长捉的普通重复, 象棋规则也不自动判和, 同样交人工确认"""
        moves = build(SHUFFLE_FENS, SHUFFLE_MOVES, SHUFFLE_SIDES)
        out = repetition.analyze_repetition(moves)
        self.assertTrue(out["threefold_candidate"])
        self.assertFalse(out["perpetual_check"])
        self.assertFalse(out["perpetual_chase"])
        self.assertFalse(out["check_chase_mix"])
        self.assertTrue(out["needs_rule_review"])
        self.assertTrue(any("人工确认" in note for note in out["notes"]))

    def test_never_returns_a_verdict(self):
        """这个模块的合同: 只出信号, 不出胜负结论"""
        moves = build(PERPETUAL_CHECK_FENS, PERPETUAL_CHECK_MOVES,
                      PERPETUAL_CHECK_SIDES)
        out = repetition.analyze_repetition(moves)
        for key in ("result", "winner", "score", "win", "loss", "draw"):
            self.assertNotIn(key, out)

    def test_window_limits_lookback(self):
        """窗口调小到 4 半着就看不出这个 8 半着的循环了"""
        moves = build(PERPETUAL_CHECK_FENS, PERPETUAL_CHECK_MOVES,
                      PERPETUAL_CHECK_SIDES)
        out = repetition.analyze_repetition(moves, window=4)
        self.assertFalse(out["threefold_candidate"])
        self.assertEqual(out["positions_seen"], 5)

    def test_short_game_is_fine(self):
        moves = build(PERPETUAL_CHECK_FENS[:3], PERPETUAL_CHECK_MOVES[:2],
                      PERPETUAL_CHECK_SIDES[:2])
        out = repetition.analyze_repetition(moves)
        self.assertFalse(out["threefold_candidate"])
        self.assertEqual(out["positions_seen"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
