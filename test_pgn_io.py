# -*- coding: utf-8 -*-
"""pgn_io.py 的自测。

跑法:
    python test_pgn_io.py

重点验证两件事:
  1. 中文着法能反查回正确的 ICCS (含异体字 車/馬/砲、红方用阿拉伯数字、
     红方把 仕/相 写成 士/象、以及同纵线两枚同种子时的 前/后)
  2. 四种格式导出再导入, 着法序列与评语都不丢
"""

import unittest

from core.chess_engine import START_FEN
from core.pgn_io import (chinese_to_iccs, detect_format, export_fenmoves, export_iccs,
                         export_json, export_pgn, import_content, parse_pgn, replay)
from core.rules import RuleError

# 四步真实开局: 炮二平五 马8进7 马二进三 车9平8
OPENING = ["h2e2", "h9g7", "h0g2", "i9h9"]
OPENING_CN = ["炮二平五", "马8进7", "马二进三", "车9平8"]
# 车二平五 之后是 车9平8; 用来当"带评语"的样例
GAME = {"name": "测试对局", "start_fen": START_FEN, "result": "*",
        "red_name": "红方", "black_name": "黑方", "event": "网战", "date": "2026-09-17"}
MOVES = [{"iccs": "h2e2", "comment": "当头炮"}, {"iccs": "h9g7"},
         {"iccs": "h0g2"}, {"iccs": "i9h9"}]
# 轮到黑方走的局面 (红方先走了炮二平五), 黑方键用阿拉伯数字
BLACK_TURN_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1"


class TestChineseLookup(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(chinese_to_iccs("炮二平五", START_FEN), "h2e2")
        self.assertEqual(chinese_to_iccs("马八进七", START_FEN), "b0c2")
        self.assertEqual(chinese_to_iccs("车九进一", START_FEN), "a0a1")
        self.assertEqual(chinese_to_iccs("兵三进一", START_FEN), "g3g4")
        self.assertEqual(chinese_to_iccs("马8进7", BLACK_TURN_FEN), "h9g7")

    def test_all_opening_moves_roundtrip(self):
        """开局每一个合法着法, 转成中文再反查, 都必须回到自己"""
        from core.rules import legal_moves
        from core.rules import fen_to_board
        grid, side = fen_to_board(START_FEN)
        for iccs in legal_moves(grid, side=side):
            from core.coord_utils import iccs_to_chinese
            chinese = iccs_to_chinese(iccs, START_FEN)
            self.assertEqual(chinese_to_iccs(chinese, START_FEN), iccs,
                             f"{iccs} -> {chinese} 反查不回来")

    def test_variant_characters(self):
        """繁体棋谱的 車/馬/砲/進 要认"""
        self.assertEqual(chinese_to_iccs("砲二平五", START_FEN), "h2e2")
        self.assertEqual(chinese_to_iccs("車九進一", START_FEN), "a0a1")
        self.assertEqual(chinese_to_iccs("馬8進7", BLACK_TURN_FEN), "h9g7")

    def test_red_with_arabic_digits(self):
        """红方着法写成阿拉伯数字 (炮2平5) 也要认"""
        self.assertEqual(chinese_to_iccs("炮2平5", START_FEN), "h2e2")
        self.assertEqual(chinese_to_iccs("马2进3", START_FEN), "h0g2")

    def test_loose_advisor_elephant(self):
        """红方把 仕/相 写成 士/象 的棋谱要能容错"""
        self.assertEqual(chinese_to_iccs("仕六进五", START_FEN), "d0e1")
        self.assertEqual(chinese_to_iccs("士六进五", START_FEN), "d0e1")
        self.assertEqual(chinese_to_iccs("相三进五", START_FEN), "g0e2")
        self.assertEqual(chinese_to_iccs("象三进五", START_FEN), "g0e2")

    def test_unknown(self):
        for bad in ("", None, "炮二平五五", "飞龙在天", "炮八平十"):
            self.assertIsNone(chinese_to_iccs(bad, START_FEN), f"{bad!r} 不该认出来")

    def test_front_back_disambiguation(self):
        """同一条纵线上两枚红车: 前车/后车"""
        fen = "9/9/9/9/9/9/9/R8/R8/4K4 w - - 0 1"
        self.assertEqual(chinese_to_iccs("前车平八", fen), "a2b2")
        self.assertEqual(chinese_to_iccs("后车退一", fen), "a1a0")
        self.assertNotEqual(chinese_to_iccs("前车平八", fen),
                            chinese_to_iccs("后车平八", fen))

    def test_black_side(self):
        """轮到黑方时, 索引里只有黑方着法, 不会串到红方"""
        fen = BLACK_TURN_FEN
        self.assertEqual(chinese_to_iccs("马8进7", fen), "h9g7")
        self.assertIsNone(chinese_to_iccs("炮二平五", fen), "黑方回合不该认红方着法")


class TestReplay(unittest.TestCase):
    def test_replay_opening(self):
        result = replay(START_FEN, OPENING)
        self.assertEqual(result["warnings"], [])
        self.assertEqual([m["iccs"] for m in result["moves"]], OPENING)
        self.assertEqual([m["chinese"] for m in result["moves"]], OPENING_CN)
        self.assertEqual([m["ply"] for m in result["moves"]], [1, 2, 3, 4])
        self.assertEqual([m["side"] for m in result["moves"]],
                         ["red", "black", "red", "black"])
        self.assertEqual(result["moves"][0]["from_sq"], "h2")
        self.assertEqual(result["moves"][0]["to_sq"], "e2")
        self.assertEqual(result["moves"][0]["fen_before"], START_FEN)
        self.assertEqual(result["moves"][1]["fen_before"], result["moves"][0]["fen_after"])
        self.assertEqual(result["moves"][-1]["fen_after"], result["fen"])
        self.assertEqual(result["side"], "red")

    def test_replay_capture(self):
        """红炮隔子吃马, captured 要记下来"""
        result = replay(START_FEN, ["b2b9"])
        self.assertEqual(result["moves"][0]["captured"], "n")
        self.assertEqual(result["moves"][0]["iccs"], "b2b9")

    def test_replay_skips_bad_move_without_aborting(self):
        result = replay(START_FEN, ["h2e2", "h2e2", "h9g7"])
        self.assertEqual([m["iccs"] for m in result["moves"]], ["h2e2", "h9g7"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("第 2 步", result["warnings"][0])
        self.assertIn("跳过", result["warnings"][0])

    def test_replay_bad_start_fen(self):
        with self.assertRaises(RuleError):
            replay("9/9/9 w - - 0 1", [])

    def test_replay_from_midgame_fen(self):
        """从半途的局面接着重放"""
        mid = replay(START_FEN, OPENING)["fen"]
        result = replay(mid, ["g3g4"])          # 兵三进一
        self.assertEqual(result["warnings"], [], result["warnings"])
        self.assertEqual(result["moves"][0]["iccs"], "g3g4")


class TestExportImport(unittest.TestCase):
    def test_fenmoves_roundtrip(self):
        text = export_fenmoves(START_FEN, MOVES)
        self.assertTrue(text.startswith(START_FEN + " moves"))
        data = import_content(text)
        self.assertEqual(data["fmt"], "fenmoves")
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)
        self.assertEqual(data["warnings"], [])

    def test_fenmoves_without_keyword(self):
        data = import_content(f"{START_FEN} {' '.join(OPENING)}")
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)

    def test_iccs_roundtrip(self):
        text = export_iccs(MOVES)
        self.assertEqual(text, "h2e2 h9g7 h0g2 i9h9")
        data = import_content(text)
        self.assertEqual(data["fmt"], "iccs")
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)

    def test_iccs_multiline(self):
        text = export_iccs(MOVES, per_line=2)
        self.assertEqual(text.split("\n"), ["h2e2 h9g7", "h0g2 i9h9"])
        self.assertEqual(len(import_content(text)["moves"]), 4)

    def test_pgn_iccs_roundtrip(self):
        text = export_pgn(GAME, MOVES)
        self.assertIn('[Format "ICCS"]', text)
        self.assertIn("1. h2e2 {当头炮} h9g7 2. h0g2 i9h9", text)
        self.assertTrue(text.rstrip().endswith("*"))
        data = import_content(text)
        self.assertEqual(data["fmt"], "pgn")
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)
        self.assertEqual(data["meta"]["name"], "测试对局")
        self.assertEqual(data["meta"]["date"], "2026-09-17")
        self.assertEqual(data["comments"], ["当头炮"])
        self.assertEqual(data["warnings"], [])

    def test_pgn_chinese_roundtrip(self):
        """用中文着法导出, 再导回来必须还是同一串坐标"""
        text = export_pgn(GAME, MOVES, move_format="chinese")
        self.assertIn('[Format "Chinese"]', text)
        self.assertIn("炮二平五", text)
        data = import_content(text)
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)
        self.assertEqual(data["comments"], ["当头炮"])
        self.assertEqual(data["warnings"], [])

    def test_pgn_black_starts(self):
        """起始局面轮到黑方时, 第一步前面要有省略号"""
        black_fen = replay(START_FEN, ["h2e2"])["fen"]
        text = export_pgn({"start_fen": black_fen}, ["h9g7"])
        self.assertIn("1. ... h9g7", text)
        self.assertEqual([m["iccs"] for m in import_content(text)["moves"]], ["h9g7"])

    def test_pgn_missing_tags_and_fallback(self):
        """缺标签用默认值; 没有 [FEN] 就用标准开局"""
        text = "[Red \"甲\"]\n\n1. h2e2 h9g7 *\n"
        data = parse_pgn(text)
        self.assertEqual(data["start_fen"], START_FEN)
        self.assertEqual(data["meta"], {"red_name": "甲"})
        self.assertEqual([m["iccs"] for m in import_content(text)["moves"]], ["h2e2", "h9g7"])

    def test_pgn_ignores_variations_with_warning(self):
        text = export_pgn(GAME, MOVES) + "\n"
        text = text.replace("h0g2 i9h9", "h0g2 (h9g7 i9h9) i9h9")
        data = parse_pgn(text)
        self.assertTrue(any("变着" in w for w in data["warnings"]), data["warnings"])
        self.assertEqual([m["iccs"] for m in import_content(text)["moves"]], OPENING)

    def test_pgn_comment_braces_escaped(self):
        text = export_pgn(GAME, [{"iccs": "h2e2", "comment": "注意 {这里} 很关键"}])
        self.assertNotIn("{这里}", text)
        self.assertIn("(这里)", text)

    def test_json_roundtrip(self):
        text = export_json(GAME, MOVES)
        data = import_content(text)
        self.assertEqual(data["fmt"], "json")
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)
        self.assertEqual(data["meta"]["name"], "测试对局")
        # json 里评语挂在每一步上, 重放之后要并回来
        self.assertEqual(data["moves"][0]["comment"], "当头炮")

    def test_json_accepts_bare_move_list(self):
        text = export_json({}, ["h2e2", "h9g7"])
        data = import_content(text)
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING[:2])

    def test_json_keeps_scores(self):
        """备份恢复不能丢评分与关键步标记"""
        moves = [{"iccs": "h2e2", "score_cp": 45, "is_key": 1, "eval_by": "engine"}]
        data = import_content(export_json({}, moves))
        self.assertEqual(data["moves"][0]["score_cp"], 45)
        self.assertEqual(data["moves"][0]["is_key"], 1)
        self.assertEqual(data["moves"][0]["eval_by"], "engine")

    def test_fen_only_import(self):
        """只给 FEN 就只存局面, 不重放着法"""
        data = import_content(START_FEN)
        self.assertEqual(data["fmt"], "fen")
        self.assertEqual(data["moves"], [])
        self.assertEqual(data["start_fen"], START_FEN)

    def test_import_warnings_do_not_abort(self):
        data = import_content(f"{START_FEN} moves h2e2 乱码 h9g7")
        self.assertEqual([m["iccs"] for m in data["moves"]], ["h2e2", "h9g7"])
        self.assertEqual(len(data["warnings"]), 1)

    def test_import_bad_fen_raises(self):
        with self.assertRaises(RuleError):
            import_content("9/9/9 moves h2e2")

    def test_import_unknown_format(self):
        with self.assertRaises(RuleError):
            import_content("随便写点什么")
        with self.assertRaises(RuleError):
            import_content("h2e2", fmt="docx")

    def test_detect_format(self):
        self.assertEqual(detect_format(START_FEN), "fen")
        self.assertEqual(detect_format(f"{START_FEN} moves h2e2"), "fenmoves")
        self.assertEqual(detect_format("h2e2 h9g7"), "iccs")
        self.assertEqual(detect_format(export_pgn(GAME, MOVES)), "pgn")
        self.assertEqual(detect_format(export_json(GAME, MOVES)), "json")
        self.assertEqual(detect_format('["h2e2"]'), "json")
        with self.assertRaises(RuleError):
            detect_format("   ")

    def test_detect_pgn_without_tags(self):
        """没有标签段的类 PGN 文本 (下棋页/我的棋局里的示例输入就是这种)"""
        self.assertEqual(detect_format("1. 炮二平五 马8进7 2. 马二进三 车9平8 *"), "pgn")
        self.assertEqual(detect_format("炮二平五 马8进7"), "pgn")
        self.assertEqual(detect_format("1. h2e2 h9g7 2. h0g2"), "pgn")

    def test_import_pgn_without_tags(self):
        data = import_content("1. 炮二平五 马8进7 2. 马二进三 车9平8 *")
        self.assertEqual(data["fmt"], "pgn")
        self.assertEqual([m["iccs"] for m in data["moves"]], OPENING)
        self.assertEqual([m["chinese"] for m in data["moves"]], OPENING_CN)
        self.assertEqual(data["start_fen"], START_FEN)
        self.assertEqual(data["warnings"], [])

    def test_explicit_format_wins(self):
        """显式指定格式时不做猜测 (哪怕是 PGN 文本也按 iccs 解析失败)"""
        text = export_fenmoves(START_FEN, MOVES)
        self.assertEqual(len(import_content(text, fmt="fenmoves")["moves"]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
