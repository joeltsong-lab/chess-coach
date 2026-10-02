# -*- coding: utf-8 -*-
"""rules.py 的自测。

跑法:
    python test_rules.py

关键的硬门槛是开局的 perft 数值 (中国象棋公认值: 深度 1 = 44, 深度 2 = 1920),
它能一次性验证"双方所有棋子的走法 + 自将/照面判定"是否整体正确。
深度 3 (79666) 在纯 Python 下太慢, 没有放进来。
"""

import time
import unittest

from core import rules
from core.chess_engine import START_FEN

# 一个"红马被黑车牵制"的局面: 黑车在 e 列, 红马挡在中间, 马一动红帅就被照面攻击
PIN_FEN = "3kr4/9/9/9/9/9/9/4N4/9/4K4 w - - 0 1"
# 将帅同线、中间夹一枚红车: 车一旦横走就是照面
FACE_FEN = "4k4/9/9/9/9/4R4/9/9/9/4K4 w - - 0 1"
# 红方被黑车在 e 列将军
CHECK_FEN = "4r3k/9/9/9/9/9/9/9/9/4K4 w - - 0 1"


def perft(grid, red, depth):
    """统计 depth 层内的合法着法总数"""
    if depth == 0:
        return 1
    total = 0
    for iccs in rules.legal_moves(grid, red=red):
        (fr, fc), (tr, tc) = rules.parse_iccs(iccs)
        after, _ = rules.apply_move(grid, fr, fc, tr, tc)
        total += perft(after, not red, depth - 1)
    return total


class TestFen(unittest.TestCase):
    def test_roundtrip_start(self):
        grid, side = rules.fen_to_board(START_FEN)
        self.assertEqual(side, "w")
        self.assertEqual(rules.board_to_fen(grid, side), START_FEN)
        # 首行是黑方底线, 末行是红方底线
        self.assertEqual("".join(grid[0]), "rnbakabnr")
        self.assertEqual("".join(grid[9]), "RNBAKABNR")
        self.assertEqual(grid[7][1], "C")

    def test_board_to_fen_empty_rows(self):
        grid = [["" for _ in range(9)] for _ in range(10)]
        self.assertEqual(rules.board_to_fen(grid), "9/9/9/9/9/9/9/9/9/9 w - - 0 1")

    def test_validate_fen_ok(self):
        self.assertEqual(rules.validate_fen(START_FEN), START_FEN)

    def test_validate_fen_bad_input(self):
        for bad in ("", "   ", "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9 w - - 0 1",
                    "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABN w - - 0 1",
                    "rnbakabnr/9/1c5x1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"):
            with self.assertRaises(rules.RuleError, msg=f"应当报错: {bad!r}"):
                rules.validate_fen(bad)

    def test_validate_fen_kings_optional(self):
        """残局摆设允许缺将, 显式要求时才查"""
        no_king = "9/9/9/9/9/9/9/4N4/9/4K4 w - - 0 1"
        self.assertEqual(rules.validate_fen(no_king), no_king)
        with self.assertRaises(rules.RuleError):
            rules.validate_fen(no_king, require_kings=True)
        # 两枚红帅也不行
        with self.assertRaises(rules.RuleError):
            rules.validate_fen("9/9/9/9/9/9/9/4K4/9/4K4 w - - 0 1", require_kings=True)


class TestMoveGeneration(unittest.TestCase):
    def setUp(self):
        self.grid, self.side = rules.fen_to_board(START_FEN)

    def moves_of(self, r, c, grid=None):
        return {rules.grid_to_iccs(r, c, tr, tc)
                for tr, tc in rules.pseudo_moves(grid or self.grid, r, c)}

    def test_start_position_per_piece_counts(self):
        """开局各子的伪合法着法条数 (含"过河炮隔子吃马"这类)"""
        self.assertEqual(len(self.moves_of(9, 0)), 2)    # 左车: 只能直上两步
        self.assertEqual(len(self.moves_of(9, 8)), 2)
        self.assertEqual(len(self.moves_of(9, 1)), 2)    # 马: 马二进三 / 马二进一
        self.assertEqual(len(self.moves_of(9, 7)), 2)
        self.assertEqual(len(self.moves_of(7, 1)), 12)   # 炮: 9 个空格 + 隔子吃马
        self.assertEqual(len(self.moves_of(7, 7)), 12)
        self.assertEqual(len(self.moves_of(9, 2)), 2)    # 相: 两个象眼都在
        self.assertEqual(len(self.moves_of(9, 6)), 2)
        self.assertEqual(len(self.moves_of(9, 3)), 1)    # 仕: 只能上中路
        self.assertEqual(len(self.moves_of(9, 5)), 1)
        self.assertEqual(len(self.moves_of(9, 4)), 1)    # 帅: 仕把自己两边挡住了
        for c in (0, 2, 4, 6, 8):
            self.assertEqual(len(self.moves_of(6, c)), 1, f"兵在 c{c} 只能直进")
        self.assertEqual(sum(len(self.moves_of(9, c)) for c in range(9))
                         + sum(len(self.moves_of(7, c)) for c in range(9))
                         + sum(len(self.moves_of(6, c)) for c in range(9)), 44)

    def test_start_position_legal_moves(self):
        moves = rules.legal_moves(self.grid, side="w")
        self.assertEqual(len(moves), 44)
        for expect in ("h2e2", "b2e2", "a0a1", "c0e2", "e0e1", "a3a4", "e3e4"):
            self.assertIn(expect, moves, f"{expect} 应当是开局的合法着法")
        self.assertEqual(moves, sorted(moves), "返回的着法应当排好序")

    def test_perft_start(self):
        """中国象棋公认的 perft 数值, 是这套规则最硬的整体校验

        深度 3 会遍历 7.9 万个局面 (约 4 秒), 它能同时压到"双方所有棋子"的走法与吃子,
        数值一旦对上, 基本可以认定着法生成没有漏判/多判。
        """
        grid, _ = rules.fen_to_board(START_FEN)
        t0 = time.time()
        self.assertEqual(perft(grid, True, 1), 44)
        self.assertEqual(perft(grid, True, 2), 1920)
        self.assertEqual(perft(grid, True, 3), 79666)
        print(f"\n    perft(1)=44, perft(2)=1920, perft(3)=79666, "
              f"共用时 {time.time() - t0:.1f}s")

    def test_horse_leg(self):
        """蹩马腿: 马八进七(9,1 -> 7,2) 的腿在 (8,1); 在腿上放子就跳不动"""
        self.assertIn("b0c2", self.moves_of(9, 1))
        grid, _ = rules.fen_to_board("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1")
        grid[8][1] = "P"          # 把自己的兵塞在马腿上
        self.assertNotIn("b0c2", self.moves_of(9, 1, grid))

    def test_elephant_eye_and_river(self):
        """塞象眼 + 相不过河"""
        self.assertEqual(self.moves_of(9, 2), {"c0a2", "c0e2"})
        grid, _ = rules.fen_to_board(START_FEN)
        grid[8][3] = "P"          # 塞住 (9,2)->(7,4) 的象眼
        self.assertNotIn("c0e2", self.moves_of(9, 2, grid))
        self.assertIn("c0a2", self.moves_of(9, 2, grid))
        # 相不过河: 红相在 (6,2) 时, 往前的两个落点 (4,0)/(4,4) 都越过了河
        grid, _ = rules.fen_to_board("9/9/9/9/9/9/2B6/9/9/4K4 w - - 0 1")
        self.assertEqual(self.moves_of(6, 2, grid), {"c3a1", "c3e1"})
        for iccs in self.moves_of(6, 2, grid):
            self.assertGreaterEqual(rules.parse_iccs(iccs)[1][0], 5, "相不能过河")

    def test_cannon_needs_screen(self):
        """炮吃子必须隔一个炮架; 没有炮架吃不到, 且炮架后不能落到空格"""
        no_screen = "4r3k/9/9/9/9/9/9/4C4/9/4K4 w - - 0 1"
        grid, _ = rules.fen_to_board(no_screen)
        self.assertNotIn("e2e9", self.moves_of(7, 4, grid), "没有炮架不该能吃")
        self.assertIn("e2e3", self.moves_of(7, 4, grid), "炮架前的空格可以走")
        with_screen = "4r3k/9/9/9/9/4P4/9/4C4/9/4K4 w - - 0 1"
        grid, _ = rules.fen_to_board(with_screen)
        self.assertIn("e2e9", self.moves_of(7, 4, grid), "隔一个炮架可以吃")
        self.assertNotIn("e2e8", self.moves_of(7, 4, grid), "炮架之后的空格不能落")

    def test_advisor_and_king_stay_in_palace(self):
        """仕只能走九宫里的斜线: (8,3) 的仕只有 (9,4)... 被帅占着, 实际只剩 (7,4)"""
        grid, _ = rules.fen_to_board("9/9/9/9/9/9/9/9/3A5/4K4 w - - 0 1")
        for iccs in self.moves_of(8, 3, grid):
            r, c = rules.parse_iccs(iccs)[1]
            self.assertTrue(rules.in_palace(r, c, True), f"{iccs} 走出了九宫")
        self.assertEqual(self.moves_of(8, 3, grid), {"d1e2"})

    def test_pawn_river(self):
        """兵过河才能横走, 且永远不能后退"""
        grid, _ = rules.fen_to_board("9/9/9/9/4P4/9/9/9/9/4K4 w - - 0 1")
        self.assertEqual(self.moves_of(4, 4, grid), {"e5e6", "e5d5", "e5f5"}, "过河兵可平可进")
        grid, _ = rules.fen_to_board("9/9/9/9/9/4P4/9/9/9/4K4 w - - 0 1")
        self.assertEqual(self.moves_of(5, 4, grid), {"e4e5"}, "没过河只能直进")
        grid, _ = rules.fen_to_board("9/9/9/9/9/9/4P4/9/9/4K4 w - - 0 1")
        self.assertEqual(self.moves_of(6, 4, grid), {"e3e4"})

    def test_black_pawn_direction(self):
        """黑卒向下走, 过河(行号 >= 5)后可横走"""
        grid, _ = rules.fen_to_board("9/9/9/4p4/9/9/9/9/9/4K4 b - - 0 1")
        self.assertEqual(self.moves_of(3, 4, grid), {"e6e5"})
        grid, _ = rules.fen_to_board("9/9/9/9/9/4p4/9/9/9/4K4 b - - 0 1")
        self.assertEqual(self.moves_of(5, 4, grid), {"e4e3", "e4d4", "e4f4"})


class TestCheckAndLegality(unittest.TestCase):
    def test_pinned_piece_cannot_move(self):
        """被牵制的马一动, 红帅就被黑车照面, 所以一步都走不了"""
        grid, _ = rules.fen_to_board(PIN_FEN)
        self.assertEqual(set(rules.pseudo_moves(grid, 7, 4)),
                         {(5, 3), (5, 5), (6, 2), (6, 6), (8, 2), (8, 6), (9, 3), (9, 5)})
        self.assertEqual(rules.legal_moves_from(grid, 7, 4), [])
        self.assertFalse(rules.is_legal_move(grid, 7, 4, 5, 3))

    def test_flying_general_blocks_sideways_move(self):
        """将帅照面: 中间那枚车不能横走"""
        grid, _ = rules.fen_to_board(FACE_FEN)
        moves = rules.legal_moves_from(grid, 5, 4)
        self.assertEqual(len(moves), 7)
        for iccs in moves:
            self.assertEqual(rules.parse_iccs(iccs)[1][1], 4, f"{iccs} 横走会造成将帅照面")
        self.assertFalse(rules.is_legal_move(grid, 5, 4, 5, 0))

    def test_king_cannot_be_captured(self):
        grid, _ = rules.fen_to_board(FACE_FEN)
        self.assertIn((0, 4), rules.pseudo_moves(grid, 5, 4), "伪合法着法里能吃将")
        self.assertFalse(rules.is_legal_move(grid, 5, 4, 0, 4), "合法着法里不允许吃将")

    def test_in_check_and_escape(self):
        """被将军时, 只有能解将的着法才算合法"""
        grid, _ = rules.fen_to_board(CHECK_FEN)
        self.assertTrue(rules.in_check(grid, "w"))
        self.assertFalse(rules.in_check(grid, "b"))
        moves = rules.legal_moves(grid, side="w")
        # 红帅只能左右躲 (e0d0 / e0f0): 往上 (8,4) 还在车锋上, 而帅不能斜走
        self.assertEqual(moves, sorted(["e0d0", "e0f0"]))
        self.assertNotIn("e0e1", moves, "顺着车锋往上走还是被将")
        for iccs in moves:
            self.assertTrue(iccs[2] == "d" or iccs[2] == "f", f"{iccs} 没躲开 e 列")

    def test_self_check_by_king_walking_into_attack(self):
        """帅不能自己走进对方车的射程"""
        grid, _ = rules.fen_to_board("4r4/9/9/9/9/9/9/9/9/4K4 w - - 0 1")
        # 黑车在 e 列, 红帅往正上方 (8,4) 走等于自己撞上去
        self.assertFalse(rules.is_legal_move(grid, 9, 4, 8, 4))
        self.assertTrue(rules.is_legal_move(grid, 9, 4, 9, 3))
        self.assertTrue(rules.is_legal_move(grid, 9, 4, 9, 5))


class TestValidateMove(unittest.TestCase):
    def setUp(self):
        self.grid, _ = rules.fen_to_board(START_FEN)

    def test_ok(self):
        info = rules.validate_move(self.grid, "h2e2", side="w")
        self.assertEqual(info["iccs"], "h2e2")
        self.assertEqual(info["from"], [7, 7])
        self.assertEqual(info["to"], [7, 4])
        self.assertEqual(info["piece"], "C")
        self.assertIsNone(info["captured"])
        self.assertEqual(info["side"], "w")

    def test_extra_suffix_ignored(self):
        """引擎偶尔会给着法带附加信息"""
        self.assertEqual(rules.validate_move(self.grid, "h2e2+!", side="w")["iccs"], "h2e2")

    def test_wrong_side(self):
        with self.assertRaises(rules.RuleError) as ctx:
            rules.validate_move(self.grid, "h2e2", side="b")
        self.assertIn("红方", str(ctx.exception))

    def test_empty_origin(self):
        with self.assertRaises(rules.RuleError) as ctx:
            rules.validate_move(self.grid, "c1c2", side="w")
        self.assertIn("没有棋子", str(ctx.exception))

    def test_illegal_shape(self):
        with self.assertRaises(rules.RuleError) as ctx:
            rules.validate_move(self.grid, "a0a5", side="w")   # 车不能穿过自己的兵
        self.assertIn("不是合法着法", str(ctx.exception))
        # 黑车在 a9, 现在轮到红方, 这是对方的子
        with self.assertRaises(rules.RuleError) as ctx:
            rules.validate_move(self.grid, "a9a8", side="w")
        self.assertIn("黑方", str(ctx.exception))

    def test_too_short(self):
        for bad in ("", "h2e", None):
            with self.assertRaises(rules.RuleError):
                rules.validate_move(self.grid, bad, side="w")

    def test_apply_iccs(self):
        after, info = rules.apply_iccs(self.grid, "h2e2", side="w")
        self.assertEqual(info["fen_after"],
                         "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1")
        self.assertEqual(after[7][7], "")
        self.assertEqual(after[7][4], "C")
        # 原始 grid 没被改动
        self.assertEqual(self.grid[7][7], "C")

    def test_apply_iccs_captures(self):
        """红炮隔着一个黑炮, 吃掉黑马 (b2b9)"""
        after, info = rules.apply_iccs(self.grid, "b2b9", side="w")
        self.assertEqual(info["captured"], "n")
        self.assertEqual(after[0][1], "C")
        self.assertEqual(after[7][1], "")
        self.assertEqual(info["fen_after"],
                         "rCbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/7C1/9/RNBAKABNR b - - 0 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
