# -*- coding: utf-8 -*-
"""endgame.analyzer 的单元测试 (标准库 unittest, 不依赖真引擎、不碰数据库)

    python test_endgame_analyzer.py
    python -m unittest test_endgame_analyzer -v

用 FakeEngine 按剧本喂 info 行, 验证的是"参数怎么拼、结论怎么判、着法怎么校验",
真引擎的部分由 test_engine_search.py 覆盖。
"""

import unittest

import rules
from chess_engine import START_FEN
from endgame import analyzer, fen_rules


def fen_after(start_fen, moves):
    """重放整串着法得到终局 FEN (分析接口要求 fen 与 moves 说的是同一盘棋)"""
    grid, side = rules.fen_to_board(start_fen)
    for move in moves:
        grid, _info = rules.apply_iccs(grid, move, side=side)
        side = "b" if side == "w" else "w"
    return rules.board_to_fen(grid, side)


class FakeEngine:
    """假引擎: 按剧本返回 search() 结果, 并在搜索过程中回调 on_info"""

    def __init__(self, lines=None, bestmove="h2e2", snapshots=None, stopped=False,
                 engine_name="Pikafish test-1", nnue="pikafish.nnue", supported=(),
                 duration_ms=1200, responses=None):
        self.lines = [dict(line) for line in (lines or [])]
        self.bestmove = bestmove
        self.snapshots = [dict(snap) for snap in (snapshots or [])]
        self.stopped = stopped
        self.engine_name = engine_name
        self.nnue = nnue
        self.supported = set(supported)
        self.duration_ms = duration_ms
        self.responses = responses
        self.calls = []
        self.options_set = {}

    def supports(self, name):
        return name in self.supported

    def set_option(self, name, value):
        self.options_set[name] = value
        return True

    def search(self, position, *, depth=None, movetime=None, multipv=None,
               searchmoves=None, cancel=None, on_info=None, timeout_ms=None):
        self.calls.append({"position": position, "depth": depth, "movetime": movetime,
                           "multipv": multipv, "searchmoves": list(searchmoves or []),
                           "timeout_ms": timeout_ms})
        index = len(self.calls) - 1
        script = {}
        if self.responses and index < len(self.responses):
            script = self.responses[index] or {}

        limit = multipv or 1
        lines = script.get("lines")
        if lines is None:
            lines = [line for line in self.lines if (line.get("multipv") or 1) <= limit]
        snapshots = script.get("snapshots")
        if snapshots is None:
            snapshots = self.snapshots
        stopped = script.get("stopped", self.stopped)

        for snap in snapshots:
            if on_info is not None and (snap.get("multipv") or 1) <= limit:
                on_info(snap)

        return {
            "bestmove": script.get("bestmove", self.bestmove),
            "lines": [dict(line) for line in lines],
            "info_count": len(snapshots),
            "stopped": stopped,
            "duration_ms": self.duration_ms,
            "engine": {"engine": self.engine_name, "author": "tester",
                       "path": "fake", "nnue_file": self.nnue, "options": []},
        }


def line(multipv=1, depth=30, score=None, mate=None, bound=None, pv=None,
         nodes=1000, seldepth=40):
    return {"multipv": multipv, "depth": depth, "seldepth": seldepth, "score": score,
            "mate": mate, "bound": bound, "pv": list(pv or []), "nodes": nodes,
            "nps": 500, "time_ms": 100, "hashfull": 0}


def snap(depth, pv, score=None, mate=None, bound=None, multipv=1):
    return {"multipv": multipv, "depth": depth, "seldepth": depth, "score": score,
            "mate": mate, "bound": bound, "pv": list(pv), "nodes": 1, "nps": 1,
            "time_ms": 1}


STABLE_SNAPSHOTS = [
    snap(28, ["h2e2", "h9g7"], score=300),
    snap(29, ["h2e2", "h9g7"], score=310),
    snap(30, ["h2e2", "h9g7"], score=305),
]

START_LINES = [
    line(multipv=1, score=305, pv=["h2e2", "h9g7"]),
    line(multipv=2, score=120, pv=["b2e2", "h9g7"]),
    line(multipv=3, score=90, pv=["h0g2", "h9g7"]),
]


# ----------------------------------------------------------------------
# 档位与配置
# ----------------------------------------------------------------------
class TestResolveConfig(unittest.TestCase):
    def test_standard_defaults(self):
        cfg = analyzer.resolve_config("standard")
        self.assertGreaterEqual(cfg["depth"], 30)
        self.assertGreaterEqual(cfg["movetime_ms"], 60000)
        self.assertEqual(cfg["multipv"], 3)
        self.assertTrue(cfg["confirm_bestmove"])
        self.assertEqual(cfg["rule_options"]["repetition_rule"], "chinese_2020_analysis")

    def test_precise_and_quick_presets(self):
        precise = analyzer.resolve_config("precise")
        self.assertEqual((precise["depth"], precise["movetime_ms"], precise["multipv"]),
                         (40, 180000, 3))
        quick = analyzer.resolve_config("quick")
        self.assertEqual((quick["depth"], quick["movetime_ms"], quick["multipv"]),
                         (24, 10000, 3))

    def test_explicit_values_may_raise_but_not_lower(self):
        cfg = analyzer.resolve_config("standard", depth=36, movetime_ms=90000, multipv=5)
        self.assertEqual((cfg["depth"], cfg["movetime_ms"], cfg["multipv"]), (36, 90000, 5))
        for kwargs in ({"depth": 24}, {"movetime_ms": 10000}, {"multipv": 1}):
            with self.assertRaises(analyzer.AnalyzerError):
                analyzer.resolve_config("standard", **kwargs)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("turbo")

    def test_root_moves_needs_searchmoves_and_multipv_one(self):
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("root_moves")
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("root_moves", searchmoves=["h2e2"], multipv=3)
        cfg = analyzer.resolve_config("root_moves", searchmoves=["h2e2", "h2e2"])
        self.assertEqual(cfg["searchmoves"], ["h2e2"])
        self.assertEqual(cfg["multipv"], 1)
        self.assertFalse(cfg["confirm_bestmove"])

    def test_bad_searchmove_rejected(self):
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("root_moves", searchmoves=["zz99"])

    def test_timeout_is_more_than_120_percent_of_movetime(self):
        cfg = analyzer.resolve_config("standard")
        self.assertGreaterEqual(cfg["timeout_ms"],
                                int(cfg["movetime_ms"] * analyzer.TIMEOUT_RATIO))
        self.assertGreater(cfg["timeout_ms"], cfg["movetime_ms"])

    def test_rule_options_validation(self):
        cfg = analyzer.resolve_config("quick", rule_options={"mate_threat_depth": 8,
                                                             "sixty_move_rule": True})
        self.assertEqual(cfg["rule_options"]["mate_threat_depth"], 8)
        self.assertTrue(cfg["rule_options"]["sixty_move_rule"])
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("quick", rule_options={"nope": 1})
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("quick", rule_options={"mate_threat_depth": 7})
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.resolve_config("quick", rule_options={"strict_three_fold": "yes"})


# ----------------------------------------------------------------------
# UCI 命令拼装
# ----------------------------------------------------------------------
class TestCommandSnapshot(unittest.TestCase):
    def test_startpos_with_full_history(self):
        self.assertEqual(
            analyzer.build_position(fen="x", moves=["h2e2", "h9g7"]),
            "startpos moves h2e2 h9g7")
        self.assertEqual(
            analyzer.build_position(start_fen=START_FEN, moves=["h2e2"], fen="x"),
            "startpos moves h2e2")

    def test_non_standard_start_fen(self):
        fen = "3k5/9/9/9/9/9/9/9/9/3RK4 w - - 0 1"
        self.assertEqual(analyzer.build_position(start_fen=fen, moves=["d0d9"]),
                         f"fen {fen} moves d0d9")

    def test_single_position(self):
        self.assertEqual(analyzer.build_position(fen=START_FEN), f"fen {START_FEN}")
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.build_position()

    def test_go_command(self):
        cfg = analyzer.resolve_config("standard")
        self.assertEqual(analyzer.build_go_command(cfg), "go depth 30 movetime 60000")
        cfg = analyzer.resolve_config("root_moves", searchmoves=["e0d0"])
        self.assertEqual(analyzer.build_go_command(cfg),
                         "go depth 30 movetime 60000 searchmoves e0d0")

    def test_rule_engine_options_are_skipped_when_unsupported(self):
        engine = FakeEngine()
        result = analyzer.apply_rule_engine_options(
            engine, analyzer.resolve_rule_options({"sixty_move_rule": True}))
        self.assertEqual(result["applied"], {})
        self.assertIn("Mate Threat Depth", result["skipped"])
        self.assertTrue(result["notes"])
        self.assertEqual(engine.options_set, {})

    def test_rule_engine_options_applied_when_supported(self):
        engine = FakeEngine(supported={"Mate Threat Depth"})
        result = analyzer.apply_rule_engine_options(
            engine, analyzer.resolve_rule_options({"mate_threat_depth": 8}))
        self.assertEqual(result["applied"], {"Mate Threat Depth": 8})
        self.assertEqual(engine.options_set, {"Mate Threat Depth": 8})

    def test_sixty_move_rule_not_sent_by_default(self):
        engine = FakeEngine(supported={"Sixty Move Rule"})
        analyzer.apply_rule_engine_options(engine, analyzer.resolve_rule_options())
        self.assertNotIn("Sixty Move Rule", engine.options_set)


# ----------------------------------------------------------------------
# 主变稳定性
# ----------------------------------------------------------------------
class TestEvaluateStability(unittest.TestCase):
    def test_stable_when_same_move_and_flat_score(self):
        result = analyzer.evaluate_stability(STABLE_SNAPSHOTS)
        self.assertTrue(result["stable"])
        self.assertTrue(result["same_bestmove"])
        self.assertEqual(result["bestmove"], "h2e2")
        self.assertEqual(result["score_range"], 10)

    def test_unstable_when_bestmove_changes(self):
        snaps = [snap(28, ["h2e2"], score=100), snap(29, ["b2e2"], score=100),
                 snap(30, ["c0e2"], score=100)]
        result = analyzer.evaluate_stability(snaps)
        self.assertFalse(result["stable"])
        self.assertIn("最佳着法", result["reason"])

    def test_unstable_when_score_swings(self):
        snaps = [snap(28, ["h2e2"], score=20), snap(29, ["h2e2"], score=200),
                 snap(30, ["h2e2"], score=-100)]
        result = analyzer.evaluate_stability(snaps)
        self.assertFalse(result["stable"])
        self.assertIn("波动", result["reason"])

    def test_too_few_deep_samples(self):
        result = analyzer.evaluate_stability([snap(30, ["h2e2"], score=100)])
        self.assertFalse(result["stable"])
        self.assertIn("样本不足", result["reason"])

    def test_mate_is_always_stable(self):
        result = analyzer.evaluate_stability([snap(24, ["e0d0"], mate=3)])
        self.assertTrue(result["stable"])
        self.assertTrue(result["mate_proven"])

    def test_bound_scores_are_ignored(self):
        snaps = [snap(30, ["h2e2"], score=100, bound="lower"),
                 snap(30, ["b2e2"], score=-100, bound="upper")]
        result = analyzer.evaluate_stability(snaps)
        self.assertEqual(result["samples"], 0)
        self.assertFalse(result["stable"])

    def test_second_pv_does_not_count(self):
        snaps = [snap(30, ["h2e2"], score=100, multipv=2),
                 snap(30, ["b2e2"], score=100, multipv=2)]
        self.assertEqual(analyzer.evaluate_stability(snaps)["samples"], 0)


# ----------------------------------------------------------------------
# 结论判定
# ----------------------------------------------------------------------
class TestDecideConclusion(unittest.TestCase):
    def decide(self, primary, to_move="w", stable=True, rep=None):
        return analyzer.decide_conclusion(
            primary, to_move=to_move, stability={"stable": stable}, repetition_result=rep)

    def test_mate_for_side_to_move_is_win(self):
        out = self.decide(line(mate=3, pv=["e0d0"]))
        self.assertEqual(out["score_type"], "mate")
        self.assertEqual(out["score_value"], 3)
        self.assertEqual(out["result"], "win")
        self.assertEqual(out["result_for"], "r")
        self.assertEqual(out["mate_plies"], 5)          # 3 步杀 = 5 个半着
        self.assertEqual(out["confidence"], "stable")

    def test_mate_against_side_to_move_is_loss(self):
        out = self.decide(line(mate=-2, pv=["e0d0"]), to_move="b")
        self.assertEqual(out["result"], "loss")
        self.assertEqual(out["result_for"], "b")
        self.assertEqual(out["mate_plies"], 3)

    def test_cp_wins_only_when_big_and_stable(self):
        out = self.decide(line(score=300))
        self.assertEqual(out["result"], "win")
        self.assertEqual(out["result_for"], "r")
        self.assertGreater(out["normalized_win"], 0.5)
        out = self.decide(line(score=-400), to_move="b")
        self.assertEqual(out["result"], "loss")
        self.assertEqual(out["result_for"], "b")

    def test_cp_below_threshold_is_unknown(self):
        out = self.decide(line(score=120))
        self.assertEqual(out["result"], "unknown")
        self.assertEqual(out["confidence"], "preliminary")
        self.assertEqual(out["score_type"], "cp")

    def test_cp_unstable_is_unknown(self):
        out = self.decide(line(score=900), stable=False)
        self.assertEqual(out["result"], "unknown")
        self.assertEqual(out["confidence"], "preliminary")

    def test_bound_score_is_unknown(self):
        out = self.decide(line(score=900, bound="lower"))
        self.assertEqual(out["result"], "unknown")

    def test_no_score_is_unknown(self):
        out = self.decide(None)
        self.assertEqual(out["score_type"], "unknown")
        self.assertEqual(out["result"], "unknown")
        self.assertEqual(out["result_for"], "r")

    def test_zero_score_is_draw_candidate_needing_review(self):
        out = self.decide(line(score=0))
        self.assertEqual(out["score_type"], "draw")
        self.assertEqual(out["draw_type"], "natural")
        self.assertEqual(out["result"], "needs_rule_review")
        self.assertEqual(out["rule_review_status"], "pending")
        self.assertEqual(out["confidence"], "rule_review_needed")
        self.assertTrue(out["needs_rule_review"])

    def test_threefold_makes_draw_candidate_repeat(self):
        rep = {"needs_rule_review": True, "rule_review_status": "pending",
               "threefold_candidate": True, "draw_type": "repeat"}
        out = self.decide(line(score=10), rep=rep)
        self.assertEqual(out["score_type"], "draw")
        self.assertEqual(out["draw_type"], "repeat")
        self.assertEqual(out["result"], "needs_rule_review")

    def test_engine_never_overrides_rule_review(self):
        """引擎算得再大也不能把待复核的棋例说成赢棋结论"""
        rep = {"needs_rule_review": True, "rule_review_status": "pending",
               "threefold_candidate": True, "draw_type": "repeat"}
        out = self.decide(line(score=900), rep=rep)
        self.assertEqual(out["result"], "win")           # 引擎数值照实保留
        self.assertEqual(out["confidence"], "rule_review_needed")
        self.assertEqual(out["rule_review_status"], "pending")

    def test_historical_repetition_with_big_cp_is_not_draw(self):
        rep = {"needs_rule_review": True, "rule_review_status": "pending",
               "threefold_candidate": True, "draw_type": "repeat"}
        out = self.decide(line(score=900), rep=rep)
        self.assertEqual(out["score_type"], "cp")

    def test_mate_plies_helper(self):
        self.assertEqual(analyzer.mate_plies(1), 1)
        self.assertEqual(analyzer.mate_plies(-4), 7)
        self.assertEqual(analyzer.mate_plies(0), 0)

    def test_normalized_win_is_monotonic(self):
        values = [analyzer.normalized_win_from_cp(cp) for cp in (-500, -100, 0, 100, 500)]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(analyzer.normalized_win_from_cp(0), 0.5)


# ----------------------------------------------------------------------
# 候选着法 (落库前必须过 validator)
# ----------------------------------------------------------------------
class TestExtractCandidates(unittest.TestCase):
    def test_legal_lines_become_candidates(self):
        out = analyzer.extract_candidates(START_LINES, START_FEN)
        self.assertEqual(out["rejected"], [])
        self.assertEqual([c["rank"] for c in out["candidates"]], [1, 2, 3])
        first = out["candidates"][0]
        self.assertEqual(first["move_uci"], "h2e2")
        self.assertEqual((first["from_square"], first["to_square"]), ("h2", "e2"))
        self.assertIsNone(first["promotion"])
        self.assertEqual(first["score_type"], "cp")
        self.assertEqual(first["score_value"], 305)
        self.assertEqual(first["pv_uci"], "h2e2 h9g7")
        self.assertTrue(first["pv_san"].startswith("炮二平五"))
        self.assertEqual(first["pv_plies_ok"], 2)
        self.assertEqual(first["is_key_move"], 0)

    def test_illegal_engine_move_is_rejected(self):
        """引擎给不合法着法时: 记进 rejected, 绝不能进候选"""
        lines = [line(multipv=1, score=10, pv=["a0a9"]), line(multipv=2, score=5, pv=["b2e2"])]
        out = analyzer.extract_candidates(lines, START_FEN)
        self.assertEqual([c["move_uci"] for c in out["candidates"]], ["b2e2"])
        self.assertEqual(out["rejected"][0]["move"], "a0a9")
        self.assertTrue(any("不合法" in note for note in out["notes"]))

    def test_key_move_and_user_variation_flags(self):
        out = analyzer.extract_candidates(START_LINES, START_FEN,
                                          key_move="b2e2", user_moves=["h0g2"])
        flags = {c["move_uci"]: (c["is_key_move"], c["is_user_variation"])
                 for c in out["candidates"]}
        self.assertEqual(flags["b2e2"], (1, 0))
        self.assertEqual(flags["h0g2"], (0, 1))
        self.assertEqual(flags["h2e2"], (0, 0))

    def test_mate_candidate_carries_plies(self):
        out = analyzer.extract_candidates([line(multipv=1, mate=2, pv=["h2e2"])],
                                          START_FEN)
        candidate = out["candidates"][0]
        self.assertEqual(candidate["score_type"], "mate")
        self.assertEqual(candidate["mate_plies"], 3)

    def test_broken_pv_is_flagged_but_move_kept(self):
        lines = [line(multipv=1, score=10, pv=["h2e2", "a9a0"])]   # a9a0 被 a6 兵挡住
        out = analyzer.extract_candidates(lines, START_FEN)
        self.assertEqual(len(out["candidates"]), 1)
        self.assertEqual(out["candidates"][0]["pv_plies_ok"], 1)
        self.assertTrue(any("校验不过" in note for note in out["notes"]))

    def test_validate_pv_walks_the_line(self):
        result = analyzer.validate_pv(["h2e2", "h9g7"], START_FEN)
        self.assertEqual(result["error"], None)
        self.assertEqual(result["plies_ok"], 2)
        broken = analyzer.validate_pv(["h2e2", "a9a0"], START_FEN)
        self.assertEqual(broken["error_ply"], 2)
        self.assertEqual(broken["plies_ok"], 1)


# ----------------------------------------------------------------------
# 整链路 (假引擎)
# ----------------------------------------------------------------------
class TestAnalyzePosition(unittest.TestCase):
    def analyze(self, engine, **kwargs):
        params = {"fen": START_FEN, "mode": "standard"}
        params.update(kwargs)
        return analyzer.analyze_position(engine, **params)

    def test_completed_result_is_ready_for_db(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS)
        result = self.analyze(engine)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["fen"], fen_rules.validate_position(START_FEN)["fen"])
        self.assertEqual(result["position_hash"], fen_rules.position_hash(START_FEN))
        self.assertEqual(result["to_move"], "r")
        self.assertEqual((result["depth"], result["movetime_ms"], result["multipv"]),
                         (30, 60000, 3))
        self.assertEqual(result["engine_version"], "Pikafish test-1")
        self.assertEqual(result["nnue_file"], "pikafish.nnue")
        self.assertEqual(result["result"], "win")
        self.assertEqual(result["result_for"], "r")
        self.assertEqual(result["confidence"], "stable")
        self.assertEqual(result["rule_review_status"], "none")
        self.assertEqual(result["rule_aware"], 1)
        self.assertEqual(result["repetition_rule"], "chinese_2020_analysis")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["command_snapshot"]["position"], f"fen {START_FEN}")
        self.assertEqual(result["command_snapshot"]["go"], "go depth 30 movetime 60000")
        self.assertGreater(result["analysis_duration_ms"], 0)

    def test_no_engine_option_is_sent_to_pikafish_but_reported(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS)
        result = self.analyze(engine)
        self.assertEqual(result["engine_options"]["applied"], {})
        self.assertIn("Mate Threat Depth", result["engine_options"]["skipped"])

    def test_movetext_uses_full_history(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS)
        moves = ["h2e2", "h9g7"]
        result = self.analyze(engine, fen=fen_after(START_FEN, moves), moves=moves)
        self.assertEqual(result["command_snapshot"]["position"],
                         "startpos moves h2e2 h9g7")
        self.assertEqual(engine.calls[0]["position"], "startpos moves h2e2 h9g7")
        # 重复检测用的局面序列由同一份历史重放出来
        self.assertEqual(result["history_plies"], 2)

    def test_history_must_match_the_given_fen(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS)
        with self.assertRaises(analyzer.AnalyzerError):
            self.analyze(engine, moves=["h2e2", "h9g7"])   # fen 还是开局, 对不上
        self.assertEqual(engine.calls, [])

    def test_build_move_records_replays_the_history(self):
        records = analyzer.build_move_records(start_fen=START_FEN, moves=["h2e2"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["side"], "red")
        self.assertEqual(records[0]["iccs"], "h2e2")
        self.assertEqual(records[0]["fen_before"], START_FEN)
        self.assertEqual(records[0]["fen_after"], fen_after(START_FEN, ["h2e2"]))
        with self.assertRaises(analyzer.AnalyzerError):
            analyzer.build_move_records(start_fen=START_FEN, moves=["h2e2", "h2e2"])

    def test_bestmove_confirmed_by_multipv_one(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS,
                            responses=[None, {"bestmove": "b2e2",
                                              "lines": [line(multipv=1, score=200, pv=["b2e2"])]}])
        result = self.analyze(engine)
        self.assertEqual(len(engine.calls), 2)
        self.assertEqual(engine.calls[1]["multipv"], 1)
        self.assertEqual(engine.calls[1]["searchmoves"], [])
        self.assertEqual(result["confirmed_bestmove"], "b2e2")
        self.assertEqual(result["bestmove"], "b2e2")
        self.assertTrue(any("MultiPV" in note for note in result["notes"]))
        self.assertEqual(result["command_snapshot"]["go_confirm"],
                         "go depth 30 movetime 60000")

    def test_quick_mode_skips_the_confirmation_search(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS)
        self.analyze(engine, mode="quick", movetime_ms=10000, depth=24)
        self.assertEqual(len(engine.calls), 1)

    def test_controlled_variation_uses_searchmoves(self):
        engine = FakeEngine(lines=[line(multipv=1, score=-30, pv=["h2e2", "h9g7"])],
                            snapshots=[snap(30, ["h2e2", "h9g7"], score=-30)],
                            bestmove="h2e2")
        result = self.analyze(engine, mode="root_moves", searchmoves=["h2e2"])
        self.assertEqual(engine.calls[0]["searchmoves"], ["h2e2"])
        self.assertEqual(engine.calls[0]["multipv"], 1)
        self.assertEqual(result["result"], "unknown")
        self.assertEqual(result["confidence"], "preliminary")

    def test_cancelled_search_is_not_a_conclusion(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS, stopped=True)
        result = self.analyze(engine)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(result["progress"]["stopped"])
        self.assertTrue(any("取消" in note for note in result["notes"]))

    def test_illegal_position_is_refused(self):
        engine = FakeEngine()
        with self.assertRaises(analyzer.AnalyzerError):
            self.analyze(engine, fen="9/9/9/9/9/9/9/9/9/9 w - - 0 1")
        self.assertEqual(engine.calls, [])

    def test_illegal_searchmove_is_refused(self):
        engine = FakeEngine()
        with self.assertRaises(analyzer.AnalyzerError):
            self.analyze(engine, mode="root_moves", searchmoves=["a0a9"])
        self.assertEqual(engine.calls, [])

    def test_repetition_signals_flow_into_conclusion(self):
        engine = FakeEngine(lines=[line(multipv=1, score=10, pv=["e0d0"])],
                            snapshots=[snap(30, ["e0d0"], score=10)], bestmove="e0d0")
        rep = {"needs_rule_review": True, "rule_review_status": "pending",
               "threefold_candidate": True, "draw_type": "repeat", "notes": ["重复"]}
        result = self.analyze(engine, fen="3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1",
                              repetition_result=rep)
        self.assertEqual(result["result"], "needs_rule_review")
        self.assertEqual(result["score_type"], "draw")
        self.assertEqual(result["draw_type"], "repeat")
        self.assertEqual(result["rule_review_status"], "pending")
        self.assertEqual(result["confidence"], "rule_review_needed")

    def test_repetition_computed_from_moves_when_not_given(self):
        engine = FakeEngine(lines=[line(multipv=1, score=400, pv=["e0d0"])],
                            snapshots=[snap(30, ["e0d0"], score=400)], bestmove="e0d0")
        result = self.analyze(engine, fen="3k5/9/9/9/9/9/9/9/9/4RK3 w - - 0 1")
        self.assertIn("threefold_candidate", result["repetition"])
        self.assertFalse(result["repetition"]["threefold_candidate"])

    def test_progress_callback_reports_info(self):
        engine = FakeEngine(lines=START_LINES, snapshots=STABLE_SNAPSHOTS)
        seen = []
        self.analyze(engine, on_progress=lambda info: seen.append(info["depth"]))
        self.assertEqual(seen, [28, 29, 30])


if __name__ == "__main__":
    unittest.main(verbosity=2)
