# -*- coding: utf-8 -*-
"""
Pikafish (皮卡鱼) 中国象棋引擎的极简 UCI 封装。

仅使用 Python 标准库, 通过 subprocess 管道与引擎进程通信。
协议参考 UCI (Universal Chess Interface), Pikafish 直接沿用该协议。
"""

import os
import queue
import subprocess
import threading
import time

from . import PROJECT_ROOT

# ============================================================
# 引擎路径配置 (按需修改, 也可用环境变量覆盖)
# ============================================================
# 默认位置 <项目根>/engine/ (项目根 = core/ 的上一级), 而不是 core/engine/
PROJECT_DIR = str(PROJECT_ROOT)
ENGINE_DIR = os.path.join(PROJECT_DIR, "engine")

# 引擎可执行文件: 默认 engine/pikafish (Windows 下为 engine/pikafish.exe)
DEFAULT_ENGINE_PATH = os.path.join(
    ENGINE_DIR, "pikafish.exe" if os.name == "nt" else "pikafish"
)
# NNUE 权重文件: 默认 engine/pikafish.nnue
DEFAULT_NNUE_PATH = os.path.join(ENGINE_DIR, "pikafish.nnue")

# 环境变量优先级高于上面的默认值
ENGINE_PATH = os.environ.get("PIKAFISH_PATH", DEFAULT_ENGINE_PATH)
NNUE_PATH = os.environ.get("PIKAFISH_NNUE", DEFAULT_NNUE_PATH)

# ============================================================
# 引擎性能参数 (由 configure_engine() 发送, 同样可用环境变量覆盖)
# ============================================================
ENGINE_THREADS = int(os.environ.get("PIKAFISH_THREADS", "4"))
ENGINE_HASH_MB = int(os.environ.get("PIKAFISH_HASH", "256"))
ENGINE_MULTIPV = int(os.environ.get("PIKAFISH_MULTIPV", "3"))

# 默认思考时间 (analyze 没传 movetime 时用)
DEFAULT_MOVETIME = 3000

# 标准开局 FEN (红先)
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"


class EngineError(RuntimeError):
    """引擎启动失败 / 通信失败 / 崩溃时抛出"""


def download_hint(path: str | None = None) -> str:
    """引擎文件缺失时打印的下载指引"""
    target = path or ENGINE_PATH
    exe_name = "pikafish.exe" if os.name == "nt" else "pikafish"
    return "\n".join([
        "未找到 Pikafish 引擎文件, 请先下载:",
        f"  期望路径 : {target}",
        "  下载步骤 :",
        "    1) 打开 https://github.com/official-pikafish/Pikafish/releases",
        "    2) 下载最新 release 的 Assets 里的 Pikafish.<日期>.7z",
        "       (现为通用二进制包, 内含 Windows / Linux / macOS / RISC-V 各平台版本,",
        "        用 7-Zip 解压; 若解压不了, 也可用 Python 的 py7zr 或系统自带 tar 试一下)",
        "    3) 找到对应系统的可执行文件, 复制到 engine/ 目录并重命名:",
        f"       {exe_name}",
        "       (macOS/Linux 还要 chmod +x)",
        "    4) 把随包提供的权重文件 pikafish.nnue 也放到 engine/ 目录",
        "  也可以直接用环境变量指定路径:",
        "    PIKAFISH_PATH / PIKAFISH_NNUE",
    ])


class ChessEngine:
    """Pikafish 引擎进程的轻量封装。

    典型用法::

        engine = ChessEngine()
        result = engine.analyze(START_FEN, movetime=2000)
        engine.quit()
    """

    def __init__(self, engine_path: str | None = None, nnue_path: str | None = None,
                 verbose: bool = False):
        self.engine_path = os.path.abspath(engine_path or ENGINE_PATH)
        self.nnue_path = os.path.abspath(nnue_path or NNUE_PATH)
        self.verbose = verbose

        self.proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        # 同一个进程内串行化搜索: 引擎共用一条 stdout, 两个线程同时搜索会互相偷输出。
        # 需要真并发就多开引擎实例 (见 endgame/tasks.py 的引擎池)。
        self._search_lock = threading.Lock()
        self._nnue_loaded = False
        self._multipv = 1  # 当前 MultiPV 设置, 由 configure_engine() 决定

        # uci 握手时收集到的身份与选项 (search() 之前要确认选项存不存在)
        self.engine_name = ""
        self.engine_author = ""
        self.options: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 进程管理
    # ------------------------------------------------------------------
    def _check_files(self) -> None:
        if not os.path.isfile(self.engine_path):
            raise EngineError(download_hint(self.engine_path))
        if os.name != "nt" and not os.access(self.engine_path, os.X_OK):
            raise EngineError(
                f"引擎文件没有可执行权限: {self.engine_path}\n"
                f"  macOS/Linux 下执行: chmod +x \"{self.engine_path}\""
            )

    def start_engine(self) -> None:
        """启动子进程并完成 uci 初始化 (等待 uciok)"""
        if self.proc is not None and self.proc.poll() is None:
            return  # 已在运行

        self._check_files()
        # cwd 设为引擎所在目录, 引擎会自动加载同目录下的 pikafish.nnue
        cwd = os.path.dirname(self.engine_path) or None
        try:
            self.proc = subprocess.Popen(
                [self.engine_path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=cwd,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            raise EngineError(f"启动引擎失败: {e}\n{download_hint(self.engine_path)}") from e

        self._nnue_loaded = False  # 每个新进程都要重新加载权重
        self.engine_name = ""
        self.engine_author = ""
        self.options = {}
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self._handshake()

    def _handshake(self) -> None:
        """uci 握手: 收集 id name / id author / option 列表, 一直读到 uciok

        顺手把引擎真正支持的选项记下来 —— Pikafish 各版本选项差别很大
        (比如 2026-09-06 版就没有 Repetition Rule / Mate Threat Depth),
        不记录的话后面 setoption 会静默失效, 排查起来很费劲。
        """
        self.send_command("uci")
        deadline = time.time() + 15
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise EngineError("等待引擎 uciok 超时 (15.0s)")
            line = self.read_line(timeout=remain)
            if line.startswith("id name "):
                self.engine_name = line[len("id name "):].strip()
            elif line.startswith("id author "):
                self.engine_author = line[len("id author "):].strip()
            elif line.startswith("option "):
                parsed = self._parse_option(line)
                if parsed:
                    self.options[parsed["name"]] = parsed
            elif line.startswith("uciok"):
                return

    @staticmethod
    def _parse_option(line: str) -> dict | None:
        """解析一行 "option name X type Y default Z [min A] [max B] [var V]..." """
        tokens = line.split()
        if len(tokens) < 4 or tokens[1] != "name":
            return None
        try:
            type_at = tokens.index("type")
        except ValueError:
            return None
        name = " ".join(tokens[2:type_at])
        kind = tokens[type_at + 1]
        out = {"name": name, "type": kind, "default": None, "min": None, "max": None,
               "vars": []}
        i = type_at + 2
        while i < len(tokens):
            key = tokens[i]
            if key in ("default", "min", "max") and i + 1 < len(tokens):
                value = tokens[i + 1]
                if key == "default":
                    out["default"] = value
                elif key == "min":
                    out["min"] = value
                else:
                    out["max"] = value
                i += 2
            elif key == "var" and i + 1 < len(tokens):
                out["vars"].append(tokens[i + 1])
                i += 2
            else:
                i += 1
        return out

    def supports(self, name: str) -> bool:
        """引擎是否声明了这个 UCI 选项"""
        return name in self.options

    def set_option(self, name: str, value) -> bool:
        """设置 UCI 选项; 引擎不认识这个选项时返回 False 并跳过(不会瞎发命令)

        发一个引擎没有的 setoption, Pikafish 会静默忽略, 表面上"设置成功了",
        实际什么都没发生 —— 所以这里先对着 options 名单核一遍。
        """
        if self.proc is None or self.proc.poll() is not None:
            self.start_engine()
        if not self.supports(name):
            return False
        self.send_command(f"setoption name {name} value {value}")
        return True

    def version_info(self) -> dict:
        """引擎身份 + 权重文件, 用于在分析结果里回显"这次是谁算的\""""
        return {"engine": self.engine_name or os.path.basename(self.engine_path),
                "author": self.engine_author,
                "path": self.engine_path,
                "nnue_file": os.path.basename(self.nnue_path) if self._nnue_loaded else "",
                "options": sorted(self.options)}

    def send_command(self, cmd: str) -> None:
        """向引擎发送单行命令"""
        if self.proc is None or self.proc.poll() is not None:
            raise EngineError("引擎进程未运行, 请先调用 start_engine()")
        if self.verbose:
            print(f">>> {cmd}")
        with self._write_lock:
            try:
                self.proc.stdin.write(cmd + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise EngineError(f"向引擎写入命令失败: {e}") from e

    def _read_loop(self) -> None:
        """后台线程: 持续读取引擎 stdout 并放入队列"""
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\r\n")
                if self.verbose:
                    print(f"<<< {line}")
                self._queue.put(line)
        except Exception:  # 管道被关闭等
            pass
        finally:
            self._queue.put(None)  # 进程结束标记

    def read_line(self, timeout: float = 5.0) -> str:
        """读取一行引擎输出, 超时或进程崩溃抛 EngineError"""
        try:
            line = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise EngineError(f"等待引擎输出超时 ({timeout:.1f}s)")
        if line is None:
            raise EngineError("引擎进程已意外退出 (崩溃或被杀)")
        return line

    def poll_line(self, timeout: float = 0.2) -> str | None:
        """读一行引擎输出; 超时返回 None(而不是抛异常), 进程结束抛 EngineError

        搜索过程中要一边读一边判断"用户是不是按了取消", 用 read_line() 的话
        没有输出就得靠超时异常来兜, 很难写。
        """
        try:
            line = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is None:
            raise EngineError("引擎进程已意外退出 (崩溃或被杀)")
        return line

    def wait_for(self, token: str, timeout: float = 10.0) -> str:
        """一直读输出直到出现以 token 开头的行"""
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                raise EngineError(f"等待引擎输出 '{token}' 超时 ({timeout:.1f}s)")
            line = self.read_line(timeout=remain)
            if line.startswith(token):
                return line

    # ------------------------------------------------------------------
    # 权重
    # ------------------------------------------------------------------
    def set_nnue(self, filepath: str) -> bool:
        """通过 setoption 加载 NNUE 权重文件"""
        if self.proc is None or self.proc.poll() is not None:
            self.start_engine()
        if not filepath:
            return False
        filepath = os.path.abspath(filepath)
        if not os.path.isfile(filepath):
            raise EngineError(
                f"NNUE 权重文件不存在: {filepath}\n"
                "  请把 pikafish.nnue 放到 engine/ 目录, "
                "或用 --nnue / 环境变量 PIKAFISH_NNUE 指定路径。"
            )
        self.send_command(f"setoption name EvalFile value {filepath}")
        self.send_command("isready")
        self.wait_for("readyok", timeout=60)  # 加载权重可能较慢
        self._nnue_loaded = True
        return True

    # ------------------------------------------------------------------
    # 性能参数
    # ------------------------------------------------------------------
    def configure_engine(self, threads: int = ENGINE_THREADS, hash_mb: int = ENGINE_HASH_MB,
                         multipv: int = ENGINE_MULTIPV) -> None:
        """设置引擎性能参数, 在 start_engine() 之后调用, 立刻生效。

        只发 Pikafish 真正支持的选项 (Threads / Hash / MultiPV)。
        注: Pikafish 没有 "Repetition Rule" 这类规则开关, 发了也会被静默忽略, 所以不发。

        :param threads: 搜索线程数
        :param hash_mb: 哈希表大小(MB)
        :param multipv: 默认返回几路候选, analyze(multipv=None) 时用这个值
        """
        self.start_engine()
        self._multipv = max(1, int(multipv))
        self.send_command(f"setoption name Threads value {max(1, int(threads))}")
        self.send_command(f"setoption name Hash value {max(1, int(hash_mb))}")
        self.send_command(f"setoption name MultiPV value {self._multipv}")
        self.send_command("isready")
        self.wait_for("readyok", timeout=30)

    # ------------------------------------------------------------------
    # 分析
    # ------------------------------------------------------------------
    def analyze(self, fen: str, movetime: int = DEFAULT_MOVETIME,
                multipv: int | None = None) -> dict:
        """分析指定 FEN 棋面, 支持 MultiPV (多路候选)。

        :param fen: 中国象棋 FEN
        :param movetime: 引擎思考时间(毫秒)
        :param multipv: 要几路候选; None 表示沿用 configure_engine() 设置的 MultiPV
        :return: {
            "moves": [{"rank": 1, "bestmove": "h2e2", "score": 45, "mate": None,
                       "depth": 18, "pv": ["h2e2", ...]}, ...],   # 按 rank 排序
            "primary": moves 的第一项 (引擎首选),
            # 兼容旧字段:
            "bestmove": str, "score": int|None, "mate": int|None,
            "pv": list[str], "depth": int|None,
        }
        """
        if not fen or not fen.strip():
            raise EngineError("FEN 不能为空")

        self.start_engine()

        if not self._nnue_loaded and self.nnue_path and os.path.isfile(self.nnue_path):
            self.set_nnue(self.nnue_path)

        # 每次都显式设置 MultiPV, 结果只取决于本次调用, 与上一次分析无关
        want = max(1, int(multipv)) if multipv else self._multipv

        self.send_command("ucinewgame")
        self.send_command(f"setoption name MultiPV value {want}")
        self.send_command("isready")
        self.wait_for("readyok", timeout=30)

        self.send_command(f"position fen {fen.strip()}")
        self.send_command(f"go movetime {int(movetime)}")

        bestmove = None
        # multipv 路数 -> 该路最深的解析结果 (引擎会不断刷新同一路, 后到的覆盖前面的)
        lines_by_pv: dict[int, dict] = {}
        # 思考时间 + 余量, 避免引擎未及时回 bestmove 就超时
        wait_timeout = movetime / 1000.0 + 15.0

        while True:
            line = self.read_line(timeout=wait_timeout)
            if "CRITICAL ERROR" in line:
                # Pikafish 新版对 FEN / UCI 命令做严格校验, 不合法会直接报错退出
                raise EngineError(f"引擎拒绝了该棋面 (通常是 FEN 不合法): {line}")
            if line.startswith("info") and " pv " in line:
                parsed = self._parse_info(line)
                lines_by_pv[parsed["multipv"]] = parsed
            elif line.startswith("bestmove"):
                parts = line.split()
                bestmove = parts[1] if len(parts) > 1 else None
                break

        # 每一路的着法/评分/杀棋/深度/变例, 按 rank (= multipv 编号) 排序
        moves = [
            {
                "rank": rank,
                "bestmove": info["pv"][0],
                "score": info["score"],
                "mate": info["mate"],
                "depth": info["depth"],
                "pv": info["pv"],
            }
            for rank, info in sorted(lines_by_pv.items())
            if info["pv"]
        ]
        # 时限很短时引擎可能一条 pv 都来不及输出, 这时退回 bestmove 那一行
        primary = moves[0] if moves else {
            "rank": 1, "bestmove": bestmove, "score": None,
            "mate": None, "depth": None, "pv": [],
        }
        return {
            "moves": moves,
            "primary": primary,
            # 兼容旧字段
            "bestmove": bestmove,
            "score": primary["score"],
            "mate": primary["mate"],
            "pv": primary["pv"],
            "depth": primary["depth"],
        }

    # ------------------------------------------------------------------
    # 通用搜索 (残局研究用)
    # ------------------------------------------------------------------
    def search(self, position: str, *, depth: int | None = None,
               movetime: int | None = None, multipv: int | None = None,
               searchmoves: list | None = None, cancel=None,
               on_info=None, timeout_ms: int | None = None) -> dict:
        """通用 UCI 搜索: 支持 depth / movetime / MultiPV / searchmoves / 中途取消

        与 analyze() 的区别:
          * analyze() 只管"给我几路候选", 适合下棋界面点一下就出结果;
          * search() 是给残局研究用的, 要能限深度、限根节点着法、还要能中途喊停,
            并且把 info 行里的 seldepth/nodes/nps 一并带回来存档。
        searchmoves 是"受控变着"的基础: 限定根节点只走某一着, 就能单独给那一着定分。

        :param position: UCI position 命令的参数部分, 例如
            "fen <fen>"  或  "startpos moves h2e2 h9g7"
        :param depth: go depth N
        :param movetime: go movetime N (毫秒); 与 depth 同时给时引擎谁先满足谁停
        :param multipv: 本次 MultiPV 路数; None 表示沿用 configure_engine 的设置
        :param searchmoves: 限定根节点着法 (ICCS 字符串列表)
        :param cancel: threading.Event; 置位后立刻发 stop, 已算出的部分照常返回
        :param on_info: 每解析到一条 info 回调一次(用于上报进度)
        :param timeout_ms: 整体超时(毫秒); 不传则按 movetime + 90s / 1800s 兜底
        :return: {"bestmove", "lines":[{multipv,depth,seldepth,score,mate,bound,
                  pv,nodes,nps,time_ms}], "info_count", "stopped", "duration_ms"}
        """
        if not depth and not movetime:
            raise EngineError("search() 至少要给 depth 或 movetime 之一")
        if not (position or "").strip():
            raise EngineError("position 不能为空")

        started = time.time()
        self.start_engine()
        if not self._nnue_loaded and self.nnue_path and os.path.isfile(self.nnue_path):
            self.set_nnue(self.nnue_path)

        want = max(1, int(multipv)) if multipv else self._multipv
        root_moves = [m.strip() for m in (searchmoves or []) if (m or "").strip()]

        # 一条 stdout 只能有一个读者, 所以同一实例上的搜索必须串行
        with self._search_lock:
            self.send_command("ucinewgame")
            # MultiPV 是核心参数, 直接发(与 analyze() 一致); 规则类开关走 set_option(),
            # 引擎不认识就跳过并在结果里如实回报
            self.send_command(f"setoption name MultiPV value {want}")
            self.send_command("isready")
            self.wait_for("readyok", timeout=60)

            self.send_command(f"position {position.strip()}")
            go = ["go"]
            if depth:
                go += ["depth", str(int(depth))]
            if movetime:
                go += ["movetime", str(int(movetime))]
            if root_moves:
                go.append("searchmoves")
                go += root_moves
            self.send_command(" ".join(go))

            # 时限留足余量, 免得引擎还在算就被判超时
            if timeout_ms:
                budget = int(timeout_ms) / 1000.0
            elif movetime:
                budget = int(movetime) / 1000.0 + 90.0
            else:
                budget = 1800.0
            deadline = time.time() + budget
            lines_by_pv: dict[int, dict] = {}
            bestmove = None
            info_count = 0
            stopped = False

            while True:
                if cancel is not None and cancel.is_set() and not stopped:
                    stopped = True
                    self.send_command("stop")
                    deadline = time.time() + 10.0   # stop 之后必须很快回 bestmove
                line = self.poll_line(timeout=0.2)
                if line is None:
                    if time.time() <= deadline:
                        continue
                    if not stopped:
                        stopped = True
                        self.send_command("stop")
                        deadline = time.time() + 10.0
                        continue
                    raise EngineError("等待引擎 bestmove 超时")
                if "CRITICAL ERROR" in line:
                    raise EngineError(f"引擎拒绝了该局面或命令: {line}")
                if line.startswith("bestmove"):
                    parts = line.split()
                    bestmove = parts[1] if len(parts) > 1 else None
                    break
                if line.startswith("info"):
                    info_count += 1
                    parsed = self._parse_info(line)
                    if parsed["pv"]:
                        lines_by_pv[parsed["multipv"]] = parsed
                    if on_info is not None:
                        on_info(parsed)

        return {
            "bestmove": bestmove,
            "lines": [lines_by_pv[rank] for rank in sorted(lines_by_pv)],
            "info_count": info_count,
            "stopped": stopped,
            "duration_ms": int((time.time() - started) * 1000),
            "engine": self.version_info(),
        }

    def dynamic_movetime(self, fen: str) -> int:
        """按剩余子力数决定思考时间(毫秒)。

        子多时局面开阔、候选着法多, 给短一点就够用; 子少时(残局)要算准,
        给长一点。阈值: >20 子 3000ms, 10~20 子 5000ms, <10 子 8000ms。
        """
        parts = (fen or "").split()
        board = parts[0] if parts else ""
        pieces = sum(1 for ch in board if ch.isalpha())  # 棋盘上所有棋子(含将/帅)
        if pieces > 20:
            return 3000
        if pieces >= 10:
            return 5000
        return 8000

    @staticmethod
    def _parse_info(line: str) -> dict:
        """解析一行 info 输出

        提取 multipv / depth / seldepth / score cp|mate / bound / nodes / nps /
        time / hashfull / pv。score 与 mate 同时只有一个非 None：
        cp 存进 "score", mate 存进 "mate"。
        """
        info = {"multipv": 1, "depth": None, "seldepth": None, "score": None,
                "mate": None, "bound": None, "pv": [], "nodes": None, "nps": None,
                "time_ms": None, "hashfull": None}
        tokens = line.split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            try:
                if tok == "depth" and i + 1 < len(tokens):
                    info["depth"] = int(tokens[i + 1])
                    i += 2
                elif tok == "seldepth" and i + 1 < len(tokens):
                    info["seldepth"] = int(tokens[i + 1])
                    i += 2
                elif tok == "multipv" and i + 1 < len(tokens):
                    info["multipv"] = int(tokens[i + 1])
                    i += 2
                elif tok == "nodes" and i + 1 < len(tokens):
                    info["nodes"] = int(tokens[i + 1])
                    i += 2
                elif tok == "nps" and i + 1 < len(tokens):
                    info["nps"] = int(tokens[i + 1])
                    i += 2
                elif tok == "hashfull" and i + 1 < len(tokens):
                    info["hashfull"] = int(tokens[i + 1])
                    i += 2
                elif tok == "time" and i + 1 < len(tokens):
                    info["time_ms"] = int(tokens[i + 1])
                    i += 2
                elif tok == "score" and i + 2 < len(tokens):
                    kind, value = tokens[i + 1], tokens[i + 2]
                    if kind == "cp":
                        info["score"] = int(value)
                    elif kind == "mate":
                        info["mate"] = int(value)
                    i += 3
                elif tok in ("lowerbound", "upperbound"):
                    # 带上下界的分数不是一个确切值, 判定结论时要排除
                    info["bound"] = tok[:-5]
                    i += 1
                elif tok == "pv":
                    info["pv"] = tokens[i + 1:]
                    break
                else:
                    i += 1
            except ValueError:
                i += 1
        return info

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def quit(self) -> None:
        """发送 quit 并确保子进程被回收"""
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write("quit\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            for pipe in (proc.stdin, proc.stdout):
                try:
                    if pipe:
                        pipe.close()
                except Exception:
                    pass
