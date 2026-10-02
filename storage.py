# -*- coding: utf-8 -*-
"""对局存储层: 只用标准库 sqlite3, 不引入任何 ORM / 第三方依赖。

数据库默认落在 <项目目录>/data/chess.db (目录不存在会自动创建),
可用环境变量 XQ_DB_PATH 覆盖(测试指向临时目录时用)。

两张表:
    games  —— 一局棋的元信息 + 当前局面
    moves  —— 每局按 ply 递增的着法记录, UNIQUE(game_id, ply)

约定:
    所有查询一律返回普通 dict (sqlite3.Row -> dict), 查不到就是 None / []。
    时间统一用本地时间字符串 "YYYY-MM-DD HH:MM:SS"。
    写操作用 tx() 包一层: 正常提交, 抛异常回滚, 最后一定关连接。
    分支(branch_of_move_id)这一轮只建列, 主线一律写 NULL。
"""

import contextlib
import datetime
import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "chess.db"
# 生效的数据库路径: 有环境变量就用环境变量(测试常见做法), 否则用项目里的 data/chess.db
DB_PATH = Path(os.environ["XQ_DB_PATH"]) if os.environ.get("XQ_DB_PATH") else DEFAULT_DB_PATH

CATEGORIES = ("game", "opening", "endgame", "study", "imported")
RESULTS = ("1-0", "0-1", "1/2-1/2", "*")
STATUSES = ("active", "finished", "archived")

# 建表 / 建索引 DDL (字段名与设计文档保持一致)
SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT,
  category      TEXT DEFAULT 'game',
  start_fen     TEXT,
  current_fen   TEXT,
  side_to_move  TEXT,
  result        TEXT DEFAULT '*',
  status        TEXT DEFAULT 'active',
  red_name      TEXT,
  black_name    TEXT,
  event         TEXT,
  site          TEXT,
  date          TEXT,
  round         TEXT,
  opening       TEXT,
  ecco          TEXT,
  tags          TEXT,
  note          TEXT,
  created_at    TEXT,
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS moves (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id            INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  ply                INTEGER NOT NULL,
  side               TEXT,
  iccs               TEXT,
  chinese            TEXT,
  from_sq            TEXT,
  to_sq              TEXT,
  captured           TEXT,
  fen_before         TEXT,
  fen_after          TEXT,
  score_cp           INTEGER,
  score_mate         INTEGER,
  eval_by            TEXT,
  comment            TEXT,
  is_key             INTEGER DEFAULT 0,
  branch_of_move_id  INTEGER,
  UNIQUE (game_id, ply)
);
CREATE INDEX IF NOT EXISTS idx_moves_game_ply ON moves(game_id, ply);
CREATE INDEX IF NOT EXISTS idx_games_category_updated ON games(category, updated_at);
CREATE INDEX IF NOT EXISTS idx_games_tags ON games(tags);
"""

# create_game 允许写入的列 (白名单: 列名拼 SQL 时只可能来自这里)
GAME_FIELDS = (
    "name", "category", "start_fen", "current_fen", "side_to_move", "result", "status",
    "red_name", "black_name", "event", "site", "date", "round", "opening", "ecco",
    "tags", "note", "created_at", "updated_at",
)

# update_game 允许改的列: id 不可改, created_at 不可改, updated_at 由存储层自己刷
GAME_UPDATE_FIELDS = (
    "name", "category", "start_fen", "current_fen", "side_to_move", "result", "status",
    "red_name", "black_name", "event", "site", "date", "round", "opening", "ecco",
    "tags", "note",
)

# moves 允许写入/修改的列: id 与 game_id 不在其中, 保证不能改掉着法的归属
MOVE_FIELDS = (
    "ply", "side", "iccs", "chinese", "from_sq", "to_sq", "captured", "fen_before",
    "fen_after", "score_cp", "score_mate", "eval_by", "comment", "is_key",
    "branch_of_move_id",
)

# keyword 模糊匹配要覆盖的列
KEYWORD_COLUMNS = ("name", "tags", "note", "opening", "red_name", "black_name")

# list_games 用的查询主体: 顺带算出步数与最后一步中文着法(不返回 moves 数组)
_GAME_LIST_SQL = """
SELECT g.*,
       (SELECT COUNT(*) FROM moves m WHERE m.game_id = g.id) AS move_count,
       (SELECT m.chinese FROM moves m WHERE m.game_id = g.id
        ORDER BY m.ply DESC LIMIT 1) AS last_chinese
FROM games g
"""


# ----------------------------------------------------------------------
# 连接 / 时间等小工具
# ----------------------------------------------------------------------
def _now() -> str:
    """当前本地时间字符串 "YYYY-MM-DD HH:MM:SS" """
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _db_path() -> Path:
    """当前生效的数据库文件路径 (环境变量优先, 方便测试整体切到临时目录)"""
    override = os.environ.get("XQ_DB_PATH")
    return Path(override) if override else DB_PATH


def get_conn() -> sqlite3.Connection:
    """每次调用返回一个新连接: row_factory=sqlite3.Row, PRAGMA foreign_keys=ON

    data/ 目录不存在会自动创建。级联删除靠这个 PRAGMA, 所以连接一建出来就开。
    """
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """幂等建表建索引, 启动时调一次"""
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def tx():
    """事务上下文: 正常提交, 抛异常回滚, 最后一定关连接"""
    conn = get_conn()
    try:
        with conn:      # 连接上下文只管 commit/rollback, 关连接还得自己来
            yield conn
    finally:
        conn.close()


def _side_from_fen(fen) -> str:
    """从 FEN 第二个字段推断走子方, 缺失/非法时按 'w'"""
    parts = (fen or "").split()
    return "b" if len(parts) > 1 and parts[1].lower().startswith("b") else "w"


def _like(value) -> str:
    """把用户输入包成 LIKE 的 %...% 模式, 顺手转义 % 与 _"""
    text = (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{text}%"


def _pick(fields: dict, allowed) -> dict:
    """按白名单挑出要写库的列, 白名单外的键直接丢掉(防注入)"""
    return {k: v for k, v in (fields or {}).items() if k in allowed}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    """照 values 的键拼一条 INSERT, 返回新行 id (列名来自白名单常量, 不会有注入)"""
    cols = ", ".join(values)
    marks = ", ".join("?" * len(values))
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
    return int(cur.lastrowid)


# ----------------------------------------------------------------------
# games
# ----------------------------------------------------------------------
def create_game(fields: dict) -> int:
    """新建对局, 返回新 id

    fields 里没给的列用 DDL 默认值 / NULL; created_at / updated_at 自动补当前时间;
    current_fen 为空时用 start_fen 兜底; side_to_move 为空时从 start_fen 推断。
    """
    values = _pick(fields, GAME_FIELDS)
    now = _now()
    if not values.get("created_at"):
        values["created_at"] = now
    if not values.get("updated_at"):
        values["updated_at"] = now
    if not values.get("current_fen"):
        values["current_fen"] = values.get("start_fen")      # 当前局面兜底为起始局面
    if not values.get("side_to_move"):
        values["side_to_move"] = _side_from_fen(values.get("start_fen"))

    with tx() as conn:
        return _insert(conn, "games", values)


def get_game_meta(game_id: int) -> dict | None:
    """取一局的元信息(不含 moves), 对局不存在返回 None"""
    with tx() as conn:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return dict(row) if row else None


def get_game(game_id: int) -> dict | None:
    """meta + moves 列表(按 ply 升序); 对局不存在返回 None"""
    with tx() as conn:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if row is None:
            return None
        moves = conn.execute(
            "SELECT * FROM moves WHERE game_id = ? ORDER BY ply ASC", (game_id,)
        ).fetchall()
        return {"game": dict(row), "moves": [dict(m) for m in moves]}


def update_game(game_id: int, fields: dict) -> bool:
    """只更新 fields 里出现的列; 白名单外的键直接忽略(防注入)

    created_at / id 改不到, updated_at 每次都由存储层自己刷成当前时间。
    返回是否有行被改(行不存在就是 False)。fields 里没有可改的列时, 也会刷一次 updated_at,
    所以这种情况返回 True —— "改到了行" 以 SQLite 的 rowcount 为准。
    """
    values = _pick(fields, GAME_UPDATE_FIELDS)
    values["updated_at"] = _now()
    assigns = ", ".join(f"{k} = ?" for k in values)
    with tx() as conn:
        cur = conn.execute(f"UPDATE games SET {assigns} WHERE id = ?",
                           [*values.values(), game_id])
        return cur.rowcount > 0


def delete_game(game_id: int) -> bool:
    """删对局并级联删 moves(靠连接上的 PRAGMA foreign_keys=ON), 返回是否真删掉了"""
    with tx() as conn:
        cur = conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
        return cur.rowcount > 0


def _game_filters(category=None, tag=None, keyword=None) -> tuple:
    """拼 list_games / count_games 共用的 WHERE 子句与参数, 返回 (where_sql, params)"""
    where, params = [], []
    if category:
        where.append("g.category = ?")
        params.append(category)
    if tag:
        # tags 是逗号分隔的字符串, 子串匹配就够了("中炮" 要能命中 "中炮,残局")
        where.append("g.tags LIKE ? ESCAPE '\\'")
        params.append(_like(tag))
    if keyword:
        like = _like(keyword)
        where.append("(" + " OR ".join(f"g.{col} LIKE ? ESCAPE '\\'" for col in KEYWORD_COLUMNS)
                     + ")")
        params.extend([like] * len(KEYWORD_COLUMNS))
    return (" WHERE " + " AND ".join(where) if where else ""), params


def _int_or(value, default: int) -> int:
    """能转 int 就用它, 否则用默认值(前端参数可能是字符串或空)"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def list_games(category=None, tag=None, keyword=None, limit=50, offset=0) -> list[dict]:
    """列对局(不含 moves 数组), 按 updated_at 倒序

    每行额外带 move_count(步数) 与 last_chinese(最后一步中文着法, 没步法时为 None)。
    category 精确匹配; tag 在 tags 里子串匹配; keyword 在 KEYWORD_COLUMNS 里模糊匹配。
    limit 夹到 [1, 200], offset 不小于 0。
    """
    limit = max(1, min(_int_or(limit, 50), 200))
    offset = max(0, _int_or(offset, 0))
    where, params = _game_filters(category, tag, keyword)
    sql = (_GAME_LIST_SQL + where
           + " ORDER BY g.updated_at DESC, g.id DESC LIMIT ? OFFSET ?")
    with tx() as conn:
        rows = conn.execute(sql, [*params, limit, offset]).fetchall()
        return [dict(r) for r in rows]


def count_games(category=None, tag=None, keyword=None) -> int:
    """过滤条件下的对局总数(list_games 的分页配套)"""
    where, params = _game_filters(category, tag, keyword)
    with tx() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM games g" + where, params).fetchone()
        return int(row["n"])


# ----------------------------------------------------------------------
# moves
# ----------------------------------------------------------------------
def append_move(game_id: int, move: dict) -> int:
    """追加一步, 返回新 move 的 id

    ply 没传就取 max(ply)+1 (从 1 开始); 同一个事务里把对局推进到这一步之后:
    current_fen = move["fen_after"], side_to_move = 下一步该谁走, 并刷新 updated_at。
    下一步走子方优先用 move["next_side"], 没传就从 fen_after 的第二个字段推断。
    对局不存在抛 ValueError。
    """
    move = move or {}
    values = _pick(move, MOVE_FIELDS)
    values["game_id"] = game_id

    with tx() as conn:
        row = conn.execute("SELECT 1 FROM games WHERE id = ?", (game_id,)).fetchone()
        if row is None:
            raise ValueError(f"对局不存在: id={game_id}")
        if not values.get("ply"):
            row = conn.execute("SELECT MAX(ply) AS max_ply FROM moves WHERE game_id = ?",
                               (game_id,)).fetchone()
            values["ply"] = (row["max_ply"] or 0) + 1

        move_id = _insert(conn, "moves", values)

        fen_after = values.get("fen_after")
        next_side = move.get("next_side") or (_side_from_fen(fen_after) if fen_after else None)
        conn.execute(
            "UPDATE games SET current_fen = COALESCE(?, current_fen),"
            "                 side_to_move = COALESCE(?, side_to_move),"
            "                 updated_at = ?"
            " WHERE id = ?",
            (fen_after, next_side, _now(), game_id),
        )
        return move_id


def list_moves(game_id: int) -> list[dict]:
    """取一局的全部着法, 按 ply 升序; 没步法返回 []"""
    with tx() as conn:
        rows = conn.execute("SELECT * FROM moves WHERE game_id = ? ORDER BY ply ASC",
                            (game_id,)).fetchall()
        return [dict(r) for r in rows]


def get_move(move_id: int) -> dict | None:
    """取单步着法, 不存在返回 None"""
    with tx() as conn:
        row = conn.execute("SELECT * FROM moves WHERE id = ?", (move_id,)).fetchone()
        return dict(row) if row else None


def update_move(move_id: int, fields: dict) -> bool:
    """只更新白名单里的列; 白名单外的键直接忽略(防注入)

    moves 表没有 updated_at, 所以改完顺带刷新所属对局的 updated_at。
    返回是否有行被改(着法不存在就是 False)。
    """
    values = _pick(fields, MOVE_FIELDS)
    with tx() as conn:
        row = conn.execute("SELECT game_id FROM moves WHERE id = ?", (move_id,)).fetchone()
        if row is None:
            return False
        if values:
            assigns = ", ".join(f"{k} = ?" for k in values)
            cur = conn.execute(f"UPDATE moves SET {assigns} WHERE id = ?",
                               [*values.values(), move_id])
            if cur.rowcount <= 0:
                return False
        conn.execute("UPDATE games SET updated_at = ? WHERE id = ?", (_now(), row["game_id"]))
        return True


def delete_moves_after(game_id: int, ply: int) -> int:
    """删掉 ply > 给定值的所有步(重存整局时截断用), 返回删除条数"""
    with tx() as conn:
        cur = conn.execute("DELETE FROM moves WHERE game_id = ? AND ply > ?", (game_id, ply))
        return cur.rowcount


def count_moves(game_id: int) -> int:
    """一局的步数"""
    with tx() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM moves WHERE game_id = ?",
                           (game_id,)).fetchone()
        return int(row["n"])


def set_game_note(game_id: int, note: str) -> bool:
    """只改对局备注, 返回是否改到了行"""
    with tx() as conn:
        cur = conn.execute("UPDATE games SET note = ?, updated_at = ? WHERE id = ?",
                           (note, _now(), game_id))
        return cur.rowcount > 0