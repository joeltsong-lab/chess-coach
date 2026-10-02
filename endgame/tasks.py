# -*- coding: utf-8 -*-
"""残局分析的任务调度: 单写者队列 + 引擎池 + 取消/超时。

规格里的性能默认, 逐条落在这里:
    * 常驻 1 个引擎: quick / root_moves 用常驻引擎(也是网页下棋用的那个),
      同一个实例上的搜索由 chess_engine 的 _search_lock 串行化, 不会互相偷输出;
    * 精确并发 2: standard / precise 最多 2 个专用引擎进程同时算;
    * 用户并发上限 3: 同一研究同时最多 3 个任务在跑;
    * 超时大于 movetime 的 120%: 交给 analyzer.resolve_config 算;
    * 崩溃保留原因: EngineError 一律落成 failed + error_code/message, 绝不自动 completed。

"单写者"的落地方式: 搜索(贵)并发跑, 数据库写(便宜)走同一把写锁串行。
这样既满足并发上限, 又不会让两个线程同时改同一张候选表。

状态机: draft -> queued -> running -> aggregating -> completed; 失败/取消是 failed/cancelled。
状态的合法性由 store.update_task() 把关, 这里只负责在正确的时机调用它。
"""

import contextlib
import json
import threading
import time

import storage
from chess_engine import START_FEN, ChessEngine, EngineError

from . import analyzer, fen_rules, store

# 各档位用哪种引擎槽位
PRECISE_MODES = ("standard", "precise")
QUICK_MODES = ("quick", "root_moves")

DEFAULT_WORKERS = 3
DEFAULT_MAX_PER_STUDY = 3
DEFAULT_POLL_INTERVAL = 0.2
# 进度入库的节流间隔: 引擎每秒能刷上百条 info, 全写库会把盘写爆
PROGRESS_MIN_INTERVAL = 0.5


class TaskError(RuntimeError):
    """提交/取消任务时的业务错误 (路由层转 400 或 409)"""


class TaskLimitError(TaskError):
    """并发上限已满"""


# 数据库写锁: 任务状态、分析结果、进度都从这里过, 保证同一时刻只有一个写者
_WRITE_LOCK = threading.RLock()


def _write(func, *args, **kwargs):
    """在写锁里调用 store 的写操作"""
    with _WRITE_LOCK:
        return func(*args, **kwargs)


# ----------------------------------------------------------------------
# 引擎池
# ----------------------------------------------------------------------
class EnginePool:
    """引擎进程池 (进程级共享, 由 app.py 建一个)

    :param resident: 常驻引擎(网页下棋用的那个); 不给就自己起一个
    :param resident_factory: 常驻引擎的创建方式; 网页里传 get_engine, 快分析就直接
        复用网页那一个进程(同一个实例上的搜索由 chess_engine 的 _search_lock 串行化)
    :param quick_limit: 常驻引擎上同时允许几个快分析
    :param precise_limit: 允许几个专用引擎同时跑精确分析
    """

    def __init__(self, factory=None, resident=None, quick_limit=1, precise_limit=2,
                 resident_factory=None):
        self._factory = factory or (lambda: ChessEngine())
        self._resident_factory = resident_factory or self._factory
        self._resident = resident
        self._quick = threading.BoundedSemaphore(max(1, int(quick_limit)))
        self._precise = threading.BoundedSemaphore(max(1, int(precise_limit)))
        self._idle = []          # 空闲的专用引擎, 复用而不是每次重启进程
        self._engines = []       # 起过的全部引擎, shutdown 用
        self._lock = threading.Lock()
        self._closed = False

    # -- 槽位 --
    @contextlib.contextmanager
    def slot(self, mode):
        """取一个引擎槽位; 满了就阻塞等待 (等的是"另一台算完", 不是忙等)"""
        precise = mode in PRECISE_MODES
        semaphore = self._precise if precise else self._quick
        if self._closed:
            raise TaskError("引擎池已关闭, 不再接受新任务")
        semaphore.acquire()
        engine = None
        try:
            engine = self._take(precise)
            yield engine
        finally:
            if engine is not None:
                self._give_back(engine, precise)
            semaphore.release()

    def _take(self, precise):
        if not precise:
            with self._lock:
                if self._resident is None:
                    self._resident = self._resident_factory()
                    self._engines.append(self._resident)
                return self._resident
        with self._lock:
            if self._idle:
                return self._idle.pop()
            engine = self._factory()
            self._engines.append(engine)
            return engine

    def _give_back(self, engine, precise):
        if not precise:
            return                    # 常驻引擎一直在池子里
        with self._lock:
            if self._closed:
                return
            self._idle.append(engine)

    def drop(self, engine):
        """某个引擎崩了: 从池子里摘掉并回收进程, 下次会新建一个

        注意调用时机: 要在 slot 的 with 块结束之后调, 否则 finally 里的
        _give_back 会把这个已经作废的引擎又放回空闲表。
        """
        with self._lock:
            if engine is self._resident:
                self._resident = None       # 常驻引擎崩了也得换新的, 不然一直拿到死进程
            if engine in self._idle:
                self._idle.remove(engine)
            if engine in self._engines:
                self._engines.remove(engine)
        with contextlib.suppress(Exception):
            engine.quit()

    def shutdown(self):
        """回收所有专用引擎; 常驻引擎留给网页继续用, 不在这里 quit"""
        with self._lock:
            self._closed = True
            engines, self._idle = list(self._engines), []
            self._engines = []
        for engine in engines:
            if engine is self._resident:
                continue
            with contextlib.suppress(Exception):
                engine.quit()

    def stats(self):
        """给接口回显用: 池子里有几个进程、几个空的"""
        with self._lock:
            return {"resident": self._resident is not None,
                    "engines": len(self._engines), "idle": len(self._idle),
                    "precise_idle": self._precise._value,   # noqa: SLF001 - 只为展示
                    "quick_idle": self._quick._value}       # noqa: SLF001

    def resident_info(self):
        """常驻引擎报出来的版本信息; 没起来就返回 None

        只为接口回显 —— 不主动起进程(否则一个 GET 就会拉起一个引擎)。
        """
        with self._lock:
            engine = self._resident
        if engine is None:
            return None
        with contextlib.suppress(Exception):
            return engine.version_info()
        return None


# ----------------------------------------------------------------------
# 进度节流
# ----------------------------------------------------------------------
class _ProgressReporter:
    """把引擎的 info 流按节流写进 endgame_tasks.progress_json / partial_best"""

    def __init__(self, task_id, min_interval=PROGRESS_MIN_INTERVAL):
        self.task_id = task_id
        self.min_interval = float(min_interval)
        self.snapshots = 0
        self._last_at = 0.0
        self._last_depth = -1

    def __call__(self, info):
        self.snapshots += 1
        depth = int(info.get("depth") or 0)
        now = time.time()
        if (now - self._last_at) < self.min_interval and depth <= self._last_depth:
            return                       # 节流: 深度没涨又没到间隔, 不写库
        self._last_at = now
        self._last_depth = depth
        progress = {"depth": info.get("depth"), "seldepth": info.get("seldepth"),
                    "multipv": info.get("multipv"), "score": info.get("score"),
                    "mate": info.get("mate"), "nodes": info.get("nodes"),
                    "nps": info.get("nps"), "timeMs": info.get("time_ms"),
                    "pv": list(info.get("pv") or [])[:8]}
        best = (info.get("pv") or [None])[0]
        with contextlib.suppress(Exception):
            _write(store.set_task_progress, self.task_id, progress=progress,
                   partial_best=best)


# ----------------------------------------------------------------------
# 任务运行器
# ----------------------------------------------------------------------
class TaskRunner:
    """后台分析任务队列

    典型用法::

        runner = TaskRunner(EnginePool(resident=app_engine))
        runner.start()
        task = runner.submit({...})          # 校验 + 建任务 + 排队
        runner.cancel(task["id"])
        runner.stop()
    """

    def __init__(self, pool=None, *, workers=DEFAULT_WORKERS,
                 max_per_study=DEFAULT_MAX_PER_STUDY, poll_interval=DEFAULT_POLL_INTERVAL):
        self.pool = pool or EnginePool()
        self.workers = max(1, int(workers))
        self.max_per_study = max(1, int(max_per_study))
        self.poll_interval = float(poll_interval)
        self._queue = []
        self._queue_cond = threading.Condition()
        self._threads = []
        self._stop = threading.Event()
        self._cancels = {}
        self._cancels_lock = threading.Lock()
        self._running = False

    # -- 生命周期 --
    def start(self):
        if self._running:
            return
        self._running = True
        self._stop.clear()
        for index in range(self.workers):
            thread = threading.Thread(target=self._worker, name=f"endgame-worker-{index}",
                                      daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout=5.0):
        self._running = False
        self._stop.set()
        with self._queue_cond:
            self._queue_cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        self.pool.shutdown()

    # -- 对外: 提交 / 取消 / 查询 --
    def resolve(self, request: dict) -> dict:
        """校验一个分析请求, 返回排进队列所需的全部信息

        :param request: {studyId/gameId, ply, fen, mode, depth, movetimeMs, multipv,
                         searchMoves, ruleOptions, priority}
        :raise TaskError: 参数不成立 / 局面不合法
        """
        game_id = request.get("gameId") or request.get("studyId")
        if not game_id:
            raise TaskError("必须给 gameId (分析结果要挂在某一局/研究下)")
        ply = int(request.get("ply") or 0)

        mode = request.get("mode") or "standard"
        try:
            config = analyzer.resolve_config(
                mode, depth=request.get("depth"), movetime_ms=request.get("movetimeMs"),
                multipv=request.get("multipv"), searchmoves=request.get("searchMoves"),
                rule_options=request.get("ruleOptions"))

            fen, start_fen, moves = self.position_at(game_id, ply, fen=request.get("fen"))
            check = fen_rules.validate_position(fen)
            if not check["legal"]:
                detail = "; ".join(item["message"] for item in check["errors"])
                raise TaskError(f"局面不合法, 不能分析: {detail}")
            # 重放一遍走子历史: 顺带确认 fen 与 moves 说的是同一盘棋
            analyzer.build_move_records(fen=check["fen"], start_fen=start_fen, moves=moves)
        except analyzer.AnalyzerError as e:
            # 档位/规则开关/着法历史不成立: 都算"请求不对", 路由层统一转 400
            raise TaskError(str(e)) from e

        return {"game_id": int(game_id), "ply": ply, "fen": check["fen"],
                "position_hash": check["position_hash"], "start_fen": start_fen,
                "moves": moves, "config": config}

    @staticmethod
    def _task_fields(request: dict, resolved: dict) -> dict:
        """请求 + 解析结果 -> endgame_tasks 的一行 (引擎用什么参数算的, 全记下来)"""
        config = resolved["config"]
        return {
            "study_id": resolved["game_id"], "game_id": resolved["game_id"],
            "ply": resolved["ply"], "fen": resolved["fen"],
            "position_hash": resolved["position_hash"],
            "mode": config["mode"], "depth": config["depth"],
            "movetime_ms": config["movetime_ms"], "multipv": config["multipv"],
            "search_moves": list(config["searchmoves"]),
            "rule_options": config["rule_options"],
            "repetition_rule": config["rule_options"]["repetition_rule"],
            "priority": int(request.get("priority") or 0),
        }

    def prepare(self, request: dict) -> dict:
        """只建"位置节点"(draft 任务), 先不排队 —— 给 POST /positions 用

        研究树里的一个节点就是一个 draft 任务: 参数和局面都校验过了, 用户看好了
        再点分析(见 submit 的 task_id 参数)才真占引擎。
        """
        resolved = self.resolve(request)
        task_id = _write(store.create_task, self._task_fields(request, resolved))
        return store.get_task(task_id)

    def _check_limit(self, study_id) -> None:
        if self.max_per_study and store.count_active_tasks(study_id=study_id) >= self.max_per_study:
            raise TaskLimitError(f"同一研究的并发分析上限是 {self.max_per_study} 个, "
                                 "请等前面算完再提交")

    def submit(self, request: dict, task_id: str | None = None) -> dict:
        """校验请求并排进队列, 返回任务行

        :param request: 见 resolve()
        :param task_id: 复用一个已有的 draft 位置节点(研究树里的节点直接开算)
        :raise TaskError: 参数不成立 / 局面不合法 / 节点状态不对
        :raise TaskLimitError: 同一研究的并发任务已满
        """
        resolved = self.resolve(request)
        self._check_limit(resolved["game_id"])

        if task_id:
            node = store.get_task(task_id)
            if node is None:
                raise TaskError(f"位置节点不存在: {task_id}")
            if node["status"] != "draft":
                raise TaskError(f"位置节点已经是 {node['status']} 状态, 不能再入队")
            if (node["game_id"] != resolved["game_id"] or node["ply"] != resolved["ply"]
                    or node["fen"] != resolved["fen"]):
                raise TaskError("请求里的局面与位置节点对不上, 不能复用它")
            fields = self._task_fields(request, resolved)
            fields.pop("game_id", None)     # 节点归属不可改
            _write(store.update_task, task_id, fields)
        else:
            task_id = _write(store.create_task, self._task_fields(request, resolved))

        _write(store.update_task, task_id, {"status": "queued"})
        with self._queue_cond:
            self._queue.append(task_id)
            self._queue_cond.notify()
        return store.get_task(task_id)

    def cancel(self, task_id: str) -> dict:
        """取消任务: 排队中的直接标 cancelled, 正在跑的立刻发 stop

        取消是异步的 —— 引擎要等这一轮 stop 完才回 bestmove, 所以这里返回
        "cancelling", 调用方轮询状态即可。已经被取消/失败/完成的任务原样返回。
        """
        task = store.get_task(task_id)
        if task is None:
            raise TaskError(f"任务不存在: {task_id}")
        status = task["status"]
        if status in store.TASK_FINAL_STATUSES:
            return task
        if status in ("draft", "queued"):
            return _write(store.update_task, task_id, {"status": "cancelled"}) or task

        with self._cancels_lock:
            event = self._cancels.get(task_id)
        if event is not None:
            event.set()                  # 搜索循环会立刻发 stop
        updated = _write(store.update_task, task_id,
                         {"message": "已请求取消, 等待引擎返回"})
        return updated or task

    def status(self, task_id: str) -> dict:
        """任务状态 + 进度 (给 GET /analysis/{taskId} 用)"""
        task = store.get_task(task_id)
        if task is None:
            raise TaskError(f"任务不存在: {task_id}")
        progress = None
        if task.get("progress_json"):
            with contextlib.suppress(ValueError, TypeError):
                progress = json.loads(task["progress_json"])
        position = None
        if task.get("position_id"):
            position = store.get_position(task["position_id"])
            if position:
                position["candidates"] = store.list_candidates(position["id"])
        return {"taskId": task["id"], "status": task["status"],
                "mode": task["mode"], "ply": task["ply"], "depth": task["depth"],
                "movetimeMs": task["movetime_ms"], "multipv": task["multipv"],
                "progress": progress, "partialBest": task["partial_best"],
                "errorCode": task["error_code"], "message": task["message"],
                "engineVersion": task["engine_version"], "nnueFile": task["nnue_file"],
                "positionId": task["position_id"], "position": position,
                "createdAt": task["created_at"], "startedAt": task["started_at"],
                "finishedAt": task["finished_at"]}

    # -- 内部: 局面定位 --
    @staticmethod
    def position_at(game_id, ply, fen=None) -> tuple:
        """取 (当前局面 FEN, 起始局面, 0..ply 的完整着法串)

        "从起始局面到当前 ply 的完整 UCI moves" 就从 moves 表里来 —— 引擎要靠它
        才能正确判断重复局面, 也顺便保证分析的局面和复盘看到的是同一个。
        """
        game = storage.get_game_meta(game_id)
        if game is None:
            raise TaskError(f"对局/研究不存在: id={game_id}")
        start_fen = game.get("start_fen") or START_FEN
        rows = [m for m in storage.list_moves(game_id) if (m.get("iccs") or "").strip()]
        history = [m for m in rows if int(m.get("ply") or 0) <= int(ply)]
        moves = [m["iccs"] for m in history]
        if ply <= 0:
            return fen or start_fen, start_fen, moves
        last = history[-1] if history else None
        current = (last or {}).get("fen_after") or game.get("current_fen") or fen
        if not current:
            raise TaskError(f"第 {ply} 着之后的局面不存在, 无法分析")
        return current, start_fen, moves

    # -- 内部: 队列消费 --
    def _worker(self):
        while not self._stop.is_set():
            task_id = self._take_task_id()
            if task_id is None:
                self._stop.wait(self.poll_interval)
                continue
            task = store.get_task(task_id)
            if task is None or task["status"] != "queued":
                continue                 # 已被取消或被别的 worker 抢走
            self._run(task)

    def _take_task_id(self):
        with self._queue_cond:
            return self._queue.pop(0) if self._queue else None

    def _transition(self, task_id, status, **fields):
        """状态迁移 (状态机不通过时返回 None, 不抛给 worker 之外的调用方)"""
        try:
            return _write(store.update_task, task_id, {"status": status, **fields})
        except ValueError:
            return None

    def _run(self, task):
        task_id = task["id"]
        cancel = threading.Event()
        with self._cancels_lock:
            self._cancels[task_id] = cancel
        # 队列里排到之后才占坑: 占坑失败说明状态被人改过(取消), 直接放弃
        if self._transition(task_id, "running") is None:
            with self._cancels_lock:
                self._cancels.pop(task_id, None)
            return

        reporter = _ProgressReporter(task_id)
        engine = None
        try:
            fen, start_fen, moves = self.position_at(task["game_id"], task["ply"],
                                                     fen=task["fen"])
            rule_options = json.loads(task["rule_options"]) if task["rule_options"] else None
            searchmoves = ([m for m in (task["search_moves"] or "").split(",") if m]
                           if task["search_moves"] else None)
            with self.pool.slot(task["mode"]) as slot_engine:
                engine = slot_engine
                result = analyzer.analyze_position(
                    engine, fen=fen, start_fen=start_fen, moves=moves,
                    mode=task["mode"], depth=task["depth"],
                    movetime_ms=task["movetime_ms"], multipv=task["multipv"],
                    searchmoves=searchmoves, rule_options=rule_options,
                    cancel=cancel, on_progress=reporter)
        except TaskError as e:
            self._fail(task_id, "invalid_position", str(e))
            return
        except analyzer.AnalyzerError as e:
            self._fail(task_id, "invalid_request", str(e))
            return
        except EngineError as e:
            # 引擎崩溃 / 超时: 原因必须留在库里, 绝不能悄悄变成 completed
            if engine is not None:
                self.pool.drop(engine)      # 崩过的进程不能再拿去算下一个局面
            self._fail(task_id, "engine_error", str(e))
            return
        except Exception as e:                          # noqa: BLE001 - 兜底保留原因
            self._fail(task_id, "internal_error", f"{type(e).__name__}: {e}")
            return
        finally:
            with self._cancels_lock:
                self._cancels.pop(task_id, None)

        if result["status"] == "cancelled" or cancel.is_set():
            self._transition(task_id, "cancelled",
                             message="已取消, 结果未落库",
                             engine_version=result["engine_version"],
                             nnue_file=result["nnue_file"])
            return

        self._transition(task_id, "aggregating")
        try:
            saved = _write(store.save_analysis, result, game_id=task["game_id"],
                           ply=task["ply"])
        except Exception as e:                          # noqa: BLE001
            self._fail(task_id, "store_error", f"{type(e).__name__}: {e}")
            return

        summary = " | ".join(result["notes"][:3])
        self._transition(task_id, "completed",
                         position_id=saved["position_id"],
                         engine_version=result["engine_version"],
                         nnue_file=result["nnue_file"],
                         partial_best=result["bestmove"],
                         message=summary or None)

    def _fail(self, task_id, code, message):
        """失败: 记原因; 状态机不允许(比如已被取消)时也把原因留在 message 上"""
        if self._transition(task_id, "failed", error_code=code, message=message) is None:
            with contextlib.suppress(Exception):
                _write(store.set_task_progress, task_id, message=message)

    # -- 便于测试: 排队深度 --
    def pending(self) -> int:
        with self._queue_cond:
            return len(self._queue)
