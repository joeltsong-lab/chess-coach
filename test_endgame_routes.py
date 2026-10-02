# -*- coding: utf-8 -*-
"""endgame.routes 的接口测试 (Flask test_client + 假引擎 + 临时库, 不起真进程)

    python test_endgame_routes.py
    python -m unittest test_endgame_routes -v

覆盖规格里 API 那一节:
  * 建研究的三条来源: 对局续研(复制 moves[0..ply], 原局不动) / FEN / 从已有分析结果
  * 摆盘: operations 重放、validateOnly 预检、previousHash 过期、有事法时另建研究
  * 位置节点 -> 排队分析 -> 状态/进度/结果 -> 取消; 不同配置不互相覆盖
  * 候选着法按 rank/moveUci/san/cp/matePlies/pv 输出; 未算完显示 partialBest
  * PATCH move 的 isKey/annotation; CSRF 校验; 错误响应带 code/message/detail
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from flask import Flask

from core.chess_engine import START_FEN

import migrate
from core import rules, storage
from endgame import commentary
from endgame import routes as endgame_routes
from endgame import store, tasks

HISTORY = ("h2e2", "h9g7", "b0c2")
# 双王残局(红先): 引擎 0 分只能当和棋候选, 不能自动判和
TWO_KINGS_FEN = "4k4/9/9/9/9/9/9/9/9/3K5 w - - 0 1"
# 白脸将(非法), 用来试"未校验不许进库"
KINGS_FACING_FEN = "4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1"


def wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def line(multipv=1, depth=30, score=None, mate=None, pv=None, nodes=1000):
    return {"multipv": multipv, "depth": depth, "seldepth": depth + 4, "score": score,
            "mate": mate, "bound": None, "pv": list(pv or []), "nodes": nodes, "nps": 500,
            "time_ms": 100, "hashfull": 0}


def snap(depth, pv, score=None, multipv=1):
    return {"multipv": multipv, "depth": depth, "seldepth": depth, "score": score,
            "mate": None, "bound": None, "pv": list(pv), "nodes": 1, "nps": 1, "time_ms": 1}


# 开局局面(红先)的三路候选
START_LINES = [
    line(multipv=1, score=305, pv=["h2e2", "h9g7"]),
    line(multipv=2, score=120, pv=["b2e2", "h9g7"]),
    line(multipv=3, score=90, pv=["h0g2", "h9g7"]),
]
START_SNAPSHOTS = [snap(28, ["h2e2", "h9g7"], score=300),
                   snap(29, ["h2e2", "h9g7"], score=310),
                   snap(30, ["h2e2", "h9g7"], score=305)]


class FakeEngine:
    """只实现 analyze 真正用到的那几个方法; gate 用来制造"正在跑"的时刻"""

    def __init__(self, lines=None, bestmove="h2e2", snapshots=None, gate=None,
                 error=None, engine_name="Pikafish test-1", nnue="pikafish.nnue"):
        self.lines = [dict(item) for item in (lines or [])]
        self.bestmove = bestmove
        self.snapshots = [dict(item) for item in (snapshots or [])]
        self.gate = gate
        self.error = error
        self.engine_name = engine_name
        self.nnue = nnue
        self.calls = []
        self.quit_called = False
        self.started = threading.Event()

    def supports(self, name):
        return False

    def set_option(self, name, value):
        return True

    def quit(self):
        self.quit_called = True

    def version_info(self):
        return {"engine": self.engine_name, "author": "tester", "path": "fake",
                "nnue_file": self.nnue, "options": []}

    def search(self, position, *, depth=None, movetime=None, multipv=None,
               searchmoves=None, cancel=None, on_info=None, timeout_ms=None):
        self.calls.append({"position": position, "depth": depth, "multipv": multipv,
                           "searchmoves": list(searchmoves or [])})
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
        return {"bestmove": self.bestmove,
                "lines": [dict(item) for item in self.lines
                          if (item.get("multipv") or 1) <= limit],
                "info_count": len(self.snapshots), "stopped": stopped, "duration_ms": 42,
                "engine": {"engine": self.engine_name, "author": "tester", "path": "fake",
                           "nnue_file": self.nnue, "options": []}}


class RouteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="xq_routes_")
        self._saved_path = storage.DB_PATH
        # storage._db_path() 认环境变量: 别的模块留下的 XQ_DB_PATH 会把连接指到别处
        self._saved_env = os.environ.pop("XQ_DB_PATH", None)
        storage.DB_PATH = Path(self.tmpdir) / "chess.db"
        storage.init_db()
        migrate.migrate(direction="up", db_path=storage.DB_PATH, shadow=False)

        self.gates = []
        self.runners = []
        self.engine = FakeEngine(lines=START_LINES, snapshots=START_SNAPSHOTS)
        self.app, self.runner = self.make_app(self.engine)
        self.client = self.app.test_client()
        self.token = self.client.get("/api/studies/csrf").get_json()["token"]

        # 一局 3 步的棋, 用来试"续研"
        self.game_id = storage.create_game({"name": "原对局", "category": "game"})
        grid, side = rules.fen_to_board(START_FEN)
        self.fens = [START_FEN]
        for move in HISTORY:
            grid, result = rules.apply_iccs(grid, move, side=side)
            side = "b" if side == "w" else "w"
            self.fens.append(result["fen_after"])
            storage.append_move(self.game_id, {"iccs": move, "fen_before": self.fens[-2],
                                              "fen_after": self.fens[-1]})
        self.move_ids = [row["id"] for row in storage.list_moves(self.game_id)]

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for runner in self.runners:
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

    def make_app(self, engine, **kwargs):
        kwargs.setdefault("workers", 2)
        runner = tasks.TaskRunner(tasks.EnginePool(resident=engine), **kwargs)
        runner.start()
        self.runners.append(runner)
        app = Flask(f"endgame_test_{len(self.runners)}")
        endgame_routes.init_app(app, runner=runner)
        return app, runner

    def post(self, path, payload=None, *, csrf=True):
        headers = {"X-CSRF-Token": self.token} if csrf else {}
        return self.client.post(path, json=payload or {}, headers=headers)

    def put(self, path, payload=None, *, csrf=True):
        headers = {"X-CSRF-Token": self.token} if csrf else {}
        return self.client.put(path, json=payload or {}, headers=headers)

    def patch(self, path, payload=None, *, csrf=True):
        headers = {"X-CSRF-Token": self.token} if csrf else {}
        return self.client.patch(path, json=payload or {}, headers=headers)

    def delete(self, path, *, csrf=True):
        headers = {"X-CSRF-Token": self.token} if csrf else {}
        return self.client.delete(path, headers=headers)

    def data(self, response):
        payload = response.get_json()
        self.assertIsNotNone(payload, "响应不是 JSON")
        return payload

    def create_study(self, **payload):
        response = self.post("/api/studies", payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        return self.data(response)["study"]["studyId"]

    def state(self, client, study_id, task_id):
        """GET /analysis/{taskId} 的状态响应"""
        return self.data(client.get(f"/api/studies/{study_id}/analysis/{task_id}"))

    def study_moves(self, study_id, client=None):
        """研究自己的走法 (续研是复制出来的新行, moveId 与原对局不同)"""
        client = client or self.client
        return self.data(client.get(f"/api/studies/{study_id}"))["moves"]

    def wait_final(self, client, study_id, task_id, timeout=5):
        """等到任务进终态, 返回最后一次状态响应"""
        def poll():
            row = self.state(client, study_id, task_id)
            return row if row["status"] in store.TASK_FINAL_STATUSES else None
        done = wait_for(poll, timeout=timeout)
        self.assertIsNotNone(done, "任务没有在超时内进入终态")
        return done

    def analyze_and_wait(self, study_id, *, ply=0, task_id=None, **payload):
        """建位置节点(可选) -> 开始分析 -> 等终态"""
        if task_id is None:
            body = {"fromPly": ply}
            body.update(payload)
            node = self.data(self.post(f"/api/studies/{study_id}/positions", body))
            task_id = node["taskId"]
            self.assertEqual(node["status"], "draft", node)
        response = self.post(f"/api/studies/{study_id}/positions/{task_id}/analyze", payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        task = self.data(response)
        done = self.wait_final(self.client, study_id, task["taskId"])
        return task_id, done


# ----------------------------------------------------------------------
# CSRF / 建研究
# ----------------------------------------------------------------------
class TestCsrfAndCreate(RouteTestCase):
    def test_writes_need_the_csrf_token(self):
        response = self.post("/api/studies", {"fen": START_FEN}, csrf=False)
        self.assertEqual(response.status_code, 403)
        body = self.data(response)
        self.assertEqual(body["code"], "csrf_failed")
        self.assertIn("message", body)
        self.assertIn("detail", body)
        self.assertFalse(body["ok"])
        # 带上令牌就行
        self.assertEqual(self.post("/api/studies", {"fen": START_FEN}).status_code, 200)
        # GET 不需要令牌
        self.assertEqual(self.client.get("/api/studies").status_code, 200)

    def test_create_from_fen(self):
        body = self.data(self.post("/api/studies", {"fen": TWO_KINGS_FEN,
                                                    "name": "双王残局"}))
        self.assertEqual(body["action"], "position")
        self.assertEqual(body["sideToMove"], "w")
        self.assertEqual(body["pieceCount"], 2)
        self.assertEqual(body["ruleSet"], "chinese_2020_analysis")
        study = body["study"]
        self.assertEqual(study["name"], "双王残局")
        self.assertEqual(study["category"], "endgame")
        self.assertEqual(study["startFen"], TWO_KINGS_FEN)
        self.assertEqual(study["fenText"], TWO_KINGS_FEN)
        self.assertEqual(study["sideToMove"], "red")

        detail = self.data(self.client.get(f"/api/studies/{study['studyId']}"))
        self.assertEqual(detail["moves"], [])
        self.assertEqual(detail["positions"], [])
        self.assertEqual(detail["study"]["studyId"], study["studyId"])
        self.assertIn("pending", detail["reviewStatuses"])

    def test_illegal_fen_is_rejected(self):
        response = self.post("/api/studies", {"fen": KINGS_FACING_FEN})
        self.assertEqual(response.status_code, 400)
        body = self.data(response)
        self.assertEqual(body["code"], "invalid_position")
        self.assertTrue(body["detail"]["errors"])
        self.assertIn("code", body["detail"]["errors"][0])
        # 没有任何研究被建出来(原对局是 category=game, 不属于研究)
        self.assertEqual(self.data(self.client.get(
            "/api/studies?category=endgame"))["studies"], [])

    def test_create_from_game_copies_history_only(self):
        study_id = self.create_study(gameId=self.game_id, ply=2)
        detail = self.data(self.client.get(f"/api/studies/{study_id}"))
        self.assertEqual([m["uci"] for m in detail["moves"]], list(HISTORY[:2]))
        self.assertEqual(detail["study"]["fenText"], self.fens[2])
        self.assertEqual(detail["study"]["studyOf"], self.game_id)
        self.assertEqual(detail["study"]["category"], "endgame")
        # 原对局一行不动(接口层只读 storage, 这里直接查库)
        original = storage.list_moves(self.game_id)
        self.assertEqual([m["iccs"] for m in original], list(HISTORY))
        self.assertEqual(len(original), 3)

    def test_create_from_game_needs_a_valid_ply(self):
        response = self.post("/api/studies", {"gameId": self.game_id, "ply": 99})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "bad_param")

    def test_create_needs_a_source(self):
        response = self.post("/api/studies", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "missing_source")

    def test_create_from_unknown_game(self):
        response = self.post("/api/studies", {"gameId": 999999})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.data(response)["code"], "game_not_found")

    def test_pattern_library_is_listed(self):
        listing = self.client.get("/api/patterns")
        self.assertEqual(listing.status_code, 200)
        body = self.data(listing)
        self.assertEqual(body["total"], len(body["patterns"]))
        self.assertGreaterEqual(body["total"], 10)
        self.assertIn("version", body)
        first = body["patterns"][0]
        for key in ("patternId", "name", "theme", "fen", "sideToMove", "result", "goal"):
            self.assertIn(key, first)
        # 定式不带任何着法序列(没跑过引擎的"正解"就是编造)
        for item in body["patterns"]:
            for key in ("moves", "solution", "line", "pv"):
                self.assertNotIn(key, item)

    def test_create_study_from_a_pattern(self):
        listing = self.data(self.client.get("/api/patterns"))
        item = listing["patterns"][1]
        body = self.data(self.post("/api/studies", {"patternId": item["patternId"]}))
        self.assertEqual(body["action"], "position")
        self.assertEqual(body["study"]["name"], item["name"])
        self.assertEqual(body["study"]["category"], "pattern")
        self.assertEqual(body["fen"], item["fen"])
        self.assertEqual(body["sideToMove"], item["sideToMove"])
        self.assertEqual(body["pattern"]["patternId"], item["patternId"])
        # 真的建出来了: 详情能查到, 且还没有着法
        detail = self.data(self.client.get(f"/api/studies/{body['study']['studyId']}"))
        self.assertEqual(detail["study"]["startFen"], item["fen"])
        self.assertEqual(detail["moves"], [])

    def test_pattern_study_rejects_unknown_id(self):
        response = self.post("/api/studies", {"patternId": "no-such-pattern"})
        self.assertEqual(response.status_code, 404)
        body = self.data(response)
        self.assertEqual(body["code"], "pattern_not_found")
        self.assertIn("available", body["detail"])

    def test_create_from_an_analysis_result(self):
        study_id = self.create_study(fen=START_FEN)
        _, done = self.analyze_and_wait(study_id, ply=0, mode="quick")
        position_id = done["position"]["positionId"]
        other = self.data(self.post("/api/studies", {"positionId": position_id}))
        self.assertEqual(other["openingPositionId"], position_id)
        self.assertEqual(other["study"]["fenText"], START_FEN)
        self.assertEqual(other["sourceGameId"], study_id)

    def test_list_studies(self):
        self.create_study(fen=START_FEN, name="甲")
        self.create_study(fen=TWO_KINGS_FEN, name="乙")
        listing = self.data(self.client.get("/api/studies?category=endgame"))
        self.assertEqual(listing["total"], 2)
        self.assertEqual({item["name"] for item in listing["studies"]}, {"甲", "乙"})
        self.assertIn("moveCount", listing["studies"][0])
        self.assertIn("analysisCount", listing["studies"][0])

    def test_unknown_study_is_404(self):
        response = self.client.get("/api/studies/999999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.data(response)["code"], "study_not_found")


# ----------------------------------------------------------------------
# 摆盘
# ----------------------------------------------------------------------
class TestBoard(RouteTestCase):
    def test_validate_only_does_not_touch_the_database(self):
        study_id = self.create_study(fen=START_FEN)
        body = self.data(self.put(f"/api/studies/{study_id}/board", {
            "validateOnly": True,
            "operations": [{"op": "clear"},
                           {"op": "set", "square": "e0", "piece": "K"},
                           {"op": "set", "square": "e9", "piece": "k"}]}))
        self.assertTrue(body["validateOnly"])
        self.assertFalse(body["legal"])                 # 白脸将
        self.assertTrue(body["errors"])
        self.assertEqual(body["baseFen"], START_FEN)
        self.assertEqual(self.data(self.client.get(f"/api/studies/{study_id}"))["study"]
                         ["startFen"], START_FEN)       # 一行没改

    def test_edit_replaces_the_position_of_an_empty_study(self):
        study_id = self.create_study(fen=START_FEN)
        body = self.data(self.put(f"/api/studies/{study_id}/board", {
            "operations": [{"op": "clear"},
                           {"op": "set", "square": "e0", "piece": "K"},
                           {"op": "set", "square": "d9", "piece": "k"},
                           {"op": "set", "square": "a0", "piece": "R"}],
            "toMove": "b"}))
        self.assertTrue(body["updated"])
        self.assertFalse(body["created"])
        self.assertEqual(body["applied"], 4)
        self.assertEqual(body["sideToMove"], "b")
        study = body["study"]
        self.assertEqual(study["startFen"], study["fenText"])
        self.assertEqual(study["sideToMove"], "black")
        self.assertEqual(study["startFen"].split()[1], "b")

    def test_edit_with_history_creates_a_new_study(self):
        study_id = self.create_study(gameId=self.game_id, ply=2)
        before = self.data(self.client.get(f"/api/studies/{study_id}"))
        body = self.data(self.put(f"/api/studies/{study_id}/board",
                                  {"fen": TWO_KINGS_FEN}))
        self.assertTrue(body["created"])
        self.assertEqual(body["sourceStudyId"], study_id)
        new_id = body["study"]["studyId"]
        self.assertNotEqual(new_id, study_id)
        self.assertEqual(body["study"]["studyOf"], self.game_id)
        # 原研究不变
        after = self.data(self.client.get(f"/api/studies/{study_id}"))
        self.assertEqual(before["study"]["fenText"], after["study"]["fenText"])
        self.assertEqual(len(after["moves"]), 2)

    def test_bad_operation_is_rejected(self):
        study_id = self.create_study(fen=START_FEN)
        response = self.put(f"/api/studies/{study_id}/board",
                            {"operations": [{"op": "jump", "square": "a0"}]})
        self.assertEqual(response.status_code, 400)
        body = self.data(response)
        self.assertEqual(body["code"], "bad_operation")
        self.assertTrue(body["detail"]["errors"])

    def test_stale_edit_is_refused(self):
        study_id = self.create_study(fen=START_FEN)
        response = self.put(f"/api/studies/{study_id}/board",
                            {"operations": [{"op": "remove", "square": "a0"}],
                             "previousHash": "0" * 40})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.data(response)["code"], "stale_board")

    def test_empty_edit_is_allowed_with_the_right_hash(self):
        study_id = self.create_study(fen=START_FEN)
        current = self.data(self.client.get(f"/api/studies/{study_id}"))["study"]
        from endgame import fen_rules
        body = self.data(self.put(f"/api/studies/{study_id}/board", {
            "operations": [], "previousHash": fen_rules.position_hash(current["fenText"])}))
        self.assertTrue(body["updated"])


# ----------------------------------------------------------------------
# 位置节点 / 分析 / 候选
# ----------------------------------------------------------------------
class TestAnalysis(RouteTestCase):
    def test_node_is_created_as_draft_without_touching_the_engine(self):
        study_id = self.create_study(fen=START_FEN)
        body = self.data(self.post(f"/api/studies/{study_id}/positions",
                                   {"fromPly": 0, "mode": "quick"}))
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["taskId"], body["positionId"])
        self.assertEqual(body["depth"], 24)
        self.assertEqual(body["ruleOptions"]["repetition_rule"], "chinese_2020_analysis")
        self.assertEqual(self.engine.calls, [])          # 只登记, 没开算
        # 引擎回显: resident 是布尔(池子里有没有常驻进程), 版本信息在 residentInfo
        self.assertTrue(body["engine"]["resident"])
        self.assertEqual(body["engine"]["residentInfo"]["engine"], "Pikafish test-1")
        state = self.data(self.client.get(
            f"/api/studies/{study_id}/analysis/{body['taskId']}"))
        self.assertEqual(state["status"], "draft")
        self.assertIsNone(state["position"])

    def test_analyze_reuses_the_draft_node_and_saves_the_result(self):
        study_id = self.create_study(fen=START_FEN)
        node_id, done = self.analyze_and_wait(study_id, ply=0, mode="quick")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["taskId"], node_id)
        self.assertEqual(done["engineVersion"], "Pikafish test-1")
        self.assertEqual(done["nnueFile"], "pikafish.nnue")
        self.assertEqual(done["ruleOptions"]["repetition_rule"], "chinese_2020_analysis")
        position = done["position"]
        self.assertEqual(position["bestmove"], "h2e2")
        self.assertEqual(position["resultFor"], "r")
        self.assertEqual(position["result"], "win")
        self.assertEqual(position["confidence"], "stable")
        self.assertFalse(position["needsRuleReview"])
        self.assertEqual(len(position["candidates"]), 3)
        # position 命令用"从起始局面到当前 ply"的完整着法
        self.assertEqual(self.engine.calls[0]["position"], f"fen {START_FEN}")

    def test_analyze_a_finished_node_makes_a_new_task(self):
        study_id = self.create_study(fen=START_FEN)
        node_id, first = self.analyze_and_wait(study_id, ply=0, mode="quick")
        response = self.post(f"/api/studies/{study_id}/positions/{node_id}/analyze",
                             {"mode": "quick", "depth": 20})
        body = self.data(response)
        self.assertFalse(body["reused"])
        self.assertNotEqual(body["taskId"], node_id)
        self.assertEqual(body["positionId"], node_id)
        done = self.wait_final(self.client, study_id, body["taskId"])
        self.assertEqual(done["status"], "completed")
        # 两次不同 depth 的结果各存一行, 谁也没覆盖谁
        rows = store.list_positions(study_id)
        self.assertEqual(sorted(row["depth"] for row in rows), [20, 24])
        self.assertEqual(first["position"]["depth"], 24)

    def test_candidates_endpoint(self):
        study_id = self.create_study(fen=START_FEN)
        node_id, done = self.analyze_and_wait(study_id, ply=0, mode="quick")
        by_task = self.data(self.client.get(
            f"/api/studies/{study_id}/candidates?taskId={node_id}"))
        self.assertEqual(by_task["position"]["positionId"], done["position"]["positionId"])
        first = by_task["candidates"][0]
        self.assertEqual(first["rank"], 1)
        self.assertEqual(first["moveUci"], "h2e2")
        self.assertEqual(first["scoreType"], "cp")
        self.assertEqual(first["cp"], 305)
        self.assertEqual(first["pv"][0], "h2e2")
        self.assertTrue(first["san"])
        self.assertFalse(first["isKey"])
        by_position = self.data(self.client.get(
            f"/api/studies/{study_id}/candidates?positionId={done['position']['positionId']}"))
        self.assertEqual(len(by_position["candidates"]), 3)
        by_ply = self.data(self.client.get(f"/api/studies/{study_id}/candidates?ply=0"))
        self.assertEqual(by_ply["candidates"][0]["moveUci"], "h2e2")
        missing = self.client.get(f"/api/studies/{study_id}/candidates")
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.data(missing)["code"], "missing_target")

    def test_partial_best_is_exposed_while_running(self):
        gate = self.make_gate()
        engine = FakeEngine(lines=START_LINES, snapshots=START_SNAPSHOTS, gate=gate)
        app, _ = self.make_app(engine)
        client = app.test_client()
        study_id = storage.create_game({"name": "运行中", "category": "endgame",
                                        "start_fen": START_FEN, "current_fen": START_FEN,
                                        "fen_text": START_FEN}) 
        node = self.data(client.post(f"/api/studies/{study_id}/positions",
                                     json={"fromPly": 0, "mode": "quick"},
                                     headers={"X-CSRF-Token": self.token}))
        running = self.data(client.post(
            f"/api/studies/{study_id}/positions/{node['taskId']}/analyze", json={},
            headers={"X-CSRF-Token": self.token}))
        self.assertEqual(running["status"], "queued")
        self.assertTrue(wait_for(lambda: self.state(client, study_id,
                                                    node["taskId"])["status"] == "running"))
        gate.set()
        state = self.wait_final(client, study_id, node["taskId"])
        self.assertEqual(state["partialBest"], "h2e2")     # 引擎跑到哪一路就记到哪
        self.assertEqual(state["progress"]["depth"], 30)
        # 请求体是空的: 分析沿用节点登记时的 quick 档(不是重新取默认的 standard 长考)
        self.assertEqual(state["depth"], 24)
        self.assertEqual(state["mode"], "quick")
        self.assertEqual(len(engine.calls), 1)             # 用的就是这个假引擎

    def test_draw_is_recorded_as_needing_rule_review(self):
        engine = FakeEngine(lines=[line(multipv=1, score=0, pv=["d0d1"])], bestmove="d0d1")
        app, _ = self.make_app(engine)
        client = app.test_client()
        study = self.data(client.post("/api/studies", json={"fen": TWO_KINGS_FEN},
                                      headers={"X-CSRF-Token": self.token}))
        study_id = study["study"]["studyId"]
        node = self.data(client.post(f"/api/studies/{study_id}/positions",
                                     json={"fromPly": 0, "mode": "quick"},
                                     headers={"X-CSRF-Token": self.token}))
        client.post(f"/api/studies/{study_id}/positions/{node['taskId']}/analyze", json={},
                    headers={"X-CSRF-Token": self.token})
        state = self.wait_final(client, study_id, node["taskId"])
        position = state["position"]
        self.assertEqual(position["scoreType"], "draw")
        self.assertEqual(position["result"], "needs_rule_review")   # 绝不自动判和
        self.assertEqual(position["ruleReviewStatus"], "pending")
        self.assertFalse(position["confidence"] == "stable")
        self.assertTrue(position["needsRuleReview"])
        self.assertEqual(position["drawType"], "natural")

    def test_cancel_a_queued_task(self):
        gate = self.make_gate()
        engine = FakeEngine(lines=START_LINES, gate=gate)
        app, runner = self.make_app(engine, workers=1)
        client = app.test_client()
        study_id = storage.create_game({"name": "排队", "category": "endgame",
                                        "start_fen": START_FEN, "current_fen": START_FEN,
                                        "fen_text": START_FEN})
        headers = {"X-CSRF-Token": self.token}
        first = self.data(client.post(f"/api/studies/{study_id}/positions",
                                      json={"fromPly": 0, "mode": "quick"}, headers=headers))
        client.post(f"/api/studies/{study_id}/positions/{first['taskId']}/analyze", json={},
                    headers=headers)
        second = self.data(client.post(f"/api/studies/{study_id}/positions",
                                       json={"fromPly": 0, "mode": "quick"}, headers=headers))
        client.post(f"/api/studies/{study_id}/positions/{second['taskId']}/analyze", json={},
                    headers=headers)
        cancelled = self.data(client.delete(
            f"/api/studies/{study_id}/analysis/{second['taskId']}", headers=headers))
        self.assertTrue(cancelled["cancelled"])
        self.assertEqual(cancelled["status"], "cancelled")
        gate.set()
        self.assertEqual(self.data(client.get(
            f"/api/studies/{study_id}/analysis/{second['taskId']}"))["status"], "cancelled")
        wait_for(lambda: store.count_active_tasks(study_id=study_id) == 0, timeout=5)

    def test_concurrency_limit_returns_429(self):
        gate = self.make_gate()
        engine = FakeEngine(lines=START_LINES, gate=gate)
        app, _ = self.make_app(engine, workers=2, max_per_study=1)
        client = app.test_client()
        study_id = storage.create_game({"name": "上限", "category": "endgame",
                                        "start_fen": START_FEN, "current_fen": START_FEN,
                                        "fen_text": START_FEN})
        headers = {"X-CSRF-Token": self.token}
        node = self.data(client.post(f"/api/studies/{study_id}/positions",
                                     json={"fromPly": 0, "mode": "quick"}, headers=headers))
        client.post(f"/api/studies/{study_id}/positions/{node['taskId']}/analyze", json={},
                    headers=headers)
        node2 = self.data(client.post(f"/api/studies/{study_id}/positions",
                                      json={"fromPly": 0, "mode": "quick"}, headers=headers))
        response = client.post(
            f"/api/studies/{study_id}/positions/{node2['taskId']}/analyze", json={},
            headers=headers)
        self.assertEqual(response.status_code, 429)
        body = self.data(response)
        self.assertEqual(body["code"], "task_limit")
        self.assertEqual(body["detail"]["maxPerStudy"], 1)
        gate.set()

    def test_engine_param_is_not_implemented(self):
        study_id = self.create_study(fen=START_FEN)
        node = self.data(self.post(f"/api/studies/{study_id}/positions",
                                   {"fromPly": 0, "mode": "quick"}))
        response = self.post(f"/api/studies/{study_id}/positions/{node['taskId']}/analyze",
                             {"engine": "some-other-nnue"})
        self.assertEqual(response.status_code, 501)
        self.assertEqual(self.data(response)["code"], "engine_not_implemented")

    def test_task_of_another_study_is_refused(self):
        one = self.create_study(fen=START_FEN)
        two = self.create_study(fen=START_FEN)
        node = self.data(self.post(f"/api/studies/{one}/positions",
                                   {"fromPly": 0, "mode": "quick"}))
        response = self.client.get(f"/api/studies/{two}/analysis/{node['taskId']}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.data(response)["code"], "task_not_in_study")
        missing = self.client.get(f"/api/studies/{one}/analysis/nope")
        self.assertEqual(missing.status_code, 404)

    def test_searchmoves_are_echoed_and_used(self):
        study_id = self.create_study(fen=START_FEN)
        engine = FakeEngine(lines=[line(multipv=1, score=120, pv=["b2e2", "h9g7"])],
                            bestmove="b2e2")
        app, _ = self.make_app(engine)
        client = app.test_client()
        headers = {"X-CSRF-Token": self.token}
        node = self.data(client.post(f"/api/studies/{study_id}/positions",
                                     json={"fromPly": 0, "mode": "root_moves",
                                           "searchMoves": ["b2e2"]}, headers=headers))
        self.assertEqual(node["searchMoves"], ["b2e2"])
        self.assertEqual(node["multipv"], 1)
        client.post(f"/api/studies/{study_id}/positions/{node['taskId']}/analyze",
                    json={"searchMoves": ["b2e2"]}, headers=headers)
        state = self.wait_final(client, study_id, node["taskId"])
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["mode"], "root_moves")      # 沿用节点登记的档位
        self.assertEqual(engine.calls[0]["searchmoves"], ["b2e2"])
        self.assertEqual([c["moveUci"] for c in state["position"]["candidates"]], ["b2e2"])


# ----------------------------------------------------------------------
# 备注 / 关键步
# ----------------------------------------------------------------------
class TestMoveAndComment(RouteTestCase):
    def test_comment_on_a_position_goes_to_that_move(self):
        study_id = self.create_study(gameId=self.game_id, ply=3)
        # 续研复制出来的着法行属于新研究, moveId 与原对局不同
        before = self.study_moves(study_id)
        self.assertEqual([m["uci"] for m in before], list(HISTORY))
        node = self.data(self.post(f"/api/studies/{study_id}/positions",
                                   {"fromPly": 2, "comment": "这里该走马", "mode": "quick"}))
        self.assertEqual(node["commentMoveId"], before[1]["moveId"])
        moves = self.study_moves(study_id)
        self.assertEqual(moves[1]["annotation"], "这里该走马")
        self.assertEqual(moves[0]["annotation"], None)

    def test_comment_without_a_move_is_refused(self):
        study_id = self.create_study(fen=START_FEN)
        response = self.post(f"/api/studies/{study_id}/positions",
                             {"fromPly": 0, "comment": "起手想法"})
        self.assertEqual(response.status_code, 400)
        body = self.data(response)
        self.assertEqual(body["code"], "comment_needs_move")
        self.assertEqual(self.data(self.client.get(f"/api/studies/{study_id}"))["tasks"], [])

    def test_patch_move_key_and_annotation(self):
        study_id = self.create_study(gameId=self.game_id, ply=3)
        move_id = self.study_moves(study_id)[0]["moveId"]
        body = self.data(self.patch(f"/api/studies/{study_id}/moves/{move_id}",
                                    {"isKey": True, "annotation": "关键步"}))
        self.assertTrue(body["move"]["isKey"])
        self.assertEqual(body["move"]["annotation"], "关键步")
        self.assertEqual(body["move"]["uci"], HISTORY[0])
        moves = self.study_moves(study_id)
        self.assertTrue(moves[0]["isKey"])
        # 关掉关键步
        body = self.data(self.patch(f"/api/studies/{study_id}/moves/{move_id}",
                                    {"isKey": False}))
        self.assertFalse(body["move"]["isKey"])
        self.assertEqual(body["move"]["annotation"], "关键步")

    def test_patch_move_rejects_tags_and_empty_payload(self):
        study_id = self.create_study(gameId=self.game_id, ply=3)
        move_id = self.study_moves(study_id)[0]["moveId"]
        response = self.patch(f"/api/studies/{study_id}/moves/{move_id}",
                              {"tags": ["七星聚会"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "tags_unsupported")
        empty = self.patch(f"/api/studies/{study_id}/moves/{move_id}", {})
        self.assertEqual(self.data(empty)["code"], "nothing_to_update")

    def test_patch_move_of_another_study_is_404(self):
        study_id = self.create_study(fen=START_FEN)
        response = self.patch(f"/api/studies/{study_id}/moves/{self.move_ids[0]}",
                              {"isKey": True})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.data(response)["code"], "move_not_found")


# ----------------------------------------------------------------------
# 解说 (LLM 冻结 prompt)
# ----------------------------------------------------------------------
class TestCommentary(RouteTestCase):
    def analyzed(self, **payload):
        study_id = self.create_study(fen=START_FEN)
        _, done = self.analyze_and_wait(study_id, ply=0, mode="quick", **payload)
        return study_id, done

    def test_needs_csrf(self):
        study_id, _ = self.analyzed()
        response = self.post(f"/api/studies/{study_id}/commentary", {}, csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.data(response)["code"], "csrf_failed")

    def test_falls_back_to_the_template_without_llm(self):
        study_id, done = self.analyzed()
        body = self.data(self.post(f"/api/studies/{study_id}/commentary",
                                   {"ply": 0, "useLlm": False}))
        self.assertFalse(body["llmUsed"])
        self.assertEqual(body["fallbackReason"], "llm_disabled")
        self.assertTrue(body["text"].strip())
        self.assertEqual(body["promptVersion"], commentary.PROMPT_VERSION)
        self.assertEqual(body["promptFingerprint"], commentary.prompt_fingerprint())
        self.assertEqual(body["inventedMoves"], [])
        self.assertIn("人工复核", body["disclaimer"])
        self.assertEqual(body["positionId"], done["position"]["positionId"])

    def test_template_text_only_quotes_engine_numbers(self):
        study_id, done = self.analyzed()
        body = self.data(self.post(f"/api/studies/{study_id}/commentary",
                                   {"positionId": done["position"]["positionId"],
                                    "useLlm": False}))
        facts = body["engineFacts"]
        # 事实表就是库里的引擎数值: 分数/结论/首选必须原样带出来
        self.assertEqual(facts["bestMove"], done["position"]["bestmove"])
        self.assertEqual(facts["scoreValue"], done["position"]["scoreValue"])
        self.assertEqual(facts["result"], done["position"]["result"])
        self.assertEqual(len(facts["candidates"]), len(done["position"]["candidates"]))
        # 模板解说只转述引擎给的数与坐标: 首选着法/评分原样出现, 且没有任何编造坐标
        self.assertIn(facts["bestMove"], body["text"])
        self.assertIn(str(facts["scoreValue"]), body["text"])
        self.assertEqual(commentary._invented_moves(body["text"], facts), [])

    def test_commentary_is_not_persisted(self):
        """解说按规格不落库: 库里只留引擎算出来的事实"""
        study_id, _ = self.analyzed()
        before = store.count_positions(study_id)
        tasks_before = len(store.list_tasks(study_id=study_id, limit=100))
        self.post(f"/api/studies/{study_id}/commentary", {"ply": 0, "useLlm": False})
        self.assertEqual(store.count_positions(study_id), before)
        self.assertEqual(len(store.list_tasks(study_id=study_id, limit=100)), tasks_before)

    def test_missing_target(self):
        study_id = self.create_study(fen=START_FEN)
        response = self.post(f"/api/studies/{study_id}/commentary", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "missing_target")

    def test_unknown_ply_has_no_position(self):
        study_id, _ = self.analyzed()
        response = self.post(f"/api/studies/{study_id}/commentary", {"ply": 99})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.data(response)["code"], "position_not_found")


# ----------------------------------------------------------------------
# 导入导出 xq-endgame-study v1
# ----------------------------------------------------------------------
class TestExchange(RouteTestCase):
    def exported(self, study_id, **query):
        from urllib.parse import urlencode
        url = f"/api/studies/{study_id}/export"
        if query:
            url += "?" + urlencode(query)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data[:200])
        return response

    def test_export_shape(self):
        study_id = self.create_study(gameId=self.game_id, ply=2)
        body = json.loads(self.exported(study_id).data.decode("utf-8"))
        self.assertEqual(body["format"], "xq-endgame-study")
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["study"]["name"], "原对局 · 第 2 手起")
        self.assertEqual(len(body["moves"]), 2)
        self.assertEqual(body["moves"][0]["uci"], HISTORY[0])
        self.assertIn("annotation", body["moves"][0])
        self.assertEqual(body["positions"], [])

    def test_export_download_header(self):
        study_id = self.create_study(fen=TWO_KINGS_FEN)
        response = self.exported(study_id, download=1)
        self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_export_rejects_unknown_format(self):
        study_id = self.create_study(fen=START_FEN)
        response = self.client.get(f"/api/studies/{study_id}/export?format=fenmoves")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "bad_format")

    def test_export_unknown_study_is_404(self):
        response = self.client.get("/api/studies/999999/export")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.data(response)["code"], "study_not_found")

    def test_import_needs_csrf(self):
        response = self.post("/api/studies/import", {"content": "{}"}, csrf=False)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.data(response)["code"], "csrf_failed")

    def test_import_needs_content(self):
        response = self.post("/api/studies/import", {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "missing_content")

    def test_import_rejects_other_formats_and_versions(self):
        bad_format = self.post("/api/studies/import",
                               {"content": {"format": "pgn", "version": 1, "moves": []}})
        self.assertEqual(bad_format.status_code, 400)
        self.assertEqual(self.data(bad_format)["code"], "bad_document")

        bad_version = self.post("/api/studies/import", {"content": {
            "format": "xq-endgame-study", "version": 99, "moves": [],
            "study": {"startFen": START_FEN}}})
        self.assertEqual(bad_version.status_code, 400)
        self.assertIn("version", self.data(bad_version)["message"])

    def test_round_trip_keeps_moves_and_analysis(self):
        study_id = self.create_study(gameId=self.game_id, ply=3)
        # 分析起始局面: 假引擎的候选是开局着法, 摆在别的局面上会被 validator 判为不合法
        self.analyze_and_wait(study_id, ply=0, mode="quick")
        before = self.data(self.client.get(f"/api/studies/{study_id}"))
        doc = self.exported(study_id).data.decode("utf-8")

        games_before = storage.count_games()
        body = self.data(self.post("/api/studies/import", {"content": doc}))
        self.assertEqual(body["format"], "xq-endgame-study")
        self.assertEqual(body["moveCount"], len(before["moves"]))
        self.assertEqual(body["positionCount"], len(before["positions"]))
        self.assertEqual(storage.count_games(), games_before + 1)

        after = self.data(self.client.get(f"/api/studies/{body['studyId']}"))
        self.assertEqual(after["study"]["startFen"], before["study"]["startFen"])
        self.assertEqual([m["uci"] for m in after["moves"]],
                         [m["uci"] for m in before["moves"]])
        self.assertEqual([m["annotation"] for m in after["moves"]],
                         [m["annotation"] for m in before["moves"]])
        self.assertEqual(len(after["positions"]), len(before["positions"]))
        self.assertEqual(after["positions"][0]["bestmove"],
                         before["positions"][0]["bestmove"])
        self.assertEqual(len(after["positions"][0]["candidates"]),
                         len(before["positions"][0]["candidates"]))
        # 导入的是新研究, 原研究一行不动
        self.assertEqual(len(self.data(self.client.get(f"/api/studies/{study_id}"))["moves"]),
                         len(before["moves"]))

    def test_import_replays_moves_and_rejects_broken_chain(self):
        study_id = self.create_study(gameId=self.game_id, ply=3)
        doc = json.loads(self.exported(study_id).data.decode("utf-8"))
        doc["moves"][1]["uci"] = "a0a1"          # 第 2 步在当时的局面走不通
        games_before = storage.count_games()
        response = self.post("/api/studies/import", {"content": doc})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.data(response)["code"], "bad_document")
        self.assertIn("整份拒绝", self.data(response)["message"])
        self.assertEqual(storage.count_games(), games_before)   # 一个字节都没写

    def test_import_drops_illegal_candidates_and_warns(self):
        study_id = self.create_study(fen=START_FEN)
        self.analyze_and_wait(study_id, ply=0, mode="quick")
        doc = json.loads(self.exported(study_id).data.decode("utf-8"))
        self.assertTrue(doc["positions"][0]["candidates"])
        doc["positions"][0]["candidates"][0]["move_uci"] = "a0a4"   # 开局局面里走不通
        body = self.data(self.post("/api/studies/import", {"content": doc}))
        self.assertTrue(any("不合法" in w for w in body["warnings"]), body["warnings"])
        after = self.data(self.client.get(f"/api/studies/{body['studyId']}"))
        moves = [c["moveUci"] for c in after["positions"][0]["candidates"]]
        self.assertNotIn("a0a4", moves)
        self.assertEqual(len(moves), len(doc["positions"][0]["candidates"]) - 1)

    def test_import_drops_cross_references_with_a_warning(self):
        study_id = self.create_study(gameId=self.game_id, ply=2)
        doc = json.loads(self.exported(study_id).data.decode("utf-8"))
        self.assertIsNotNone(doc["study"]["studyOf"])     # 续研带着原对局 id
        body = self.data(self.post("/api/studies/import", {"content": doc}))
        self.assertTrue(any("已忽略" in w for w in body["warnings"]), body["warnings"])
        after = self.data(self.client.get(f"/api/studies/{body['studyId']}"))
        self.assertIsNone(after["study"]["studyOf"])

    def test_import_renames_and_recategorises(self):
        study_id = self.create_study(fen=TWO_KINGS_FEN)
        doc = self.exported(study_id).data.decode("utf-8")
        body = self.data(self.post("/api/studies/import",
                                   {"content": doc, "name": "换个名字", "category": "custom"}))
        self.assertEqual(body["name"], "换个名字")
        self.assertEqual(body["category"], "custom")

    def test_import_from_a_document_object(self):
        study_id = self.create_study(fen=TWO_KINGS_FEN)
        doc = json.loads(self.exported(study_id).data.decode("utf-8"))
        body = self.data(self.post("/api/studies/import", {"document": doc}))
        self.assertEqual(body["version"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
