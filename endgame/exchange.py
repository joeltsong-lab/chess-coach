# -*- coding: utf-8 -*-
"""残局研究的导入导出: `xq-endgame-study v1`。

为什么不复用整局棋那套 (pgn_io / /api/games/*) :
    那套格式 (fen/fenmoves/iccs/pgn/json) 是"一盘棋 + 着法"的, 规格要求不改它的
    核心格式; 残局研究额外带着分析结果 (局面评分 / 候选着法 / 棋例复核状态 / 规则集),
    这些在那套格式里没有地方放, 硬塞进去就等于改格式。所以另起一个带版本号的文档格式,
    走 /api/studies/<id>/export 与 /api/studies/import 两个新接口。

文档长这样:
    {
      "format": "xq-endgame-study",
      "version": 1,
      "exportedAt": "...", "generator": "...",
      "study": {name, category, startFen, currentFen, sideToMove, ruleSet, ...},
      "moves": [{ply, side, uci, san, from, to, captured, fenBefore, fenAfter,
                 isKey, annotation, scoreCp, scoreMate}, ...],
      "positions": [{...endgame_positions 的列..., "candidates": [...]}]
    }

导入时的三条底线 (规格要求):
  1. 起始局面与每一步都重新过校验 (rules/pgn_io), 导入的着法绝不绕过 validator 入库;
     只要有一步走不通就整份拒绝 —— 中间断层会让后面的分析结果失去意义。
  2. 分析结果里的 bestmove / 候选着法也重新过 rules.validate_move, 不合法就丢掉并记
     warning, 不写进库。
  3. 指向"另一份研究/另一个局面"的引用 (studyOf / openingPositionId) 落到新库时
     目标不存在, 一律丢掉并记 warning, 不留下悬空引用。

导入不改 games/moves 的既有语义, 只调 storage / store 的公开接口。
"""

import datetime
import json

import pgn_io
import rules
import storage

from . import analyzer, fen_rules, store

FORMAT = "xq-endgame-study"
VERSION = 1
GENERATOR = "xiangqi_ai endgame.exchange"

# 导入时允许写进 games 的元信息
STUDY_META_FIELDS = ("name", "category", "tags", "note", "result", "status",
                     "study_id", "fen_text", "rule_set", "opening_position_id")


class ExchangeError(Exception):
    """文档格式/版本/内容不对 -> 接口层翻成 400 + 中文说明"""


# ----------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------
def _move_doc(row: dict) -> dict:
    return {
        "ply": row.get("ply"), "side": row.get("side"), "uci": row.get("iccs"),
        "san": row.get("chinese"), "from": row.get("from_sq"), "to": row.get("to_sq"),
        "captured": row.get("captured"),
        "fenBefore": row.get("fen_before"), "fenAfter": row.get("fen_after"),
        "isKey": bool(row.get("is_key")), "annotation": row.get("comment"),
        "scoreCp": row.get("score_cp"), "scoreMate": row.get("score_mate"),
    }


CANDIDATE_DOC_FIELDS = ("rank", "multipv_index", "move_uci", "from_square", "to_square",
                        "promotion", "score_type", "score_value", "mate_plies", "pv_uci",
                        "pv_san", "visited_nodes", "is_key_move", "is_user_variation")
POSITION_DOC_FIELDS = ("ply", "fen", "position_hash", "to_move", "repetition_rule",
                       "engine_version", "nnue_file", "depth", "seldepth", "movetime_ms",
                       "multipv", "rule_aware", "bestmove", "best_pv_uci", "score_type",
                       "score_value", "normalized_win", "result", "result_for",
                       "mate_plies", "draw_type", "rule_review_status", "confidence",
                       "analysis_duration_ms", "created_at")


def export_document(study_id: int) -> dict:
    """一份研究的完整文档 (元信息 + 着法 + 分析结果 + 候选着法)"""
    payload = storage.get_game(study_id)
    if payload is None:
        raise ExchangeError(f"研究/对局不存在: id={study_id}")
    game = payload["game"]

    positions = []
    for row in store.list_positions(study_id, limit=500):
        item = {key: row.get(key) for key in POSITION_DOC_FIELDS}
        item["candidates"] = [
            {key: cand.get(key) for key in CANDIDATE_DOC_FIELDS}
            for cand in store.list_candidates(row["id"])
        ]
        positions.append(item)

    return {
        "format": FORMAT,
        "version": VERSION,
        "exportedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "generator": GENERATOR,
        "study": {
            "name": game.get("name"), "category": game.get("category"),
            "startFen": game.get("start_fen"), "currentFen": game.get("current_fen"),
            "sideToMove": game.get("side_to_move"), "ruleSet": game.get("rule_set"),
            "result": game.get("result"), "status": game.get("status"),
            "tags": game.get("tags"), "note": game.get("note"),
            # 这两个是指向本库其他行的引用, 导出只作记录
            "studyOf": game.get("study_id"),
            "openingPositionId": game.get("opening_position_id"),
        },
        "moves": [_move_doc(m) for m in payload["moves"]],
        "positions": positions,
    }


def export_text(study_id: int, indent=2) -> str:
    return json.dumps(export_document(study_id), ensure_ascii=False, indent=indent)


# ----------------------------------------------------------------------
# 导入
# ----------------------------------------------------------------------
def parse_document(raw) -> dict:
    """文本/对象 -> 校验过格式与版本的文档"""
    if isinstance(raw, str):
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise ExchangeError(f"内容不是合法 JSON: {e}") from None
    elif isinstance(raw, dict):
        doc = raw
    else:
        raise ExchangeError("内容必须是 JSON 文本或对象")

    if not isinstance(doc, dict):
        raise ExchangeError("文档根节点必须是对象")
    fmt = doc.get("format")
    if fmt != FORMAT:
        raise ExchangeError(f"format 必须是 {FORMAT!r}, 实际 {fmt!r}")
    version = doc.get("version")
    if version != VERSION:
        raise ExchangeError(f"只认识 version={VERSION} 的文档, 实际 {version!r}")
    if not isinstance(doc.get("moves"), list):
        raise ExchangeError("moves 必须是数组")
    if doc.get("positions") is not None and not isinstance(doc["positions"], list):
        raise ExchangeError("positions 必须是数组")
    return doc


def _study_meta(doc: dict, *, name=None, category=None) -> tuple:
    """文档里的 study -> games 的列 (丢掉跨库引用)"""
    src = doc.get("study") or {}
    if not isinstance(src, dict):
        raise ExchangeError("study 必须是对象")
    warnings = []
    meta = {}
    if src.get("tags"):
        meta["tags"] = src["tags"] if isinstance(src["tags"], str) else ",".join(src["tags"])
    for key, column in (("note", "note"), ("result", "result"), ("status", "status"),
                        ("ruleSet", "rule_set")):
        if src.get(key):
            meta[column] = src[key]
    for key in ("studyOf", "openingPositionId"):
        if src.get(key):
            warnings.append(f"文档里的 {key}={src[key]} 指向原库里的另一条记录, "
                            "导入时不保留(目标库没有对应行), 已忽略")
    if meta.get("result") and meta["result"] not in storage.RESULTS:
        warnings.append(f"result={meta['result']!r} 不是 {storage.RESULTS} 之一, 已改为 '*'")
        meta["result"] = "*"
    if meta.get("status") and meta["status"] not in storage.STATUSES:
        warnings.append(f"status={meta['status']!r} 不认识, 已改为 'active'")
        meta["status"] = "active"

    meta["name"] = (name or src.get("name") or "导入的残局研究").strip()
    meta["category"] = category or src.get("category") or "study"
    if meta["category"] not in storage.CATEGORIES:
        warnings.append(f"category={meta['category']!r} 不认识, 已改为 'study'")
        meta["category"] = "study"
    return meta, warnings


def _replay_moves(start_fen: str, move_docs: list) -> tuple:
    """文档里的着法 -> 重新校验过的落库着法

    整串重放: 任何一步走不通就整份拒绝 (断层会让后面的分析结果对不上局面)。
    """
    tokens = []
    for index, item in enumerate(move_docs, 1):
        if not isinstance(item, dict) or not (item.get("uci") or "").strip():
            raise ExchangeError(f"moves 第 {index} 项缺少 uci")
        tokens.append(item["uci"].strip())
    try:
        replayed = pgn_io.replay(start_fen, tokens)
    except rules.RuleError as e:
        raise ExchangeError(f"起始局面不合法: {e}") from e
    if replayed["warnings"]:
        raise ExchangeError("着法串里有走不通的着法, 整份拒绝: "
                            + "；".join(replayed["warnings"][:3]))

    out, warnings = [], []
    for move, src in zip(replayed["moves"], move_docs):
        move = dict(move)
        # 空备注仍然写 NULL: 导入前后的注释语义要一致(没写备注 != 备注是空串)
        move["comment"] = (src.get("annotation") or "").strip() or None
        move["is_key"] = 1 if src.get("isKey") else 0
        if src.get("scoreCp") is not None:
            move["score_cp"] = src["scoreCp"]
            move["eval_by"] = "engine"
        if src.get("scoreMate") is not None:
            move["score_mate"] = src["scoreMate"]
            move["eval_by"] = "engine"
        san = (src.get("san") or "").strip()
        if san and san != move["chinese"]:
            # 中文记谱以"重新算出来的"为准: 同一着法在不同局面下叫法不同, 不能照抄文件
            warnings.append(f"第 {move['ply']} 步的中文记谱按当前局面重算为 "
                            f"{move['chinese']}(文件里是 {san})")
        out.append(move)
    return out, warnings


def _import_candidates(position: dict, candidate_docs: list, warnings: list) -> list:
    """候选着法逐条重新过规则校验; 不合法的丢掉并记 warning"""
    grid, side = rules.fen_to_board(position["fen"])
    kept = []
    for index, raw in enumerate(candidate_docs or [], 1):
        if not isinstance(raw, dict):
            warnings.append(f"第 {index} 条候选不是对象, 已丢弃")
            continue
        move = (raw.get("move_uci") or "").strip()
        if not move:
            warnings.append(f"第 {index} 条候选缺少 move_uci, 已丢弃")
            continue
        try:
            info = rules.validate_move(grid, move, side=side)
        except rules.RuleError as e:
            warnings.append(f"候选 {move} 在当前局面不合法({e}), 已丢弃")
            continue
        item = {key: raw.get(key) for key in CANDIDATE_DOC_FIELDS}
        item["move_uci"] = info["iccs"]
        item["from_square"] = info["iccs"][:2]
        item["to_square"] = info["iccs"][2:4]
        item["rank"] = int(raw.get("rank") or len(kept) + 1)
        item.setdefault("promotion", None)
        pv = [tok for tok in str(raw.get("pv_uci") or "").replace(",", " ").split() if tok]
        if pv:
            check = analyzer.validate_pv(pv, position["fen"], side=side)
            if check["error"]:
                warnings.append(f"候选 {move} 的变例第 {check['error_ply']} 步不合法, "
                                f"已截断到前 {check['plies_ok']} 步")
                pv = pv[:check["plies_ok"]]
            item["pv_uci"] = " ".join(pv)
        kept.append(item)
    return kept


def _import_positions(study_id: int, position_docs: list, warnings: list) -> int:
    """分析结果逐条重新校验后落库, 返回成功条数"""
    saved = 0
    for index, raw in enumerate(position_docs or [], 1):
        if not isinstance(raw, dict):
            warnings.append(f"第 {index} 条分析结果不是对象, 已丢弃")
            continue
        fen = (raw.get("fen") or "").strip()
        # 走子方以 fen 为准: 文档里的 to_move 是本库的 r/b 记法, 而 fen 自带 w/b,
        # 两者不一致时按 fen 走(并记一条 warning), 否则重算出来的局面会对不上。
        check = fen_rules.validate_position(fen)
        if not check["legal"]:
            warnings.append(f"第 {index} 条分析结果的局面不合法, 已丢弃: "
                            + "; ".join(e["message"] for e in check["errors"]))
            continue

        grid, side = rules.fen_to_board(check["fen"])
        doc_side = {"r": "w", "b": "b", "w": "w"}.get(str(raw.get("to_move") or "").strip())
        if doc_side and doc_side != side:
            warnings.append(f"第 {index} 条分析结果的 to_move={raw.get('to_move')!r} 与 fen "
                            f"里的走子方({side})不一致, 已按 fen 重新判定")
        result = {key: raw.get(key) for key in POSITION_DOC_FIELDS}
        result["fen"] = check["fen"]
        result["position_hash"] = check["position_hash"]
        result["to_move"] = "r" if side == "w" else "b"

        best = (result.get("bestmove") or "").strip()
        if best:
            try:
                result["bestmove"] = rules.validate_move(grid, best, side=side)["iccs"]
            except rules.RuleError as e:
                warnings.append(f"第 {index} 条的最佳着法 {best} 在当前局面不合法({e}), "
                                "已清空(保留其余分析数据)")
                result["bestmove"] = None
        best_pv = [tok for tok in str(result.get("best_pv_uci") or "").replace(",", " ").split()
                   if tok]
        if best_pv:
            pv_check = analyzer.validate_pv(best_pv, check["fen"], side=side)
            if pv_check["error"]:
                warnings.append(f"第 {index} 条的主变第 {pv_check['error_ply']} 步不合法, "
                                f"已截断到前 {pv_check['plies_ok']} 步")
                best_pv = best_pv[:pv_check["plies_ok"]]
            result["best_pv_uci"] = " ".join(best_pv)

        result["candidates"] = _import_candidates(result, raw.get("candidates"), warnings)
        try:
            store.save_analysis(result, game_id=study_id, ply=int(raw.get("ply") or 0))
        except ValueError as e:
            warnings.append(f"第 {index} 条分析结果写库失败({e}), 已跳过")
            continue
        saved += 1
    return saved


def import_document(raw, *, name=None, category=None) -> dict:
    """把一份文档导入成新研究, 返回 {studyId, ...}

    :raise ExchangeError: 格式/版本/起始局面/着法串有问题(一个字节都不写)
    """
    doc = parse_document(raw)
    meta, warnings = _study_meta(doc, name=name, category=category)

    src = doc.get("study") or {}
    start_fen = (src.get("startFen") or "").strip()
    if not start_fen:
        raise ExchangeError("study.startFen 不能为空")
    try:
        start_fen = rules.validate_fen(start_fen)
    except rules.RuleError as e:
        raise ExchangeError(f"study.startFen 不合法: {e}") from e

    moves, move_warnings = _replay_moves(start_fen, doc["moves"])
    warnings.extend(move_warnings)

    current_fen = moves[-1]["fen_after"] if moves else start_fen
    fields = dict(meta, start_fen=start_fen, current_fen=current_fen,
                  side_to_move=("black" if current_fen.split()[1] == "b" else "red"),
                  fen_text=current_fen)
    study_id = storage.create_game(fields)

    try:
        for move in moves:
            storage.append_move(study_id, dict(
                move, next_side=("black" if (move["fen_after"] or "").split()[1] == "b"
                                 else "red")))
    except ValueError as e:
        storage.delete_game(study_id)
        raise ExchangeError(f"导入失败, 已回滚: {e}") from e

    try:
        saved = _import_positions(study_id, doc.get("positions"), warnings)
    except Exception:
        storage.delete_game(study_id)
        raise

    return {"studyId": study_id, "name": fields["name"], "category": fields["category"],
            "format": FORMAT, "version": VERSION,
            "moveCount": len(moves), "positionCount": saved, "warnings": warnings}
