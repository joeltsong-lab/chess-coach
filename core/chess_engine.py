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
        self._nnue_loaded = False
        self._multipv = 1  # 当前 MultiPV 设置, 由 configure_engine() 决定

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
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

        self.send_command("uci")
        self.wait_for("uciok", timeout=15)

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
        """解析一行 info 输出, 提取 multipv / depth / score / mate / pv"""
        info = {"multipv": 1, "depth": None, "score": None, "mate": None, "pv": []}
        tokens = line.split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            try:
                if tok == "depth" and i + 1 < len(tokens):
                    info["depth"] = int(tokens[i + 1])
                    i += 2
                elif tok == "multipv" and i + 1 < len(tokens):
                    info["multipv"] = int(tokens[i + 1])
                    i += 2
                elif tok == "score" and i + 2 < len(tokens):
                    kind, value = tokens[i + 1], int(tokens[i + 2])
                    if kind == "cp":
                        info["score"] = value
                    elif kind == "mate":
                        info["mate"] = value
                    i += 3
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
