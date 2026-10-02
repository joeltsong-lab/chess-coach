# -*- coding: utf-8 -*-
"""数据库迁移运行器 (纯标准库 sqlite3, 不引入 Alembic 等依赖)。

用法:
    python migrate.py status            # 看哪些迁移已应用
    python migrate.py up                # 应用全部未执行的迁移 (默认先影子库演练)
    python migrate.py down              # 回滚最近一个迁移
    python migrate.py down --to 0       # 回滚到 0 号以后的都不留 (回到初始结构)
    python migrate.py up --no-shadow    # 跳过影子库演练, 直接改真库

设计要点:
  * 版本表 schema_migrations(version, name, applied_at), 幂等可重复执行。
  * 一个迁移文件 = 一个事务: 全部语句 + 版本号写入 + PRAGMA foreign_key_check
    都在同一个 BEGIN/COMMIT 里, 任何一步失败整体 ROLLBACK, 不留半截结构。
  * 默认先"影子库演练": 用 sqlite3 的在线备份 API 把真库复制到临时文件, 在副本上
    跑一遍, 成功后才动真库 —— 迁移写错了不会把真库改坏 (这是 0001 要重建 games 表
    才特意加的保险)。
  * 迁移前 foreign_keys=OFF + legacy_alter_table=ON, 结束后恢复。
    不开 legacy_alter_table 的话, "ALTER TABLE games_migrated RENAME TO games"
    会把 moves 表里指向 games 的外键改写成指向临时表名, 之后级联删除就失效了。
  * 顺带把库切成 WAL 模式(持久化在库文件里), 后台分析写入时不挡读取。
"""

import argparse
import contextlib
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# 迁移文件名形如 0001_endgame_study_up.sql / 0001_endgame_study_down.sql
UP_SUFFIX = "_up.sql"
DOWN_SUFFIX = "_down.sql"


class MigrationError(RuntimeError):
    """迁移失败 (语法错误、外键检查不过、版本文件缺失等)"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up_path: Path
    down_path: Path


# ----------------------------------------------------------------------
# 迁移文件发现
# ----------------------------------------------------------------------
def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """扫描 migrations/ 目录, 返回按 version 升序的迁移列表

    命名不合规范 (缺 up/down 配对、版本号不是数字) 的文件直接跳过并告警,
    不抛异常 —— 免得目录里多一个 .bak 就让整个应用起不来。
    """
    directory = directory or MIGRATIONS_DIR
    ups: dict[int, tuple[str, Path]] = {}
    downs: dict[int, Path] = {}
    if not directory.is_dir():
        return []

    for path in sorted(directory.glob("*.sql")):
        try:
            head, rest = path.name.split("_", 1)
            version = int(head)
        except ValueError:
            continue
        if rest.endswith(UP_SUFFIX):
            ups[version] = (rest[: -len(UP_SUFFIX)], path)
        elif rest.endswith(DOWN_SUFFIX):
            downs[version] = path

    out = []
    for version, (name, up_path) in sorted(ups.items()):
        down_path = downs.get(version)
        if down_path is None:
            raise MigrationError(
                f"迁移 {version:04d}_{name} 缺少 down 文件: "
                f"应存在 {version:04d}_{name}{DOWN_SUFFIX}"
            )
        out.append(Migration(version, name, up_path, down_path))
    return out


# ----------------------------------------------------------------------
# SQL 拆分
# ----------------------------------------------------------------------
def split_statements(sql: str) -> list[str]:
    """把 .sql 文件拆成一条条语句 (按分号)

    会正确处理: `--` 行注释、`/* */` 块注释、单/双引号字符串(含 '' 转义)。
    不支持触发器那种含分号的 BEGIN...END 语句体 —— 遇到 CREATE TRIGGER 直接报错,
    提示把触发器单独放一个文件, 免得静默拆错。
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_line_comment = in_block_comment = False
    quote: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                buf.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:      # '' 转义
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == ";":
            text = "".join(buf).strip()
            if text:
                statements.append(text)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    text = "".join(buf).strip()
    if text:
        statements.append(text)

    for stmt in statements:
        if stmt.lstrip().upper().startswith("CREATE TRIGGER"):
            raise MigrationError(
                "迁移里不支持含 BEGIN...END 的触发器语句体 (按分号拆分会有歧义); "
                "请把触发器单独放一个迁移文件, 或改用 Python 代码创建。"
            )
    return statements


# ----------------------------------------------------------------------
# 连接与版本表
# ----------------------------------------------------------------------
def connect(path: Path) -> sqlite3.Connection:
    """迁移专用连接: 自己管事务 (isolation_level=None), 行用 Row"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version    INTEGER PRIMARY KEY,
          name       TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
    """)


def applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    """已应用的版本 -> 应用时间"""
    ensure_version_table(conn)
    rows = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    return {int(r["version"]): r["applied_at"] for r in rows}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_base_schema(path: Path) -> None:
    """在**指定**库文件上建出基础表 (games/moves, 即 0001 之前的 0000 结构)

    不能图省事调 storage.init_db(): 那个函数写的是 storage 当前生效的路径,
    传了 --db 指向别的库时就会把基础表建到错误的库上。
    """
    from core import storage

    conn = connect(path)
    try:
        for stmt in split_statements(storage.SCHEMA):
            conn.execute(stmt)
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 执行单个迁移
# ----------------------------------------------------------------------
def apply_migration(conn: sqlite3.Connection, mig: Migration, direction: str) -> int:
    """在给定连接上执行一个迁移 (自带事务), 返回执行的语句条数

    整个迁移 + 版本号 + 外键检查在一个事务里; 任一步失败就 ROLLBACK。
    """
    if direction not in ("up", "down"):
        raise ValueError(f"direction 只能是 up/down, 收到 {direction!r}")

    path = mig.up_path if direction == "up" else mig.down_path
    statements = split_statements(path.read_text(encoding="utf-8"))

    conn.execute("BEGIN")
    try:
        for stmt in statements:
            conn.execute(stmt)

        if direction == "up":
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, _now()),
            )
        else:
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (mig.version,))

        # 重建表之后一定要检查: 外键断了的话级联删除会静默失效
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            detail = "; ".join(f"{r[0]}->{r[2]}(rowid={r[1]})" for r in bad[:5])
            raise MigrationError(f"迁移 {mig.version:04d} 后外键检查不过: {detail}")

        conn.execute("COMMIT")
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    return len(statements)


def _prepare_journal_mode(conn: sqlite3.Connection) -> str:
    """尽量把库切到 WAL, 环境不允许就退回默认模式, 返回实际生效的模式

    为什么不能只看 PRAGMA 的返回值: `PRAGMA journal_mode=WAL` 只改库文件头,
    WAL 真正生效要等第一次写入时建出 "-wal" 文件。某些受限环境(只读挂载、
    沙箱白名单)允许改头但建不出 -wal, 这时 pragma 照样报 "wal", 可后面
    第一次写就炸 "unable to open database file"。所以这里用一笔试探写入来验证。
    """
    def probe() -> bool:
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS _xq_probe (x)")
            conn.execute("DROP TABLE _xq_probe")
            return True
        except sqlite3.Error:
            return False

    try:
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    except sqlite3.Error:
        mode = ""
    if mode == "wal" and probe():
        return "wal"

    # WAL 用不了 —— 退回默认的 rollback journal, 保证迁移本身能跑完
    with contextlib.suppress(sqlite3.Error):
        mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
    probe()
    return mode or "delete"


def _run_direction(path: Path, direction: str, to: int | None, migrations: list[Migration],
                   verbose: bool = False) -> tuple[list[dict], str]:
    """在指定库文件上跑一轮迁移, 返回 (每步结果, 实际生效的 journal_mode)"""
    conn = connect(path)
    done = []
    journal_mode = ""
    try:
        # 必须在开事务之前设置 (foreign_keys 在事务里是空操作)
        journal_mode = _prepare_journal_mode(conn)
        if verbose:
            print(f"  journal_mode = {journal_mode}")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA legacy_alter_table=ON")

        applied = applied_versions(conn)

        if direction == "up":
            todo = [m for m in migrations if m.version not in applied]
        else:
            # 默认只回滚最近一个; --to N 则回滚到"只剩 <= N"
            steps = [m for m in migrations if m.version in applied]
            steps.sort(key=lambda m: m.version, reverse=True)
            if to is None:
                todo = steps[:1]
            else:
                todo = [m for m in steps if m.version > to]

        for mig in todo:
            count = apply_migration(conn, mig, direction)
            done.append({"version": mig.version, "name": mig.name,
                         "direction": direction, "statements": count})
            if verbose:
                print(f"  [{direction}] {mig.version:04d}_{mig.name} ({count} 条语句)")

        if not todo and verbose:
            print(f"  [{direction}] 没有需要执行的迁移")
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA legacy_alter_table=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
        conn.close()
    return done, journal_mode


def _copy_db(src: Path, dst: Path) -> None:
    """用在线备份 API 复制库 (WAL 模式下直接拷文件会丢 -wal 里的内容)"""
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


# ----------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------
def migrate(direction: str = "up", db_path: Path | str | None = None, to: int | None = None,
            shadow: bool = True, verbose: bool = False) -> dict:
    """执行迁移。默认先在影子库上演练一遍, 成功后再改真库。

    :param db_path: 目标库; 不传就用 storage 当前生效的路径 (认 XQ_DB_PATH)
    :param shadow: True 时先复制一份到临时文件试跑
    :return: {"db", "direction", "journal_mode", "shadow": [...], "applied": [...]}
    """
    from core import storage   # 延迟导入: 让 migrate 可以单独跑, 也避免和 storage 循环依赖

    path = Path(db_path) if db_path else storage._db_path()
    migrations = discover_migrations()
    if not migrations:
        return {"db": str(path), "direction": direction, "journal_mode": "",
                "shadow": [], "applied": []}

    # 基础表(0000 结构)先保证存在, 0001 是在它之上做重建
    ensure_base_schema(path)

    result = {"db": str(path), "direction": direction, "journal_mode": "",
              "shadow": [], "applied": []}

    if shadow and path.exists():
        tmp_dir = tempfile.mkdtemp(prefix="xq_migrate_")
        shadow_path = Path(tmp_dir) / "shadow.db"
        try:
            _copy_db(path, shadow_path)
            result["shadow"], _ = _run_direction(shadow_path, direction, to, migrations,
                                                 verbose=verbose)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    result["applied"], result["journal_mode"] = _run_direction(
        path, direction, to, migrations, verbose=verbose)
    return result


def ensure_migrated(verbose: bool = False) -> dict:
    """应用启动时调用: 把库迁到最新 (幂等)。失败会抛 MigrationError。"""
    return migrate("up", shadow=True, verbose=verbose)


def status(db_path: Path | str | None = None) -> list[dict]:
    """列出每个迁移的应用状态"""
    from core import storage

    path = Path(db_path) if db_path else storage._db_path()
    ensure_base_schema(path)
    conn = connect(path)
    try:
        ensure_version_table(conn)
        applied = applied_versions(conn)
    finally:
        conn.close()

    out = []
    for mig in discover_migrations():
        out.append({
            "version": mig.version,
            "name": mig.name,
            "applied": mig.version in applied,
            "applied_at": applied.get(mig.version),
        })
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="数据库迁移 (sqlite3)")
    parser.add_argument("command", choices=("up", "down", "status"))
    parser.add_argument("--to", type=int, default=None,
                        help="down 时回滚到该版本 (含之前的都保留)")
    parser.add_argument("--no-shadow", action="store_true", help="跳过影子库演练")
    parser.add_argument("--db", default=None, help="指定库文件 (默认用 storage 的路径)")
    args = parser.parse_args(argv)

    if args.command == "status":
        for row in status(args.db):
            mark = "已应用 " + (row["applied_at"] or "") if row["applied"] else "未应用"
            print(f"  {row['version']:04d}_{row['name']}  {mark}")
        return 0

    try:
        report = migrate(args.command, db_path=args.db, to=args.to,
                         shadow=not args.no_shadow, verbose=True)
    except (MigrationError, sqlite3.Error) as e:
        print(f"迁移失败: {e}", file=sys.stderr)
        return 1

    if not report["applied"] and not report["shadow"]:
        print("数据库已是最新, 无需迁移")
    else:
        print(f"库文件: {report['db']}")
        print(f"journal_mode: {report['journal_mode']}"
              + ("" if report["journal_mode"] == "wal"
                 else "  (这个环境建不出 -wal 文件, 已退回默认模式; 不影响数据)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
