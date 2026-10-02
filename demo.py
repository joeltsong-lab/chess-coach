# -*- coding: utf-8 -*-
"""Pikafish 中国象棋 AI 分析演示脚本

用法:
    python demo.py                          # 分析标准开局
    python demo.py "<FEN>"                  # 分析指定棋面
    python demo.py "<FEN>" --movetime 5000  # 指定引擎思考时间(毫秒)
"""

import argparse
import sys

from chess_engine import (
    ChessEngine,
    EngineError,
    ENGINE_PATH,
    NNUE_PATH,
    START_FEN,
)
from coord_utils import move_to_chinese, parse_fen


def format_result(fen, result, movetime, engine_path):
    """把 analyze() 的结果格式化成便于阅读的文本"""
    try:
        board, side = parse_fen(fen)
    except ValueError:
        board, side = {}, "w"

    lines = [
        "=" * 60,
        "Pikafish 中国象棋分析结果",
        "=" * 60,
        f"棋面 FEN   : {fen}",
        f"走子方     : {'红方' if side == 'w' else '黑方'}",
        f"引擎       : {engine_path}",
        f"思考时间   : {movetime} 毫秒",
        "",
    ]

    bestmove = result.get("bestmove")
    if bestmove:
        best_cn = move_to_chinese(dict(board), bestmove)
        lines.append(f"最佳着法   : {bestmove}   [{best_cn}]")
    else:
        lines.append("最佳着法   : (引擎未返回)")

    if result.get("mate") is not None:
        mate = result["mate"]
        winner = "走子方" if mate > 0 else "对方"
        lines.append(f"评分       : 绝杀, {winner} {abs(mate)} 步内将死")
    elif result.get("score") is not None:
        lines.append(f"评分       : {result['score']:+d} 厘兵 (走子方视角, 正数表示走子方占优)")
    else:
        lines.append("评分       : 未获取到")

    lines.append(f"搜索深度   : {result.get('depth')}")
    lines.append("")

    pv = result.get("pv") or []
    lines.append("候选变例 (PV):")
    if pv:
        pv_board = dict(board)
        pv_cn = [f"{mv}={move_to_chinese(pv_board, mv)}" for mv in pv]
        lines.append("  ICCS: " + " ".join(pv))
        lines.append("  中文: " + " ".join(pv_cn))
    else:
        lines.append("  (无)")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Pikafish 中国象棋引擎分析演示 (纯标准库)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"默认 FEN (标准开局):\n  {START_FEN}",
    )
    parser.add_argument("fen", nargs="?", default=START_FEN, help="要分析的 FEN 棋面, 默认标准开局")
    parser.add_argument("--movetime", type=int, default=2000,
                        help="引擎思考时间(毫秒), 默认 2000")
    parser.add_argument("--multipv", type=int, default=1,
                        help="MultiPV 条数 (>1 时给出多个候选着法), 默认 1")
    parser.add_argument("--engine", default=ENGINE_PATH,
                        help=f"引擎可执行文件路径, 默认 {ENGINE_PATH}")
    parser.add_argument("--nnue", default=NNUE_PATH,
                        help=f"NNUE 权重文件路径, 默认 {NNUE_PATH}")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印与引擎的原始通信")
    args = parser.parse_args()

    engine = ChessEngine(args.engine, args.nnue, verbose=args.verbose)
    try:
        try:
            parse_fen(args.fen)  # 先校验 FEN, 避免把非法棋面丢给引擎
        except ValueError as e:
            print(f"[错误] FEN 非法: {e}", file=sys.stderr)
            return 2

        result = engine.analyze(args.fen, movetime=args.movetime, multipv=args.multipv)
        print(format_result(args.fen, result, args.movetime, engine.engine_path))
        return 0
    except EngineError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[中断] 已退出", file=sys.stderr)
        return 130
    finally:
        engine.quit()


if __name__ == "__main__":
    sys.exit(main())
