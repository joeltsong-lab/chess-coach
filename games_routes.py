# -*- coding: utf-8 -*-
"""棋局保存 / 复盘接口 (Flask Blueprint)。

    POST   /api/games/save                         新建或更新一局(可带整串着法)
    POST   /api/games/<id>/append_move             续下一步
    GET    /api/games/list?category&tag&keyword    列表(带步数, 不含着法数组)
    GET    /api/games/<id>                         详情(元信息 + 全部着法)
    POST   /api/games/<id>/update                  改元信息/备注/结果
    DELETE /api/games/<id>                         删局(级联删着法)
    POST   /api/games/<id>/move/<move_id>/comment  改某步的评语/关键步/评分
    GET    /api/games/<id>/export?fmt=...          导出 fen/fenmoves/iccs/pgn/json
    POST   /api/games/import                       导入(自动认格式或显式给 fmt)

约定:
    - 所有响应都是 {"ok": true/false, ...}; 参数/规则错误一律 400 + 中文 error, 不写库。
    - 引擎只用来"顺手给某步打分", 由 app.py 通过 init_app(app, get_engine, lock) 注入,
      所以本模块不 import app, 既没有循环依赖, 也不会起第二个引擎进程。
    - 引擎没装好/被打分时挂了, 只是这一步没有评分, 不影响保存本身。
"""

import datetime
import re
import threading
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request

from core import pgn_io, storage
from core.chess_engine import START_FEN
from core.coord_utils import FILES, iccs_to_chinese
from core.rules import RuleError, apply_iccs, board_to_fen, fen_to_board, validate_fen

bp = Blueprint("games", __name__)

# 一次请求最多给多少步打分 / 打分时引擎思考多久(打分是顺手做的事, 不能拖太久)
MAX_SCORE_MOVES = 20
DEFAULT_SCORE_MOVETIME = 300

# 请求体里可以写进 games 表的列 (顶层或 meta 子对象里都认)
META_FIELDS = ("name", "category", "tags", "note", "result", "status", "event", "site",
               "date", "round", "opening", "ecco", "red_name", "black_name")
# meta 里常见的简写
META_ALIASES = {"red": "red_name", "black": "black_name", "red_name": "red_name"}
# 着法上一起写库的附加字段
MOVE_EXTRA_FIELDS = ("comment", "score_cp", "score_mate", "eval_by", "is_key")

EXPORT_EXT = {"fen": "txt", "fenmoves": "txt", "iccs": "txt", "pgn": "pgn", "json": "json"}

# 由 init_app 注入
_get_engine = None
_analyze_lock = None


def init_app(app, get_engine=None, analyze_lock=None):
    """由 app.py 调用: 建表 + 注入引擎访问方式 + 注册蓝图"""
    global _get_engine, _analyze_lock
    _get_engine = get_engine
    _analyze_lock = analyze_lock or threading.Lock()
    storage.init_db()
    app.register_blueprint(bp)


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _err(message: str, code: int = 400):
    """统一的失败响应"""
    return jsonify({"ok": False, "error": message}), code


def _sq(row: int, col: int) -> str:
    """棋盘行列 -> ICCS 坐标名, 如 h2 """
    return f"{FILES[col]}{9 - row}"


def _side_name(fen: str) -> str:
    """FEN 的走子方 -> red / black"""
    parts = (fen or "").split()
    return "black" if len(parts) > 1 and parts[1].lower().startswith("b") else "red"


def _clean_fen(fen, field):
    """结构校验 + 规范化; 空值返回 None, 不合法抛 RuleError(中文)"""
    text = (fen or "").strip()
    if not text:
        return None
    try:
        return validate_fen(text)
    except RuleError as e:
        raise RuleError(f"{field} 不合法: {e}") from e


def _meta_fields(payload: dict) -> dict:
    """把请求体里的元信息收拢成一个 dict (顶层 + meta 子对象, 只认白名单列)

    顺手把 category/result/status 的取值校验掉, 免得库里出现列表页认不出的值。
    """
    meta = payload.get("meta") or {}
    if not isinstance(meta, dict):
        raise RuleError("meta 必须是对象")

    out = {}
    for source in (payload, meta):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            field = META_ALIASES.get(key, key)
            if field in META_FIELDS and value is not None:
                out[field] = value

    if "category" in out and out["category"] not in storage.CATEGORIES:
        raise RuleError(f"category 只能是 {'/'.join(storage.CATEGORIES)}")
    if "result" in out and out["result"] not in storage.RESULTS:
        raise RuleError(f"result 只能是 {'/'.join(storage.RESULTS)}")
    if "status" in out and out["status"] not in storage.STATUSES:
        raise RuleError(f"status 只能是 {'/'.join(storage.STATUSES)}")

    for key in ("name", "tags", "note", "event", "site", "date", "round",
                "opening", "ecco", "red_name", "black_name"):
        if key in out and out[key] is not None:
            out[key] = ",".join(str(t).strip() for t in out[key] if str(t).strip()) \
                if isinstance(out[key], (list, tuple)) else str(out[key]).strip()
    return out


def _normalize_move_list(move_list) -> list:
    """着法列表: [iccs] 或 [{iccs, chinese?, comment?, ...}] -> [{iccs, ...}]"""
    if not isinstance(move_list, (list, tuple)):
        raise RuleError("move_list 必须是数组")
    out = []
    for i, item in enumerate(move_list, 1):
        if isinstance(item, str):
            out.append({"iccs": item.strip()})
            continue
        if not isinstance(item, dict):
            raise RuleError(f"move_list 第 {i} 项既不是 ICCS 字符串也不是对象")
        item = dict(item)
        if not (item.get("iccs") or "").strip():
            raise RuleError(f"move_list 第 {i} 项缺少 iccs")
        item["iccs"] = item["iccs"].strip()
        out.append(item)
    return out


def _replay(base_fen: str, move_list) -> list:
    """按 move_list 从 base_fen 重放, 返回带完整信息的着法列表

    只要有一走着不通就整体拒绝(返回错误, 不写库), 免得存进去半截对局。
    """
    raw = _normalize_move_list(move_list)
    try:
        replayed = pgn_io.replay(base_fen, [m["iccs"] for m in raw])
    except RuleError as e:
        raise RuleError(f"起始局面不合法: {e}") from e
    if replayed["warnings"]:
        raise RuleError("；".join(replayed["warnings"][:3]))

    pgn_io.merge_move_extras(replayed["moves"], raw)
    for move, original in zip(replayed["moves"], raw):
        chinese = (original.get("chinese") or "").strip()
        if chinese:
            move["chinese"] = chinese
    return replayed["moves"]


def _move_row(move: dict) -> dict:
    """入库前补上"下一步该谁走"

    storage 自己从 FEN 推断出来的是 w/b, 而 side_to_move 这一列按设计存 red/black, 所以这里显式给。
    """
    return dict(move, next_side=_side_name(move.get("fen_after")))


def _sync_moves(game_id: int, replayed: list) -> None:
    """把库里的着法与 replayed 对齐: 保留公共前缀(顺手补评语), 多余的截掉, 缺的追加

    这样"同一局里又走了几步"只会写新增的那几步, 不会整表删了重建。
    """
    old = storage.list_moves(game_id)
    same = 0
    while (same < len(old) and same < len(replayed)
           and old[same]["iccs"] == replayed[same]["iccs"]):
        same += 1

    for i in range(same):
        extra = {k: replayed[i][k] for k in MOVE_EXTRA_FIELDS
                 if replayed[i].get(k) not in (None, "")}
        if extra and any(old[i].get(k) != v for k, v in extra.items()):
            storage.update_move(old[i]["id"], extra)

    if same < len(old):
        storage.delete_moves_after(game_id, same)
    for move in replayed[same:]:
        storage.append_move(game_id, _move_row(move))


def _engine_score(fen: str, movetime: int):
    """请引擎给一个局面打分, 返回 (score_cp, score_mate); 引擎不可用就返回 (None, None)"""
    if _get_engine is None:
        return None, None
    try:
        engine = _get_engine()
        lock = _analyze_lock or threading.Lock()
        with lock:
            result = engine.analyze(fen, movetime=movetime, multipv=1)
    except Exception:            # 引擎缺失/崩溃/超时都只当作"这一步没评分"
        return None, None
    return result.get("score"), result.get("mate")


def _score_move(move: dict, movetime: int):
    """按"走这一步的人"的视角给这一步打分 (引擎报的是走完之后那一方, 所以要取负)"""
    score, mate = _engine_score(move["fen_after"], movetime)
    if score is None and mate is None:
        return None
    extra = {"eval_by": "engine"}
    if mate is not None:
        extra["score_mate"] = -mate
        extra["score_cp"] = None
    else:
        extra["score_cp"] = -score
        extra["score_mate"] = None
    return extra


def _score_moves(game_id: int, movetime: int, limit: int) -> int:
    """把库里还没评分的着法按顺序补上前 limit 步的评分, 返回真正评了几步"""
    scored = 0
    for move in storage.list_moves(game_id):
        if scored >= limit:
            break
        if move["score_cp"] is not None or move["score_mate"] is not None:
            continue
        extra = _score_move(move, movetime)
        if extra:
            storage.update_move(move["id"], extra)
            scored += 1
    return scored


def _read_score_flag(payload: dict):
    """请求里的"要不要顺便打分"与思考时间"""
    movetime = payload.get("movetime")
    try:
        movetime = int(movetime) if movetime else DEFAULT_SCORE_MOVETIME
    except (TypeError, ValueError):
        movetime = DEFAULT_SCORE_MOVETIME
    return bool(payload.get("score")), max(50, min(movetime, 10000))


# ----------------------------------------------------------------------
# 保存 / 续下
# ----------------------------------------------------------------------
@bp.route("/api/games/save", methods=["POST"])
def api_games_save():
    """新建或更新一局

    body: {game_id?, name?, category?, start_fen?, current_fen?, move_list?, meta{...},
           score?, movetime?}
      - 没有 game_id 就新建; 有就是更新(并按 move_list 增量写 moves)。
      - move_list 给了就整串重放, 库里的着法与之对齐; 不给就只更新局面快照
        (研究/残局场景常常只关心当前这个局面)。
      - start_fen / current_fen 都会做结构校验, 不合法直接报错, 一个字节都不写。
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _err("请求体必须是 JSON 对象")

    try:
        meta = _meta_fields(payload)
        start_fen = _clean_fen(payload.get("start_fen"), "start_fen")
        current_fen = _clean_fen(payload.get("current_fen"), "current_fen")
    except RuleError as e:
        return _err(str(e))

    game_id = payload.get("game_id")
    existing = None
    if game_id not in (None, "", 0):
        try:
            game_id = int(game_id)
        except (TypeError, ValueError):
            return _err(f"game_id 必须是整数: {game_id!r}")
        existing = storage.get_game_meta(game_id)
        if existing is None:
            return _err(f"对局不存在: id={game_id}", 404)

    # 起始局面: 请求里给了就用它, 否则沿用库里的, 再没有就是标准开局
    base_fen = start_fen or (existing or {}).get("start_fen") or START_FEN
    try:
        base_fen = validate_fen(base_fen)
    except RuleError as e:
        return _err(f"起始局面不合法: {e}")

    has_move_list = payload.get("move_list") is not None
    replayed = None
    if has_move_list:
        try:
            replayed = _replay(base_fen, payload.get("move_list"))
        except RuleError as e:
            return _err(str(e))

    fields = dict(meta)
    fields["start_fen"] = base_fen
    if replayed:
        fields["current_fen"] = replayed[-1]["fen_after"]
    elif current_fen:
        fields["current_fen"] = current_fen
    effective_fen = fields.get("current_fen") or (existing or {}).get("current_fen") or base_fen
    fields["side_to_move"] = _side_name(effective_fen)

    if existing:
        target_id = existing["id"]
        storage.update_game(target_id, fields)
    else:
        fields.setdefault("category", "game")
        target_id = storage.create_game(fields)

    if has_move_list:
        _sync_moves(target_id, replayed)
        # 一步不剩时(用户撤空了), 当前局面回到快照
        if not replayed:
            storage.update_game(target_id, {"current_fen": fields.get("current_fen") or base_fen,
                                            "side_to_move": _side_name(
                                                fields.get("current_fen") or base_fen)})

    score, movetime = _read_score_flag(payload)
    scored = _score_moves(target_id, movetime, MAX_SCORE_MOVES) if score else 0

    game = storage.get_game_meta(target_id)
    return jsonify({"ok": True, "game_id": target_id, "created": existing is None,
                    "current_fen": game["current_fen"], "side_to_move": game["side_to_move"],
                    "move_count": storage.count_moves(target_id), "scored": scored})


@bp.route("/api/games/<int:game_id>/append_move", methods=["POST"])
def api_games_append_move(game_id):
    """续下一步: body {iccs, chinese?, comment?, score?, movetime?}

    先按当前局面校验合法性(非法着法返回中文错误, 不写库), 再落子、算新 FEN、写 moves,
    同一个事务里把对局推进到新局面。
    """
    payload = request.get_json(silent=True) or {}
    iccs = (payload.get("iccs") or "").strip()
    if not iccs:
        return _err("iccs 不能为空")

    game = storage.get_game(game_id)
    if game is None:
        return _err(f"对局不存在: id={game_id}", 404)

    current = game["game"]["current_fen"] or game["game"]["start_fen"] or START_FEN
    try:
        grid, side = fen_to_board(current)
    except RuleError as e:
        return _err(f"当前局面不合法: {e}")

    fen_before = board_to_fen(grid, side)
    try:
        _after, info = apply_iccs(grid, iccs, side=side)
    except RuleError as e:
        return _err(str(e))

    move = {
        "side": "red" if info["side"] == "w" else "black",
        "iccs": info["iccs"],
        "chinese": (payload.get("chinese") or "").strip() or iccs_to_chinese(info["iccs"], fen_before),
        "from_sq": _sq(*info["from"]),
        "to_sq": _sq(*info["to"]),
        "captured": info["captured"],
        "fen_before": fen_before,
        "fen_after": info["fen_after"],
    }
    for key in MOVE_EXTRA_FIELDS:
        if payload.get(key) not in (None, ""):
            move[key] = payload[key]

    score, movetime = _read_score_flag(payload)
    if score and move.get("score_cp") is None and move.get("score_mate") is None:
        move.update(_score_move(move, movetime) or {})

    try:
        move_id = storage.append_move(game_id, _move_row(move))
    except ValueError as e:
        return _err(str(e), 404)

    saved = storage.get_move(move_id)
    return jsonify({"ok": True, "game_id": game_id, "move_id": move_id,
                    "ply": saved["ply"], "side": saved["side"], "iccs": saved["iccs"],
                    "chinese": saved["chinese"], "fen": saved["fen_after"],
                    "side_to_move": _side_name(saved["fen_after"]),
                    "score_cp": saved["score_cp"], "score_mate": saved["score_mate"],
                    "comment": saved["comment"]})


# ----------------------------------------------------------------------
# 列表 / 详情 / 改 / 删
# ----------------------------------------------------------------------
@bp.route("/api/games/list", methods=["GET"])
def api_games_list():
    """列表: query category/tag/keyword/limit/offset; 不返回 moves 数组"""
    category = (request.args.get("category") or "").strip() or None
    tag = (request.args.get("tag") or "").strip() or None
    keyword = (request.args.get("keyword") or "").strip() or None
    if category and category not in storage.CATEGORIES:
        return _err(f"category 只能是 {'/'.join(storage.CATEGORIES)}")

    limit = request.args.get("limit", "50")
    offset = request.args.get("offset", "0")
    games = storage.list_games(category=category, tag=tag, keyword=keyword,
                               limit=limit, offset=offset)
    return jsonify({"ok": True, "games": games,
                    "total": storage.count_games(category=category, tag=tag, keyword=keyword),
                    "categories": list(storage.CATEGORIES)})


@bp.route("/api/games/<int:game_id>", methods=["GET"])
def api_games_detail(game_id):
    """详情: 元信息 + 全部着法(含中文/前后 FEN/评分/评语/分支)"""
    data = storage.get_game(game_id)
    if data is None:
        return _err(f"对局不存在: id={game_id}", 404)
    return jsonify({"ok": True, **data["game"], "moves": data["moves"]})


@bp.route("/api/games/<int:game_id>/update", methods=["POST"])
def api_games_update(game_id):
    """改元信息: name/category/tags/note/result/status/event/site/date/round 等"""
    if storage.get_game_meta(game_id) is None:
        return _err(f"对局不存在: id={game_id}", 404)
    payload = request.get_json(silent=True) or {}
    try:
        fields = _meta_fields(payload)
    except RuleError as e:
        return _err(str(e))
    if not fields:
        return _err("没有可更新的字段 (name/category/tags/note/result/status/meta...)")
    storage.update_game(game_id, fields)
    return jsonify({"ok": True, "game": storage.get_game_meta(game_id)})


@bp.route("/api/games/<int:game_id>", methods=["DELETE"])
def api_games_delete(game_id):
    """删局(级联删着法)"""
    if not storage.delete_game(game_id):
        return _err(f"对局不存在: id={game_id}", 404)
    return jsonify({"ok": True, "game_id": game_id})


@bp.route("/api/games/<int:game_id>/move/<int:move_id>/comment", methods=["POST"])
def api_games_move_comment(game_id, move_id):
    """改某一步: body {comment?, is_key?, score_cp?, score_mate?, eval_by?}

    复盘时点"AI 分析该局面"后, 前端把引擎分数用这个接口回填到该步上。
    """
    move = storage.get_move(move_id)
    if move is None or move["game_id"] != game_id:
        return _err(f"这一步不存在: move_id={move_id}", 404)

    payload = request.get_json(silent=True) or {}
    fields = {}
    if "comment" in payload:
        fields["comment"] = str(payload.get("comment") or "").strip()
    if "is_key" in payload:
        fields["is_key"] = 1 if payload.get("is_key") else 0
    for key in ("score_cp", "score_mate", "eval_by"):
        if key in payload:
            fields[key] = payload[key]
    if not fields:
        return _err("没有可更新的字段 (comment/is_key/score_cp/score_mate/eval_by)")

    storage.update_move(move_id, fields)
    return jsonify({"ok": True, "move": storage.get_move(move_id)})


# ----------------------------------------------------------------------
# 导出 / 导入
# ----------------------------------------------------------------------
@bp.route("/api/games/<int:game_id>/export", methods=["GET"])
def api_games_export(game_id):
    """导出: query fmt=fen|fenmoves|iccs|pgn|json, move_format=iccs|chinese(仅 pgn),
             download=1 时带 Content-Disposition 直接下载
    """
    fmt = (request.args.get("fmt") or "fenmoves").strip().lower()
    if fmt not in pgn_io.FORMATS:
        return _err(f"fmt 只能是 {'/'.join(pgn_io.FORMATS)}")

    data = storage.get_game(game_id)
    if data is None:
        return _err(f"对局不存在: id={game_id}", 404)
    game, moves = data["game"], data["moves"]
    start_fen = game["start_fen"] or START_FEN

    if fmt == "fen":
        text = game["current_fen"] or start_fen
    elif fmt == "fenmoves":
        text = pgn_io.export_fenmoves(start_fen, moves)
    elif fmt == "iccs":
        text = pgn_io.export_iccs(moves)
    elif fmt == "pgn":
        move_format = "chinese" if (request.args.get("move_format") or "").startswith("chin") else "iccs"
        text = pgn_io.export_pgn(game, moves, move_format=move_format)
    else:
        text = pgn_io.export_json(game, moves)

    response = Response(text, mimetype="text/plain; charset=utf-8")
    if request.args.get("download"):
        # 文件名用对局名, 去掉 Windows/Unix 不允许的字符; 用 RFC 5987 编码, 中文名也不会乱码
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", game["name"] or "game").strip() or "game"
        name = f"{safe}-{game_id}.{EXPORT_EXT[fmt]}"
        response.headers["Content-Disposition"] = \
            f"attachment; filename*=UTF-8''{quote(name)}"
    return response


@bp.route("/api/games/import", methods=["POST"])
def api_games_import():
    """导入: body {content, fmt?, name?, category?, ...}

    fmt 不给就自己认 (PGN / JSON / fenmoves / 裸 FEN / ICCS)。
    导入后直接建库: 着法逐步重放算好 FEN, 解析不了的那几步记在 warnings 里不影响整局。
    """
    payload = request.get_json(silent=True) or {}
    content = payload.get("content") or ""
    if not content.strip():
        return _err("content 不能为空")

    fmt = (payload.get("fmt") or "").strip().lower() or None
    if fmt and fmt not in pgn_io.FORMATS:
        return _err(f"fmt 只能是 {'/'.join(pgn_io.FORMATS)}")

    try:
        data = pgn_io.import_content(content, fmt=fmt)
        meta = _meta_fields(payload)
    except RuleError as e:
        return _err(str(e))

    # 内容里的标签补空缺(用户显式给的名字/分类优先)
    for key, value in (data.get("meta") or {}).items():
        if key in META_FIELDS and value and not meta.get(key):
            meta[key] = value
    meta.setdefault("category", "imported")
    if not meta.get("name"):
        meta["name"] = f"{datetime.date.today().isoformat()} 导入的{data['fmt']}棋谱"

    if data["moves"]:
        current_fen = data["moves"][-1]["fen_after"]
    else:
        current_fen = data["start_fen"]
    fields = dict(meta, start_fen=data["start_fen"], current_fen=current_fen,
                  side_to_move=_side_name(current_fen))

    game_id = storage.create_game(fields)
    try:
        for move in data["moves"]:
            storage.append_move(game_id, _move_row(move))
    except ValueError:
        storage.delete_game(game_id)
        return _err("导入失败: 数据库写入异常")

    return jsonify({"ok": True, "game_id": game_id, "fmt": data["fmt"],
                    "name": fields.get("name"), "move_count": len(data["moves"]),
                    "current_fen": current_fen, "warnings": data["warnings"]})
