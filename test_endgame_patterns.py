# -*- coding: utf-8 -*-
"""内置定式库的测试。

这里最要紧的一条: **每个定式的 FEN 都必须过 fen_rules.validate_position**。
定式是要给用户"点一下就开始研究"的, 摆出来的局面如果根本走不下去(白脸将、
缺将、兵退到底线……), 前端一建研究就会 400, 所以把校验放在测试里当硬门槛,
而不是等运行时才发现。
"""

import unittest

from endgame import fen_rules, patterns


class TestPatternLibrary(unittest.TestCase):
    def test_library_has_ten_patterns_with_unique_ids(self):
        ids = patterns.pattern_ids()
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10, "定式 id 必须唯一(导入导出要引用它)")

    def test_every_pattern_fen_is_a_legal_position(self):
        """逐个过严格校验: 能当研究起点才允许留在库里"""
        for item in patterns.PATTERNS:
            with self.subTest(pattern=item["id"]):
                check = fen_rules.validate_position(item["fen"], to_move=item["sideToMove"])
                messages = [e["message"] for e in check["errors"]]
                self.assertTrue(check["legal"], f"{item['id']} 局面不合法: {messages}")
                self.assertEqual(check["side_to_move"], item["sideToMove"])

    def test_pattern_fen_is_already_canonical(self):
        """库里存的 FEN 必须就是规范化形式, 免得建研究时又被改一遍"""
        for item in patterns.PATTERNS:
            with self.subTest(pattern=item["id"]):
                check = fen_rules.validate_position(item["fen"], to_move=item["sideToMove"])
                self.assertEqual(check["fen"], item["fen"])

    def test_pattern_carries_no_fabricated_solution(self):
        """不许写着法序列: 没有引擎跑过的"正解"就是编造, 规格禁止"""
        forbidden = ("solution", "solutions", "line", "lines", "moves", "pv",
                     "bestmove", "variation")
        for item in patterns.PATTERNS:
            with self.subTest(pattern=item["id"]):
                for key in forbidden:
                    self.assertNotIn(key, item, f"{item['id']} 不该带着法字段 {key}")

    def test_required_fields_present(self):
        for item in patterns.PATTERNS:
            with self.subTest(pattern=item["id"]):
                for key in ("id", "name", "theme", "fen", "sideToMove",
                            "result", "resultNote", "goal", "tags"):
                    self.assertTrue(item.get(key), f"{item['id']} 缺少 {key}")
                self.assertIn(item["result"], ("例胜", "例和"))
                self.assertIn("非引擎结论", item["resultNote"])
                self.assertIsInstance(item["tags"], tuple)

    def test_get_pattern_and_copies(self):
        one = patterns.get_pattern("rook-vs-single-advisor")
        self.assertEqual(one["name"], "单车例胜单士")
        self.assertIsNone(patterns.get_pattern("no-such-pattern"))
        self.assertIsNone(patterns.get_pattern(None))

        # 拿到的必须是副本: 外部改 tags 不能污染模块常量表
        one["tags"].append("被改坏了")
        one["name"] = "被改坏了"
        fresh = patterns.get_pattern("rook-vs-single-advisor")
        self.assertEqual(fresh["name"], "单车例胜单士")
        self.assertNotIn("被改坏了", fresh["tags"])

    def test_list_patterns_is_a_copy(self):
        listed = patterns.list_patterns()
        listed[0]["tags"].append("被改坏了")
        listed[0]["id"] = "hacked"
        self.assertEqual(patterns.PATTERNS[0]["id"], "rook-vs-single-advisor")
        self.assertNotIn("被改坏了", patterns.PATTERNS[0]["tags"])

    def test_version_is_an_int(self):
        self.assertIsInstance(patterns.PATTERN_VERSION, int)
        self.assertGreaterEqual(patterns.PATTERN_VERSION, 1)


if __name__ == "__main__":
    unittest.main()
