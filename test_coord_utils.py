# -*- coding: utf-8 -*-
"""coord_utils 的单元测试 (标准库 unittest, 不依赖引擎)

    python test_coord_utils.py
    python -m unittest test_coord_utils -v
"""

import unittest

from chess_engine import START_FEN
from coord_utils import (
    FILES,
    fen_to_grid,
    grid_to_iccs,
    iccs_to_chinese,
    iccs_to_grid,
    parse_fen,
    pv_to_chinese,
)

# 前端格子列号 -> 红方记谱数字: 列 8(i) 是一, 列 0(a) 是九
RED_DIGIT_BY_COL = "九八七六五四三二一"

# 两枚红车同在 a 线, 用来验证 前/后
TWO_ROOKS_FEN = "4k4/9/9/9/9/9/R8/9/R8/4K4 w - - 0 1"


class TestIccsToGrid(unittest.TestCase):
    def test_red_bottom_h2e2(self):
        """red_bottom: 行 0 在上=黑方底线 -> row = 9 - rank"""
        self.assertEqual(iccs_to_grid("h2e2"), {"from": [7, 7], "to": [7, 4]})

    def test_red_bottom_corners(self):
        # a9 = 左上角(黑方底线最左) -> row 0, col 0
        self.assertEqual(iccs_to_grid("a9a8")["from"], [0, 0])
        # i0 = 右下角(红方底线最右) -> row 9, col 8
        self.assertEqual(iccs_to_grid("i0i1")["from"], [9, 8])

    def test_black_bottom_is_flipped(self):
        """black_bottom: 行 0 在上=红方底线 -> row = rank, 与 red_bottom 上下镜像"""
        self.assertEqual(iccs_to_grid("h2e2", "black_bottom"),
                         {"from": [2, 7], "to": [2, 4]})
        red = iccs_to_grid("h2e2", "red_bottom")
        black = iccs_to_grid("h2e2", "black_bottom")
        for key in ("from", "to"):
            self.assertEqual(red[key][0] + black[key][0], 9)  # 行号上下互补
            self.assertEqual(red[key][1], black[key][1])      # 列号一致

    def test_extra_suffix_ignored(self):
        """引擎偶尔带附加信息, 只取前 4 位"""
        self.assertEqual(iccs_to_grid("h2e2 ponder c6c5"), iccs_to_grid("h2e2"))

    def test_bad_input(self):
        for bad in ("", "h", "h2e", "z2e2", "h2x2", "h2e"):
            with self.assertRaises(ValueError):
                iccs_to_grid(bad)
        with self.assertRaises(ValueError):
            iccs_to_grid("h2e2", "upside_down")

    def test_roundtrip_with_grid_to_iccs(self):
        for move in ("h2e2", "a0a1", "i0i9", "e0e1", "b0c2"):
            coord = iccs_to_grid(move)
            self.assertEqual(
                grid_to_iccs(coord["from"][0], coord["from"][1],
                             coord["to"][0], coord["to"][1]),
                move,
            )


class TestGridMatchesFrontend(unittest.TestCase):
    """ICCS -> Grid 的行列号, 必须和前端渲染出的棋盘一致, 否则箭头会画错位置"""

    def test_h2e2_lands_on_red_cannon(self):
        grid, side = fen_to_grid(START_FEN)
        self.assertEqual(side, "w")
        self.assertEqual(len(grid), 10)
        self.assertTrue(all(len(row) == 9 for row in grid))

        coord = iccs_to_grid("h2e2")
        fr, fc = coord["from"]
        tr, tc = coord["to"]
        # 起点必须正好是标准开局里那门红炮, 终点必须为空
        self.assertEqual(grid[fr][fc], "C")
        self.assertEqual(grid[tr][tc], "")
        self.assertEqual(grid[7][7], "C")   # h2 -> 行 7 列 7
        self.assertEqual(grid[7][1], "C")   # 另一门炮在 b2 -> 行 7 列 1
        self.assertEqual(grid[9][0], "R")   # a0 -> 行 9 列 0
        self.assertEqual(grid[0][4], "k")   # e9 -> 行 0 列 4

    def test_chinese_notation_agrees_with_grid_columns(self):
        """中文着法里的线号与前端列号用的是同一套映射"""
        coord = iccs_to_grid("h2e2")
        self.assertEqual(RED_DIGIT_BY_COL[coord["from"][1]], "二")
        self.assertEqual(RED_DIGIT_BY_COL[coord["to"][1]], "五")
        self.assertEqual(iccs_to_chinese("h2e2", START_FEN), "炮二平五")

    def test_top_row_matches_fen(self):
        """grid 的行顺序: 行 0 = 黑方底线, 行 9 = 红方底线"""
        grid, _ = fen_to_grid(START_FEN)
        self.assertEqual(grid[0], list("rnbakabnr"))
        self.assertEqual(set(grid[1]), {""})
        self.assertEqual(grid[2], ["", "c", "", "", "", "", "", "c", ""])  # 1c5c1
        self.assertEqual(grid[9], list("RNBAKABNR"))


class TestIccsToChinese(unittest.TestCase):
    def test_red_moves(self):
        self.assertEqual(iccs_to_chinese("h2e2", START_FEN), "炮二平五")
        self.assertEqual(iccs_to_chinese("b0c2", START_FEN), "马八进七")
        self.assertEqual(iccs_to_chinese("h0g2", START_FEN), "马二进三")
        self.assertEqual(iccs_to_chinese("a0a1", START_FEN), "车九进一")
        self.assertEqual(iccs_to_chinese("g3g4", START_FEN), "兵三进一")

    def test_black_moves(self):
        black_fen = START_FEN.replace(" w ", " b ")
        self.assertEqual(iccs_to_chinese("h9g7", black_fen), "马8进7")
        self.assertEqual(iccs_to_chinese("b9c7", black_fen), "马2进3")
        self.assertEqual(iccs_to_chinese("a9a8", black_fen), "车1进1")

    def test_front_back_disambiguation(self):
        self.assertEqual(iccs_to_chinese("a3a4", TWO_ROOKS_FEN), "前车进一")
        self.assertEqual(iccs_to_chinese("a1a4", TWO_ROOKS_FEN), "后车进三")

    def test_unresolvable_move_returned_as_is(self):
        # 起点没有棋子时原样返回, 不抛异常
        self.assertEqual(iccs_to_chinese("e5e6", START_FEN), "e5e6")


class TestPvToChinese(unittest.TestCase):
    def test_sequential_conversion(self):
        """PV 是一串连着的着法, 后面的要落子后才能正确命名"""
        pv = ["h2e2", "h9g7", "h0g2"]
        self.assertEqual(pv_to_chinese(pv, START_FEN), ["炮二平五", "马8进7", "马二进三"])

    def test_empty(self):
        self.assertEqual(pv_to_chinese([], START_FEN), [])
        self.assertEqual(pv_to_chinese(None, START_FEN), [])


class TestParseFen(unittest.TestCase):
    def test_bad_fen(self):
        with self.assertRaises(ValueError):
            parse_fen("")
        with self.assertRaises(ValueError):
            parse_fen("9/9/9 w - - 0 1")          # 行数不对
        with self.assertRaises(ValueError):
            parse_fen("8/9/9/9/9/9/9/9/9/9 w - - 0 1")  # 每行不是 9 列

    def test_file_letters(self):
        self.assertEqual(FILES, "abcdefghi")
        self.assertEqual(FILES.index("a"), 0)
        self.assertEqual(FILES.index("i"), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
