# -*- coding: utf-8 -*-
"""手机 Web 远程访问版接口 (Flask Blueprint)。

电脑上跑 Flask, 手机同 Wi-Fi 打开 http://<电脑IP>:5000/mobile 就能用:
响应式触摸棋盘 + AI 实时建议 + 外部棋局同步 + 教练解说 + 保存研究局。

    GET  /mobile                     手机版单页
    GET  /api/mobile/status          引擎/LLM 就绪状态 + 默认参数
    POST /api/mobile/sync            导入 FEN / ICCS / PGN / fenmoves / json -> 局面 + 着法
    POST /api/mobile/move            在给定局面上走一步 -> 新局面 + 合法着法表
    POST /api/mobile/hint            AI 实时建议 (引擎 MultiPV + 可选 LLM 二次筛选)
    POST /api/mobile/coach           教练解说: 给"刚走的一步"定档 + 讲原因
    POST /api/mobile/save            存为研究局 (core.storage)

约定:
    - 所有接口都返回 JSON, 形如 {"ok": true/false, ...};
      参数/规则错误 400, 引擎缺失或崩溃 503 (前端统一按 ok 判断)。
    - 引擎由 app.py 通过 init_app(app, get_engine, analyze_lock, engine_config) 注入,
      本模块不 import app, 既无循环依赖, 也不会起第二个引擎进程。
    - 设了环境变量 MOBILE_TOKEN 就开启 Token 保护: /api/mobile/* 需带
      X-Mobile-Token 请求头或 ?token= 查询参数 (两者都认)。
    - 引擎只支持"按时间搜索"(movetime), 没有 depth 档, 所以"深度"预设是更长的思考时间。
"""

import hmac
import os
import threading

from flask import Blueprint, jsonify, render_template, request

from core import pgn_io, rules, storage
from core.chess_engine import (
    DEFAULT_MOVETIME,
    ENGINE_MULTIPV,
    ENGINE_PATH,
    START_FEN,
    EngineError,
    download_hint,
)
from core.coord_utils import (
    FILES,
    GRID_ORIENTATION,
    iccs_to_chinese,
    iccs_to_grid,
    pv_to_chinese,
)
from core.explainer import coach, llm_configured, pick_best_move
from core.rules import RuleError

bp = Blueprint("mobile", __name__)

# 环境变量打开 Token 保护（空 = 不校验）
MOBILE_TOKEN = os.environ.get("MOBILE_TOKEN", "").strip()

# 思考时长预设: 快速 2s / 标准 5s / 深度 10s
# （chess_engine 只暴露按时间搜索, 所以"深度"用更长的时间近似"算得更深"）
HINT_PRESETS = {
    "fast": {"movetime": 2000, "label": "快速"},
    "standard": {"movetime": 5000, "label": "标准"},
    "deep": {"movetime": 10000, "label": "深度"},
}
DEFAULT_HINT_PRESET = "standard"

MIN_MOVETIME = 100
MAX_MOVETIME = 60000

# 教练定档: 评分差(厘兵) <= 阈值就是该档, 都超了算失误
COACH_THRESHOLDS = (("最佳", 30), ("可更好", 100), ("疑手", 250))

# 由 init_app 注入
_get_engine = None
_analyze_lock = None
_engine_config: dict = {}


def init_app(app, get_engine=None, analyze_lock=None, engine_config=None):
    """由 app.py 调用: 注入引擎访问方式 + 运行期引擎参数 + 注册蓝图"""
    global _get_engine, _analyze_lock, _engine_config
    _get_engine = get_engine
    _analyze_lock = analyze_lock or threading.Lock()
    _engine_config = engine_config if isinstance(engine_config, dict) else {}
    app.register_blueprint(bp)


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _err(message: str, code: int = 400):
    """统一的失败响应"""
    return jsonify({"ok": False, "error": message}), code


def _json_body() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _sq(row: int, col: int) -> str:
    """棋盘行列 -> ICCS 坐标名, 如 h2"""
    return f"{FILES[col]}{9 - row}"


def _side_code(fen: str) -> str:
    """FEN 的走子方 -> 'w' / 'b'"""
    parts = (fen or "").split()
    return "b" if len(parts) > 1 and parts[1].lower().startswith("b") else "w"


def _side_name(fen: str) -> str:
    """FEN 的走子方 -> red / black (写库用)"""
    return "black" if _side_code(fen) == "b" else "red"


def _clean_fen(fen, field: str) -> str:
    """结构校验 + 规范化; 不合法抛 RuleError(中文)"""
    text = (fen or "").strip()
    if not text:
        raise RuleError(f"{field} 不能为空")
    try:
        return rules.validate_fen(text)
    except RuleError as e:
        raise RuleError(f"{field} 不合法: {e}") from e


def _default_movetime() -> int:
    """运行期配置里的兜底思考时间"""
    try:
        return int(_engine_config.get("movetime", DEFAULT_MOVETIME))
    except (TypeError, ValueError):
        return DEFAULT_MOVETIME


def _resolve_movetime(payload: dict, default: int):
    """请求 -> (movetime, 模式)。preset 优先, 其次 movetime, 都没有就用默认"""
    preset = str(payload.get("preset") or "").strip().lower()
    if preset in HINT_PRESETS:
        return HINT_PRESETS[preset]["movetime"], preset
    raw = payload.get("movetime")
    if raw in (None, ""):
        return default, DEFAULT_HINT_PRESET
    try:
        movetime = int(raw)
    except (TypeError, ValueError):
        movetime = default
    return max(MIN_MOVETIME, min(movetime, MAX_MOVETIME)), "manual"


def _analyze(fen: str, movetime: int, multipv: int) -> dict:
    """串行调用注入的引擎实例"""
    if _get_engine is None:
        raise EngineError("引擎未初始化 (服务端未注入 get_engine)")
    engine = _get_engine()
    lock = _analyze_lock or threading.Lock()
    with lock:
        return engine.analyze(fen, movetime=movetime, multipv=multipv)


def _board_state(fen: str, last_move=None) -> dict:
    """把局面整理成手机端直接能画的形态

    legal 是 {起点坐标: [终点坐标, ...]} (都是 ICCS 名), 前端据此做"点棋子高亮落点"。
    手机端只做坐标换算, 不做棋规, 合法性以后端 core.rules 为唯一来源。
    """
    grid, side = rules.fen_to_board(fen)
    legal: dict = {}
    for iccs in rules.legal_moves(grid, side=side):
        coord = iccs_to_grid(iccs)
        legal.setdefault(_sq(*coord["from"]), []).append(_sq(*coord["to"]))
    return {
        "fen": fen,
        "side_code": side,
        "side_to_move": "red" if side == "w" else "black",
        "in_check": rules.in_check(grid, side),
        "game_over": not legal,
        "legal": legal,
        "last_move": last_move,
    }


def _candidates(moves: list, fen: str, limit: int) -> list:
    """引擎 MultiPV 结果 -> 前端能直接用的形态(中文着法 + 棋盘行列 + 中文变例)"""
    out = []
    for mv in moves[:max(1, limit)]:
        iccs = mv.get("bestmove")
        if not iccs or iccs in ("(none)", "0000"):
            continue
        try:
            coord = iccs_to_grid(iccs)
        except ValueError:
            continue
        pv = mv.get("pv") or []
        out.append({
            "rank": mv.get("rank"),
            "iccs": iccs,
            "chinese": iccs_to_chinese(iccs, fen),
            "from": coord["from"],
            "to": coord["to"],
            "score": mv.get("score"),
            "mate": mv.get("mate"),
            "depth": mv.get("depth"),
            "pv_iccs": pv,
            "pv_chinese": pv_to_chinese(pv, fen),
        })
    return out


def _move_row(move: dict) -> dict:
    """入库前补上"下一步该谁走" (storage.append_move 用 next_side)"""
    return dict(move, next_side=_side_name(move.get("fen_after")))


def _classify(gap_cp, best_mate, played_mate) -> str:
    """按评分差 + 杀棋情况定档: 最佳 / 可更好 / 疑手 / 失误"""
    if best_mate is not None and best_mate > 0:
        return "最佳" if (played_mate is not None and played_mate > 0) else "失误"
    if gap_cp is None:
        return "可更好"
    for category, threshold in COACH_THRESHOLDS:
        if gap_cp <= threshold:
            return category
    return "失误"


# ----------------------------------------------------------------------
# Token 保护（只护 /api/mobile/*）
# ----------------------------------------------------------------------
def _token_ok() -> bool:
    if not MOBILE_TOKEN:
        return True
    supplied = request.headers.get("X-Mobile-Token") or request.args.get("token") or ""
    if not supplied:
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            supplied = str(body.get("token") or "")
    return hmac.compare_digest(str(supplied), MOBILE_TOKEN)


@bp.before_request
def _guard_api():
    if request.path.startswith("/api/mobile/") and not _token_ok():
        return jsonify({"ok": False, "error": "Token 校验失败（请在设置里填写访问 Token）",
                        "token_required": True}), 401


# ----------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------
@bp.route("/mobile")
def mobile_page():
    """手机版单页: 触摸棋盘 + AI 建议 + 同步 + 教练"""
    return render_template("mobile/coach.html",
                           start_fen=START_FEN,
                           token_required=bool(MOBILE_TOKEN),
                           engine_config=dict(_engine_config))


# ----------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------
@bp.route("/api/mobile/status", methods=["GET"])
def api_status():
    """引擎 / LLM 就绪状态 + 默认参数, 手机端加载时问一次"""
    engine_exists = os.path.isfile(ENGINE_PATH)
    return jsonify({
        "ok": True,
        "engine_path": ENGINE_PATH,
        "engine_exists": engine_exists,
        "engine_error": None if engine_exists else download_hint(ENGINE_PATH),
        "llm_ready": llm_configured(),
        "token_required": bool(MOBILE_TOKEN),
        "orientation": GRID_ORIENTATION,
        "start_fen": START_FEN,
        "presets": {name: cfg["movetime"] for name, cfg in HINT_PRESETS.items()},
        "default_preset": DEFAULT_HINT_PRESET,
        "engine_config": dict(_engine_config),
    })


# ----------------------------------------------------------------------
# 同步外部棋局
# ----------------------------------------------------------------------
@bp.route("/api/mobile/sync", methods=["POST"])
def api_sync():
    """粘贴 FEN / ICCS / PGN / fenmoves / json -> 解析 + 重放 -> 回到可分析的桌面

    body: {text, format?}
      format 不给就自动识别; 解析出的着法都会带中文与坐标, 前端可逐步回放。
    """
    payload = _json_body()
    text = str(payload.get("text") or "").strip()
    if not text:
        return _err("text 不能为空")
    fmt = str(payload.get("format") or "").strip().lower() or None

    try:
        imported = pgn_io.import_content(text, fmt)
    except RuleError as e:
        return _err(str(e))
    except Exception as e:                       # 解析器兜底, 别把 HTML 错误页丢给前端
        return _err(f"解析失败: {type(e).__name__}: {e}")

    try:
        start_fen = _clean_fen(imported.get("start_fen") or START_FEN, "起始局面")
        final_fen = _clean_fen(imported.get("fen") or start_fen, "当前局面")
    except RuleError as e:
        return _err(str(e))

    moves = imported.get("moves") or []
    last_move = None
    if moves:
        last = moves[-1]
        last_move = {"from_sq": last.get("from_sq"), "to_sq": last.get("to_sq"),
                     "chinese": last.get("chinese"), "iccs": last.get("iccs")}

    return jsonify({
        "ok": True,
        "fmt": imported.get("fmt"),
        "start_fen": start_fen,
        "moves": [{"ply": m.get("ply"), "iccs": m.get("iccs"), "chinese": m.get("chinese"),
                   "from_sq": m.get("from_sq"), "to_sq": m.get("to_sq")} for m in moves],
        "warnings": imported.get("warnings") or [],
        "meta": imported.get("meta") or {},
        **_board_state(final_fen, last_move),
    })


# ----------------------------------------------------------------------
# 走子
# ----------------------------------------------------------------------
@bp.route("/api/mobile/move", methods=["POST"])
def api_move():
    """在给定局面上走一步

    body: {fen, iccs} 或 {fen, from_sq, to_sq}
    返回新局面 + 新合法着法表; 非法着法返回中文错误, 局面不变。
    """
    payload = _json_body()
    iccs = str(payload.get("iccs") or "").strip()
    if not iccs:
        iccs = str(payload.get("from_sq") or "").strip() + str(payload.get("to_sq") or "").strip()
    if not iccs:
        return _err("需要 iccs 或 from_sq/to_sq")

    try:
        fen = _clean_fen(payload.get("fen"), "fen")
        grid, side = rules.fen_to_board(fen)
        _after, info = rules.apply_iccs(grid, iccs, side=side)
    except RuleError as e:
        return _err(str(e))

    chinese = iccs_to_chinese(info["iccs"], fen)
    last_move = {"from_sq": _sq(*info["from"]), "to_sq": _sq(*info["to"]),
                 "chinese": chinese, "iccs": info["iccs"]}
    return jsonify({
        "ok": True,
        "iccs": info["iccs"],
        "chinese": chinese,
        "captured": info["captured"],
        **_board_state(info["fen_after"], last_move),
    })


# ----------------------------------------------------------------------
# AI 实时建议
# ----------------------------------------------------------------------
@bp.route("/api/mobile/hint", methods=["POST"])
def api_hint():
    """AI 提示下一步: 引擎 MultiPV 候选 + (可选) LLM 二次筛选

    body: {fen, preset?|movetime?, use_llm?, token?}
      返回 best/箭头坐标/评分/中文着法/候选; 引擎没装好返回 ok:false, 不抛 500。
    """
    payload = _json_body()
    try:
        fen = _clean_fen(payload.get("fen"), "fen")
    except RuleError as e:
        return _err(str(e))

    _, side = rules.fen_to_board(fen)
    movetime, mode = _resolve_movetime(payload, _default_movetime())
    llm_ready = llm_configured()
    use_llm = bool(payload.get("use_llm", True)) and llm_ready
    try:
        multipv = int(_engine_config.get("multipv", ENGINE_MULTIPV))
    except (TypeError, ValueError):
        multipv = ENGINE_MULTIPV
    multipv = max(1, min(multipv, 8))

    try:
        result = _analyze(fen, movetime, multipv)
    except EngineError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:                       # 兜底, 别让前端拿到 HTML 错误页
        return jsonify({"ok": False, "error": f"引擎异常: {type(e).__name__}: {e}"}), 200

    candidates = _candidates(result.get("moves") or [], fen, multipv)
    if not candidates:
        return jsonify({"ok": False, "error": "引擎未返回可解析的候选着法",
                        "info": {"score": result.get("score"), "depth": result.get("depth")}}), 200

    primary = candidates[0]
    if use_llm:
        pick_iccs, reason, llm_used = pick_best_move(fen, candidates)
    else:
        pick_iccs, llm_used = primary["iccs"], False
        reason = ("引擎首选（未配置 LLM，没做二次筛选）" if not llm_ready
                  else "引擎首选（已关闭 LLM 二次筛选）")
    picked = next((c for c in candidates if c["iccs"] == pick_iccs), primary)

    return jsonify({
        "ok": True,
        "primary": primary,
        "candidates": candidates,
        "llm_pick": dict(picked, reason=reason, llm_used=llm_used,
                         engine_first=picked["iccs"] == primary["iccs"]),
        "movetime": movetime,
        "movetime_mode": mode,
        "side": "红方" if side == "w" else "黑方",
    })


# ----------------------------------------------------------------------
# 教练解说
# ----------------------------------------------------------------------
@bp.route("/api/mobile/coach", methods=["POST"])
def api_coach():
    """给"刚走的那一步"定档(最佳/可更好/疑手/失误) + 讲原因

    body: {prev_fen, played_iccs, movetime?, token?}
      - prev_fen: 走这一步**之前**的局面
      - played_iccs: 刚走的着法 (ICCS)
      定档用"引擎首选评分 - 实走着法评分"的差; 原因优先交给 LLM(可降级为模板点评)。
    """
    payload = _json_body()
    played_iccs = str(payload.get("played_iccs") or "").strip()
    if not played_iccs:
        return _err("played_iccs 不能为空")

    try:
        prev_fen = _clean_fen(payload.get("prev_fen"), "prev_fen")
        grid, side = rules.fen_to_board(prev_fen)
        _after, info = rules.apply_iccs(grid, played_iccs, side=side)
    except RuleError as e:
        return _err(str(e))

    played_cn = iccs_to_chinese(info["iccs"], prev_fen)
    fen_after = info["fen_after"]
    movetime, _ = _resolve_movetime(payload, _default_movetime())

    try:
        best_result = _analyze(prev_fen, movetime, 1)
        played_result = _analyze(fen_after, movetime, 1)
    except EngineError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": f"引擎异常: {type(e).__name__}: {e}"}), 200

    best_iccs = best_result.get("bestmove")
    best_cn = iccs_to_chinese(best_iccs, prev_fen) if best_iccs else None
    best_score = best_result.get("score")
    best_mate = best_result.get("mate")
    # 引擎报的是"走完之后那一方(=对手)"的视角, 取负换算成走这步棋的人
    after_score = played_result.get("score")
    after_mate = played_result.get("mate")
    played_score = None if after_score is None else -after_score
    played_mate = None if after_mate is None else -after_mate
    gap_cp = None
    if best_score is not None and played_score is not None:
        gap_cp = max(0, best_score - played_score)

    category = _classify(gap_cp, best_mate, played_mate)
    reason, llm_used = coach(prev_fen, played_cn, best_cn, gap_cp, category)

    return jsonify({
        "ok": True,
        "prev_fen": prev_fen,
        "fen": fen_after,
        "played": {"iccs": info["iccs"], "chinese": played_cn},
        "best": {"iccs": best_iccs, "chinese": best_cn,
                 "score": best_score, "mate": best_mate},
        "played_score": played_score,
        "played_mate": played_mate,
        "gap_cp": gap_cp,
        "category": category,
        "reason": reason,
        "llm_used": llm_used,
    })


# ----------------------------------------------------------------------
# 保存研究
# ----------------------------------------------------------------------
@bp.route("/api/mobile/save", methods=["POST"])
def api_save():
    """把当前局面(或整串着法)存成研究局

    body: {name?, category?, start_fen?, move_list?, current_fen?, note?, tags?, meta?, token?}
      默认 category=study(研究); 给了 move_list 就逐个重放后入库。
    """
    payload = _json_body()
    category = str(payload.get("category") or "study").strip()
    if category not in storage.CATEGORIES:
        return _err(f"category 只能是 {'/'.join(storage.CATEGORIES)}")

    try:
        start_fen = _clean_fen(payload.get("start_fen") or START_FEN, "start_fen")
    except RuleError as e:
        return _err(str(e))

    move_list = payload.get("move_list")
    replayed = None
    if move_list:
        if not isinstance(move_list, (list, tuple)):
            return _err("move_list 必须是数组")
        seq = [m.get("iccs") if isinstance(m, dict) else m for m in move_list]
        try:
            replayed = pgn_io.replay(start_fen, seq)
        except RuleError as e:
            return _err(f"起始局面不合法: {e}")
        if replayed["warnings"]:
            return _err("；".join(replayed["warnings"][:3]))

    current_fen = replayed["fen"] if replayed else (payload.get("current_fen") or start_fen)
    try:
        current_fen = _clean_fen(current_fen, "current_fen")
    except RuleError as e:
        return _err(str(e))

    tags = payload.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(t).strip() for t in tags if str(t).strip())

    result = str(payload.get("result") or "*").strip()
    if result not in storage.RESULTS:
        return _err(f"result 只能是 {'/'.join(storage.RESULTS)}")

    fields = {
        "name": str(payload.get("name") or "").strip() or "手机研究局",
        "category": category,
        "start_fen": start_fen,
        "current_fen": current_fen,
        "side_to_move": _side_name(current_fen),
        "note": str(payload.get("note") or "").strip() or None,
        "tags": tags or None,
        "red_name": payload.get("red_name"),
        "black_name": payload.get("black_name"),
        "event": payload.get("event"),
        "date": payload.get("date"),
        "result": result,
    }
    fields = {k: v for k, v in fields.items() if v is not None}

    try:
        game_id = storage.create_game(fields)
        if replayed:
            for move in replayed["moves"]:
                storage.append_move(game_id, _move_row(move))
    except ValueError as e:                      # 理论上不会, 防库异常
        return _err(str(e))

    return jsonify({"ok": True, "game_id": game_id,
                    "move_count": storage.count_moves(game_id)})
