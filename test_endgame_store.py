# -*- coding: utf-8 -*-
"""endgame.store 的单元测试 (标准库 unittest, 全程临时库, 不依赖引擎)

    python test_endgame_store.py
    python -m unittest test_endgame_store -v

重点覆盖:
  * 不同 depth / MultiPV / 规则集的分析结果互不覆盖 (规格的硬要求)
  * 同一套配置重跑是覆盖自己: 候选表不留半截数据、不重复
  * 并发写同一局面互斥
  * 任务状态机只允许规定的那几条迁移
"""

import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from chess_engine import START_FEN

import migrate
import storage
from endgame import fen_rules, store

AFTER_H2E2 = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1"


def candidate(rank, move, score=305, pv=None):
    return {
        "rank": rank, "multipv_index": rank, "move_uci": move,
        "from_square": move[:2], "to_square": move[2:4], "promotion": None,
        "score_type": "cp", "score_value": score, "mate_plies": None,
        "pv_uci": " ".join(pv or [move]), "pv_san": "炮二平五",
        "visited_nodes": 1000, "is_key_move": 0, "is_user_variation": 0,
    }


def analysis(fen=START_FEN, **over):
    """一份 analyzer.analyze_position() 风格的返回值 (字段与 endgame_positions 对齐)"""
    result = {
        "status": "completed",
        "fen": fen,
        "position_hash": fen_rules.position_hash(fen),
        "to_move": "r",
        "to_move_side": "w",
        "repetition_rule": store.DEFAULT_REPETITION_RULE,
        "engine_version": "Pikafish test-1",
        "nnue_file": "pikafish.nnue",
        "depth": 30, "seldepth": 42, "movetime_ms": 60000, "multipv": 3,
        "rule_aware": 1,
        "bestmove": "h2e2", "best_pv_uci": "h2e2 h9g7",
        "score_type": "cp", "score_value": 305, "normalized_win": 0.68,
        "result": "win", "result_for": "r", "mate_plies": None, "draw_type": None,
        "rule_review_status": "none", "confidence": "stable",
        "analysis_duration_ms": 61234,
        "candidates": [candidate(1, "h2e2", 305), candidate(2, "b2e2", 120)],
    }
    result.update(over)
    return result


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="xq_store_")
        self._saved_path = storage.DB_PATH
        # storage._db_path() 认环境变量, 别的模块(如 test_games_routes)留下的
        # XQ_DB_PATH 会把连接指到别处, 这里按 test_storage 的做法先摘掉
        self._saved_env = os.environ.pop("XQ_DB_PATH", None)
        storage.DB_PATH = Path(self.tmpdir) / "chess.db"
        storage.init_db()
        migrate.migrate(direction="up", db_path=storage.DB_PATH, shadow=False)
        self.game_id = storage.create_game({"name": "残局研究", "category": "study"})
        self.move_id = storage.append_move(self.game_id, {
            "iccs": "h2e2", "fen_before": START_FEN, "fen_after": AFTER_H2E2})

    def tearDown(self):
        storage.DB_PATH = self._saved_path
        if self._saved_env is not None:
            os.environ["XQ_DB_PATH"] = self._saved_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestSaveAnalysis(StoreTestCase):
    def test_first_save_creates_position_and_candidates(self):
        saved = store.save_analysis(analysis(), game_id=self.game_id, ply=1)
        self.assertTrue(saved["created"])
        self.assertEqual(len(saved["candidate_ids"]), 2)
        row = store.get_position(saved["position_id"])
        self.assertEqual(row["game_id"], self.game_id)
        self.assertEqual(row["ply"], 1)
        self.assertEqual(row["fen"], START_FEN)
        self.assertEqual(row["to_move"], "r")
        self.assertEqual(row["result"], "win")
        self.assertEqual(row["result_for"], "r")
        self.assertEqual(row["depth"], 30)
        self.assertEqual(row["multipv"], 3)
        self.assertEqual(row["rule_aware"], 1)
        self.assertEqual(row["engine_version"], "Pikafish test-1")
        self.assertEqual(row["nnue_file"], "pikafish.nnue")
        self.assertTrue(row["created_at"])

    def test_rerun_same_config_replaces_candidates(self):
        first = store.save_analysis(analysis(), game_id=self.game_id, ply=1)
        again = store.save_analysis(
            analysis(score_value=999, candidates=[candidate(1, "h2e2", 999)]),
            game_id=self.game_id, ply=1)
        self.assertFalse(again["created"])
        self.assertEqual(again["position_id"], first["position_id"])
        self.assertEqual(store.count_positions(self.game_id), 1)
        candidates = store.list_candidates(again["position_id"])
        self.assertEqual(len(candidates), 1)          # 旧的候选没有残留
        self.assertEqual(candidates[0]["score_value"], 999)

    def test_different_depth_does_not_overwrite(self):
        standard = store.save_analysis(analysis(), game_id=self.game_id, ply=1)
        precise = store.save_analysis(analysis(depth=40, movetime_ms=180000),
                                      game_id=self.game_id, ply=1)
        self.assertNotEqual(standard["position_id"], precise["position_id"])
        self.assertEqual(store.count_positions(self.game_id), 2)
        self.assertEqual(store.get_position(standard["position_id"])["depth"], 30)
        self.assertEqual(store.get_position(precise["position_id"])["depth"], 40)

    def test_different_multipv_does_not_overwrite(self):
        one = store.save_analysis(analysis(multipv=1), game_id=self.game_id, ply=1)
        three = store.save_analysis(analysis(multipv=3), game_id=self.game_id, ply=1)
        self.assertNotEqual(one["position_id"], three["position_id"])

    def test_different_rule_set_does_not_overwrite(self):
        base = store.save_analysis(analysis(), game_id=self.game_id, ply=1)
        strict = store.save_analysis(
            analysis(repetition_rule="chinese_2020_strict"), game_id=self.game_id, ply=1)
        self.assertNotEqual(base["position_id"], strict["position_id"])
        self.assertEqual(store.count_positions(self.game_id), 2)

    def test_empty_engine_version_does_not_break_uniqueness(self):
        """engine_version 留 NULL 会让唯一索引失效, 所以必须落成 ''"""
        first = store.save_analysis(analysis(engine_version=None, nnue_file=None),
                                    game_id=self.game_id, ply=1)
        second = store.save_analysis(analysis(engine_version=None, nnue_file=None),
                                     game_id=self.game_id, ply=1)
        self.assertEqual(first["position_id"], second["position_id"])
        self.assertEqual(store.count_positions(self.game_id), 1)
        self.assertEqual(store.get_position(first["position_id"])["engine_version"], "")

    def test_missing_required_fields_rejected(self):
        for broken in (analysis(fen=None), analysis(position_hash=None),
                       analysis(to_move=None), analysis(to_move="red")):
            with self.assertRaises(ValueError):
                store.save_analysis(broken, game_id=self.game_id, ply=1)
        self.assertEqual(store.count_positions(self.game_id), 0)

    def test_review_status_and_confidence_defaults(self):
        saved = store.save_analysis(
            analysis(rule_review_status=None, confidence=None), game_id=self.game_id, ply=1)
        row = store.get_position(saved["position_id"])
        self.assertEqual(row["rule_review_status"], "none")
        self.assertEqual(row["confidence"], "preliminary")

    def test_candidate_without_rank_gets_one(self):
        broken = candidate(1, "h2e2")
        del broken["rank"]
        saved = store.save_analysis(analysis(candidates=[broken]),
                                    game_id=self.game_id, ply=1)
        self.assertEqual(store.list_candidates(saved["position_id"])[0]["rank"], 1)

    def test_candidate_without_move_is_skipped(self):
        saved = store.save_analysis(analysis(candidates=[candidate(1, "h2e2"), {}]),
                                    game_id=self.game_id, ply=1)
        self.assertEqual(len(saved["candidate_ids"]), 1)

    def test_links_are_stored_and_deduplicated(self):
        saved = store.save_analysis(analysis(), game_id=self.game_id, ply=1,
                                    links=[(self.move_id, "key_move"),
                                           (self.move_id, "key_move"),
                                           (self.move_id, "critical_line")])
        self.assertTrue(store.link_move(saved["position_id"], self.move_id, "refutation"))
        roles = sorted(item["role"] for item
                       in store.list_position_moves(saved["position_id"]))
        self.assertEqual(roles, ["critical_line", "key_move", "refutation"])
        linked = store.list_position_moves(saved["position_id"], role="key_move")
        self.assertEqual(linked[0]["iccs"], "h2e2")   # 顺带带出 moves 表的内容

    def test_bad_role_rejected(self):
        with self.assertRaises(ValueError):
            store.save_analysis(analysis(), game_id=self.game_id, ply=1,
                                links=[(self.move_id, "winner")])
        with self.assertRaises(ValueError):
            store.link_move(1, self.move_id, "winner")

    def test_unlink_move(self):
        saved = store.save_analysis(analysis(), game_id=self.game_id, ply=1,
                                    links=[(self.move_id, "key_move")])
        self.assertEqual(store.unlink_move(saved["position_id"], self.move_id), 1)
        self.assertEqual(store.list_position_moves(saved["position_id"]), [])

    def test_delete_game_cascades(self):
        saved = store.save_analysis(analysis(), game_id=self.game_id, ply=1,
                                    links=[(self.move_id, "key_move")])
        storage.delete_game(self.game_id)
        self.assertIsNone(store.get_position(saved["position_id"]))
        self.assertEqual(store.list_candidates(saved["position_id"]), [])
        self.assertEqual(store.list_position_moves(saved["position_id"]), [])

    def test_concurrent_save_same_config_is_mutually_exclusive(self):
        """两个线程同时写同一套配置: 只能有一行, 候选也不能写重"""
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                store.save_analysis(analysis(), game_id=self.game_id, ply=1)
            except Exception as e:       # pragma: no cover - 失败时打印用
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(store.count_positions(self.game_id), 1)
        position = store.latest_position(self.game_id, ply=1)
        self.assertEqual(len(store.list_candidates(position["id"])), 2)


class TestPositionQueries(StoreTestCase):
    def test_list_filters_and_counts(self):
        store.save_analysis(analysis(), game_id=self.game_id, ply=1)
        store.save_analysis(analysis(fen=AFTER_H2E2, to_move="b"), game_id=self.game_id, ply=2)
        other = storage.create_game({"name": "另一局", "category": "study"})
        store.save_analysis(analysis(), game_id=other, ply=1)

        self.assertEqual(len(store.list_positions()), 3)
        self.assertEqual(len(store.list_positions(self.game_id)), 2)
        rows = store.list_positions(self.game_id, ply=2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to_move"], "b")
        self.assertEqual(rows[0]["candidate_count"], 2)

    def test_latest_position_picks_newest(self):
        store.save_analysis(analysis(), game_id=self.game_id, ply=1)
        store.save_analysis(analysis(depth=40, movetime_ms=180000),
                            game_id=self.game_id, ply=1)
        latest = store.latest_position(self.game_id, ply=1)
        self.assertEqual(latest["depth"], 40)
        self.assertIsNone(store.latest_position(self.game_id, ply=9))

    def test_list_candidates_order(self):
        saved = store.save_analysis(
            analysis(candidates=[candidate(1, "h2e2"), candidate(2, "b2e2"),
                                 candidate(3, "h0g2")]),
            game_id=self.game_id, ply=1)
        moves = [c["move_uci"] for c in store.list_candidates(saved["position_id"])]
        self.assertEqual(moves, ["h2e2", "b2e2", "h0g2"])


class TestReview(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.saved = store.save_analysis(
            analysis(result="needs_rule_review", rule_review_status="pending",
                     confidence="rule_review_needed"),
            game_id=self.game_id, ply=1)

    def test_human_verification(self):
        row = store.set_position_review(self.saved["position_id"], "human_verified",
                                        confidence="verified")
        self.assertEqual(row["rule_review_status"], "human_verified")
        self.assertEqual(row["confidence"], "verified")

    def test_override_result(self):
        row = store.set_position_review(self.saved["position_id"], "override",
                                        result="draw")
        self.assertEqual(row["result"], "draw")
        self.assertEqual(row["rule_review_status"], "override")

    def test_engine_numbers_are_untouched(self):
        before = store.get_position(self.saved["position_id"])
        after = store.set_position_review(self.saved["position_id"], "human_verified")
        for column in ("bestmove", "best_pv_uci", "score_type", "score_value",
                       "depth", "multipv", "engine_version"):
            self.assertEqual(after[column], before[column])

    def test_bad_status_or_missing_row(self):
        with self.assertRaises(ValueError):
            store.set_position_review(self.saved["position_id"], "approved")
        self.assertIsNone(store.set_position_review(999999, "human_verified"))


class TestTasks(StoreTestCase):
    def task_fields(self, **over):
        fields = {"study_id": self.game_id, "game_id": self.game_id, "ply": 1,
                  "fen": START_FEN, "position_hash": fen_rules.position_hash(START_FEN),
                  "mode": "standard", "depth": 30, "movetime_ms": 60000, "multipv": 3}
        fields.update(over)
        return fields

    def test_create_task_starts_as_draft(self):
        task_id = store.create_task(self.task_fields())
        task = store.get_task(task_id)
        self.assertEqual(task["status"], "draft")
        self.assertEqual(task["priority"], 0)
        self.assertIsNone(task["started_at"])

    def test_rule_options_and_search_moves_are_serialized(self):
        task_id = store.create_task(self.task_fields(
            rule_options={"mate_threat_depth": 8}, search_moves=["h2e2", "b2e2"]))
        task = store.get_task(task_id)
        self.assertIn("mate_threat_depth", task["rule_options"])
        self.assertEqual(task["search_moves"], "h2e2,b2e2")

    def test_transitions_are_enforced(self):
        task_id = store.create_task(self.task_fields())
        with self.assertRaises(ValueError):
            store.update_task(task_id, {"status": "running"})     # draft 不能直接跑
        store.update_task(task_id, {"status": "queued"})
        started = store.update_task(task_id, {"status": "running"})
        self.assertTrue(started["started_at"])
        done = store.update_task(task_id, {"status": "completed"})
        self.assertTrue(done["finished_at"])
        with self.assertRaises(ValueError):
            store.update_task(task_id, {"status": "running"})     # completed 是终态
        store.update_task(task_id, {"status": "superseded"})

    def test_unknown_status_rejected(self):
        task_id = store.create_task(self.task_fields())
        with self.assertRaises(ValueError):
            store.update_task(task_id, {"status": "finished"})

    def test_progress_and_partial_best(self):
        task_id = store.create_task(self.task_fields())
        self.assertTrue(store.set_task_progress(task_id, progress={"depth": 20},
                                                partial_best="h2e2"))
        task = store.get_task(task_id)
        self.assertIn('"depth": 20', task["progress_json"])
        self.assertEqual(task["partial_best"], "h2e2")

    def test_claim_next_task_respects_priority(self):
        low = store.create_task(self.task_fields())
        high = store.create_task(self.task_fields(priority=5))
        store.update_task(low, {"status": "queued"})
        store.update_task(high, {"status": "queued"})
        claimed = store.claim_next_task()
        self.assertEqual(claimed["id"], high)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(store.get_task(high)["status"], "running")
        second = store.claim_next_task()
        self.assertEqual(second["id"], low)

    def test_claim_returns_none_when_nothing_queued(self):
        task_id = store.create_task(self.task_fields())
        self.assertIsNone(store.claim_next_task())
        store.update_task(task_id, {"status": "queued"})
        self.assertIsNotNone(store.claim_next_task())
        self.assertIsNone(store.claim_next_task())

    def test_count_active_tasks(self):
        for status in ("draft", "queued", "running"):
            task_id = store.create_task(self.task_fields())
            if status != "draft":
                store.update_task(task_id, {"status": "queued"})
            if status == "running":
                store.update_task(task_id, {"status": "running"})
        self.assertEqual(store.count_active_tasks(), 2)
        self.assertEqual(store.count_active_tasks(study_id=self.game_id), 2)
        self.assertEqual(store.count_active_tasks(study_id=999), 0)

    def test_list_tasks_filters(self):
        first = store.create_task(self.task_fields(ply=1))
        second = store.create_task(self.task_fields(ply=2))
        store.update_task(second, {"status": "queued"})
        self.assertEqual(len(store.list_tasks(game_id=self.game_id)), 2)
        self.assertEqual([t["id"] for t in store.list_tasks(status="queued")], [second])
        self.assertEqual(len(store.list_tasks(statuses=["draft", "queued"])), 2)
        self.assertEqual(len(store.list_tasks(study_id=self.game_id)), 2)
        self.assertEqual(store.list_tasks(status="completed"), [])
        self.assertNotEqual(first, second)

    def test_supersede_after_undo(self):
        """撤销: 已跑完的标 superseded, 排队中的标 cancelled, 都不删"""
        done = store.create_task(self.task_fields(ply=3))
        store.update_task(done, {"status": "queued"})
        store.update_task(done, {"status": "running"})
        store.update_task(done, {"status": "completed"})
        waiting = store.create_task(self.task_fields(ply=4))
        store.update_task(waiting, {"status": "queued"})
        before = store.create_task(self.task_fields(ply=1))
        store.update_task(before, {"status": "queued"})
        store.update_task(before, {"status": "running"})
        store.update_task(before, {"status": "completed"})

        counts = store.supersede_tasks(self.game_id, from_ply=2)
        self.assertEqual(counts, {"superseded": 1, "cancelled": 1})
        self.assertEqual(store.get_task(done)["status"], "superseded")
        self.assertEqual(store.get_task(waiting)["status"], "cancelled")
        self.assertEqual(store.get_task(before)["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
