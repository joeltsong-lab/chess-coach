# -*- coding: utf-8 -*-
"""残局研究功能包。

放在子包里而不是继续往项目根目录堆文件, 是为了和"整局棋"那条线分开:
    rules.py / storage.py / pgn_io.py / games_routes.py   <- 整局棋与复盘 (原有, 本次不动)
    endgame/*                                             <- 残局研究 (本次新增)

模块分工:
    fen_rules   严格 FEN 校验、局面规范化与哈希、摆盘操作重放
    repetition  最近 16 半着的重复局面 / 长将 / 长捉信号 (只出信号, 不判胜负)
    analyzer    引擎适配层: depth/movetime/multipv/searchmoves/取消, 以及结论判定
    store       endgame_* 表的增删改查 (引擎数值只经这里落库)
    tasks       分析任务状态机 + 单写者队列 + 并发上限
    csrf        本地写接口的 CSRF token
    patterns    内置经典定式库 (10 个实用残局, 只有局面与练习目标, 不写着法序列)
    commentary  对已落库的分析结果做解说 (冻结 prompt, 只转述引擎数值, 异常降级)
    exchange    xq-endgame-study v1 导入导出 (导入的着法/引擎着法全部重新过校验)
    routes      /api/studies、/api/patterns 的 Flask Blueprint

两条硬约束 (贯穿全包):
  1. 任何引擎着法入库前都要过 rules.validate_move / apply_iccs, 不允许绕过校验写 UCI。
  2. 引擎的 draw 与重复检测只是"候选信号", 永远不自动等于正式棋例裁决 ——
     长将/长捉一律进人工复核 (rule_review_status='pending')。
"""
