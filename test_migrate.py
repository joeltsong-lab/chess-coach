# -*- coding: utf-8 -*-
"""migrate.py 的单元测试 (标准库 unittest, 全程临时库, 绝不碰 data/chess.db)

    python test_migrate.py
    python -m unittest test_migrate -v

重点覆盖 0001 迁移里风险最高的两处:
  1. games 表是"重建"出来的 —— 旧数据必须一条不少、moves 的级联删除必须还有效
  2. 影子库演练 —— 迁移写坏时真库不能被改
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from chess_engine import START_FEN

import migrate
import storage

AFTER_1 = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1"

EG_TABLES = ("endgame_positions", "endgame_candidates",
             "endgame_position_moves", "endgame_tasks")
NEW_GAME_COLUMNS = ("study_id", "fen_text", "rule_set", "opening_position_id")


def tables_of(db_path: Path) -> set:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def columns_of(db_path: Path, table: str) -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


class MigrateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="xq_migrate_")
        self._saved_path = storage.DB_PATH
        self._saved_env = os.environ.pop("XQ_DB_PATH", None)
        storage.DB_PATH = Path(self.tmpdir) / "chess.db"
        storage.init_db()          # 先建出 0001 之前的旧结构
        self.db = storage.DB_PATH

    def tearDown(self):
        storage.DB_PATH = self._saved_path
        if self._saved_env is not None:
            os.environ["XQ_DB_PATH"] = self._saved_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def seed_legacy(self) -> dict:
        """塞入 0001 之前的旧数据: 三种旧分类 + 一局带两步着法"""
        ids = {}
        ids["imported"] = storage.create_game(
            {"name": "旧导入局", "category": "imported", "start_fen": START_FEN})
        storage.append_move(ids["imported"], {
            "iccs": "h2e2", "fen_before": START_FEN, "fen_after": AFTER_1})
        storage.append_move(ids["imported"], {
            "iccs": "h9g7", "fen_before": AFTER_1,
            "fen_after": AFTER_1.replace(" b ", " w ")})
        ids["opening"] = storage.create_game({"name": "旧开局", "category": "opening"})
        ids["study"] = storage.create_game({"name": "旧研究", "category": "study"})
        return ids


class TestSplitStatements(unittest.TestCase):
    def test_splits_on_semicolons(self):
        self.assertEqual(migrate.split_statements("SELECT 1; SELECT 2;"),
                         ["SELECT 1", "SELECT 2"])

    def test_ignores_line_and_block_comments(self):
        sql = "-- 注释; 不算语句\nSELECT 1; /* 块注释; 也不算 */ SELECT 2;"
        self.assertEqual(migrate.split_statements(sql), ["SELECT 1", "SELECT 2"])

    def test_keeps_semicolon_inside_string_literal(self):
        sql = "INSERT INTO t VALUES ('a;b'); SELECT 2;"
        self.assertEqual(migrate.split_statements(sql),
                         ["INSERT INTO t VALUES ('a;b')", "SELECT 2"])

    def test_handles_doubled_quote_escape(self):
        sql = "INSERT INTO t VALUES ('it''s; fine');"
        self.assertEqual(migrate.split_statements(sql),
                         ["INSERT INTO t VALUES ('it''s; fine')"])

    def test_rejects_trigger_body(self):
        with self.assertRaises(migrate.MigrationError):
            migrate.split_statements(
                "CREATE TRIGGER t AFTER INSERT ON games BEGIN SELECT 1; END;")


class TestUp(MigrateTestCase):
    def test_creates_new_tables_and_columns(self):
        migrate.migrate("up")
        tables = tables_of(self.db)
        for name in EG_TABLES:
            self.assertIn(name, tables)
        self.assertIn("schema_migrations", tables)
        cols = columns_of(self.db, "games")
        for col in NEW_GAME_COLUMNS:
            self.assertIn(col, cols)

    def test_keeps_legacy_rows_and_categories(self):
        ids = self.seed_legacy()
        migrate.migrate("up")

        for key, name in (("imported", "旧导入局"), ("opening", "旧开局"), ("study", "旧研究")):
            meta = storage.get_game_meta(ids[key])
            self.assertIsNotNone(meta, f"{name} 迁移后丢了")
            self.assertEqual(meta["name"], name)
            self.assertEqual(meta["category"], key)          # 旧分类原样保留, 没被改写
            self.assertIsNone(meta["study_id"])              # 新列给默认值
            self.assertEqual(meta["rule_set"], "chinese_2020_analysis")

        # 步数也一条不少
        self.assertEqual(storage.count_moves(ids["imported"]), 2)

    def test_move_cascade_still_works_after_rebuild(self):
        """重建 games 表最容易踩的坑: moves 的外键被 RENAME 改写 → 级联删除失效"""
        ids = self.seed_legacy()
        migrate.migrate("up")

        self.assertTrue(storage.delete_game(ids["imported"]))
        self.assertEqual(storage.count_moves(ids["imported"]), 0)
        with storage.tx() as conn:
            left = conn.execute("SELECT COUNT(*) AS n FROM moves").fetchone()["n"]
        self.assertEqual(left, 0)

    def test_category_check_is_enforced(self):
        migrate.migrate("up")
        conn = storage.get_conn()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO games (name, category) VALUES ('x', '乱写')")
            conn.rollback()
            # 8 个合法值都要能插进去
            for cat in storage.CATEGORIES:
                conn.execute("INSERT INTO games (name, category) VALUES (?, ?)", ("x", cat))
            conn.rollback()
        finally:
            conn.close()

    def test_updated_at_default_is_local_time(self):
        migrate.migrate("up")
        conn = storage.get_conn()
        try:
            # datetime('now') 是 UTC, 这里必须用 localtime, 否则和队列里的时间混着排不对序
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'games'").fetchone()["sql"]
        finally:
            conn.close()
        self.assertIn("localtime", ddl)

    def test_up_is_idempotent(self):
        first = migrate.migrate("up")
        self.assertEqual(len(first["applied"]), 1)
        second = migrate.migrate("up")
        self.assertEqual(second["applied"], [])
        self.assertEqual(second["shadow"], [])

    def test_journal_mode_report_matches_reality(self):
        """优先 WAL; 建不出 -wal 的受限环境退回默认模式, 但必须如实上报"""
        report = migrate.migrate("up")
        conn = sqlite3.connect(str(self.db))
        try:
            actual = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        finally:
            conn.close()
        self.assertIn(report["journal_mode"], ("wal", "delete"))
        self.assertEqual(report["journal_mode"], actual)
        # 不管哪种模式, 迁移完整跑完 + 库能正常写
        self.assertEqual(len(report["applied"]), 1)
        self.assertIsNotNone(storage.create_game({"name": "写一笔试试"}))

    def test_new_tables_reference_games_with_cascade(self):
        gid = storage.create_game({"name": "研究局", "category": "endgame"})
        migrate.migrate("up")
        with storage.tx() as conn:
            conn.execute(
                "INSERT INTO endgame_positions (game_id, ply, fen, position_hash, to_move)"
                " VALUES (?, 0, ?, 'h', 'r')", (gid, START_FEN))
        self.assertTrue(storage.delete_game(gid))
        with storage.tx() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM endgame_positions").fetchone()["n"]
        self.assertEqual(n, 0)


class TestDown(MigrateTestCase):
    def test_down_removes_tables_and_columns(self):
        ids = self.seed_legacy()
        migrate.migrate("up")
        migrate.migrate("down")

        tables = tables_of(self.db)
        for name in EG_TABLES:
            self.assertNotIn(name, tables)
        cols = columns_of(self.db, "games")
        for col in NEW_GAME_COLUMNS:
            self.assertNotIn(col, cols)
        # 旧数据照样在
        self.assertEqual(storage.get_game_meta(ids["opening"])["category"], "opening")
        self.assertEqual(storage.count_moves(ids["imported"]), 2)

    def test_down_maps_new_categories_back_to_game(self):
        """category 的回滚是有损的: pattern/custom 并回 game, 否则旧筛选看不到"""
        migrate.migrate("up")
        pid = storage.create_game({"name": "定式", "category": "pattern"})
        cid = storage.create_game({"name": "摆设", "category": "custom"})
        migrate.migrate("down")
        self.assertEqual(storage.get_game_meta(pid)["category"], "game")
        self.assertEqual(storage.get_game_meta(cid)["category"], "game")

    def test_up_down_up_round_trip(self):
        ids = self.seed_legacy()
        migrate.migrate("up")
        migrate.migrate("down")
        report = migrate.migrate("up")

        self.assertEqual(len(report["applied"]), 1)
        for name in EG_TABLES:
            self.assertIn(name, tables_of(self.db))
        self.assertEqual(storage.count_moves(ids["imported"]), 2)
        self.assertEqual(len(storage.list_games()), 3)

    def test_status_reports_applied(self):
        self.assertFalse(migrate.status()[0]["applied"])
        migrate.migrate("up")
        row = migrate.status()[0]
        self.assertTrue(row["applied"])
        self.assertEqual(row["version"], 1)


class TestShadowRehearsal(MigrateTestCase):
    def test_failed_migration_leaves_real_db_untouched(self):
        """影子库演练的意义: 迁移写坏时真库一个字节都不能动"""
        self.seed_legacy()

        bad_dir = Path(self.tmpdir) / "bad_migrations"
        bad_dir.mkdir()
        (bad_dir / "0002_broken_up.sql").write_text(
            "CREATE TABLE broken (", encoding="utf-8")
        (bad_dir / "0002_broken_down.sql").write_text(
            "DROP TABLE IF EXISTS broken;", encoding="utf-8")

        real_discover = migrate.discover_migrations
        migrate.discover_migrations = lambda *a, **k: (
            real_discover() + real_discover(bad_dir))
        try:
            with self.assertRaises(sqlite3.Error):
                migrate.migrate("up", shadow=True)
        finally:
            migrate.discover_migrations = real_discover

        # 真库必须还是迁移前的样子: 0001 都没应用, 新表新列都不存在, 旧数据一条不少
        tables = tables_of(self.db)
        for name in EG_TABLES:
            self.assertNotIn(name, tables)
        self.assertNotIn("schema_migrations", tables)
        self.assertNotIn("study_id", columns_of(self.db, "games"))
        self.assertEqual(len(storage.list_games()), 3)

    def test_no_shadow_flag_skips_rehearsal(self):
        report = migrate.migrate("up", shadow=False)
        self.assertEqual(report["shadow"], [])
        self.assertEqual(len(report["applied"]), 1)


class TestDbPathOption(MigrateTestCase):
    def test_db_path_argument_does_not_touch_default_db(self):
        """回归: --db 指向别的库时, 基础表也得建在那个库上, 不能写默认库"""
        other = Path(self.tmpdir) / "other.db"
        migrate.migrate("up", db_path=other, shadow=False)

        self.assertIn("endgame_positions", tables_of(other))
        tables = tables_of(self.db)
        self.assertNotIn("endgame_positions", tables)
        self.assertNotIn("study_id", columns_of(self.db, "games"))


class TestDiscovery(unittest.TestCase):
    def test_missing_down_file_is_reported(self):
        tmp = tempfile.mkdtemp(prefix="xq_mig_disc_")
        try:
            (Path(tmp) / "0007_only_up_up.sql").write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaises(migrate.MigrationError):
                migrate.discover_migrations(Path(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_migrations_are_paired(self):
        versions = [(m.version, m.name) for m in migrate.discover_migrations()]
        self.assertEqual(versions, [(1, "endgame_study")])
        for mig in migrate.discover_migrations():
            self.assertTrue(mig.up_path.is_file())
            self.assertTrue(mig.down_path.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
