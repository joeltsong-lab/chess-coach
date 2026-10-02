# -*- coding: utf-8 -*-
"""endgame.fen_rules 的单元测试 (标准库 unittest, 不依赖引擎、不碰数据库)

    python test_endgame_fen_rules.py
    python -m unittest test_endgame_fen_rules -v
"""

import unittest

from core.chess_engine import START_FEN

from endgame import fen_rules


def codes(issues):
    return [i["code"] for i in issues]


# 只用两个将/帅: 红帅在 e0(9,4), 黑将在 d9(0,3), 不同纵线 -> 不照面
KINGS_ONLY = "3k5/9/9/9/9/9/9/9/9/4K4 w - - 0 1"


class TestStructure(unittest.TestCase):
    def test_empty(self):
        result = fen_rules.validate_position("")
        self.assertFalse(result["legal"])
        self.assertEqual(codes(result["errors"]), ["FEN_EMPTY"])

    def test_row_count(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/4K4 w - - 0 1")
        self.assertIn("FEN_ROW_COUNT", codes(result["errors"]))

    def test_column_count(self):
        # 第 1 行少一条纵线
        result = fen_rules.validate_position("3k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1")
        self.assertIn("FEN_COLUMN_COUNT", codes(result["errors"]))

    def test_bad_char(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/4K4X w - - 0 1")
        self.assertIn("FEN_BAD_CHAR", codes(result["errors"]))

    def test_zero_digit_is_rejected(self):
        # FEN 里 0 不是合法空格符, 写成 0 会让整行少一条纵线
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/40K4 w - - 0 1")
        self.assertIn("FEN_BAD_CHAR", codes(result["errors"]))

    def test_structural_errors_skip_semantic_checks(self):
        """结构都读不出来时不要再报一堆语义错误, 一次只让用户改一件对的事"""
        result = fen_rules.validate_position("9/9 w - - 0 1")
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["code"], "FEN_ROW_COUNT")
        self.assertFalse(result["legal"])


class TestSideToMove(unittest.TestCase):
    def test_missing_side_is_warning(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/4K4")
        self.assertTrue(result["legal"])
        self.assertEqual(result["side_to_move"], "w")
        self.assertIn("SIDE_FIELD_MISSING", codes(result["warnings"]))

    def test_invalid_side_is_error(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/4K4 x - - 0 1")
        self.assertFalse(result["legal"])
        self.assertEqual(codes(result["errors"]), ["SIDE_INVALID"])

    def test_to_move_overrides_fen(self):
        """界面上"设置轮次"走这条路: 只改轮次, 不动 FEN"""
        result = fen_rules.validate_position(KINGS_ONLY, to_move="b")
        self.assertTrue(result["legal"])
        self.assertEqual(result["side_to_move"], "b")
        self.assertEqual(result["fen"].split()[1], "b")

    def test_to_move_invalid(self):
        result = fen_rules.validate_position(KINGS_ONLY, to_move="red")
        self.assertFalse(result["legal"])
        self.assertEqual(codes(result["errors"]), ["SIDE_INVALID"])


class TestPieces(unittest.TestCase):
    def test_kings_required(self):
        result = fen_rules.validate_position("9/9/9/9/9/9/9/9/9/4K4 w - - 0 1")
        self.assertIn("KING_MISSING", codes(result["errors"]))

    def test_duplicate_king(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/3KK4 w - - 0 1")
        self.assertIn("KING_DUPLICATE", codes(result["errors"]))

    def test_too_many_rooks(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/RRR1K4 w - - 0 1")
        self.assertIn("PIECE_TOO_MANY", codes(result["errors"]))

    def test_king_outside_palace(self):
        result = fen_rules.validate_position("9/9/9/9/4k4/9/9/9/9/4K4 w - - 0 1")
        self.assertIn("KING_OUTSIDE_PALACE", codes(result["errors"]))

    def test_advisor_outside_palace(self):
        # 红仕摆在 a0(9,0), 不是九宫里的士位
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/A3K4 w - - 0 1")
        self.assertIn("ADVISOR_OUTSIDE_PALACE", codes(result["errors"]))

    def test_elephant_cannot_cross_river(self):
        # 红相摆在 (4,4): 红相只能在下半 (行 >= 5) 的 7 个象位上
        result = fen_rules.validate_position("3k5/9/9/9/4B4/9/9/9/9/4K4 w - - 0 1")
        self.assertIn("ELEPHANT_OUTSIDE_POINTS", codes(result["errors"]))

    def test_pawn_on_own_back_rank(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/P3K4 w - - 0 1")
        self.assertIn("PAWN_ON_OWN_BACK_RANKS", codes(result["errors"]))

    def test_piece_count_reported(self):
        result = fen_rules.validate_position(KINGS_ONLY)
        self.assertEqual(result["piece_count"], 2)


class TestKingsFacing(unittest.TestCase):
    def test_facing_is_rejected(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/3K5 w - - 0 1")
        self.assertFalse(result["legal"])
        self.assertIn("KINGS_FACING", codes(result["errors"]))

    def test_blocked_by_piece_is_fine(self):
        # 中间夹着一枚红车就不算照面
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/3RK4 w - - 0 1")
        self.assertNotIn("KINGS_FACING", codes(result["errors"]))


class TestCheckStates(unittest.TestCase):
    def test_side_not_to_move_in_check_is_illegal(self):
        """轮到红走, 但黑将正被红车将着 —— 这样的局面现实中走不出来"""
        result = fen_rules.validate_position("R2k5/9/9/9/9/9/9/9/9/4K4 w - - 0 1")
        self.assertFalse(result["legal"])
        self.assertIn("SIDE_NOT_TO_MOVE_IN_CHECK", codes(result["errors"]))

    def test_side_to_move_in_check_is_only_a_warning(self):
        # 黑车在 e2 那条纵线上将着红帅, 轮到红走, 这是正常的被将军局面
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/4r4/9/4K4 w - - 0 1")
        self.assertTrue(result["legal"], result["errors"])
        self.assertTrue(result["in_check"])
        self.assertIn("SIDE_TO_MOVE_IN_CHECK", codes(result["warnings"]))

    def test_stalemate_is_an_error(self):
        """困毙: 轮到红走, 红帅没被将军但一步也走不动 —— 不能作为研究起点"""
        fen = "3k5/9/9/9/9/9/9/9/5r3/4K4 w - - 0 1"
        result = fen_rules.validate_position(fen)
        self.assertFalse(result["in_check"])
        self.assertFalse(result["has_legal_move"])
        self.assertIn("NO_LEGAL_MOVE", codes(result["errors"]))

    def test_checkmate_is_only_a_warning(self):
        """已被将死: 同样一步走不动, 但这是个"结果局面", 允许存下来展示"""
        fen = "3k5/9/9/9/9/4r4/9/9/9/r3K4 w - - 0 1"
        result = fen_rules.validate_position(fen)
        self.assertTrue(result["in_check"])
        self.assertFalse(result["has_legal_move"])
        self.assertTrue(result["legal"], result["errors"])
        self.assertIn("CHECKMATED", codes(result["warnings"]))


class TestValidPositions(unittest.TestCase):
    def test_start_position_is_legal(self):
        result = fen_rules.validate_position(START_FEN)
        self.assertTrue(result["legal"], result["errors"])
        self.assertTrue(result["has_legal_move"])
        self.assertEqual(result["piece_count"], 32)
        self.assertEqual(result["warnings"], [])

    def test_normalized_fen_roundtrip(self):
        result = fen_rules.validate_position("  3k5/9/9/9/9/9/9/9/9/4K4   b  ",
                                             to_move="b")
        self.assertTrue(result["legal"], result["errors"])
        self.assertEqual(result["fen"],
                         "3k5/9/9/9/9/9/9/9/9/4K4 b - - 0 1")
        # 规范化后的 FEN 再验一次必须还是合法 (幂等)
        again = fen_rules.validate_position(result["fen"])
        self.assertEqual(again["fen"], result["fen"])

    def test_truncated_fen_warns_but_passes(self):
        result = fen_rules.validate_position("3k5/9/9/9/9/9/9/9/9/4K4 w")
        self.assertTrue(result["legal"], result["errors"])
        self.assertIn("FEN_FIELDS_TRUNCATED", codes(result["warnings"]))


class TestPositionKey(unittest.TestCase):
    def test_key_ignores_move_counters(self):
        """重复局面只看"盘面 + 该谁走", 走了几回合与半个回合计数不影响"""
        a = fen_rules.position_key("3k5/9/9/9/9/9/9/9/9/4K4 w - - 0 1")
        b = fen_rules.position_key("3k5/9/9/9/9/9/9/9/9/4K4 w - - 7 42")
        self.assertEqual(a, b)

    def test_key_depends_on_side(self):
        a = fen_rules.position_key(KINGS_ONLY, to_move="w")
        b = fen_rules.position_key(KINGS_ONLY, to_move="b")
        self.assertNotEqual(a, b)

    def test_hash_is_stable_and_distinct(self):
        h1 = fen_rules.position_hash(KINGS_ONLY)
        h2 = fen_rules.position_hash(KINGS_ONLY)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 40)
        self.assertNotEqual(h1, fen_rules.position_hash(KINGS_ONLY, to_move="b"))

    def test_illegal_fen_gives_none(self):
        self.assertIsNone(fen_rules.position_hash("bad fen"))
        self.assertIsNone(fen_rules.position_key(""))


class TestParseBoard(unittest.TestCase):
    def test_loose_parse_accepts_midway_state(self):
        """摆盘中间态(只有一枚将)结构合法就要能读出来, 否则清空后没法往上摆子"""
        parsed = fen_rules.parse_board("9/9/9/9/9/9/9/9/9/4K4 b")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["side"], "b")
        self.assertEqual(parsed["grid"][9][4], "K")

    def test_loose_parse_still_checks_structure(self):
        self.assertFalse(fen_rules.parse_board("9/9/9")["ok"])
        self.assertFalse(fen_rules.parse_board("")["ok"])

    def test_to_move_override(self):
        parsed = fen_rules.parse_board(KINGS_ONLY, to_move="b")
        self.assertEqual(parsed["side"], "b")


class TestApplyOperations(unittest.TestCase):
    def test_set_and_remove(self):
        result = fen_rules.apply_operations(
            KINGS_ONLY,
            [{"op": "set", "square": "a0", "piece": "R"},
             {"op": "set", "square": "e1", "piece": "P"},
             {"op": "remove", "square": "e1"}],
        )
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["fen"].split()[0],
                         "3k5/9/9/9/9/9/9/9/9/R3K4")
        final = fen_rules.validate_position(result["fen"])
        self.assertTrue(final["legal"], final["errors"])

    def test_clear_then_build(self):
        """清空 + 逐子摆放 = 摆盘页最常见的用法"""
        ops = [{"op": "clear"},
               {"op": "set", "square": "d9", "piece": "k"},
               {"op": "set", "square": "e0", "piece": "K"},
               {"op": "set", "square": "a0", "piece": "R"}]
        result = fen_rules.apply_operations(START_FEN, ops)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["fen"].split()[0],
                         "3k5/9/9/9/9/9/9/9/9/R3K4")
        self.assertEqual(result["applied"], 4)

    def test_drag_move(self):
        result = fen_rules.apply_operations(
            "3k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",
            [{"op": "move", "from": "a0", "to": "a4"}])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["fen"].split()[0],
                         "3k5/9/9/9/9/R8/9/9/9/4K4")

    def test_drag_onto_occupied_square_warns(self):
        result = fen_rules.apply_operations(
            "3k5/9/9/9/9/9/9/9/9/R3K4 w - - 0 1",
            [{"op": "move", "from": "a0", "to": "e0"}])
        self.assertEqual(result["errors"], [])
        self.assertIn("OP_OVERWRITE", codes(result["warnings"]))

    def test_drag_from_empty_square(self):
        result = fen_rules.apply_operations(
            KINGS_ONLY, [{"op": "move", "from": "a0", "to": "a1"}])
        self.assertIn("OP_EMPTY_SOURCE", codes(result["errors"]))
        self.assertIsNone(result["fen"])

    def test_unknown_op(self):
        result = fen_rules.apply_operations(KINGS_ONLY, [{"op": "flip"}])
        self.assertIn("OP_UNKNOWN", codes(result["errors"]))

    def test_bad_square(self):
        result = fen_rules.apply_operations(
            KINGS_ONLY, [{"op": "set", "square": "z9", "piece": "R"}])
        self.assertIn("OP_BAD_SQUARE", codes(result["errors"]))

    def test_bad_piece(self):
        result = fen_rules.apply_operations(
            KINGS_ONLY, [{"op": "set", "square": "a0", "piece": "X"}])
        self.assertIn("OP_BAD_PIECE", codes(result["errors"]))

    def test_operations_must_be_list(self):
        result = fen_rules.apply_operations(KINGS_ONLY, {"op": "clear"})
        self.assertIn("OPERATIONS_NOT_LIST", codes(result["errors"]))

    def test_load_replaces_board_and_side(self):
        result = fen_rules.apply_operations(
            START_FEN, [{"op": "load", "fen": KINGS_ONLY.replace(" w ", " b ")}])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["side_to_move"], "b")

    def test_base_fen_may_be_a_midway_state(self):
        """起点是"只有一枚帅"也能继续摆子 —— 摆盘过程必须允许中间态"""
        result = fen_rules.apply_operations(
            "9/9/9/9/9/9/9/9/9/4K4 w", [{"op": "set", "square": "d9", "piece": "k"}])
        self.assertEqual(result["errors"], [])
        self.assertTrue(fen_rules.validate_position(result["fen"])["legal"])

    def test_base_fen_structural_error_reported(self):
        result = fen_rules.apply_operations("nonsense", [{"op": "clear"}])
        self.assertIn("BASE_FEN_INVALID", codes(result["errors"]))

    def test_empty_operations_returns_normalized_fen(self):
        result = fen_rules.apply_operations(KINGS_ONLY, [])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["fen"],
                         "3k5/9/9/9/9/9/9/9/9/4K4 w - - 0 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
