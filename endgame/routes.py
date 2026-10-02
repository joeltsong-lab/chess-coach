# -*- coding: utf-8 -*-
"""残局研究接口 (Flask Blueprint)。

    GET    /api/studies/csrf                                取 CSRF 令牌(写请求都要带)
    POST   /api/studies                                     建研究: gameId+ply 续研 / fen 摆盘 / positionId
    GET    /api/studies                                     列研究
    GET    /api/studies/<study_id>                          研究详情: 树 + 分析结果 + 任务
    PUT    /api/studies/<study_id>/board                    摆盘: 重放 operation 列表并严格校验
    POST   /api/studies/<study_id>/positions                建位置节点(draft 任务, 不占引擎)
    POST   /api/studies/<study_id>/positions/<pid>/analyze  开始分析(可复用节点)
    GET    /api/studies/<study_id>/analysis/<task_id>       任务状态/进度/结果
    DELETE /api/studies/<study_id>/analysis/<task_id>       取消任务
    GET    /api/studies/<study_id>/candidates               候选着法(taskId / positionId / ply 三选一)
    PATCH  /api/studies/<study_id>/moves/<move_id>          改某步: isKey / annotation
    POST   /api/studies/<study_id>/commentary               对某个局面的分析结果做解说(LLM 冻结 prompt)
    GET    /api/studies/<study_id>/export?format=...        导出(默认 xq-endgame-study v1)
    POST   /api/studies/import                              导入 xq-endgame-study v1
    GET    /api/patterns                                    内置经典定式库(10 个实用残局)

约定:
    * 所有响应 {"ok": true/false, ...}; 失败一律带 code / message / detail 三项。
    * 写请求(POST/PUT/PATCH/DELETE)要过 CSRF; GET 不要 —— 本地单人使用, 不做账号鉴权。
    * 坐标一律输出 uci(iccs), 中文着法只作为展示字段 san; 引擎数值与棋例结论只从库里读,
      接口层不重算、不改写, 也不把 preliminary 说成定论。
    * 分析一定排在后台线程上跑(见 tasks.py), 本模块只负责校验与翻译, 不阻塞请求。

关于"续研"的落库方式:  从对局第 ply 手建研究 = 新建一局(study_id 指向原对局),
复制 moves[0..ply], 原对局一行不动; 分析结果挂在新对局上。
关于"摆盘":  研究里还没有着法时直接改这一局的起始局面; 已经有着法时另建一局
(category 不变, study_id 指向原研究), 因为改掉起始局面等于换了一盘棋。
"""

import contextlib
import json
from functools import wraps
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request

from core import storage
from core.chess_engine import START_FEN

from . import commentary, csrf, exchange, fen_rules, patterns, store, tasks

bp = Blueprint("endgame", __name__)

# 建研究时允许的分类(games.category 的 CHECK 里还有 game/opening/imported, 那两个不属于研究)
STUDY_CATEGORIES = ("endgame", "pattern", "custom", "study")
DEFAULT_STUDY_CATEGORY = "endgame"
# 分析用的规则集名(落 games.rule_set / endgame_positions.repetition_rule)
RULE_SET = store.DEFAULT_REPETITION_RULE

# 详情接口一次带多少东西
MAX_POSITIONS = 100
MAX_TASKS = 50
# analyze 请求里可以透传给引擎/规则层的字段
ANALYZE_KEYS = (("mode", "mode"), ("depth", "depth"), ("movetimeMs", "movetimeMs"),
                ("multipv", "multipv"), ("searchMoves", "searchMoves"),
                ("ruleOptions", "ruleOptions"), ("priority", "priority"))

# 由 init_app 注入
_runner: tasks.TaskRunner | None = None


class ApiError(Exception):
    """接口层业务错误 -> {code, message, detail} + HTTP 状态码"""

    def __init__(self, code, message, detail=None, status=400):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.status = status


def init_app(app, get_engine=None, runner=None):
    """由 app.py 调用: 建引擎池 + 任务队列 + 注册蓝图

    :param get_engine: app.py 的 get_engine; 快分析复用网页那一个引擎进程
    :param runner: 测试可以直接塞一个自己造的 TaskRunner
    """
    global _runner
    if runner is None:
        runner = tasks.TaskRunner(tasks.EnginePool(resident_factory=get_engine))
    _runner = runner
    runner.start()
    app.register_blueprint(bp)
    return runner


def shutdown():
    """进程退出时收掉后台线程与专用引擎(常驻引擎留给 app.py 自己 quit)"""
    if _runner is not None:
        _runner.stop()


# ----------------------------------------------------------------------
# 响应 / 参数小工具
# ----------------------------------------------------------------------
def _ok(**payload):
    payload["ok"] = True
    return jsonify(payload)


def _err(code, message, detail=None, status=400):
    body = {"ok": False, "code": code, "error": message, "message": message}
    if detail is not None:
        body["detail"] = detail
    return jsonify(body), status


def _api(view):
    """把业务异常统一翻成 {code, message, detail}"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except ApiError as e:
            return _err(e.code, str(e), e.detail, e.status)
        except tasks.TaskLimitError as e:
            limit = _runner.max_per_study if _runner else None
            return _err("task_limit", str(e), {"maxPerStudy": limit}, 429)
        except tasks.TaskError as e:
            return _err("task_rejected", str(e), None, 400)
        except ValueError as e:
            # store 的状态机报错、int() 解析失败之类: 参数/状态问题, 不算 500
            return _err("invalid_request", str(e), {"type": type(e).__name__}, 400)
    return wrapper


def _payload() -> dict:
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError("bad_json", "请求体必须是 JSON 对象")
    return data


def _int_arg(value, field, default=None, minimum=None, maximum=None):
    if value is None or value == "":
        return default
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ApiError("bad_param", f"{field} 必须是整数, 实际 {value!r}") from None
    if minimum is not None and out < minimum:
        raise ApiError("bad_param", f"{field} 不能小于 {minimum}, 实际 {out}")
    if maximum is not None and out > maximum:
        raise ApiError("bad_param", f"{field} 不能大于 {maximum}, 实际 {out}")
    return out


def _split_text(value):
    """库里存的 pv / search_moves 是空格或逗号分隔的字符串, 统一拆成 list"""
    text = (value or "").replace(",", " ")
    return [item for item in text.split() if item]


def _parse_json(value, default=None):
    if not value:
        return default
    with contextlib.suppress(ValueError, TypeError):
        return json.loads(value)
    return default


def _side_word(side):
    """fen 的走子方 -> games.side_to_move 用的 red/black"""
    return "red" if side == "w" else "black"


# ----------------------------------------------------------------------
# 出参整形 (库里的行 -> 前端字段; 这里只做改名与拆分, 不改数值)
# ----------------------------------------------------------------------
def _study_json(row) -> dict:
    return {
        "studyId": row["id"], "name": row["name"], "category": row["category"],
        "startFen": row.get("start_fen"), "currentFen": row.get("current_fen"),
        "fenText": row.get("fen_text"), "sideToMove": row.get("side_to_move"),
        "ruleSet": row.get("rule_set"), "studyOf": row.get("study_id"),
        "openingPositionId": row.get("opening_position_id"),
        "result": row.get("result"), "status": row.get("status"),
        "tags": row.get("tags"), "note": row.get("note"),
        "createdAt": row.get("created_at"), "updatedAt": row.get("updated_at"),
    }


def _move_json(row) -> dict:
    return {
        "moveId": row["id"], "ply": row["ply"], "side": row["side"],
        "uci": row["iccs"], "san": row["chinese"],
        "from": row["from_sq"], "to": row["to_sq"], "captured": row["captured"],
        "isKey": bool(row["is_key"]), "annotation": row["comment"],
        "scoreCp": row["score_cp"], "scoreMate": row["score_mate"],
        "fenBefore": row["fen_before"], "fenAfter": row["fen_after"],
    }


def _candidate_json(row) -> dict:
    return {
        "candidateId": row["id"], "positionId": row["position_id"],
        "rank": row["rank"], "multipvIndex": row["multipv_index"],
        "moveUci": row["move_uci"], "from": row["from_square"], "to": row["to_square"],
        "promotion": row["promotion"],
        "scoreType": row["score_type"], "scoreValue": row["score_value"],
        "cp": row["score_value"] if row["score_type"] == "cp" else None,
        "matePlies": row["mate_plies"],
        "pv": _split_text(row["pv_uci"]), "san": _split_text(row["pv_san"]),
        "visitedNodes": row["visited_nodes"],
        "isKey": bool(row["is_key_move"]), "isUserVariation": bool(row["is_user_variation"]),
    }


def _position_json(row) -> dict:
    return {
        "positionId": row["id"], "gameId": row["game_id"], "ply": row["ply"],
        "fen": row["fen"], "positionHash": row["position_hash"], "toMove": row["to_move"],
        "repetitionRule": row["repetition_rule"],
        "engineVersion": row["engine_version"], "nnueFile": row["nnue_file"],
        "depth": row["depth"], "seldepth": row["seldepth"], "movetimeMs": row["movetime_ms"],
        "multipv": row["multipv"], "ruleAware": bool(row["rule_aware"]),
        "bestmove": row["bestmove"], "bestPvUci": _split_text(row["best_pv_uci"]),
        "scoreType": row["score_type"], "scoreValue": row["score_value"],
        "normalizedWin": row["normalized_win"], "result": row["result"],
        "resultFor": row["result_for"], "matePlies": row["mate_plies"],
        "drawType": row["draw_type"], "ruleReviewStatus": row["rule_review_status"],
        "needsRuleReview": row["rule_review_status"] == "pending",
        "confidence": row["confidence"], "analysisDurationMs": row["analysis_duration_ms"],
        "createdAt": row["created_at"],
        "candidateCount": row.get("candidate_count"),
    }


def _task_json(row) -> dict:
    return {
        "taskId": row["id"], "studyId": row["study_id"], "gameId": row["game_id"],
        "ply": row["ply"], "fen": row["fen"], "positionHash": row["position_hash"],
        "status": row["status"], "mode": row["mode"], "depth": row["depth"],
        "movetimeMs": row["movetime_ms"], "multipv": row["multipv"],
        "searchMoves": _split_text(row["search_moves"]),
        "ruleOptions": _parse_json(row["rule_options"]),
        "repetitionRule": row["repetition_rule"], "priority": row["priority"],
        "positionId": row["position_id"], "errorCode": row["error_code"],
        "message": row["message"], "engineVersion": row["engine_version"],
        "nnueFile": row["nnue_file"], "createdAt": row["created_at"],
        "startedAt": row["started_at"], "finishedAt": row["finished_at"],
    }


def _pattern_json(item) -> dict:
    """定式条目 -> 接口出参 (tags 转数组, 前端好直接渲染)"""
    return {
        "patternId": item["id"], "name": item["name"], "theme": item["theme"],
        "fen": item["fen"], "sideToMove": item["sideToMove"],
        "result": item["result"], "resultNote": item["resultNote"],
        "goal": item["goal"], "tags": list(item["tags"]),
    }


def _engine_json() -> dict:
    """引擎/池子状态回显; 不为了回显去起进程(常驻引擎没起来时 version 就是 None)

    注意 key 不要撞: pool.stats() 里的 resident 是"有没有常驻进程"的布尔值,
    引擎的版本信息单独放在 residentInfo 下。
    """
    if _runner is None:
        return {}
    pool = _runner.pool
    info = dict(pool.stats())
    info["residentInfo"] = None
    with contextlib.suppress(Exception):
        info["residentInfo"] = pool.resident_info()
    info["workers"] = _runner.workers
    info["maxPerStudy"] = _runner.max_per_study
    return info


def _task_state(task) -> dict:
    """任务行 -> 状态响应(状态 + 进度 + 结果), 给 GET /analysis/{taskId}"""
    out = _task_json(task)
    out["progress"] = _parse_json(task["progress_json"])
    out["partialBest"] = task["partial_best"]
    out["position"] = None
    if task["position_id"]:
        row = store.get_position(task["position_id"])
        if row:
            position = _position_json(row)
            position["candidates"] = [_candidate_json(c)
                                      for c in store.list_candidates(row["id"])]
            out["position"] = position
    return out


# ----------------------------------------------------------------------
# 研究: 建 / 列 / 详情
# ----------------------------------------------------------------------
def _study_or_404(study_id):
    game = storage.get_game_meta(study_id)
    if game is None:
        raise ApiError("study_not_found", f"研究/对局不存在: id={study_id}", None, 404)
    return game


def _task_or_404(study_id, task_id):
    task = store.get_task(task_id)
    if task is None:
        raise ApiError("task_not_found", f"任务不存在: id={task_id}", None, 404)
    if int(task["game_id"]) != int(study_id):
        raise ApiError("task_not_in_study", "这个任务不属于该研究",
                       {"studyId": study_id, "taskGameId": task["game_id"]}, 409)
    return task


def _study_name(payload, source_name, ply, *, suffix=True):
    """研究名: 用户给了就用用户的; 否则按来源起名

    定式 (suffix=False) 直接用定式名 —— "单车例胜双士" 本身就是完整名字,
    再加 "· 研究" 反而看不出是哪个定式。
    """
    name = (payload.get("name") or "").strip()
    if name:
        return name
    if source_name:
        if not suffix:
            return source_name
        return f"{source_name} · 第 {ply} 手起" if ply else f"{source_name} · 研究"
    return "自定局面研究"


def _study_fields(payload, *, name, category, start_fen, fen_text, side, parent_study_id=None,
                  opening_position_id=None):
    return {
        "name": name, "category": category,
        "start_fen": start_fen, "current_fen": fen_text, "side_to_move": _side_word(side),
        "fen_text": fen_text, "rule_set": RULE_SET,
        "study_id": parent_study_id, "opening_position_id": opening_position_id,
    }


def _study_from_fen(payload, fen, *, category, name, parent_study_id=None,
                    opening_position_id=None):
    """从摆盘 / FEN 建研究: 局面就是起始局面, 没有任何着法"""
    check = fen_rules.validate_position(fen, to_move=payload.get("toMove"))
    if not check["legal"]:
        raise ApiError("invalid_position", "局面不合法, 不能作为研究起点",
                       {"errors": check["errors"], "warnings": check["warnings"]})
    study_id = storage.create_game(_study_fields(
        payload, name=name, category=category, start_fen=check["fen"],
        fen_text=check["fen"], side=check["side_to_move"],
        parent_study_id=parent_study_id, opening_position_id=opening_position_id))
    return study_id, check


def _study_from_game(payload, game, ply, *, category, name, opening_position_id=None):
    """续研: 复制原对局的 moves[0..ply], 原对局一行不动"""
    source = game["game"]
    copied = [m for m in game["moves"] if int(m.get("ply") or 0) <= int(ply)]
    base = source.get("start_fen") or START_FEN
    target = base if int(ply) <= 0 else (copied[-1].get("fen_after") if copied else None)
    if not target:
        raise ApiError("position_not_found", f"第 {ply} 手之后的局面不存在", {"ply": ply}, 409)
    check = fen_rules.validate_position(target)
    if not check["legal"]:
        raise ApiError("invalid_position", "对局里的这个局面不合法, 不能拿来研究",
                       {"errors": check["errors"], "warnings": check["warnings"]})
    study_id = storage.create_game(_study_fields(
        payload, name=name, category=category, start_fen=base, fen_text=check["fen"],
        side=check["side_to_move"], parent_study_id=source["id"],
        opening_position_id=opening_position_id))
    for move in copied:
        storage.append_move(study_id, {
            "ply": move["ply"], "side": move["side"], "iccs": move["iccs"],
            "chinese": move["chinese"], "from_sq": move["from_sq"], "to_sq": move["to_sq"],
            "captured": move["captured"], "fen_before": move["fen_before"],
            "fen_after": move["fen_after"], "comment": move["comment"],
            "is_key": move["is_key"],
        })
    return study_id, check


@bp.route("/api/studies/csrf", methods=["GET"])
def api_csrf_token():
    """前端启动时取一次, 之后每个写请求放进 X-CSRF-Token"""
    return _ok(token=csrf.current_token(), header=csrf.TOKEN_HEADER,
               bodyField=csrf.BODY_FIELD)


def _pattern_from(payload) -> dict | None:
    """请求里的 patternId -> 定式条目(Object 404), 没给就返回 None

    定式是"点一下就开始研究"的入口: 定式库里存的是局面 + 练什么, 不带任何着法,
    所以走的就是普通的"从 FEN 建研究"那条路。
    """
    pattern_id = str(payload.get("patternId") or "").strip()
    if not pattern_id:
        return None
    item = patterns.get_pattern(pattern_id)
    if item is None:
        raise ApiError("pattern_not_found", f"定式库里没有这个定式: {pattern_id!r}",
                       {"patternId": pattern_id, "available": list(patterns.pattern_ids())}, 404)
    return item


@bp.route("/api/studies", methods=["POST"])
@csrf.protect
@_api
def api_create_study():
    payload = _payload()
    pattern = _pattern_from(payload)
    warnings = []
    if pattern:
        # 定式优先: 局面/轮次都按定式库里的来, 同时给了别的来源就提示一下按哪个处理
        if payload.get("gameId") or payload.get("positionId") or (payload.get("fen") or "").strip():
            warnings.append("同时给了 patternId 和 gameId/fen/positionId, 按定式处理, 其余已忽略")
        payload = {k: v for k, v in payload.items()
                   if k not in ("gameId", "positionId", "ply", "fen")}
        payload["fen"] = pattern["fen"]
        payload.setdefault("category", "pattern")

    category = str(payload.get("category") or "").strip() or DEFAULT_STUDY_CATEGORY
    if category not in STUDY_CATEGORIES:
        raise ApiError("bad_category",
                       f"category 只能是 {'/'.join(STUDY_CATEGORIES)}, 实际 {category!r}")

    position_id = _int_arg(payload.get("positionId"), "positionId")
    game_id = _int_arg(payload.get("gameId"), "gameId")
    ply = _int_arg(payload.get("ply"), "ply", minimum=0)
    fen = (payload.get("fen") or "").strip()

    if position_id:
        source = store.get_position(position_id)
        if source is None:
            raise ApiError("position_not_found", f"分析结果不存在: id={position_id}", None, 404)
        game_id = game_id or source["game_id"]
        ply = source["ply"] if ply is None else ply
        fen = fen or source["fen"]

    if game_id:
        if fen and not pattern:
            warnings.append("同时给了 gameId 和 fen, 按“从对局续研”处理, fen 已忽略")
        game = storage.get_game(game_id)
        if game is None:
            raise ApiError("game_not_found", f"对局不存在: id={game_id}", None, 404)
        source_ply = 0 if ply is None else ply
        played = len([m for m in game["moves"] if (m.get("iccs") or "").strip()])
        if source_ply > played:
            raise ApiError("bad_param", f"ply 超出对局范围: {source_ply} > {played}")
        study_id, check = _study_from_game(
            payload, game, source_ply, category=category,
            name=_study_name(payload, game["game"].get("name"), source_ply),
            opening_position_id=position_id)
        action = "branch"
        source_ply_out = source_ply
    elif fen:
        if ply:
            warnings.append("只给 fen 时 ply 无意义, 已忽略")
        study_id, check = _study_from_fen(
            payload, fen, category=category,
            name=_study_name(payload, pattern["name"] if pattern else None, 0,
                             suffix=pattern is None),
            opening_position_id=position_id)
        action = "position"
        source_ply_out = 0
    else:
        raise ApiError("missing_source",
                       "必须给 gameId / fen / positionId / patternId 之一")

    return _ok(action=action, warnings=warnings, ruleSet=RULE_SET,
               study=_study_json(storage.get_game_meta(study_id)),
               fen=check["fen"], sideToMove=check["side_to_move"],
               pieceCount=check["piece_count"], positionHash=check["position_hash"],
               sourceGameId=game_id, sourcePly=source_ply_out,
               openingPositionId=position_id,
               pattern=(_pattern_json(pattern) if pattern else None))


@bp.route("/api/patterns", methods=["GET"])
@_api
def api_patterns():
    """内置经典定式库

    只给局面和"练什么", **不给着法序列** —— 没跑过引擎的"正解"就是编造,
    规格禁止; 定式的着法请用分析接口现场算。
    """
    keyword = (request.args.get("keyword") or "").strip()
    theme = (request.args.get("theme") or "").strip()
    items = patterns.list_patterns()
    if keyword:
        items = [p for p in items
                 if keyword in p["name"] or keyword in p["goal"] or keyword in p["id"]]
    if theme:
        items = [p for p in items if p["theme"] == theme]
    return _ok(patterns=[_pattern_json(p) for p in items], total=len(items),
               version=patterns.PATTERN_VERSION,
               themes=sorted({p["theme"] for p in patterns.PATTERNS}),
               note="定式只提供局面与练习目标, 不含着法序列(避免写死未经验证的正解)")


@bp.route("/api/studies", methods=["GET"])
@_api
def api_list_studies():
    category = request.args.get("category") or None
    keyword = request.args.get("keyword") or None
    limit = _int_arg(request.args.get("limit"), "limit", default=50, minimum=1, maximum=200)
    offset = _int_arg(request.args.get("offset"), "offset", default=0, minimum=0)
    rows = storage.list_games(category=category, keyword=keyword, limit=limit, offset=offset)
    studies = []
    for row in rows:
        item = _study_json(row)
        item["moveCount"] = row.get("move_count")
        item["lastSan"] = row.get("last_chinese")
        item["analysisCount"] = store.count_positions(row["id"])
        studies.append(item)
    return _ok(studies=studies, total=storage.count_games(category=category, keyword=keyword),
               limit=limit, offset=offset, categories=list(STUDY_CATEGORIES))


@bp.route("/api/studies/<int:study_id>", methods=["GET"])
@_api
def api_study_detail(study_id):
    """研究详情 = 复盘时间轴(moves) + 各局面的分析结果(positions) + 任务(tasks)"""
    game = _study_or_404(study_id)
    payload = storage.get_game(study_id)
    positions = []
    for row in store.list_positions(study_id, limit=MAX_POSITIONS):
        item = _position_json(row)
        item["candidates"] = [_candidate_json(c) for c in store.list_candidates(row["id"])]
        positions.append(item)
    return _ok(
        study=_study_json(game),
        moves=[_move_json(m) for m in payload["moves"]],
        positions=positions,
        tasks=[_task_json(t) for t in store.list_tasks(study_id=study_id, limit=MAX_TASKS)],
        activeTasks=store.count_active_tasks(study_id=study_id),
        ruleSet=RULE_SET,
        reviewStatuses=["pending", "human_verified", "override"],
        engine=_engine_json())


# ----------------------------------------------------------------------
# 摆盘
# ----------------------------------------------------------------------
@bp.route("/api/studies/<int:study_id>/board", methods=["PUT"])
@csrf.protect
@_api
def api_edit_board(study_id):
    """摆盘: 重放 operations 或整盘替换 fen, 严格校验后才落库

    body: {operations?, fen?, toMove?, clear?, validateOnly?, previousHash?}
      validateOnly=true 时只回校验结果(永远 200), 前端"预检"按钮用。
    """
    game = _study_or_404(study_id)
    payload = _payload()
    validate_only = bool(payload.get("validateOnly"))
    fen_in = (payload.get("fen") or "").strip()
    operations = payload.get("operations")
    clear = bool(payload.get("clear"))
    to_move = payload.get("toMove")

    base = game.get("fen_text") or game.get("current_fen") or game.get("start_fen") or START_FEN

    if validate_only:
        # 预检: 操作本身的问题(格子名写错之类)也当校验结果回, 不抛 400
        if fen_in and not operations and not clear:
            result = {"fen": fen_in, "side_to_move": None, "applied": 0,
                      "errors": [], "warnings": []}
        else:
            result = fen_rules.apply_operations(fen_in or base, operations, to_move=to_move,
                                                clear=clear)
        if result["errors"]:
            return _ok(validateOnly=True, legal=False, errors=result["errors"],
                       warnings=result.get("warnings", []), fen=result["fen"],
                       baseFen=base, applied=result["applied"])
        check = fen_rules.validate_position(result["fen"], to_move=to_move)
        return _ok(validateOnly=True, legal=check["legal"], errors=check["errors"],
                   warnings=check["warnings"] + result.get("warnings", []),
                   fen=check["fen"], baseFen=base, applied=result["applied"],
                   sideToMove=check["side_to_move"], pieceCount=check["piece_count"],
                   positionHash=check["position_hash"], inCheck=check["in_check"],
                   hasLegalMove=check["has_legal_move"])

    # 真改盘: 先确认客户端看到的还是同一盘棋(没有别人在中间改过)
    if not fen_in and payload.get("previousHash"):
        current_hash = fen_rules.position_hash(base)
        if payload["previousHash"] != current_hash:
            raise ApiError("stale_board", "棋盘已经被改过了(previousHash 对不上), 请刷新重试",
                           {"expected": current_hash, "given": payload["previousHash"]}, 409)

    if fen_in and not operations and not clear:
        target = fen_in
        applied, op_warnings = 0, []
    else:
        result = fen_rules.apply_operations(fen_in or base, operations, to_move=to_move,
                                            clear=clear)
        if result["errors"]:
            raise ApiError("bad_operation", "摆盘操作有问题, 没有执行",
                           {"errors": result["errors"], "warnings": result.get("warnings", [])})
        target, applied, op_warnings = result["fen"], result["applied"], result.get("warnings", [])

    check = fen_rules.validate_position(target, to_move=to_move)
    if not check["legal"]:
        raise ApiError("invalid_position", "这个局面不合法, 没有保存",
                       {"errors": check["errors"], "warnings": check["warnings"]})

    moves_count = storage.count_moves(study_id)
    if moves_count == 0:
        storage.update_game(study_id, {
            "start_fen": check["fen"], "current_fen": check["fen"], "fen_text": check["fen"],
            "side_to_move": _side_word(check["side_to_move"]),
        })
        return _ok(created=False, updated=True, applied=applied,
                   warnings=op_warnings + check["warnings"],
                   study=_study_json(storage.get_game_meta(study_id)),
                   fen=check["fen"], sideToMove=check["side_to_move"],
                   pieceCount=check["piece_count"], positionHash=check["position_hash"],
                   inCheck=check["in_check"], hasLegalMove=check["has_legal_move"])

    # 已经有着法: 改起始局面等于换一盘棋, 所以另建一局, 原研究一行不动
    new_id, check = _study_from_fen(
        payload, check["fen"], category=game["category"],
        name=f"{game['name'] or '研究'} · 摆盘变例",
        parent_study_id=game.get("study_id") or game["id"],
        opening_position_id=game.get("opening_position_id"))
    return _ok(created=True, updated=False, applied=applied, sourceStudyId=study_id,
               warnings=op_warnings + ["原研究已有着法, 摆盘结果另存为新研究"],
               study=_study_json(storage.get_game_meta(new_id)),
               fen=check["fen"], sideToMove=check["side_to_move"],
               pieceCount=check["piece_count"], positionHash=check["position_hash"])


# ----------------------------------------------------------------------
# 位置节点 / 分析
# ----------------------------------------------------------------------
def _analyze_overrides(payload) -> dict:
    engine = payload.get("engine")
    if engine not in (None, "", "default", "auto"):
        raise ApiError("engine_not_implemented",
                       "暂时只支持默认引擎(engine 参数留给以后接入别的 NNUE)",
                       {"engine": engine}, 501)
    body = {}
    for key, target in ANALYZE_KEYS:
        if payload.get(key) is not None:
            body[target] = payload[key]
    return body


def _position_request(study_id, payload) -> dict:
    """{fromPly, fen, mode, depth, movetimeMs, multipv, searchMoves, ruleOptions, priority}"""
    body = {"gameId": study_id}
    ply = payload.get("fromPly", payload.get("ply"))
    if ply is not None:
        body["ply"] = ply
    fen = (payload.get("fen") or "").strip()
    if fen:
        body["fen"] = fen
    body.update(_analyze_overrides(payload))
    return body


def _comment_move(study_id, ply):
    """备注落在"第 ply 着"这一步上 (moves.comment 就是现有的注释列)"""
    for move in storage.list_moves(study_id):
        if int(move.get("ply") or 0) == int(ply):
            return move
    return None


@bp.route("/api/studies/<int:study_id>/positions", methods=["POST"])
@csrf.protect
@_api
def api_create_position(study_id):
    """在研究树里登记一个位置节点(draft 任务): 只校验, 不占引擎

    body: {fromPly, comment?, mode?, depth?, movetimeMs?, multipv?, searchMoves?, ruleOptions?}
    """
    _study_or_404(study_id)
    payload = _payload()
    body = _position_request(study_id, payload)
    ply = body.get("ply") or 0

    comment = (payload.get("comment") or "").strip()
    target = None
    if comment:
        target = _comment_move(study_id, ply)
        if target is None:
            raise ApiError("comment_needs_move",
                           f"第 {ply} 着没有对应的着法行, 备注只能写在具体某一步上"
                           "(研究树里先走出这一步, 再用 PATCH /moves/<moveId>)",
                           {"ply": ply})

    node = _runner.prepare(body)
    if target is not None:
        storage.update_move(target["id"], {"comment": comment})

    return _ok(positionId=node["id"], taskId=node["id"], studyId=study_id,
               status=node["status"], ply=node["ply"], fen=node["fen"],
               positionHash=node["position_hash"], mode=node["mode"], depth=node["depth"],
               movetimeMs=node["movetime_ms"], multipv=node["multipv"],
               searchMoves=_split_text(node["search_moves"]),
               ruleOptions=_parse_json(node["rule_options"]),
               repetitionRule=node["repetition_rule"], ruleSet=RULE_SET,
               comment=comment, commentMoveId=(target["id"] if target else None),
               engine=_engine_json())


@bp.route("/api/studies/<int:study_id>/positions/<string:position_id>/analyze", methods=["POST"])
@csrf.protect
@_api
def api_analyze_position(study_id, position_id):
    """开始分析一个位置节点

    * 节点还是 draft -> 复用它(参数按本次请求更新);
    * 节点已经算过 -> 另建一个任务(不同 depth/MultiPV/规则的结果互不覆盖), 返回新 taskId。

    本次请求没提到的参数沿用节点登记时的那套(不是重新取默认值): 建节点时选了
    quick, 点"分析"就该按 quick 算, 不能因为请求体是空的就偷偷换成 standard 长考。
    """
    _study_or_404(study_id)
    node = _task_or_404(study_id, position_id)
    payload = _payload()
    body = {"gameId": study_id, "ply": node["ply"], "fen": node["fen"]}
    body.update({
        "mode": node["mode"], "depth": node["depth"],
        "movetimeMs": node["movetime_ms"], "multipv": node["multipv"],
        "searchMoves": _split_text(node["search_moves"]) or None,
        "ruleOptions": _parse_json(node["rule_options"]),
        "priority": node["priority"],
    })
    body.update(_analyze_overrides(payload))

    reused = node["status"] == "draft"
    task = _runner.submit(body, task_id=node["id"] if reused else None)
    return _ok(reused=reused, taskId=task["id"], positionId=node["id"],
               studyId=study_id, status=task["status"], task=_task_json(task),
               ruleSet=RULE_SET, engine=_engine_json())


@bp.route("/api/studies/<int:study_id>/analysis/<string:task_id>", methods=["GET"])
@_api
def api_task_status(study_id, task_id):
    _study_or_404(study_id)
    task = _task_or_404(study_id, task_id)
    return _ok(**_task_state(task), engine=_engine_json())


@bp.route("/api/studies/<int:study_id>/analysis/<string:task_id>", methods=["DELETE"])
@csrf.protect
@_api
def api_cancel_task(study_id, task_id):
    """取消: 排队中的立刻转 cancelled; 正在跑的只是置取消位, 引擎返回后才转"""
    _study_or_404(study_id)
    _task_or_404(study_id, task_id)
    task = _runner.cancel(task_id)
    requested = task["status"] not in store.TASK_FINAL_STATUSES
    return _ok(taskId=task["id"], status=task["status"], cancelled=not requested,
               cancelRequested=requested, message=task["message"])


def _resolve_position(study_id, *, task_id=None, position_id=None, ply=None):
    """定位一条分析结果: taskId / positionId / ply 三选一

    三个来源都指向 endgame_positions 的同一行; 找不到就按情况回 404/400。
    :return: (position 行, task 行或 None, partialBest)
    """
    partial_best, task = None, None
    position_id = _int_arg(position_id, "positionId")
    ply = _int_arg(ply, "ply", minimum=0)
    if task_id:
        task = _task_or_404(study_id, task_id)
        position_id = task["position_id"]
        partial_best = task["partial_best"]
    if position_id is None and ply is not None:
        row = store.latest_position(study_id, ply=ply)
        position_id = row["id"] if row else None
    if position_id is None:
        if task_id:
            raise ApiError("position_not_found", "这个任务还没有分析结果(没算完或已失败)",
                           {"taskId": task_id}, 404)
        if ply is not None:
            raise ApiError("position_not_found", f"第 {ply} 手还没有分析结果", {"ply": ply}, 404)
        raise ApiError("missing_target", "得给 taskId / positionId / ply 之一")

    row = store.get_position(position_id)
    if row is None or int(row["game_id"]) != int(study_id):
        raise ApiError("position_not_found", f"分析结果不存在: id={position_id}", None, 404)
    return row, task, partial_best


@bp.route("/api/studies/<int:study_id>/candidates", methods=["GET"])
@_api
def api_candidates(study_id):
    """候选着法: ?taskId= / ?positionId= / ?ply= 三选一

    rank/moveUci/san/scoreType/cp/matePlies/pv/isKey 都在候选数组里;
    还没算完时另有 partialBest(=当前最佳着法, 未定论)。
    """
    _study_or_404(study_id)
    row, task, partial_best = _resolve_position(
        study_id, task_id=request.args.get("taskId"),
        position_id=request.args.get("positionId"), ply=request.args.get("ply"))
    position = _position_json(row)
    return _ok(position=position,
               candidates=[_candidate_json(c) for c in store.list_candidates(row["id"])],
               partialBest=partial_best,
               task=(_task_json(task) if task else None),
               ruleSet=RULE_SET, reviewStatuses=list(store.REVIEW_STATUSES))


@bp.route("/api/studies/<int:study_id>/commentary", methods=["POST"])
@csrf.protect
@_api
def api_commentary(study_id):
    """对一个已算完的局面做解说 (LLM 冻结 prompt, 只转述引擎数值)

    body: {taskId?|positionId?|ply?, useLlm?}
      * useLlm=false 或没配 LLM -> 出模板解说, llmUsed=false, 不影响接口成功;
      * 解说不落库(库里只存引擎算出来的事实), 想留就把它写进某步的备注(annotation)。

    规格里"解说不得改写引擎数值"这条, 在这里落成两件事:
      1. 送进 prompt 的只有 engineFacts(全部取自库里);
      2. 返回的文字里若出现引擎数据之外的 ICCS 坐标, 整条作废并降级。
    """
    _study_or_404(study_id)
    payload = _payload()
    row, task, _partial = _resolve_position(
        study_id, task_id=payload.get("taskId"), position_id=payload.get("positionId"),
        ply=payload.get("ply"))
    use_llm = payload.get("useLlm")
    use_llm = True if use_llm is None else bool(use_llm)

    result = commentary.explain(row, store.list_candidates(row["id"]), use_llm=use_llm)
    return _ok(positionId=row["id"], taskId=(task["id"] if task else None),
               ply=row["ply"],
               promptVersion=result["promptVersion"],
               promptFingerprint=result["promptFingerprint"],
               llmUsed=result["llmUsed"], text=result["text"],
               fallbackReason=result["fallbackReason"],
               inventedMoves=result["inventedMoves"],
               disclaimer=result["disclaimer"],
               engineFacts=result["engineFacts"],
               ruleSet=RULE_SET, engine=_engine_json())


# ----------------------------------------------------------------------
# 导入导出 (xq-endgame-study v1)
# ----------------------------------------------------------------------
@bp.route("/api/studies/<int:study_id>/export", methods=["GET"])
@_api
def api_export_study(study_id):
    """导出研究: ?format=xq-endgame-study&download=1

    与整局棋的 /api/games/<id>/export 分开: 那个格式只有"着法", 装不下分析结果,
    改它等于改既有格式, 所以研究另走这一条。
    """
    fmt = (request.args.get("format") or exchange.FORMAT).strip().lower()
    if fmt not in (exchange.FORMAT, exchange.FORMAT.replace("-", "_")):
        raise ApiError("bad_format",
                       f"format 只能是 {exchange.FORMAT}(导出的研究文档)",
                       {"format": fmt}, 400)
    game = _study_or_404(study_id)
    text = exchange.export_text(study_id)
    response = Response(text, mimetype="application/json; charset=utf-8")
    if request.args.get("download"):
        name = "".join(ch for ch in (game.get("name") or "study")
                       if ch not in '\\/:*?"<>|\r\n').strip() or "study"
        response.headers["Content-Disposition"] = \
            f"attachment; filename*=UTF-8''{quote(f'{name}-{study_id}.json')}"
    return response


@bp.route("/api/studies/import", methods=["POST"])
@csrf.protect
@_api
def api_import_study():
    """导入研究: body {content | document, name?, category?}

    content 是 xq-endgame-study 文档(JSON 文本或对象); 着法与引擎着法都会重新过
    校验, 走不通就整份拒绝(一个字节都不写)。导入出来的是**新研究**, 不动原研究。
    """
    payload = _payload()
    raw = payload.get("content")
    if raw is None:
        raw = payload.get("document")
    if raw is None:
        raise ApiError("missing_content", "得给 content(文档 JSON 文本或对象)")
    try:
        result = exchange.import_document(raw, name=payload.get("name"),
                                          category=payload.get("category"))
    except exchange.ExchangeError as e:
        raise ApiError("bad_document", str(e), {"format": exchange.FORMAT,
                                                "version": exchange.VERSION}) from None
    return _ok(**result, ruleSet=RULE_SET,
               study=_study_json(storage.get_game_meta(result["studyId"])))


# ----------------------------------------------------------------------
# 某一步: 关键步 / 注释
# ----------------------------------------------------------------------
@bp.route("/api/studies/<int:study_id>/moves/<int:move_id>", methods=["PATCH"])
@csrf.protect
@_api
def api_patch_move(study_id, move_id):
    """关键步 / 注释走现有的 moves.is_key / moves.comment, 研究树只引用 moveId"""
    _study_or_404(study_id)
    move = storage.get_move(move_id)
    if move is None or int(move["game_id"]) != int(study_id):
        raise ApiError("move_not_found", f"这一步不存在: id={move_id}", None, 404)

    payload = _payload()
    if "tags" in payload:
        raise ApiError("tags_unsupported",
                       "moves 表没有 tags 列(现有语义不改), 请用 annotation; "
                       "定式/开局标签请记在研究级别(games.tags)",
                       {"field": "tags"}, 400)
    fields = {}
    if "isKey" in payload:
        fields["is_key"] = 1 if payload["isKey"] else 0
    if "annotation" in payload:
        fields["comment"] = payload["annotation"]
    if not fields:
        raise ApiError("nothing_to_update", "得给 isKey 或 annotation")

    storage.update_move(move_id, fields)
    return _ok(move=_move_json(storage.get_move(move_id)))
