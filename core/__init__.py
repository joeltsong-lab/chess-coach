# -*- coding: utf-8 -*-
"""core —— 与平台无关的公共层。

只依赖 Python 标准库, 不 import Flask 或任何 Web 框架, 所以 Web 端、命令行演示、
以及未来的手机端(安卓 / iOS / Flutter)都能直接复用同一份棋规、记谱与引擎封装。

模块:
    coord_utils   ICCS <-> 棋盘行列、中文着法
    chess_engine  Pikafish 引擎的 UCI 封装
    rules         纯 Python 棋规: FEN 校验 / 合法着法 / 自将与照面 / 落子
    pgn_io        棋谱导入导出: fen / fenmoves / iccs / pgn / json
    storage       棋局存储层: sqlite3 建表 + 增删改查
    explainer     LLM 二次筛选(纯 urllib, 未配置则降级为引擎首选)

引用方式(Web / CLI / 手机端一致):
    from core import rules, coord_utils
    from core.chess_engine import ChessEngine

目录约定:
    <项目根>/core/     本包
    <项目根>/engine/   引擎可执行文件与 NNUE 权重(不入库)
    <项目根>/data/     SQLite 运行时库(不入库)
"""

from pathlib import Path

# 项目根 = core/ 的上一级。引擎目录与数据库默认位置都以此为基准,
# 这样 core/ 单独搬进其它宿主工程时, 默认路径仍然指向宿主自己的 engine/ 与 data/。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

__all__ = ["PROJECT_ROOT"]
