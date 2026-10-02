# -*- coding: utf-8 -*-
"""残局研究的落库层: endgame_positions / endgame_candidates / endgame_position_moves /
endgame_tasks 四张表的读写。

设计要点:
    * 结果按"局面 + 一整套分析配置"存: 唯一键是
      (game_id, ply, fen, engine_version, repetition_rule, depth, movetime_ms, multipv)。
      换个 depth / MultiPV / 规则集重新分析会新增一行, **不会覆盖**别的配置的结果 ——
      规格明确禁止"不同配置的结果互相覆盖"。
    * 同一套配置重跑: 覆盖自己那一行, 候选着法整批替换 (先删后插, 一个事务),
      不会留下半截数据。
    * 引擎着法在进这里之前已经过了 rules.validate_move (见 analyzer.extract_candidates),
      本层不再放行来路不明的着法。
    * 空值一律落成 '' 而不是 NULL: 唯一索引里 NULL 互不相等, 留 NULL 会让同一套配置
      重复插入(迁移文件里也写了这条)。
    * 同一套配置的写入用条带锁互斥, 避免两个线程同时替换候选表。

不做: 任务调度 / 引擎调度 (见 endgame/tasks.py), 也不改 games/moves 的既有语义。
"""

import contextlib
import datetime
import json
import threading
import uuid

from core import storage

# 落库时缺省用的规则集名 (与迁移里的 DEFAULT 一致)
DEFAULT_REPETITION_RULE = "chinese_2020_analysis"

# endgame_positions 允许写入的列
POSITION_FIELDS = (
    "game_id", "ply", "fen", "position_hash", "to_move", "repetition_rule",
    "engine_version", "nnue_file", "depth", "seldepth", "movetime_ms", "multipv",
    "rule_aware", "bestmove", "best_pv_uci", "score_type", "score_value",
    "normalized_win", "result", "result_for", "mate_plies", "draw_type",
    "rule_review_status", "confidence", "analysis_duration_ms", "created_at",
)
# 决定"这是不是同一次分析"的列, 必须和迁移里的 UNIQUE 完全一致
POSITION_KEY_COLUMNS = ("game_id", "ply", "fen", "engine_version", "repetition_rule",
                        "depth", "movetime_ms", "multipv")
# NOT NULL 且参与唯一键的列, 绝不能留 None
POSITION_REQUIRED_INTS = ("depth", "movetime_ms", "multipv", "rule_aware")

# endgame_candidates 允许写入的列 (analyzer 多给的 depth/seldepth/bound 无处可放, 丢弃)
CANDIDATE_FIELDS = (
    "rank", "multipv_index", "move_uci", "from_square", "to_square", "promotion",
    "score_type", "score_value", "mate_plies", "pv_uci", "pv_san", "visited_nodes",
    "is_key_move", "is_user_variation",
)

# endgame_position_moves.role 的取值
POSITION_MOVE_ROLES = ("key_move", "critical_line", "refutation")

# endgame_tasks 允许写入的列 (id / created_at 由本层管)
TASK_FIELDS = (
    "study_id", "game_id", "ply", "fen", "position_hash", "mode", "depth", "movetime_ms",
    "multipv", "search_moves", "rule_options", "repetition_rule", "priority", "status",
    "progress_json", "partial_best", "position_id", "error_code", "message",
    "engine_version", "nnue_file", "started_at", "finished_at",
)

TASK_STATUSES = ("draft", "queued", "running", "aggregating", "completed",
                 "failed", "cancelled", "superseded")
TASK_FINAL_STATUSES = ("completed", "failed", "cancelled", "superseded")

# 状态机: 只允许这些迁移, 别的组合直接报错 (免得库里出现"completed 又变 running"这种怪状态)
TASK_TRANSITIONS = {
    "draft": ("queued", "cancelled"),
    "queued": ("running", "cancelled", "failed"),
    "running": ("aggregating", "completed", "failed", "cancelled"),
    "aggregating": ("completed", "failed", "cancelled"),
    "completed": ("superseded",),
    "failed": (),
    "cancelled": (),
    "superseded": (),
}

REVIEW_STATUSES = ("none", "pending", "human_verified", "override")

# 条带锁的数量: 用固定条带而不是"一个 key 一把锁", 锁表就不会随着研究变多而膨胀;
# 不同配置偶尔撞到同一把锁只是少一点并发, 不影响正确性。
_STRIPES = 64
_LOCKS = [threading.Lock() for _ in range(_STRIPES)]


# ----------------------------------------------------------------------
# 连接 / 小工具
# ----------------------------------------------------------------------
def _now() -> str:
    """本地时间字符串, 与库里其他表保持一致"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _int_or(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@contextlib.contextmanager
def _tx():
    """事务上下文 (复用 storage 的路径与外键设置, 另加 busy_timeout)

    分析结果由后台线程写、接口由请求线程读, 多一个连接就可能撞上
    "database is locked"; 给几秒重试窗口比直接抛异常友好。
    """
    conn = storage.get_conn()
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        with conn:
            yield conn
    finally:
        conn.close()


def _pick(fields: dict, allowed) -> dict:
    """按白名单挑列 (列名来自模块常量, 不会有注入)"""
    return {k: v for k, v in (fields or {}).items() if k in allowed}


@contextlib.contextmanager
def position_lock(key):
    """按 "局面 + 分析配置" 互斥

    :param key: 可哈希的元组 (一般直接传 POSITION_KEY_COLUMNS 对应的值)
    """
    lock = _LOCKS[hash(tuple(key)) % _STRIPES]
    with lock:
        yield


# ----------------------------------------------------------------------
# 局面分析结果
# ----------------------------------------------------------------------
def _normalize_position(result: dict, game_id, ply) -> dict:
    """analyzer 的输出 -> endgame_positions 的一行 (空值落 '' / 0)"""
    values = _pick(result or {}, POSITION_FIELDS)
    values["game_id"] = _int_or(game_id, 0)
    values["ply"] = _int_or(ply, 0)

    for column in ("fen", "position_hash", "to_move"):
        if not values.get(column):
            raise ValueError(f"分析结果缺少 {column}, 不能落库")
    if values["to_move"] not in ("r", "b"):
        raise ValueError(f"to_move 只能是 r/b, 收到 {values['to_move']!r}")

    # 参与唯一键的文本列留 NULL 会让唯一索引失效, 一律落成 ''
    values["engine_version"] = values.get("engine_version") or ""
    values["nnue_file"] = values.get("nnue_file") or ""
    values["repetition_rule"] = values.get("repetition_rule") or DEFAULT_REPETITION_RULE
    for column in POSITION_REQUIRED_INTS:
        values[column] = _int_or(values.get(column), 0)
    values["rule_aware"] = 1 if values.get("rule_aware") else 0
    values["rule_review_status"] = values.get("rule_review_status") or "none"
    values["confidence"] = values.get("confidence") or "preliminary"
    values.setdefault("created_at", _now())
    return values


def _upsert_position(conn, values: dict) -> tuple[int, bool]:
    """按唯一键插入或更新, 返回 (position_id, 是否新建)"""
    columns = list(values)
    existing = conn.execute(
        "SELECT id FROM endgame_positions WHERE "
        + " AND ".join(f"{col} = ?" for col in POSITION_KEY_COLUMNS),
        [values[col] for col in POSITION_KEY_COLUMNS],
    ).fetchone()

    if existing:
        assignable = [col for col in columns if col not in POSITION_KEY_COLUMNS]
        if assignable:
            conn.execute(
                "UPDATE endgame_positions SET "
                + ", ".join(f"{col} = ?" for col in assignable)
                + " WHERE id = ?",
                [values[col] for col in assignable] + [existing["id"]],
            )
        return int(existing["id"]), False

    cur = conn.execute(
        f"INSERT INTO endgame_positions ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        [values[col] for col in columns],
    )
    return int(cur.lastrowid), True


def save_analysis(result: dict, *, game_id, ply, links=None) -> dict:
    """把一次分析结果连同候选着法写库 (单事务)

    :param result: endgame.analyzer.analyze_position() 的返回值
    :param game_id: 结果挂在哪一局(研究)下
    :param ply: 结果对应第几着之后的局面
    :param links: [(move_id, role), ...] 需要关联的现有走法, role 见 POSITION_MOVE_ROLES
    :raise ValueError: 缺少必要字段 / 状态不合法 / role 不认识
    :return: {"position_id", "created", "candidate_ids", "links"}
    """
    values = _normalize_position(result, game_id, ply)
    key = tuple(values[col] for col in POSITION_KEY_COLUMNS)

    with position_lock(key):
        with _tx() as conn:
            position_id, created = _upsert_position(conn, values)

            # 候选整批替换: 同一套配置重跑时不会和上一次的候选混在一起
            conn.execute("DELETE FROM endgame_candidates WHERE position_id = ?",
                         (position_id,))
            candidate_ids = []
            for index, candidate in enumerate(result.get("candidates") or []):
                row = _pick(candidate, CANDIDATE_FIELDS)
                row["position_id"] = position_id
                row.setdefault("rank", index + 1)
                if not row.get("move_uci"):
                    continue
                candidate_ids.append(_insert(conn, "endgame_candidates", row))

            saved_links = []
            for move_id, role in (links or []):
                if role not in POSITION_MOVE_ROLES:
                    raise ValueError(f"未知的关联角色: {role!r}, "
                                     f"可选 {POSITION_MOVE_ROLES}")
                conn.execute(
                    "INSERT OR IGNORE INTO endgame_position_moves "
                    "(position_id, move_id, role) VALUES (?, ?, ?)",
                    (position_id, int(move_id), role))
                saved_links.append({"move_id": int(move_id), "role": role})

            row = conn.execute("SELECT * FROM endgame_positions WHERE id = ?",
                               (position_id,)).fetchone()
    return {"position_id": position_id, "created": created,
            "candidate_ids": candidate_ids, "links": saved_links,
            "position": dict(row) if row else None}


def _insert(conn, table: str, values: dict) -> int:
    """照 values 拼一条 INSERT, 返回新行 id"""
    columns = list(values)
    cur = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' * len(columns))})",
        [values[col] for col in columns],
    )
    return int(cur.lastrowid)


def get_position(position_id: int) -> dict | None:
    """取一条分析结果(不含候选)"""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM endgame_positions WHERE id = ?",
                           (position_id,)).fetchone()
        return dict(row) if row else None


def list_positions(game_id=None, *, ply=None, limit=200, offset=0) -> list[dict]:
    """列分析结果, 按 ply 升序 + 新的在前, 每行带 candidate_count

    同一局面用不同配置分析过会有多行 —— 这是设计如此, 不是重复数据。
    """
    where, params = [], []
    if game_id is not None:
        where.append("p.game_id = ?")
        params.append(int(game_id))
    if ply is not None:
        where.append("p.ply = ?")
        params.append(int(ply))
    sql = ("SELECT p.*, "
           "(SELECT COUNT(*) FROM endgame_candidates c WHERE c.position_id = p.id) "
           "AS candidate_count FROM endgame_positions p"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY p.ply ASC, p.id DESC LIMIT ? OFFSET ?")
    with _tx() as conn:
        rows = conn.execute(sql, [*params, max(1, min(_int_or(limit, 200), 500)),
                                  max(0, _int_or(offset, 0))]).fetchall()
        return [dict(r) for r in rows]


def latest_position(game_id, *, ply=None, fen=None) -> dict | None:
    """取最近一次分析结果(研究树默认展示的那条)"""
    where, params = ["game_id = ?"], [int(game_id)]
    if ply is not None:
        where.append("ply = ?")
        params.append(int(ply))
    if fen:
        where.append("fen = ?")
        params.append(fen)
    with _tx() as conn:
        row = conn.execute("SELECT * FROM endgame_positions WHERE "
                           + " AND ".join(where)
                           + " ORDER BY id DESC LIMIT 1", params).fetchone()
        return dict(row) if row else None


def list_candidates(position_id: int) -> list[dict]:
    """取某条分析结果的候选着法, 按 rank 升序"""
    with _tx() as conn:
        rows = conn.execute("SELECT * FROM endgame_candidates WHERE position_id = ? "
                            "ORDER BY rank ASC", (position_id,)).fetchall()
        return [dict(r) for r in rows]


def set_position_review(position_id: int, review_status: str, *, confidence=None,
                        result=None) -> dict | None:
    """人工棋例复核: pending -> human_verified / override

    只允许人工改 rule_review_status / confidence / result 这三项 —— 引擎的
    bestmove / score / mate 属于"算出来的事实", 谁都不能在库里改掉它们。
    """
    if review_status not in REVIEW_STATUSES:
        raise ValueError(f"未知的棋例复核状态: {review_status!r}, 可选 {REVIEW_STATUSES}")
    assigns, params = ["rule_review_status = ?"], [review_status]
    if confidence:
        assigns.append("confidence = ?")
        params.append(confidence)
    if result:
        assigns.append("result = ?")
        params.append(result)
    with _tx() as conn:
        cur = conn.execute("UPDATE endgame_positions SET " + ", ".join(assigns)
                           + " WHERE id = ?", [*params, int(position_id)])
        if cur.rowcount <= 0:
            return None
        row = conn.execute("SELECT * FROM endgame_positions WHERE id = ?",
                           (int(position_id),)).fetchone()
        return dict(row) if row else None


def link_move(position_id: int, move_id: int, role: str) -> bool:
    """把一条分析结果关联到现有走法 (关键步 / 主变 / 反证着), 重复关联不报错"""
    if role not in POSITION_MOVE_ROLES:
        raise ValueError(f"未知的关联角色: {role!r}, 可选 {POSITION_MOVE_ROLES}")
    with _tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO endgame_position_moves (position_id, move_id, role) "
            "VALUES (?, ?, ?)", (int(position_id), int(move_id), role))
        return cur.rowcount > 0


def unlink_move(position_id: int, move_id: int, role=None) -> int:
    """解除关联, 返回删除条数"""
    where = ["position_id = ?", "move_id = ?"]
    params = [int(position_id), int(move_id)]
    if role:
        if role not in POSITION_MOVE_ROLES:
            raise ValueError(f"未知的关联角色: {role!r}, 可选 {POSITION_MOVE_ROLES}")
        where.append("role = ?")
        params.append(role)
    with _tx() as conn:
        cur = conn.execute("DELETE FROM endgame_position_moves WHERE "
                           + " AND ".join(where), params)
        return cur.rowcount


def list_position_moves(position_id: int, role=None) -> list[dict]:
    """取某条分析结果关联的走法(带 moves 表的着法内容), 供研究树展示"""
    where = ["pm.position_id = ?"]
    params = [int(position_id)]
    if role:
        where.append("pm.role = ?")
        params.append(role)
    with _tx() as conn:
        rows = conn.execute(
            "SELECT pm.role, m.* FROM endgame_position_moves pm "
            "JOIN moves m ON m.id = pm.move_id WHERE " + " AND ".join(where)
            + " ORDER BY m.ply ASC", params).fetchall()
        return [dict(r) for r in rows]


def count_positions(game_id=None) -> int:
    """分析结果条数(按配置粒度)"""
    with _tx() as conn:
        if game_id is None:
            row = conn.execute("SELECT COUNT(*) AS n FROM endgame_positions").fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) AS n FROM endgame_positions "
                               "WHERE game_id = ?", (int(game_id),)).fetchone()
        return int(row["n"])


# ----------------------------------------------------------------------
# 分析任务
# ----------------------------------------------------------------------
def _task_values(fields: dict) -> dict:
    """挑列 + 把 JSON / 列表列转成库里的存法(create_task 与 update_task 共用)

    rule_options 是 dict, search_moves 是 list —— sqlite3 都绑不了, 必须先转成
    TEXT。新建与更新走同一套规则, 免得"复用 draft 节点改参数"时漏掉这一步。
    """
    values = _pick(fields, TASK_FIELDS)
    if "rule_options" in values and not isinstance(values["rule_options"], str):
        values["rule_options"] = json.dumps(values["rule_options"], ensure_ascii=False)
    if "search_moves" in values and isinstance(values["search_moves"], (list, tuple)):
        values["search_moves"] = ",".join(str(m).strip() for m in values["search_moves"]
                                          if str(m).strip())
    return values


def create_task(fields: dict, task_id: str | None = None) -> str:
    """新建分析任务, 返回 taskId

    默认停在 draft: 先让调用方把参数、局面都校验完, 再 transition 到 queued,
    免得一条脏任务被后台线程捡走。
    """
    values = _task_values(fields)
    values["id"] = task_id or uuid.uuid4().hex
    values.setdefault("status", "draft")
    if values["status"] not in TASK_STATUSES:
        raise ValueError(f"未知的任务状态: {values['status']!r}, 可选 {TASK_STATUSES}")
    values.setdefault("created_at", _now())
    with _tx() as conn:
        _insert(conn, "endgame_tasks", values)
    return values["id"]


def get_task(task_id: str) -> dict | None:
    """取任务(含状态、进度、错误信息)"""
    with _tx() as conn:
        row = conn.execute("SELECT * FROM endgame_tasks WHERE id = ?",
                           (str(task_id),)).fetchone()
        return dict(row) if row else None


def update_task(task_id: str, fields: dict, *, enforce=True) -> dict | None:
    """更新任务, 状态变化走状态机校验

    :param enforce: 关闭状态机校验(仅供内部维护用), 默认开启
    :raise ValueError: 非法状态迁移 / 未知状态
    """
    values = _task_values(fields)
    if not values:
        return get_task(task_id)
    if "status" in values:
        new_status = values["status"]
        if new_status not in TASK_STATUSES:
            raise ValueError(f"未知的任务状态: {new_status!r}, 可选 {TASK_STATUSES}")
        if enforce:
            current = get_task(task_id)
            if current is None:
                return None
            old_status = current["status"]
            if new_status != old_status and new_status not in TASK_TRANSITIONS.get(old_status, ()):
                raise ValueError(f"非法状态迁移: {old_status} -> {new_status}")
        if new_status == "running" and not values.get("started_at"):
            values["started_at"] = _now()
        if new_status in TASK_FINAL_STATUSES and not values.get("finished_at"):
            values["finished_at"] = _now()

    assigns = ", ".join(f"{col} = ?" for col in values)
    with _tx() as conn:
        cur = conn.execute(f"UPDATE endgame_tasks SET {assigns} WHERE id = ?",
                           [*values.values(), str(task_id)])
        if cur.rowcount <= 0:
            return None
        row = conn.execute("SELECT * FROM endgame_tasks WHERE id = ?",
                           (str(task_id),)).fetchone()
        return dict(row) if row else None


def set_task_progress(task_id: str, *, progress=None, partial_best=None,
                      message=None) -> bool:
    """任务进度上报 (后台线程每收到一条 info 就可能调一次)"""
    values = {}
    if progress is not None:
        values["progress_json"] = json.dumps(progress, ensure_ascii=False)
    if partial_best is not None:
        values["partial_best"] = partial_best
    if message is not None:
        values["message"] = message
    if not values:
        return False
    assigns = ", ".join(f"{col} = ?" for col in values)
    with _tx() as conn:
        cur = conn.execute(f"UPDATE endgame_tasks SET {assigns} WHERE id = ?",
                           [*values.values(), str(task_id)])
        return cur.rowcount > 0


def claim_next_task(*, study_id=None) -> dict | None:
    """捡一个 queued 任务并占成 running (后台 worker 用)

    "挑"和"占"在同一个事务里完成, 多个 worker 同时抢也不会拿到同一条。
    """
    where = ["status = 'queued'"]
    params = []
    if study_id is not None:
        where.append("study_id = ?")
        params.append(int(study_id))
    with _tx() as conn:
        row = conn.execute("SELECT id FROM endgame_tasks WHERE " + " AND ".join(where)
                           + " ORDER BY priority DESC, created_at ASC, id ASC LIMIT 1",
                           params).fetchone()
        if row is None:
            return None
        cur = conn.execute(
            "UPDATE endgame_tasks SET status = 'running', started_at = ? "
            "WHERE id = ? AND status = 'queued'", (_now(), row["id"]))
        if cur.rowcount <= 0:
            return None
        claimed = conn.execute("SELECT * FROM endgame_tasks WHERE id = ?",
                               (row["id"],)).fetchone()
        return dict(claimed) if claimed else None


def list_tasks(*, game_id=None, study_id=None, status=None, statuses=None,
               limit=50) -> list[dict]:
    """列任务, 新的在前"""
    where, params = [], []
    if game_id is not None:
        where.append("game_id = ?")
        params.append(int(game_id))
    if study_id is not None:
        where.append("study_id = ?")
        params.append(int(study_id))
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if statuses:
        where.append("status IN (" + ", ".join("?" * len(statuses)) + ")")
        params.extend(statuses)
    sql = ("SELECT * FROM endgame_tasks"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY created_at DESC, rowid DESC LIMIT ?")
    with _tx() as conn:
        rows = conn.execute(sql, [*params, max(1, min(_int_or(limit, 50), 500))]).fetchall()
        return [dict(r) for r in rows]


def count_active_tasks(*, study_id=None) -> int:
    """在跑的任务数 (并发上限的判据: queued/running/aggregating)"""
    where = ["status IN ('queued', 'running', 'aggregating')"]
    params = []
    if study_id is not None:
        where.append("study_id = ?")
        params.append(int(study_id))
    with _tx() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM endgame_tasks WHERE "
                           + " AND ".join(where), params).fetchone()
        return int(row["n"])


def supersede_tasks(game_id, from_ply=0) -> dict:
    """撤销到某个 ply 之后: 已跑完的分析标 superseded, 排队中的标 cancelled

    不删除任何任务和结果 —— 规格要求保留审计痕迹。正在跑的任务要由 tasks.py
    先发取消信号, 这里只负责把状态落下来。
    :return: {"superseded": n, "cancelled": n}
    """
    with _tx() as conn:
        superseded = conn.execute(
            "UPDATE endgame_tasks SET status = 'superseded', finished_at = ? "
            "WHERE game_id = ? AND ply > ? AND status = 'completed'",
            (_now(), int(game_id), int(from_ply))).rowcount
        cancelled = conn.execute(
            "UPDATE endgame_tasks SET status = 'cancelled', finished_at = ? "
            "WHERE game_id = ? AND ply > ? AND status IN ('draft', 'queued')",
            (_now(), int(game_id), int(from_ply))).rowcount
        return {"superseded": superseded, "cancelled": cancelled}
