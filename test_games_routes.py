# -*- coding: utf-8 -*-
"""games_routes.py 的端到端自测: 用 Flask 测试客户端把九个接口跑一遍。

跑法:
    python test_games_routes.py

用的是临时数据库(XQ_DB_PATH 指向临时目录), 不会碰到 data/chess.db。
不碰引擎: 打分要 score=true 才触发, 只有 test_24 用到, 而且是把引擎"摘掉"来测降级。
"""

import os
import tempfile
import unittest

# 必须在 import storage / app 之前把数据库指到临时目录
_TMP_DIR = tempfile.mkdtemp(prefix="xq_games_test_")
os.environ["XQ_DB_PATH"] = os.path.join(_TMP_DIR, "test.db")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from app import app                                    # noqa: E402
from core.chess_engine import START_FEN                # noqa: E402

OPENING = ["h2e2", "h9g7", "h0g2", "i9h9"]
OPENING_CN = ["炮二平五", "马8进7", "马二进三", "车9平8"]
# 中炮对屏风马开局, 带中文着法与评语, 用来测"导入 PGN 再复盘"
PGN_TEXT = """[Game "屏风马示例"]
[Red "甲"]
[Black "乙"]
[Date "2026.09.17"]
[Result "*"]

1. 炮二平五 {当头炮} 马8进7 2. 马二进三 {稳} 车9平8 *
"""
# 一个轮到黑方的中局局面, 用来测"只存局面"
MID_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1"


class GamesApiTest(unittest.TestCase):
    client = None

    @classmethod
    def setUpClass(cls):
        cls.client = app.test_client()

    def _save(self, **payload):
        return self.client.post("/api/games/save", json=payload)

    def _new_game(self, **payload) -> int:
        """建一局并把 id 拿出来, 让每个用例自己带棋局, 不依赖别的用例"""
        payload.setdefault("name", "临时对局")
        payload.setdefault("start_fen", START_FEN)
        resp = self._save(**payload)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()["game_id"]

    # ---------- 主线: 走子 -> 保存 -> 列表 -> 续下 -> 复盘 -> 导出 -> 导入 ----------
    def test_01_main_flow(self):
        # 1) 保存当前整局
        resp = self._save(name="端到端主线", category="game", start_fen=START_FEN,
                          move_list=OPENING, meta={"red": "甲", "black": "乙", "tags": "中炮,开局"})
        data = resp.get_json()
        self.assertEqual(resp.status_code, 200, data)
        self.assertTrue(data["created"])
        self.assertEqual(data["move_count"], 4)
        self.assertEqual(data["side_to_move"], "red")     # 走了 4 步(红黑各 2), 又轮到红方
        game_id = data["game_id"]

        # 2) 列表: 带步数/最后一步, 不返回着法数组
        row = self.client.get("/api/games/list?keyword=端到端主线").get_json()["games"][0]
        self.assertEqual(row["id"], game_id)
        self.assertEqual(row["move_count"], 4)
        self.assertEqual(row["last_chinese"], OPENING_CN[-1])
        self.assertEqual(row["red_name"], "甲")
        self.assertEqual(row["tags"], "中炮,开局")
        self.assertNotIn("moves", row)

        # 3) 详情: 每步都带中文/前后 FEN/行列
        detail = self.client.get(f"/api/games/{game_id}").get_json()
        self.assertEqual([m["iccs"] for m in detail["moves"]], OPENING)
        self.assertEqual([m["chinese"] for m in detail["moves"]], OPENING_CN)
        self.assertEqual([m["ply"] for m in detail["moves"]], [1, 2, 3, 4])
        self.assertEqual([m["side"] for m in detail["moves"]], ["red", "black", "red", "black"])
        self.assertEqual(detail["moves"][0]["fen_before"], START_FEN)
        self.assertEqual(detail["moves"][0]["fen_after"], detail["moves"][1]["fen_before"])
        self.assertEqual(detail["moves"][0]["from_sq"], "h2")
        self.assertEqual(detail["current_fen"], detail["moves"][-1]["fen_after"])

        # 4) 续下一步: 马八进七
        data = self.client.post(f"/api/games/{game_id}/append_move",
                                json={"iccs": "b0c2", "comment": "出马"}).get_json()
        self.assertEqual((data["ply"], data["side"], data["chinese"], data["side_to_move"]),
                         (5, "red", "马八进七", "black"))
        detail = self.client.get(f"/api/games/{game_id}").get_json()
        self.assertEqual(len(detail["moves"]), 5)
        self.assertEqual(detail["moves"][-1]["comment"], "出马")
        self.assertEqual(detail["current_fen"], detail["moves"][-1]["fen_after"])

        # 5) 同一局再存一次: 多走的写进去, 少走的截掉 (复盘时"悔棋后再存"就是这个路径)
        self.assertEqual(self._save(game_id=game_id,
                                    move_list=OPENING + ["b0c2", "b9c7"]
                                    ).get_json()["move_count"], 6)
        self.assertEqual(self._save(game_id=game_id, move_list=OPENING
                                    ).get_json()["move_count"], 4)
        detail = self.client.get(f"/api/games/{game_id}").get_json()
        self.assertEqual([m["iccs"] for m in detail["moves"]], OPENING)
        self.assertEqual(detail["current_fen"], detail["moves"][-1]["fen_after"])

        # 6) 导出四种格式
        fenmoves = self.client.get(f"/api/games/{game_id}/export?fmt=fenmoves").get_data(as_text=True)
        self.assertEqual(fenmoves, f"{START_FEN} moves " + " ".join(OPENING))
        self.assertEqual(self.client.get(f"/api/games/{game_id}/export?fmt=iccs"
                                         ).get_data(as_text=True), " ".join(OPENING))
        pgn = self.client.get(f"/api/games/{game_id}/export?fmt=pgn").get_data(as_text=True)
        self.assertIn('[Game "端到端主线"]', pgn)
        self.assertIn('[Format "ICCS"]', pgn)
        self.assertIn("[Red \"甲\"]", pgn)
        self.assertTrue(pgn.rstrip().endswith("*"))
        chinese_pgn = self.client.get(f"/api/games/{game_id}/export?fmt=pgn&move_format=chinese"
                                      ).get_data(as_text=True)
        self.assertIn("炮二平五", chinese_pgn)
        jso = self.client.get(f"/api/games/{game_id}/export?fmt=json").get_data(as_text=True)
        self.assertIn('"moves"', jso)

        # 7) 导出的棋谱都能原样导入回来 (复盘)
        for content in (fenmoves, pgn, chinese_pgn, jso):
            resp = self.client.post("/api/games/import", json={"content": content, "name": "导入回来"})
            self.assertEqual(resp.status_code, 200, resp.get_json())
            new_id = resp.get_json()["game_id"]
            moves = self.client.get(f"/api/games/{new_id}").get_json()["moves"]
            self.assertEqual([m["iccs"] for m in moves], OPENING)

        # 8) 下载
        resp = self.client.get(f"/api/games/{game_id}/export?fmt=pgn&download=1")
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(".pgn", resp.headers["Content-Disposition"])

        # 9) 某步加评语/标关键; 改元信息
        move_id = self.client.get(f"/api/games/{game_id}").get_json()["moves"][0]["id"]
        data = self.client.post(f"/api/games/{game_id}/move/{move_id}/comment",
                                json={"comment": "当头炮, 进攻性最强", "is_key": True}).get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["move"]["comment"], "当头炮, 进攻性最强")
        self.assertEqual(data["move"]["is_key"], 1)

        game = self.client.post(f"/api/games/{game_id}/update",
                                json={"name": "改名了", "category": "study", "result": "1-0",
                                      "note": "复盘用"}).get_json()["game"]
        self.assertEqual((game["name"], game["category"], game["result"], game["note"]),
                         ("改名了", "study", "1-0", "复盘用"))

    # ---------- 只存局面 (残局/研究) ----------
    def test_10_save_snapshot_only(self):
        """只给一个 FEN: 不重放着法, 只把这个局面存下来, 之后可以接着走"""
        resp = self._save(name="残局研究", category="endgame", start_fen=MID_FEN,
                          current_fen=MID_FEN, meta={"note": "只存局面"})
        data = resp.get_json()
        self.assertEqual(data["move_count"], 0)
        self.assertEqual(data["current_fen"], MID_FEN)
        self.assertEqual(data["side_to_move"], "black")

        game_id = data["game_id"]
        detail = self.client.get(f"/api/games/{game_id}").get_json()
        self.assertEqual(detail["moves"], [])
        self.assertEqual(detail["start_fen"], MID_FEN)
        self.assertEqual(self.client.get(f"/api/games/{game_id}/export?fmt=fen"
                                         ).get_data(as_text=True), MID_FEN)

        # 这个摆设局面照样能续下 (黑方 马8进7)
        step = self.client.post(f"/api/games/{game_id}/append_move", json={"iccs": "h9g7"}).get_json()
        self.assertEqual(step["chinese"], "马8进7")
        self.assertEqual(step["side_to_move"], "red")

    # ---------- 导入 PGN 并复盘 ----------
    def test_11_import_pgn_chinese(self):
        data = self.client.post("/api/games/import",
                                json={"content": PGN_TEXT, "name": "屏风马复盘",
                                      "category": "imported"}).get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["fmt"], "pgn")
        self.assertEqual(data["move_count"], 4)
        self.assertEqual(data["warnings"], [])

        detail = self.client.get(f"/api/games/{data['game_id']}").get_json()
        self.assertEqual([m["iccs"] for m in detail["moves"]], OPENING)
        self.assertEqual([m["chinese"] for m in detail["moves"]], OPENING_CN)
        self.assertEqual(detail["red_name"], "甲")
        self.assertEqual(detail["date"], "2026-09-17")
        # 评语按位置挂在前一步上
        self.assertEqual(detail["moves"][0]["comment"], "当头炮")
        self.assertEqual(detail["moves"][2]["comment"], "稳")

    def test_12_import_fenmoves_with_bad_move(self):
        """坏着法只记 warning, 不中断整局"""
        data = self.client.post("/api/games/import",
                                json={"content": f"{START_FEN} moves h2e2 乱码 h9g7"}).get_json()
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["move_count"], 2)
        self.assertEqual(len(data["warnings"]), 1)

    # ---------- 错误处理: 一律 400 + 中文原因, 且不写库 ----------
    def test_20_bad_fen_rejected(self):
        before = self.client.get("/api/games/list").get_json()["total"]
        resp = self._save(name="坏 FEN", start_fen="9/9/9 w - - 0 1")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不合法", resp.get_json()["error"])
        self.assertEqual(self.client.get("/api/games/list").get_json()["total"], before)

    def test_21_illegal_move_rejected(self):
        before = self.client.get("/api/games/list").get_json()["total"]
        resp = self._save(name="坏着法", start_fen=START_FEN, move_list=["h2e2", "h2e2"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("跳过", resp.get_json()["error"])
        self.assertEqual(self.client.get("/api/games/list").get_json()["total"], before)

    def test_22_append_move_illegal_and_missing_game(self):
        game_id = self._new_game(move_list=OPENING)
        resp = self.client.post(f"/api/games/{game_id}/append_move", json={"iccs": "a0a9"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("不是合法着法", resp.get_json()["error"])
        # 非法着法不写库
        self.assertEqual(len(self.client.get(f"/api/games/{game_id}").get_json()["moves"]), 4)

        self.assertEqual(self.client.post("/api/games/999999/append_move",
                                          json={"iccs": "h2e2"}).status_code, 404)
        self.assertEqual(self.client.get("/api/games/999999").status_code, 404)
        self.assertEqual(self.client.delete("/api/games/999999").status_code, 404)
        self.assertEqual(self.client.post("/api/games/999999/update",
                                          json={"name": "x"}).status_code, 404)

    def test_23_bad_params(self):
        game_id = self._new_game()
        self.assertEqual(self._save(name="x", category="乱写").status_code, 400)
        self.assertEqual(self._save(name="x", result="赢").status_code, 400)
        self.assertEqual(self._save(name="x", start_fen=START_FEN,
                                    move_list="h2e2").status_code, 400)
        self.assertEqual(self.client.post("/api/games/import",
                                          json={"content": "   "}).status_code, 400)
        self.assertEqual(self.client.post("/api/games/import",
                                          json={"content": "随便写点什么"}).status_code, 400)
        self.assertEqual(self.client.post("/api/games/import",
                                          json={"content": "h2e2", "fmt": "docx"}).status_code, 400)
        self.assertEqual(self.client.get("/api/games/list?category=乱写").status_code, 400)
        self.assertEqual(self.client.get(f"/api/games/{game_id}/export?fmt=docx").status_code, 400)

    def test_24_score_flag_without_engine(self):
        """引擎不可用时打分只是"这一步没评分", 不能影响保存"""
        import games_routes
        getter = games_routes._get_engine
        games_routes._get_engine = None
        try:
            resp = self._save(name="无引擎打分", start_fen=START_FEN, move_list=["h2e2"], score=True)
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertEqual(resp.get_json()["scored"], 0)
            moves = self.client.get(f"/api/games/{resp.get_json()['game_id']}").get_json()["moves"]
            self.assertEqual(moves[0]["iccs"], "h2e2")
            self.assertIsNone(moves[0]["score_cp"])
            self.assertIsNone(moves[0]["eval_by"])
        finally:
            games_routes._get_engine = getter

    def test_30_delete_game(self):
        game_id = self._new_game(move_list=OPENING)
        self.assertEqual(self.client.delete(f"/api/games/{game_id}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/games/{game_id}").status_code, 404)
        # 级联: 着法也没了
        from core.storage import list_moves
        self.assertEqual(list_moves(game_id), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
