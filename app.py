# -*- coding: utf-8 -*-
"""Flask Web 界面: 棋盘页 + 棋局保存/复盘接口, 只做转发和翻译, 不实现棋规。

运行:
    pip install -r requirements.txt
    python app.py
    浏览器打开 http://127.0.0.1:5000          (下棋 / 分析)
             或 http://127.0.0.1:5000/studies (我的棋局 / 复盘)
             或 http://127.0.0.1:5000/study   (残局研究: 摆盘/候选/主变/备注)

接口:
    POST /api/analyze        分析棋面, 返回最佳着法/PV (给"分析"按钮用)
    POST /api/ai_hint        AI 提示下一步: 引擎 MultiPV 候选 + LLM 二次筛选 (给"AI提示"按钮用)
    POST /api/engine_config  运行期调整引擎参数 (Threads / Hash / MultiPV / 默认思考时间)
    /api/games/*             棋局保存、续下、列表、复盘、导出导入 (见 games_routes.py)
    /api/studies/*           残局研究: 建研究/摆盘/分析任务/候选着法 (见 endgame/routes.py)
"""

import atexit
import threading

from flask import Flask, jsonify, render_template, request

import games_routes
from core.chess_engine import (
    DEFAULT_MOVETIME,
    ENGINE_HASH_MB,
    ENGINE_MULTIPV,
    ENGINE_PATH,
    ENGINE_THREADS,
    ChessEngine,
    EngineError,
    NNUE_PATH,
    START_FEN,
)
from core.coord_utils import GRID_ORIENTATION, iccs_to_chinese, iccs_to_grid, parse_fen, pv_to_chinese
from endgame import routes as endgame_routes
from core.explainer import llm_configured, pick_best_move

app = Flask(__name__)

# Pikafish 是单进程 UCI 会话, 全局复用一个实例, 并用锁把分析请求串行化
_engine: ChessEngine | None = None
_engine_lock = threading.Lock()
_analyze_lock = threading.Lock()

MIN_MOVETIME = 100
MAX_MOVETIME = 60000

# 引擎参数: 默认值来自 chess_engine (可用环境变量 PIKAFISH_THREADS / _HASH / _MULTIPV 覆盖),
# 运行期可由 POST /api/engine_config 改掉, 改完立刻 setoption 给正在跑的那个引擎进程。
ENGINE_CONFIG = {
    "threads": ENGINE_THREADS,
    "hash_mb": ENGINE_HASH_MB,
    "multipv": ENGINE_MULTIPV,
    "movetime": DEFAULT_MOVETIME,   # 手动档没给毫秒数时的兜底思考时间
}
# 各项的合法区间, 挡住前端把参数开到离谱
CONFIG_LIMITS = {
    "threads": (1, 64),
    "hash_mb": (1, 4096),
    "multipv": (1, 8),
    "movetime": (MIN_MOVETIME, MAX_MOVETIME),
}


def get_engine() -> ChessEngine:
    """懒加载并复用同一个引擎进程 (崩溃后 analyze 会自动重启)"""
    global _engine
    with _engine_lock:
        if _engine is None:
            engine = ChessEngine(ENGINE_PATH, NNUE_PATH)
            try:
                engine.start_engine()  # 让引擎缺失/启动失败在第一次请求就暴露
                engine.configure_engine(ENGINE_CONFIG["threads"], ENGINE_CONFIG["hash_mb"],
                                        ENGINE_CONFIG["multipv"])
            except EngineError:
                engine.quit()
                raise
            _engine = engine
        return _engine


@atexit.register
def _shutdown_engine() -> None:
    global _engine
    endgame_routes.shutdown()      # 先收掉后台分析线程与专用引擎进程
    if _engine is not None:
        _engine.quit()
        _engine = None


def _read_movetime(payload: dict, default: int, maximum: int) -> int:
    """从请求里取 movetime 并夹到 [MIN_MOVETIME, maximum]"""
    try:
        movetime = int(payload.get("movetime", default))
    except (TypeError, ValueError):
        movetime = default
    return max(MIN_MOVETIME, min(movetime, maximum))


# 棋局保存/复盘蓝图: 建表 + 注册路由, 并把引擎访问方式注入过去(打分要用, 复用同一个引擎进程)
games_routes.init_app(app, get_engine=get_engine, analyze_lock=_analyze_lock)

# 残局研究蓝图: 建后台分析队列 + 引擎池(快分析复用上面那个常驻引擎进程)
endgame_routes.init_app(app, get_engine=get_engine)


@app.route("/")
def index():
    return render_template("index.html", start_fen=START_FEN, engine_config=ENGINE_CONFIG)


@app.route("/studies")
def studies():
    """我的棋局: 列表 / 复盘 / 导入导出"""
    return render_template("studies.html", start_fen=START_FEN)


@app.route("/study")
@app.route("/study/<int:study_id>")
def study(study_id=None):
    """残局研究: 摆盘 / 候选 / 主变 / 备注 四栏 + 研究树"""
    return render_template("study.html", start_fen=START_FEN, study_id=study_id)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(silent=True) or {}
    fen = (payload.get("fen") or "").strip()
    movetime = _read_movetime(payload, ENGINE_CONFIG["movetime"], MAX_MOVETIME)

    if not fen:
        return jsonify({"error": "FEN 不能为空"}), 400

    try:
        _, side = parse_fen(fen)
    except ValueError as e:
        return jsonify({"error": f"FEN 不合法: {e}"}), 400

    try:
        # 只给最佳着法就够了, MultiPV=1 时引擎把全部时间投在这一路上
        with _analyze_lock:
            result = get_engine().analyze(fen, movetime=movetime, multipv=1)
    except EngineError as e:
        return jsonify({"error": str(e)}), 503

    # 顺带把 ICCS 着法翻成中文、并算好棋盘行列, 前端直接显示/画高亮
    bestmove = result.get("bestmove")
    result["bestmove_cn"] = iccs_to_chinese(bestmove, fen) if bestmove else None
    result["pv_cn"] = pv_to_chinese(result.get("pv"), fen)
    result["side"] = "红方" if side == "w" else "黑方"
    result["movetime"] = movetime
    try:
        coord = iccs_to_grid(bestmove) if bestmove else None
    except ValueError:
        coord = None
    result["from"] = coord["from"] if coord else None
    result["to"] = coord["to"] if coord else None
    return jsonify(result)


def _build_candidates(moves: list, fen: str, limit: int) -> list:
    """把引擎的 MultiPV 结果翻成前端能直接用的形态: 中文着法 + 棋盘行列 + 中文变例"""
    out = []
    for mv in moves[:max(1, limit)]:
        iccs = mv.get("bestmove")
        if not iccs or iccs in ("(none)", "0000"):
            continue
        try:
            coord = iccs_to_grid(iccs)
        except ValueError:
            continue   # 引擎输出的着法解析不了, 跳过这一路
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


@app.route("/api/ai_hint", methods=["POST"])
def api_ai_hint():
    """AI 提示下一步: 引擎 MultiPV 候选 + (可选) LLM 二次筛选, 挑一条最值得推荐的。

    请求体: {fen, movetime?, use_llm?}
      不传 movetime 就是"自动"档, 由引擎按剩余子力数决定思考时间。
    响应: {ok, primary, candidates, llm_pick, movetime, movetime_mode, side}

    约定: 只要不是参数错误, 一律返回 HTTP 200 + {"ok": false, "error": ...},
    让前端统一按 ok 判断, 不会因为引擎没装好就抛 500。
    """
    payload = request.get_json(silent=True) or {}
    fen = (payload.get("fen") or "").strip()
    auto_movetime = not payload.get("movetime")   # 自动档: 交回后端决定搜多久
    llm_ready = llm_configured()
    # 没配 LLM(或前端关了)就跳过二次筛选, 只给引擎首选
    use_llm = bool(payload.get("use_llm", True)) and llm_ready

    if not fen:
        return jsonify({"ok": False, "error": "FEN 不能为空"}), 400

    try:
        _, side = parse_fen(fen)
    except ValueError as e:
        return jsonify({"ok": False, "error": f"FEN 不合法: {e}"}), 400

    # 引擎缺失 / 未启动 / NNUE 缺失 / 崩溃, 都只返回可读错误, 不抛 500
    try:
        engine = get_engine()
        movetime = (min(engine.dynamic_movetime(fen), MAX_MOVETIME) if auto_movetime
                    else _read_movetime(payload, ENGINE_CONFIG["movetime"], MAX_MOVETIME))
        with _analyze_lock:
            result = engine.analyze(fen, movetime=movetime, multipv=ENGINE_CONFIG["multipv"])
    except EngineError as e:
        return jsonify({"ok": False, "error": str(e)}), 200
    except Exception as e:  # 兜底, 别让前端拿到一个 HTML 错误页
        return jsonify({"ok": False, "error": f"引擎异常: {type(e).__name__}: {e}"}), 200

    candidates = _build_candidates(result.get("moves") or [], fen, ENGINE_CONFIG["multipv"])
    if not candidates:
        return jsonify({"ok": False, "error": "引擎未返回可解析的候选着法",
                        "info": {"score": result.get("score"), "depth": result.get("depth")}}), 200

    primary = candidates[0]

    # LLM 二次筛选: 只能从候选里挑, 任何异常都在 pick_best_move 内部降级
    if use_llm:
        pick_iccs, reason, pick_llm_used = pick_best_move(fen, candidates)
    else:
        pick_iccs, pick_llm_used = primary["iccs"], False
        reason = ("引擎首选（未配置 LLM，没做二次筛选）" if not llm_ready
                  else "引擎首选（已关闭 LLM 二次筛选）")
    picked = next((c for c in candidates if c["iccs"] == pick_iccs), primary)

    return jsonify({
        "ok": True,
        "primary": primary,          # 引擎首选 (MultiPV 第 1 路)
        "candidates": candidates,    # 前 N 路候选, 含 primary
        "llm_pick": dict(picked,     # 最终推荐: LLM 二次筛选的结果(降级时等于引擎首选)
                         reason=reason,
                         llm_used=pick_llm_used,
                         engine_first=picked["iccs"] == primary["iccs"]),
        "movetime": movetime,
        "movetime_mode": "auto" if auto_movetime else "manual",
        "side": "红方" if side == "w" else "黑方",
    })


@app.route("/api/engine_config", methods=["POST"])
def api_engine_config():
    """运行期调整引擎参数: {threads?, hash_mb?, multipv?, movetime?}

    前三项立刻以 setoption 发给一直在跑的那个引擎进程(不用重启),
    movetime 是手动档没给毫秒数时的兜底思考时间。
    响应里带回生效后的完整配置。
    """
    payload = request.get_json(silent=True) or {}
    cfg = dict(ENGINE_CONFIG)
    for key, (low, high) in CONFIG_LIMITS.items():
        if key not in payload:
            continue
        try:
            cfg[key] = max(low, min(int(payload[key]), high))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": f"{key} 必须是整数"}), 400

    changed = cfg != ENGINE_CONFIG
    if changed:
        # 先改引擎: 失败就整体放弃, 不留下"配置改了但引擎没跟上"的状态
        try:
            with _analyze_lock:
                get_engine().configure_engine(cfg["threads"], cfg["hash_mb"], cfg["multipv"])
        except EngineError as e:
            return jsonify({"ok": False, "error": str(e), "engine": dict(ENGINE_CONFIG)}), 200
        ENGINE_CONFIG.update(cfg)

    return jsonify({"ok": True, "changed": changed, "engine": dict(ENGINE_CONFIG)})


if __name__ == "__main__":
    print(f"引擎路径: {ENGINE_PATH}")
    print(f"棋盘朝向: {GRID_ORIENTATION}")
    print(f"引擎参数: Threads={ENGINE_CONFIG['threads']} "
          f"Hash={ENGINE_CONFIG['hash_mb']}MB MultiPV={ENGINE_CONFIG['multipv']} "
          f"默认思考时间={ENGINE_CONFIG['movetime']}ms")
    print(f"LLM 二次筛选: {'已配置' if llm_configured() else '未配置 (AI 提示只有引擎首选)'}")
    print("启动后打开 http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
