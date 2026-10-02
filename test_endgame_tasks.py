# -*- coding: utf-8 -*-
"""endgame.tasks 的单元测试 (标准库 unittest, 全程临时库 + 假引擎, 不起真进程)

    python test_endgame_tasks.py
    python -m unittest test_endgame_tasks -v

覆盖的是规格里"性能默认 / 可靠性"那几条:
  * 并发上限: 同一研究最多 3 个任务在跑, 第 4 个立刻被拒
  * 引擎池: 快档共用常驻引擎(1 个), 精确档最多 2 个专用进程, 崩了要换新的
  * 状态机: draft -> queued -> running -> aggregating -> completed
  * 取消: 排队中的直接取消; 正在跑的异步取消, 中途结果绝不落库
  * 失败: 引擎崩溃必须把原因留在库里, 绝不自动变成 completed
  * 进度: 节流写库; position 命令用"从起始局面到当前 ply"的完整着法
"""

import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from chess_engine import START_FEN, EngineError

import migrate
import rules
import storage
from endgame import analyzer, store, tasks

# 一局 3 步的棋, 用来验证"从起始局面到当前 ply 的完整着法"
HISTORY = ("h2e2", "h9g7", "b0c2")

# 双王残局(红先): 引擎给 0 分 —— 只能当和棋候选, 绝不能自动判和
TWO_KINGS_FEN = "4k4/9/9/9/9/9/9/9/9/3K5 w - - 0 1"


def wait_for(predicate, timeout=5.0, interval=0.02):
    """轮询等待, 超时返回 None (后台线程是异步的, 只能等)"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def line(multipv=1, depth=30, score=None, mate=None, pv=None, nodes=1000, seldepth=40):
    return {"multipv": multipv, "depth": depth, "seldepth": seldepth, "score": score,
            "mate": mate, "bound": None, "pv": list(pv or []), "nodes": nodes,
            "nps": 500, "time_ms": 100, "hashfull": 0}


def snap(depth, pv, score=None, mate=None, multipv=1):
    return {"multipv": multipv, "depth": depth, "seldepth": depth, "score": score,
            "mate": mate, "bound": None, "pv": list(pv), "nodes": 1, "nps": 1,
            "time_ms": 1}


def info(depth=20, multipv=1, score=10, pv=("h2e2",)):
    return snap(depth, pv, score=score, multipv=multipv)


# 开局局面(红先)的三路候选: 主变稳定(28/29/30 层同着, 分数只差 10cp)
START_LINES = [
    line(multipv=1, score=305, pv=["h2e2", "h9g7"]),
    line(multipv=2, score=120, pv=["b2e2", "h9g7"]),
    line(multipv=3, score=90, pv=["h0g2", "h9g7"]),
]
START_SNAPSHOTS = [snap(28, ["h2e2", "h9g7"], score=300),
                   snap(29, ["h2e2", "h9g7"], score=310),
                   snap(30, ["h2e2", "h9g7"], score=305)]

# 走完 h2e2 之后轮到黑方
BLACK_LINES = [
    line(multipv=1, score=-180, pv=["h9g7", "b0c2"]),
    line(multipv=2, score=-200, pv=["b9c7", "b0c2"]),
]


class FakeEngine:
    """假引擎: 只实现 tasks / analyzer 真正用到的那几个方法

    :param gate: 给了就卡在搜索里等它置位 —— 用来制造"任务正在跑"的时刻
    :param error: 给了就在搜索时抛出 (模拟进程崩溃 / 请求不成立)
    """

    def __init__(self, *, lines=None, bestmove="h2e2", snapshots=None, gate=None,
                 error=None, engine_name="Pikafish test-1", nnue="pikafish.nnue",
                 supported=()):
        self.lines = [dict(item) for item in (lines or [])]
        self.bestmove = bestmove
        self.snapshots = [dict(item) for item in (snapshots or [])]
        self.gate = gate
        self.error = error
        self.engine_name = engine_name
        self.nnue = nnue
        self.supported = set(supported)
        self.calls = []
        self.options_set = {}
        self.quit_called = False
        self.started = threading.Event()      # 进入 search 就置位

    def supports(self, name):
        return name in self.supported

    def set_option(self, name, value):
        self.options_set[name] = value
        return True

    def quit(self):
        self.quit_called = True

    def search(self, position, *, depth=None, movetime=None, multipv=None,
               searchmoves=None, cancel=None, on_info=None, timeout_ms=None):
        self.calls.append({"position": position, "depth": depth, "movetime": movetime,
                           "multipv": multipv, "searchmoves": list(searchmoves or []),
                           "timeout_ms": timeout_ms})
        self.started.set()
        if self.gate is not None:
            self.gate.wait(10)
        if self.error is not None:
            raise self.error
        stopped = bool(cancel is not None and cancel.is_set())
        limit = multipv or 1
        for item in self.snapshots:
            if on_info is not None and (item.get("multipv") or 1) <= limit:
                on_info(item)
        return {
            "bestmove": self.bestmove,
            "lines": [dict(item) for item in self.lines
                      if (item.get("multipv") or 1) <= limit],
            "info_count": len(self.snapshots),
            "stopped": stopped,
            "duration_ms": 42,
            "engine": {"engine": self.engine_name, "author": "tester",
                       "path": "fake", "nnue_file": self.nnue, "options": []},
        }


class Holder(threading.Thread):
    """占着一个引擎槽位不放, 直到 release 置位 (用来量并发上限)"""

    def __init__(self, pool, mode):
        super().__init__(daemon=True)
        self.pool = pool
        self.mode = mode
        self.entered = threading.Event()
        self.release = threading.Event()
        self.engine = None

    def run(self):
        with self.pool.slot(self.mode) as engine:
            self.engine = engine
            self.entered.set()
            self.release.wait(10)


# ----------------------------------------------------------------------
# 引擎池
# ----------------------------------------------------------------------
class TestEnginePool(unittest.TestCase):
    def setUp(self):
        self.made = []

    def factory(self):
        engine = FakeEngine()
        self.made.append(engine)
        return engine

    def test_quick_and_root_moves_share_the_resident_engine(self):
        resident = FakeEngine()
        pool = tasks.EnginePool(factory=self.factory, resident=resident)
        with pool.slot("quick") as first:
            self.assertIs(first, resident)
        with pool.slot("root_moves") as second:
            self.assertIs(second, resident)
        self.assertEqual(self.made, [])        # 快档不额外起进程
        self.assertEqual(pool.stats()["engines"], 0)

    def test_precise_modes_get_a_dedicated_engine_and_reuse_it(self):
        pool = tasks.EnginePool(factory=self.factory)
        with pool.slot("standard") as first:
            with pool.slot("precise") as second:
                self.assertIsNot(first, second)
        self.assertEqual(len(self.made), 2)
        with pool.slot("standard") as again:
            self.assertIs(again, first)        # 复用了刚还回去的空闲进程
        self.assertEqual(len(self.made), 2)

    def test_quick_slot_is_serialized(self):
        pool = tasks.EnginePool(resident=FakeEngine())
        first = Holder(pool, "quick")
        first.start()
        self.assertTrue(first.entered.wait(2), "第一个快分析没能拿到槽位")
        second = Holder(pool, "root_moves")
        second.start()
        self.assertFalse(second.entered.wait(0.2), "常驻引擎同时跑了两个快分析")
        first.release.set()
        self.assertTrue(second.entered.wait(2), "第一个算完后第二个没被放行")
        second.release.set()
        second.join(2)
        first.join(2)

    def test_precise_slots_allow_two_engines(self):
        pool = tasks.EnginePool(factory=self.factory, precise_limit=2)
        held = [Holder(pool, "standard") for _ in range(2)]
        for holder in held:
            holder.start()
        for holder in held:
            self.assertTrue(holder.entered.wait(2), "精确槽位应该能同时进 2 个")
        third = Holder(pool, "precise")
        third.start()
        self.assertFalse(third.entered.wait(0.2), "精确槽位超过 2 个了")
        for holder in held:
            holder.release.set()
        self.assertTrue(third.entered.wait(2), "前两个算完后第三个没被放行")
        third.release.set()
        for holder in held + [third]:
            holder.join(2)

    def test_drop_quits_the_engine_and_replaces_it(self):
        resident = FakeEngine()
        pool = tasks.EnginePool(factory=self.factory, resident=resident)
        with pool.slot("quick") as engine:
            held = engine
        pool.drop(held)                        # 要在 slot 之外调 (见 drop 的文档)
        self.assertTrue(resident.quit_called)
        self.assertFalse(pool.stats()["resident"])
        with pool.slot("quick") as fresh:
            self.assertIsNot(fresh, resident)  # 常驻引擎崩了要换新的
            self.assertIn(fresh, self.made)
        self.assertEqual(len(self.made), 1)

    def test_shutdown_recycles_dedicated_engines_only(self):
        resident = FakeEngine()
        pool = tasks.EnginePool(factory=self.factory, resident=resident)
        with pool.slot("standard") as dedicated:
            pass
        pool.shutdown()
        self.assertTrue(dedicated.quit_called)
        self.assertFalse(resident.quit_called)  # 常驻引擎留给网页继续用
        with self.assertRaises(tasks.TaskError):
            with pool.slot("quick"):
                pass


# ----------------------------------------------------------------------
# 提交前的校验与快照
# ----------------------------------------------------------------------
class TaskTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="xq_tasks_")
        self._saved_path = storage.DB_PATH
        # storage._db_path() 认环境变量, 别的模块(如 test_games_routes)留下的
        # XQ_DB_PATH 会把连接指到别处, 这里按 test_storage 的做法先摘掉
        self._saved_env = os.environ.pop("XQ_DB_PATH", None)
        storage.DB_PATH = Path(self.tmpdir) / "chess.db"
        storage.init_db()
        migrate.migrate(direction="up", db_path=storage.DB_PATH, shadow=False)

        self.game_id = storage.create_game({"name": "残局研究", "category": "study"})
        grid, side = rules.fen_to_board(START_FEN)
        self.fens = [START_FEN]
        for move in HISTORY:
            grid, result = rules.apply_iccs(grid, move, side=side)
            side = "b" if side == "w" else "w"
            self.fens.append(result["fen_after"])
            storage.append_move(self.game_id, {"iccs": move, "fen_before": self.fens[-2],
                                               "fen_after": self.fens[-1]})
        self.move_ids = [row["id"] for row in storage.list_moves(self.game_id)]

        self.gates = []
        self.runners = []

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for runner in self.runners:
            with contextlib.suppress(Exception):
                runner.stop(timeout=2.0)
        storage.DB_PATH = self._saved_path
        if self._saved_env is not None:
            os.environ["XQ_DB_PATH"] = self._saved_env
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 助手 --
    def make_gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def make_runner(self, engine=None, **kwargs):
        engine = engine or FakeEngine()
        kwargs.setdefault("workers", 1)
        runner = tasks.TaskRunner(tasks.EnginePool(resident=engine), **kwargs)
        runner.start()
        self.runners.append(runner)
        return runner

    def wait_status(self, task_id, timeout=5.0):
        done = wait_for(lambda: (row := store.get_task(task_id)) and
                        row["status"] in store.TASK_FINAL_STATUSES and row, timeout)
        self.assertIsNotNone(done, f"任务 {task_id} 没有在超时内进入终态")
        return done


class TestSubmit(TaskTestCase):
    def test_stores_the_full_config_snapshot(self):
        runner = self.make_runner()
        task = runner.submit({"gameId": self.game_id, "ply": 1, "mode": "quick",
                              "ruleOptions": {"mate_threat_depth": 8},
                              "searchMoves": ["h9g7"], "priority": 5})
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["ply"], 1)
        self.assertEqual(task["fen"], self.fens[1])
        self.assertEqual(task["mode"], "quick")
        self.assertEqual((task["depth"], task["movetime_ms"], task["multipv"]),
                         (24, 10000, 3))
        self.assertEqual(task["search_moves"], "h9g7")
        self.assertEqual(task["repetition_rule"], "chinese_2020_analysis")
        self.assertEqual(task["priority"], 5)
        self.assertEqual(json.loads(task["rule_options"])["mate_threat_depth"], 8)
        self.assertTrue(task["position_hash"])

    def test_requires_a_game(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.submit({"ply": 0, "mode": "quick"})

    def test_unknown_game_is_rejected(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.submit({"gameId": 999999, "ply": 0, "mode": "quick"})

    def test_unknown_mode_is_rejected(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError) as ctx:
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "fast"})
        self.assertIn("档位", str(ctx.exception))

    def test_depth_below_floor_is_rejected(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "standard",
                           "depth": 10})

    def test_unknown_rule_switch_is_rejected(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick",
                           "ruleOptions": {"make_it_win": True}})

    def test_root_moves_needs_searchmoves(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "root_moves"})

    def test_broken_searchmove_is_rejected_before_queueing(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick",
                           "searchMoves": ["z9z8"]})
        self.assertEqual(store.list_tasks(game_id=self.game_id), [])

    def test_illegal_position_is_rejected(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError) as ctx:
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick",
                           "fen": "4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1"})   # 白脸将
        self.assertIn("不合法", str(ctx.exception))
        self.assertEqual(store.list_tasks(game_id=self.game_id), [])

    def test_dirty_move_history_is_rejected_before_queueing(self):
        """moves 里的 fen_after 与着法重放对不上(旧导入数据可能有): 开搜前拦下"""
        broken = storage.create_game({"name": "脏数据", "category": "study"})
        storage.append_move(broken, {"iccs": "h2e2", "fen_before": START_FEN,
                                     "fen_after": self.fens[1]})
        storage.append_move(broken, {"iccs": "h9g7", "fen_before": self.fens[1],
                                     "fen_after": START_FEN})       # 错的 fen_after
        runner = self.make_runner(FakeEngine(gate=self.make_gate()))
        with self.assertRaises(tasks.TaskError) as ctx:
            runner.submit({"gameId": broken, "ply": 2, "mode": "quick"})
        self.assertIn("不一致", str(ctx.exception))
        self.assertEqual(store.list_tasks(game_id=broken), [])


class TestConcurrencyLimit(TaskTestCase):
    def test_fourth_task_in_the_same_study_is_rejected(self):
        gate = self.make_gate()
        runner = self.make_runner(FakeEngine(gate=gate), workers=3, max_per_study=3)
        for _ in range(3):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        with self.assertRaises(tasks.TaskLimitError):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        gate.set()
        wait_for(lambda: store.count_active_tasks(study_id=self.game_id) == 0, timeout=5)

    def test_limit_is_per_study(self):
        gate = self.make_gate()
        other = storage.create_game({"name": "另一局", "category": "study"})
        runner = self.make_runner(FakeEngine(gate=gate), workers=1, max_per_study=1)
        runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        with self.assertRaises(tasks.TaskLimitError):
            runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        # 另一个研究不该被本研究的额度牵连
        task = runner.submit({"gameId": other, "ply": 0, "mode": "quick"})
        self.assertEqual(task["status"], "queued")
        gate.set()


# ----------------------------------------------------------------------
# 跑任务
# ----------------------------------------------------------------------
class TestRun(TaskTestCase):
    def test_task_completes_and_saves_the_position(self):
        engine = FakeEngine(lines=START_LINES, snapshots=START_SNAPSHOTS)
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        done = self.wait_status(task["id"])

        self.assertEqual(done["status"], "completed")
        self.assertIsNone(done["error_code"])
        self.assertTrue(done["started_at"] and done["finished_at"])
        self.assertEqual(done["engine_version"], "Pikafish test-1")
        self.assertEqual(done["nnue_file"], "pikafish.nnue")
        self.assertIsNotNone(done["position_id"])

        position = store.get_position(done["position_id"])
        self.assertEqual(position["game_id"], self.game_id)
        self.assertEqual(position["ply"], 0)
        self.assertEqual(position["fen"], START_FEN)
        self.assertEqual((position["depth"], position["movetime_ms"], position["multipv"]),
                         (24, 10000, 3))
        self.assertEqual(position["bestmove"], "h2e2")
        self.assertEqual(position["rule_aware"], 1)
        # 结论按"当前走子方(红)"记, 不能把走子方视角的分数写成别人的胜负
        self.assertEqual(position["result_for"], "r")
        self.assertEqual(position["result"], "win")
        self.assertEqual(position["confidence"], "stable")
        self.assertEqual(len(store.list_candidates(done["position_id"])), 3)

        # 引擎收到的是"从起始局面到当前 ply"的完整着法
        self.assertEqual(engine.calls[0]["position"], f"fen {START_FEN}")
        self.assertEqual(engine.calls[0]["movetime"], 10000)
        self.assertEqual(engine.calls[0]["timeout_ms"],
                         analyzer.default_timeout_ms(10000))

    def test_submit_uses_the_whole_move_history(self):
        engine = FakeEngine(lines=BLACK_LINES, bestmove="h9g7")
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 1, "mode": "quick"})
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(engine.calls[0]["position"], "startpos moves h2e2")
        self.assertEqual(store.get_position(done["position_id"])["to_move"], "b")

    def test_searchmoves_round_trip(self):
        engine = FakeEngine(lines=[line(multipv=1, score=120, pv=["b2e2", "h9g7"])],
                            bestmove="b2e2")
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "root_moves",
                              "searchMoves": ["b2e2"]})
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["multipv"], 1)
        self.assertEqual(engine.calls[0]["searchmoves"], ["b2e2"])
        candidates = store.list_candidates(done["position_id"])
        self.assertEqual([row["move_uci"] for row in candidates], ["b2e2"])

    def test_engine_draw_is_never_recorded_as_a_verdict(self):
        engine = FakeEngine(lines=[line(multipv=1, score=0, pv=["d0d1"])],
                            bestmove="d0d1")
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick",
                              "fen": TWO_KINGS_FEN})
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "completed")
        position = store.get_position(done["position_id"])
        self.assertEqual(position["score_type"], "draw")
        self.assertEqual(position["result"], "needs_rule_review")
        self.assertEqual(position["rule_review_status"], "pending")
        self.assertEqual(position["confidence"], "rule_review_needed")
        self.assertEqual(position["draw_type"], "natural")

    def test_progress_is_reported_while_running(self):
        engine = FakeEngine(lines=START_LINES, snapshots=START_SNAPSHOTS)
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        self.wait_status(task["id"])
        progress = json.loads(store.get_task(task["id"])["progress_json"])
        # 第一条 info 立刻入库, 之后深度每涨一层也都会写 (只有"深度没涨"才节流)
        self.assertEqual(progress["depth"], 30)
        self.assertEqual(progress["pv"], ["h2e2", "h9g7"])

    def test_cancel_while_running_does_not_save_anything(self):
        gate = self.make_gate()
        engine = FakeEngine(lines=START_LINES, gate=gate)
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        self.assertTrue(wait_for(lambda: store.get_task(task["id"])["status"] == "running"))
        runner.cancel(task["id"])
        gate.set()
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "cancelled")
        self.assertIsNone(done["position_id"])
        self.assertEqual(store.count_positions(self.game_id), 0)

    def test_cancel_queued_task_is_immediate(self):
        gate = self.make_gate()
        runner = self.make_runner(FakeEngine(lines=START_LINES, gate=gate), workers=1)
        running = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        self.assertTrue(wait_for(lambda: store.get_task(running["id"])["status"] == "running"))
        queued = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        self.assertEqual(queued["status"], "queued")
        cancelled = runner.cancel(queued["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        gate.set()
        self.assertEqual(self.wait_status(running["id"])["status"], "completed")
        self.assertEqual(store.get_task(queued["id"])["status"], "cancelled")
        self.assertEqual(store.count_positions(self.game_id), 1)   # 只有跑完的那个

    def test_cancel_finished_task_is_a_noop(self):
        runner = self.make_runner(FakeEngine(lines=START_LINES))
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        done = self.wait_status(task["id"])
        again = runner.cancel(task["id"])
        self.assertEqual(again["status"], done["status"])

    def test_cancel_unknown_task_raises(self):
        runner = self.make_runner()
        with self.assertRaises(tasks.TaskError):
            runner.cancel("nope")

    def test_engine_crash_is_recorded_and_the_engine_is_replaced(self):
        engine = FakeEngine(error=EngineError("引擎进程意外退出"))
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "failed")       # 绝不自动 completed
        self.assertEqual(done["error_code"], "engine_error")
        self.assertIn("引擎进程意外退出", done["message"])
        self.assertIsNone(done["position_id"])
        self.assertEqual(store.count_positions(self.game_id), 0)
        self.assertTrue(engine.quit_called)              # 崩过的进程不能再用
        self.assertFalse(runner.pool.stats()["resident"])

    def test_analyzer_error_becomes_invalid_request(self):
        engine = FakeEngine(error=analyzer.AnalyzerError("这个局面分析不了"))
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error_code"], "invalid_request")
        self.assertIn("分析不了", done["message"])

    def test_unexpected_error_keeps_the_reason(self):
        engine = FakeEngine(error=RuntimeError("莫名其妙"))
        runner = self.make_runner(engine)
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        done = self.wait_status(task["id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error_code"], "internal_error")
        self.assertIn("莫名其妙", done["message"])

    def test_status_reports_progress_and_position(self):
        runner = self.make_runner(FakeEngine(lines=START_LINES,
                                             snapshots=START_SNAPSHOTS))
        task = runner.submit({"gameId": self.game_id, "ply": 0, "mode": "quick"})
        self.wait_status(task["id"])
        payload = runner.status(task["id"])
        self.assertEqual(payload["taskId"], task["id"])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["engineVersion"], "Pikafish test-1")
        self.assertEqual(payload["position"]["ply"], 0)
        self.assertEqual(len(payload["position"]["candidates"]), 3)
        with self.assertRaises(tasks.TaskError):
            runner.status("nope")


class TestPositionAt(TaskTestCase):
    def test_from_start(self):
        fen, start_fen, moves = tasks.TaskRunner.position_at(self.game_id, 0)
        self.assertEqual((fen, start_fen, moves), (START_FEN, START_FEN, []))

    def test_explicit_fen_wins_at_ply_zero(self):
        fen, _start, moves = tasks.TaskRunner.position_at(self.game_id, 0,
                                                          fen=TWO_KINGS_FEN)
        self.assertEqual((fen, moves), (TWO_KINGS_FEN, []))

    def test_history_is_replayed_up_to_the_ply(self):
        for ply in (1, 2, 3):
            fen, start_fen, moves = tasks.TaskRunner.position_at(self.game_id, ply)
            self.assertEqual(fen, self.fens[ply])
            self.assertEqual(start_fen, START_FEN)
            self.assertEqual(moves, list(HISTORY[:ply]))

    def test_unknown_game_raises(self):
        with self.assertRaises(tasks.TaskError):
            tasks.TaskRunner.position_at(999999, 0)

    def test_ply_without_moves_raises(self):
        empty = storage.create_game({"name": "空局", "category": "study"})
        with self.assertRaises(tasks.TaskError):
            tasks.TaskRunner.position_at(empty, 1)


class TestProgressReporter(TaskTestCase):
    def make_task(self):
        return store.create_task({"study_id": self.game_id, "game_id": self.game_id,
                                  "ply": 0, "fen": START_FEN,
                                  "position_hash": "x" * 40, "depth": 24,
                                  "movetime_ms": 10000, "multipv": 3})

    def test_throttle_skips_repeated_depths(self):
        task_id = self.make_task()
        reporter = tasks._ProgressReporter(task_id, min_interval=1000)   # noqa: SLF001
        reporter(info(depth=20, score=10))
        first = json.loads(store.get_task(task_id)["progress_json"])
        self.assertEqual(first["depth"], 20)
        self.assertEqual(store.get_task(task_id)["partial_best"], "h2e2")

        reporter(info(depth=20, score=99))          # 深度没涨, 节流掉
        self.assertEqual(json.loads(store.get_task(task_id)["progress_json"])["score"], 10)

        reporter(info(depth=25, score=120))         # 深度涨了, 照样写
        self.assertEqual(json.loads(store.get_task(task_id)["progress_json"])["depth"], 25)
        self.assertEqual(reporter.snapshots, 3)

    def test_missing_task_does_not_blow_up(self):
        reporter = tasks._ProgressReporter("nope", min_interval=0)
        reporter(info(depth=1))                     # 写不进去也不能把分析打断
        self.assertEqual(reporter.snapshots, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
