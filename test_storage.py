# -*- coding: utf-8 -*-
"""storage 的单元测试 (标准库 unittest, 全程用临时数据库, 绝不碰真实的 data/chess.db)

    python test_storage.py
    python -m unittest test_storage -v
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core.chess_engine import START_FEN

from core import storage

START_FEN_BLACK = START_FEN.replace(" w ", " b ")


def make_game(**fields) -> int:
    """建一局最小的棋(默认给标准起始 FEN), 返回对局 id"""
    fields.setdefault("name", "测试对局")
    fields.setdefault("start_fen", START_FEN)
    return storage.create_game(fields)


class StorageTestCase(unittest.TestCase):
    """每个测试方法一个全新的临时库: 换掉 storage.DB_PATH, 并暂时清掉 XQ_DB_PATH"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="xq_storage_")
        self._saved_path = storage.DB_PATH
        self._saved_env = os.environ.pop("XQ_DB_PATH", None)
        storage.DB_PATH = Path(self.tmpdir) / "chess.db"
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self._saved_path
        if self._saved_env is not None:
            os.environ["XQ_DB_PATH"] = self._saved_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestInitDb(StorageTestCase):
    def test_init_twice_is_idempotent(self):
        storage.init_db()
        storage.init_db()   # 再建一次不能报错
        with storage.tx() as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            indexes = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("games", tables)
        self.assertIn("moves", tables)
        for idx in ("idx_moves_game_ply", "idx_games_category_updated", "idx_games_tags"):
            self.assertIn(idx, indexes)


class TestCreateGame(StorageTestCase):
    def test_defaults_and_timestamps(self):
        gid = storage.create_game({"name": "开局研究", "start_fen": START_FEN})
        meta = storage.get_game_meta(gid)
        self.assertEqual(meta["id"], gid)
        self.assertEqual(meta["name"], "开局研究")
        self.assertEqual(meta["category"], "game")
        self.assertEqual(meta["result"], "*")
        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["start_fen"], START_FEN)
        self.assertEqual(meta["current_fen"], START_FEN)      # 空时用 start_fen 兜底
        self.assertTrue(meta["created_at"])
        self.assertTrue(meta["updated_at"])
        self.assertEqual(meta["created_at"], meta["updated_at"])
        time.strptime(meta["created_at"], "%Y-%m-%d %H:%M:%S")   # 格式必须是本地时间字符串
        self.assertIsNone(meta["note"])

    def test_unknown_fields_ignored(self):
        gid = storage.create_game({"name": "x", "id": 999, "不存在的列": 1})
        self.assertNotEqual(gid, 999)
        self.assertEqual(storage.get_game_meta(gid)["id"], gid)

    def test_defaults_tuple_constants(self):
        # pattern / custom 是残局研究迁移新加的两个分类, 前 5 个是原有的
        self.assertEqual(storage.CATEGORIES,
                         ("game", "opening", "endgame", "study", "imported",
                          "pattern", "custom"))
        self.assertEqual(storage.RESULTS, ("1-0", "0-1", "1/2-1/2", "*"))
        self.assertEqual(storage.STATUSES, ("active", "finished", "archived"))


class TestSideToMove(StorageTestCase):
    def test_inferred_from_start_fen(self):
        red = make_game(start_fen=START_FEN)
        black = make_game(start_fen=START_FEN_BLACK)
        self.assertEqual(storage.get_game_meta(red)["side_to_move"], "w")
        self.assertEqual(storage.get_game_meta(black)["side_to_move"], "b")

    def test_missing_fen_defaults_to_red(self):
        gid = storage.create_game({"name": "没有 FEN"})
        self.assertEqual(storage.get_game_meta(gid)["side_to_move"], "w")


class TestAppendMove(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.gid = make_game()

    def test_ply_and_game_refresh(self):
        created = storage.get_game_meta(self.gid)["updated_at"]
        time.sleep(1.05)   # updated_at 是秒级, 睡够一秒才看得出被刷新
        m1 = storage.append_move(self.gid, {"iccs": "h2e2", "chinese": "炮二平五",
                                            "fen_before": START_FEN,
                                            "fen_after": START_FEN_BLACK})
        meta = storage.get_game_meta(self.gid)
        self.assertEqual(meta["current_fen"], START_FEN_BLACK)
        self.assertEqual(meta["side_to_move"], "b")        # 从 fen_after 的第二个字段推断
        self.assertGreater(meta["updated_at"], created)

        m2 = storage.append_move(self.gid, {"iccs": "h9g7", "chinese": "马8进7",
                                            "fen_after": START_FEN})
        self.assertEqual(storage.get_game_meta(self.gid)["side_to_move"], "w")
        # next_side 显式传了就用它, 不看 fen_after
        m3 = storage.append_move(self.gid, {"iccs": "h0g2", "chinese": "马二进三",
                                            "fen_after": START_FEN, "next_side": "b"})
        self.assertEqual(storage.get_game_meta(self.gid)["side_to_move"], "b")

        self.assertLess(m1, m2)
        self.assertLess(m2, m3)
        moves = storage.list_moves(self.gid)
        self.assertEqual([m["ply"] for m in moves], [1, 2, 3])
        self.assertEqual(storage.count_moves(self.gid), 3)
        self.assertEqual([m["chinese"] for m in moves], ["炮二平五", "马8进7", "马二进三"])

    def test_all_columns_persisted(self):
        mid = storage.append_move(self.gid, {
            "iccs": "h2e2", "chinese": "炮二平五", "side": "w", "from_sq": "h2", "to_sq": "e2",
            "captured": "p", "fen_before": START_FEN, "fen_after": START_FEN_BLACK,
            "score_cp": 30, "score_mate": None, "eval_by": "engine", "comment": "开局",
            "is_key": 1,
        })
        mv = storage.get_move(mid)
        self.assertEqual(mv["game_id"], self.gid)
        self.assertEqual(mv["side"], "w")
        self.assertEqual(mv["from_sq"], "h2")
        self.assertEqual(mv["to_sq"], "e2")
        self.assertEqual(mv["captured"], "p")
        self.assertEqual(mv["fen_before"], START_FEN)
        self.assertEqual(mv["fen_after"], START_FEN_BLACK)
        self.assertEqual(mv["score_cp"], 30)
        self.assertIsNone(mv["score_mate"])
        self.assertEqual(mv["eval_by"], "engine")
        self.assertEqual(mv["is_key"], 1)
        self.assertIsNone(mv["branch_of_move_id"])    # 这一轮主线一律 NULL

    def test_explicit_ply_kept_then_continue(self):
        mid = storage.append_move(self.gid, {"ply": 10, "iccs": "h2e2"})
        self.assertEqual(storage.get_move(mid)["ply"], 10)
        mid2 = storage.append_move(self.gid, {"iccs": "h9g7"})
        self.assertEqual(storage.get_move(mid2)["ply"], 11)

    def test_unknown_game_id_raises(self):
        with self.assertRaises(ValueError):
            storage.append_move(99999, {"iccs": "h2e2"})
        self.assertEqual(storage.count_moves(99999), 0)   # 事务回滚, 没留下垃圾

    def test_get_game_returns_moves_in_order(self):
        for iccs in ("h2e2", "h9g7", "h0g2"):
            storage.append_move(self.gid, {"iccs": iccs, "fen_after": START_FEN})
        got = storage.get_game(self.gid)
        self.assertEqual(got["game"]["id"], self.gid)
        self.assertEqual([m["iccs"] for m in got["moves"]], ["h2e2", "h9g7", "h0g2"])


class TestListGames(StorageTestCase):
    def test_move_count_last_chinese_and_order(self):
        a = make_game(name="A")
        b = make_game(name="B")
        time.sleep(1.05)   # 让 A 的 updated_at 明确晚于 B
        storage.append_move(a, {"iccs": "h2e2", "chinese": "炮二平五",
                                "fen_after": START_FEN_BLACK})
        storage.append_move(a, {"iccs": "h9g7", "chinese": "马8进7", "fen_after": START_FEN})

        rows = storage.list_games()
        self.assertEqual([r["id"] for r in rows], [a, b])    # 按 updated_at 倒序
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(by_id[a]["move_count"], 2)
        self.assertEqual(by_id[a]["last_chinese"], "马8进7")   # 最后一步
        self.assertEqual(by_id[b]["move_count"], 0)
        self.assertIsNone(by_id[b]["last_chinese"])
        self.assertNotIn("moves", by_id[a])                  # 列表里不带 moves 数组
        self.assertEqual(storage.count_games(), 2)

    def test_category_exact_match(self):
        a = make_game(category="opening")
        make_game(category="game")
        self.assertEqual([r["id"] for r in storage.list_games(category="opening")], [a])
        self.assertEqual(storage.list_games(category="openingX"), [])   # 是精确匹配, 不是模糊
        self.assertEqual(storage.count_games(category="opening"), 1)
        self.assertEqual(storage.count_games(category="openingX"), 0)

    def test_combined_filters(self):
        a = make_game(name="中炮对屏风马", category="opening", tags="中炮,开局")
        make_game(name="中炮对反宫马", category="game", tags="中炮,开局")
        self.assertEqual(storage.count_games(category="opening", tag="中炮"), 1)
        self.assertEqual([r["id"] for r in storage.list_games(category="opening",
                                                              keyword="屏风马")], [a])


class TestTagFilter(StorageTestCase):
    def test_substring_match(self):
        a = make_game(name="中炮残局", tags="中炮,残局")
        b = make_game(name="仙人指路局", tags="仙人指路,开局")
        self.assertEqual([r["id"] for r in storage.list_games(tag="中炮")], [a])
        self.assertEqual([r["id"] for r in storage.list_games(tag="残局")], [a])
        self.assertEqual([r["id"] for r in storage.list_games(tag="仙人指路")], [b])
        self.assertEqual(storage.list_games(tag="仙人指路对"), [])   # 不命中
        self.assertEqual(storage.list_games(tag="当头炮"), [])
        self.assertEqual(storage.count_games(tag="中炮"), 1)
        self.assertEqual(storage.count_games(tag="当头炮"), 0)


class TestKeywordFilter(StorageTestCase):
    def test_name_note_and_other_columns(self):
        a = make_game(name="仙人指路对局")
        b = make_game(name="随便一局", note="这局走了中炮")
        c = make_game(name="别人的局", red_name="张三", opening="顺炮")
        d = make_game(name="再一局", black_name="李四")

        def hits(kw):
            return {r["id"] for r in storage.list_games(keyword=kw)}

        self.assertEqual(hits("仙人指路"), {a})     # 命中 name
        self.assertEqual(hits("中炮"), {b})         # 命中 note
        self.assertEqual(hits("张三"), {c})         # 命中 red_name
        self.assertEqual(hits("顺炮"), {c})         # 命中 opening
        self.assertEqual(hits("李四"), {d})         # 命中 black_name
        self.assertEqual(hits("不存在的词"), set())
        self.assertEqual(storage.count_games(keyword="中炮"), 1)


class TestUpdateWhitelist(StorageTestCase):
    def test_update_game_ignores_unknown_and_id(self):
        gid = make_game()
        before = storage.get_game_meta(gid)
        self.assertTrue(storage.update_game(gid, {
            "name": "改过的名字", "result": "1-0", "status": "finished",
            "id": 999, "game_id": 999, "created_at": "1970-01-01 00:00:00", "不存在的列": 1,
        }))
        after = storage.get_game_meta(gid)
        self.assertEqual(after["id"], gid)                            # id 改不到
        self.assertEqual(after["name"], "改过的名字")
        self.assertEqual(after["result"], "1-0")
        self.assertEqual(after["status"], "finished")
        self.assertEqual(after["created_at"], before["created_at"])   # created_at 改不到
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])

    def test_update_move_comment_visible_in_get_game(self):
        gid = make_game()
        mid = storage.append_move(gid, {"iccs": "h2e2", "chinese": "炮二平五",
                                        "fen_after": START_FEN_BLACK})
        self.assertTrue(storage.update_move(mid, {
            "comment": "开局好棋", "is_key": 1, "id": 999, "game_id": 999, "不存在的列": 1,
        }))
        mv = storage.get_move(mid)
        self.assertEqual(mv["id"], mid)          # id 改不到
        self.assertEqual(mv["game_id"], gid)     # 归属也改不到
        self.assertEqual(mv["comment"], "开局好棋")
        self.assertEqual(mv["is_key"], 1)

        got = storage.get_game(gid)
        self.assertEqual(got["moves"][0]["comment"], "开局好棋")
        self.assertEqual(got["game"]["note"], storage.get_game_meta(gid)["note"])

    def test_append_move_cannot_override_game_id(self):
        gid = make_game()
        other = make_game(name="另一局")
        mid = storage.append_move(gid, {"iccs": "h2e2", "game_id": other})
        self.assertEqual(storage.get_move(mid)["game_id"], gid)

    def test_update_move_refreshes_game_updated_at(self):
        gid = make_game()
        mid = storage.append_move(gid, {"iccs": "h2e2"})
        before = storage.get_game_meta(gid)["updated_at"]
        time.sleep(1.05)
        self.assertTrue(storage.update_move(mid, {"comment": "补个注释"}))
        self.assertGreater(storage.get_game_meta(gid)["updated_at"], before)

    def test_set_game_note(self):
        gid = make_game()
        self.assertTrue(storage.set_game_note(gid, "这里是我的复盘笔记"))
        self.assertEqual(storage.get_game_meta(gid)["note"], "这里是我的复盘笔记")


class TestDeleteMovesAfter(StorageTestCase):
    def test_truncate_keeps_contiguous_plies(self):
        gid = make_game()
        for i in range(5):
            storage.append_move(gid, {"iccs": f"a{i}a{i}", "chinese": f"第{i + 1}步"})
        self.assertEqual(storage.count_moves(gid), 5)
        self.assertEqual(storage.delete_moves_after(gid, 3), 2)     # 删掉 ply 4、5
        plies = [m["ply"] for m in storage.list_moves(gid)]
        self.assertEqual(plies, [1, 2, 3])
        self.assertEqual(plies, list(range(1, len(plies) + 1)))      # 仍然连续
        self.assertEqual(storage.delete_moves_after(gid, 3), 0)      # 再删就没东西可删了
        self.assertEqual(storage.count_moves(gid), 3)

    def test_appending_after_truncate_reuses_ply(self):
        gid = make_game()
        for i in range(3):
            storage.append_move(gid, {"iccs": f"a{i}a{i}"})
        storage.delete_moves_after(gid, 1)
        mid = storage.append_move(gid, {"iccs": "b0c2"})
        self.assertEqual(storage.get_move(mid)["ply"], 2)


class TestDeleteGameCascade(StorageTestCase):
    def test_foreign_keys_pragma_is_on(self):
        with storage.tx() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'moves'").fetchone()
        self.assertIn("ON DELETE CASCADE", ddl["sql"])

    def test_delete_game_removes_its_moves(self):
        gid = make_game()
        storage.append_move(gid, {"iccs": "h2e2", "fen_after": START_FEN_BLACK})
        storage.append_move(gid, {"iccs": "h9g7", "fen_after": START_FEN})
        keep = make_game(name="别删我")
        storage.append_move(keep, {"iccs": "h2e2"})
        self.assertEqual(storage.count_moves(gid), 2)

        self.assertTrue(storage.delete_game(gid))
        self.assertEqual(storage.list_moves(gid), [])     # 级联删干净了
        self.assertEqual(storage.count_moves(gid), 0)
        self.assertIsNone(storage.get_game(gid))
        self.assertEqual(storage.count_moves(keep), 1)    # 没误伤别人
        self.assertFalse(storage.delete_game(gid))        # 再删一次: 没删到


class TestMissingIds(StorageTestCase):
    def test_reads_return_none_or_empty(self):
        self.assertIsNone(storage.get_game(123456))
        self.assertIsNone(storage.get_game_meta(123456))
        self.assertIsNone(storage.get_move(123456))
        self.assertEqual(storage.list_moves(123456), [])
        self.assertEqual(storage.count_moves(123456), 0)
        self.assertEqual(storage.delete_moves_after(123456, 0), 0)

    def test_writes_return_false(self):
        self.assertFalse(storage.update_game(123456, {"name": "x"}))
        self.assertFalse(storage.delete_game(123456))
        self.assertFalse(storage.update_move(123456, {"comment": "x"}))
        self.assertFalse(storage.set_game_note(123456, "x"))
        with self.assertRaises(ValueError):
            storage.append_move(123456, {"iccs": "h2e2"})


class TestPagination(StorageTestCase):
    def test_limit_offset_pages(self):
        ids = [make_game(name=f"分页{i}") for i in range(7)]
        all_rows = storage.list_games(limit=200)
        self.assertEqual(len(all_rows), 7)
        # 同一秒创建的按 id 兜底, 整体仍是"新的在前"
        self.assertEqual([r["id"] for r in all_rows], sorted(ids, reverse=True))

        pages = [storage.list_games(limit=3, offset=off) for off in (0, 3, 6)]
        self.assertEqual([len(p) for p in pages], [3, 3, 1])
        self.assertEqual([r["id"] for p in pages for r in p],
                         [r["id"] for r in all_rows])            # 不重不漏
        self.assertEqual(storage.list_games(limit=3, offset=99), [])

    def test_limit_offset_clamped(self):
        for _ in range(3):
            make_game()
        self.assertEqual(len(storage.list_games(limit=0)), 1)        # limit 至少 1
        self.assertEqual(len(storage.list_games(limit=-5)), 1)
        self.assertEqual(len(storage.list_games(limit="2")), 2)      # 字符串也吃得下
        self.assertEqual(storage.list_games(limit=10, offset=-3),
                         storage.list_games(limit=10))               # offset 不小于 0

    def test_limit_capped_at_200(self):
        for i in range(205):
            make_game(name=f"批量{i}")
        self.assertEqual(storage.count_games(), 205)
        self.assertEqual(len(storage.list_games(limit=1000)), 200)
        self.assertEqual(len(storage.list_games(limit=1000, offset=200)), 5)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"\n通过 {result.testsRun - failed} 个 / 失败 {failed} 个 / 共 {result.testsRun} 个")
    sys.exit(0 if result.wasSuccessful() else 1)
