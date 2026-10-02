# -*- coding: utf-8 -*-
"""内置经典定式库: 10 个教科书上的实用残局, 点一下就能开研究。

为什么是"实用残局"而不是"杀法题":
    实用残局的结论 (例胜 / 例和) 是教科书上写死的, 不依赖引擎算力, 也不需要
    我们编造一条"正解着法序列"。杀法题的"正解"没有引擎跑过就不能写死, 写死
    就是编造 —— 规格里禁止编造, 所以这里不提供任何着法序列, 只给局面 + 练什么。

每个定式:
    id         稳定的字符串主键 (导出/导入定式引用它, 不用下标)
    name       中文名
    theme      分类 (实用残局)
    fen        局面; **必须**能过 fen_rules.validate_position 的严格校验
    sideToMove 轮到谁走 ('w' 红 / 'b' 黑)
    result     教科书结论: "例胜" / "例和" (强方视角)
    resultNote 结论的出处说明 —— 明确写"教科书结论, 非引擎结论",
               免得前端把它当成引擎算出来的裁决
    goal       这个局面用来练什么
    tags       标签

定式的 FEN 由 test_endgame_patterns.py 逐个过 validate_position, 不合法直接测试失败:
摆出来的局面必须真的能当成一局棋继续走, 这是硬门槛。

本模块不落库、不碰引擎, 只提供数据 + 查询。
"""

# 定式库版本: 增删改定式时必须 +1 (导入导出里会带上, 便于说明"这份数据按哪版定式出的")
PATTERN_VERSION = 1

THEME_ENDGAME = "实用残局"

PATTERNS = (
    {
        "id": "rook-vs-single-advisor",
        "name": "单车例胜单士",
        "theme": THEME_ENDGAME,
        "fen": "4k4/4a4/9/9/9/4R4/9/9/9/4K4 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方单车, 弱方只有单士), 非引擎结论",
        "goal": "单车取胜的关键是先把士吃掉或把将逼到无士可走的位置, 练「车制士」的走法",
        "tags": ("单车", "单士", "实用残局"),
    },
    {
        "id": "rook-vs-two-advisors",
        "name": "单车例胜双士",
        "theme": THEME_ENDGAME,
        "fen": "3aka3/9/9/9/9/3R5/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方单车, 弱方双士), 非引擎结论",
        "goal": "双士比单士多一层遮挡, 练「车压肋道 + 停着」的次序",
        "tags": ("单车", "双士", "实用残局"),
    },
    {
        "id": "rook-vs-two-elephants",
        "name": "单车例胜双象",
        "theme": THEME_ENDGAME,
        "fen": "2b1k1b2/9/9/9/9/3R5/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方单车, 弱方双象), 非引擎结论",
        "goal": "象不能护住中路, 练「车占中路、逐个吃象」的顺序",
        "tags": ("单车", "双象", "实用残局"),
    },
    {
        "id": "rook-vs-full-guards",
        "name": "单车例和士象全",
        "theme": THEME_ENDGAME,
        "fen": "2bakab2/9/9/9/9/3R5/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例和",
        "resultNote": "教科书结论(强方单车, 弱方士象全), 非引擎结论",
        "goal": "从弱方视角练守和: 士象不要自己挤在一起, 别让车白吃子",
        "tags": ("单车", "士象全", "实用残局"),
    },
    {
        "id": "knight-vs-single-advisor",
        "name": "单马擒单士",
        "theme": THEME_ENDGAME,
        "fen": "4k4/9/3a5/9/9/9/9/4N4/9/4K4 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方单马, 弱方单士), 非引擎结论",
        "goal": "单马取胜全靠「帅占中路 + 马控士位」, 练马与帅的配合",
        "tags": ("单马", "单士", "实用残局"),
    },
    {
        "id": "knight-pawn-vs-full-guards",
        "name": "马兵例胜士象全",
        "theme": THEME_ENDGAME,
        "fen": "2bakab2/9/9/9/4P4/2N6/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方马兵, 弱方士象全), 非引擎结论",
        "goal": "兵换士象、马控九宫, 练「马兵破士象全」的取势思路",
        "tags": ("马兵", "士象全", "实用残局"),
    },
    {
        "id": "rook-pawn-vs-full-guards",
        "name": "车兵例胜士象全",
        "theme": THEME_ENDGAME,
        "fen": "2bakab2/9/9/9/4P4/3R5/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方车兵, 弱方士象全), 非引擎结论",
        "goal": "练车兵配合: 兵先手换士, 车再逐个清士象",
        "tags": ("车兵", "士象全", "实用残局"),
    },
    {
        "id": "rook-knight-vs-rook-full-guards",
        "name": "车马例胜车士象全",
        "theme": THEME_ENDGAME,
        "fen": "2bakab2/9/8r/9/9/3R2N2/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方车马, 弱方车士象全), 非引擎结论",
        "goal": "强子残局: 车马比车多一个马, 练「马做炮架/车控肋」的配合",
        "tags": ("车马", "车士象全", "实用残局"),
    },
    {
        "id": "cannon-high-pawn-vs-full-guards",
        "name": "炮高兵例胜士象全",
        "theme": THEME_ENDGAME,
        "fen": "2bakab2/9/9/5P3/9/4C4/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方炮高兵, 弱方士象全; 需要己方有仕相), 非引擎结论",
        "goal": "练「中炮镇住 + 兵逐步逼近」的推进节奏, 注意别让炮脱根",
        "tags": ("炮兵", "士象全", "实用残局"),
    },
    {
        "id": "two-rooks-vs-rook-full-guards",
        "name": "双车例胜车士象全",
        "theme": THEME_ENDGAME,
        "fen": "2bakab2/9/8r/9/9/3R3R1/9/9/9/3K5 w - - 0 1",
        "sideToMove": "w",
        "result": "例胜",
        "resultNote": "教科书结论(强方双车, 弱方车士象全), 非引擎结论",
        "goal": "练双车的经典配合: 一车吊住士象, 另一车切入九宫",
        "tags": ("双车", "车士象全", "实用残局"),
    },
)

_BY_ID = {item["id"]: item for item in PATTERNS}


def list_patterns() -> list[dict]:
    """定式清单 (深拷贝一份, 免得调用方改到模块里的常量表)"""
    return [dict(item, tags=list(item["tags"])) for item in PATTERNS]


def get_pattern(pattern_id) -> dict | None:
    """按 id 取一个定式; 没有就返回 None (调用方决定是 404 还是 400)"""
    item = _BY_ID.get(str(pattern_id or "").strip())
    return dict(item, tags=list(item["tags"])) if item else None


def pattern_ids() -> tuple:
    """全部定式 id, 顺序与 PATTERNS 一致"""
    return tuple(item["id"] for item in PATTERNS)
