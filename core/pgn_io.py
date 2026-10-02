# -*- coding: utf-8 -*-
"""棋谱导入导出: fenmoves / ICCS / PGN / JSON 四种格式, 以及中文着法 <-> ICCS 互转。

导出方向依赖 coord_utils.iccs_to_chinese(); 导入方向没有现成的"中文着法 -> 坐标"
解析器 (中文记谱有 前/后/进/退/平 加各种异体字), 这里用最稳的办法:
把当前局面下**所有合法着法**都生成出来、逐个转成中文, 建一张对照表再反查。
好处是只认规则允许的着法, 天然不会把非法着法当成合法。

格式约定 (都是纯文本, 便于贴进文本框或用 curl 传):
  fen      : 只有一行 FEN, 表示"只存这个局面"
  fenmoves : "<start_fen> moves h2e2 h9g7 ..."   (象棋云库/引擎通用的复盘格式)
  iccs     : "h2e2 h9g7 e3e4 ..." 纯坐标序列
  pgn      : 标签段 + 着法段, 着法可写中文或 ICCS (由 [Format] 标明), 评语写在 {} 里
  json     : {"game": {...}, "moves": [...]} 完整结构, 用于备份
"""

import json
import re

from .chess_engine import START_FEN
from .coord_utils import FILES, iccs_to_chinese, pv_to_chinese
from .rules import RuleError, apply_iccs, board_to_fen, fen_to_board, legal_moves

FORMATS = ("fen", "fenmoves", "iccs", "pgn", "json")

RESULT_TOKENS = ("1-0", "0-1", "1/2-1/2", "*", "1/2")

# 中文记谱的异体字: 車/馬/砲/進 是繁体棋谱的常见写法, 统一成生成器用的字
CHAR_VARIANTS = str.maketrans({"車": "车", "馬": "马", "砲": "炮", "包": "炮",
                               "進": "进", "帥": "帅", "將": "将"})
# 网上不少棋谱红方也写 士/象, 生成器红方写 仕/相, 反查时按"宽匹配"兜一层
LOOSE_VARIANTS = str.maketrans({"仕": "士", "相": "象"})

RED_NUMERALS = "一二三四五六七八九"
ARABIC = "123456789"

ICCS_MOVE_RE = re.compile(r"^[a-i][0-9][a-i][0-9]")
PGN_TAG_RE = re.compile(r'\[\s*([A-Za-z_][A-Za-z0-9_]*)\s+"([^"]*)"\s*\]')
PGN_COMMENT_RE = re.compile(r"\{([^}]*)\}")
PGN_MOVE_NO_RE = re.compile(r"\d+\s*\.(\s*\.\.)?")
FEN_BOARD_RE = re.compile(r"^[rnbakcpRNBAKCP0-9/]+$")


# ----------------------------------------------------------------------
# 中文着法 -> ICCS
# ----------------------------------------------------------------------
def _digits_to_numerals(text):
    """阿拉伯数字 -> 汉字数字 (红方记谱)"""
    return text.translate(str.maketrans(ARABIC, RED_NUMERALS))


def build_chinese_index(fen):
    """当前局面下 "中文着法 -> ICCS" 的对照表

    只列**该走子方**的合法着法, 键用该方的标准体例:
    红方汉字数字(车九进一)、黑方阿拉伯数字(马8进7)。

    :return: (exact, loose) 两张 dict: 严格匹配(区分仕/士、相/象) 与 宽松匹配
    """
    grid, side = fen_to_board(fen)
    exact, loose = {}, {}
    for iccs in legal_moves(grid, side=side):
        chinese = iccs_to_chinese(iccs, fen)
        if not chinese or ICCS_MOVE_RE.match(chinese):
            continue                       # 生成器没认出来, 这张表里就不要它
        exact.setdefault(chinese, iccs)
        loose.setdefault(chinese.translate(LOOSE_VARIANTS), iccs)
    return exact, loose


def chinese_to_iccs(text, fen, index=None):
    """中文着法 -> ICCS; 认不出来返回 None

    只按"该走子方"的体例查表: 红方键是汉字数字(炮二平五), 黑方键是阿拉伯数字(炮2平5),
    所以不会把黑方的 "炮2平5" 当成红方的 "炮二平五"。
    额外容忍两件事:
      - 异体字 車/馬/砲/進 (先统一成生成器用的字)
      - 红方着法写成阿拉伯数字 (炮2平5 = 炮二平五, 只是换字体不换视角)
    每种写法都先严格匹配、再宽松匹配 (仕/士、相/象 不分)。
    """
    token = (text or "").strip().translate(CHAR_VARIANTS)
    if not token:
        return None
    if index is None:
        index = build_chinese_index(fen)
    exact, loose = index

    candidates = [token]
    if any(ch in token for ch in ARABIC):
        candidates.append(_digits_to_numerals(token))

    for candidate in candidates:
        for table in (exact, loose):
            hit = table.get(candidate)
            if hit:
                return hit
    return None


def _looks_like_chinese_move(token):
    """像不像中文着法: 含棋子名或走子字 (进/退/平/前/后)"""
    return any(ch in token for ch in "车马炮相象仕士帅将兵卒进退平前后")


# ----------------------------------------------------------------------
# 重放着法
# ----------------------------------------------------------------------
def replay(start_fen, moves, max_warnings=50):
    """从 start_fen 开始逐个重放着法, 算出每一步的完整信息

    某一步走不通时**不中断整局**: 记一条 warning、跳过这一步继续往下放,
    这样导入一份中间有坏着法的棋谱, 仍然能把其余着法收进来。
    (跳过会让后续着法跟着对不上, 所以 warning 里会写清是哪一步开始出问题的)

    :return: {"start_fen", "fen", "side", "moves": [...], "warnings": [...]}
    """
    try:
        grid, side = fen_to_board(start_fen)
    except RuleError as e:
        raise RuleError(f"起始 FEN 不合法: {e}") from e
    start_grid, start_side = grid, side

    out, warnings, ply = [], [], 0
    for raw in moves:
        token = (raw or "").strip()
        if not token:
            continue
        ply += 1
        fen_before = board_to_fen(grid, side)
        try:
            after, info = apply_iccs(grid, token, side=side)
        except RuleError as e:
            if len(warnings) < max_warnings:
                warnings.append(f"第 {ply} 步 {token} 跳过: {e}")
            continue
        chinese = iccs_to_chinese(info["iccs"], fen_before)
        out.append({
            "ply": len(out) + 1,
            "side": "red" if info["side"] == "w" else "black",
            "iccs": info["iccs"],
            "chinese": chinese,
            "from_sq": f"{FILES[info['from'][1]]}{9 - info['from'][0]}",
            "to_sq": f"{FILES[info['to'][1]]}{9 - info['to'][0]}",
            "captured": info["captured"],
            "fen_before": fen_before,
            "fen_after": info["fen_after"],
        })
        grid, side = after, ("b" if side == "w" else "w")

    return {"start_fen": board_to_fen(start_grid, start_side),
            "fen": board_to_fen(grid, side),
            "side": "red" if side == "w" else "black",
            "moves": out, "warnings": warnings}


# ----------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------
def export_fenmoves(start_fen, moves):
    """云库/引擎复盘格式: '<FEN> moves h2e2 h9g7 ...'"""
    seq = " ".join(m["iccs"] if isinstance(m, dict) else m for m in moves)
    return f"{start_fen} moves {seq}".strip()


def export_iccs(moves, per_line=0):
    """纯 ICCS 序列; per_line > 0 时按该数量换行"""
    seq = [m["iccs"] if isinstance(m, dict) else m for m in moves]
    if per_line and per_line > 0:
        return "\n".join(" ".join(seq[i:i + per_line]) for i in range(0, len(seq), per_line))
    return " ".join(seq)


def _clean_comment(text):
    """评语写进 {} 里, 花括号要转义掉"""
    if not text:
        return ""
    return str(text).replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def export_pgn(game, moves, move_format="iccs"):
    """中国象棋类 PGN: 标签段 + 着法段, 评语写进 {} 注释

    :param game: 对局元信息 dict (name/event/site/date/round/red_name/black_name/
                 result/opening/ecco/start_fen)
    :param moves: 着法列表 (dict 或 ICCS 字符串)
    :param move_format: 'iccs' 或 'chinese', 会写进 [Format] 标签
    """
    game = game or {}
    start_fen = game.get("start_fen") or START_FEN
    fmt_tag = "Chinese" if move_format == "chinese" else "ICCS"
    tags = {
        "Game": game.get("name") or "中国象棋对局",
        "Event": game.get("event") or "?",
        "Site": game.get("site") or "?",
        "Date": game.get("date") or "?",
        "Round": game.get("round") or "?",
        "Red": game.get("red_name") or "?",
        "Black": game.get("black_name") or "?",
        "Result": game.get("result") or "*",
        "Opening": game.get("opening") or "?",
        "ECCO": game.get("ecco") or "?",
        "FEN": start_fen,
        "Format": fmt_tag,
    }

    # 中文着法要按局面逐个生成 (吃子/走子后局面会变)
    names = pv_to_chinese([m["iccs"] if isinstance(m, dict) else m for m in moves],
                          start_fen) if move_format == "chinese" else None

    lines, buf = [], []
    _grid, side = fen_to_board(start_fen)
    red_to_move = side == "w"
    for i, move in enumerate(moves):
        iccs = move["iccs"] if isinstance(move, dict) else move
        text = names[i] if names and i < len(names) else iccs
        comment = _clean_comment(move.get("comment")) if isinstance(move, dict) else ""
        ply = i + 1                      # 半回合序号, 从 1 开始
        head = []
        if red_to_move:
            head = [f"{(ply + 1) // 2}."]            # 红方开始一个新的回合
        elif ply == 1:
            head = [f"{(ply + 1) // 2}.", "..."]     # 开局就轮到黑方
        buf.append(" ".join(head + [text + (f" {{{comment}}}" if comment else "")]))
        red_to_move = not red_to_move
        if len(buf) >= 8:                # 一行走几步就换行, 便于人看
            lines.append(" ".join(buf))
            buf = []
    if buf:
        lines.append(" ".join(buf))
    lines.append(game.get("result") or "*")

    tag_lines = "".join(f'[{key} "{value}"]\n' for key, value in tags.items())
    return tag_lines + "\n" + "\n".join(lines) + "\n"


def export_json(game, moves, indent=2):
    """完整结构, 用于备份/迁移"""
    return json.dumps({"game": game or {}, "moves": moves or []},
                      ensure_ascii=False, indent=indent)


# ----------------------------------------------------------------------
# 导入
# ----------------------------------------------------------------------
def _split_fen_and_moves(text):
    """把 '<FEN> moves a b c' 拆成 (fen, [着法]); FEN 可能是 6 段也可能是前 2 段"""
    tokens = text.split()
    if not tokens:
        raise RuleError("内容是空的")
    for size in (6, 5, 4, 2, 1):
        if len(tokens) >= size and all(part.isdigit() or part in ("w", "b", "-")
                                       for part in tokens[1:size]):
            board = tokens[0]
            if "/" not in board:
                continue
            fen = " ".join(tokens[:size])
            rest = tokens[size:]
            if rest and rest[0].lower().rstrip(":") in ("moves", "move"):
                rest = rest[1:]
            return fen, rest
    raise RuleError(f"认不出这个 FEN: {tokens[0]!r}")


def parse_fenmoves(text):
    """'<FEN> moves h2e2 ...' -> (start_fen, [iccs]); 也接受不带 moves 关键字的写法"""
    fen, rest = _split_fen_and_moves(text.strip())
    return fen, [t for t in rest if not t.startswith("//")]


def parse_iccs(text):
    """纯 ICCS 序列 -> [iccs]; 顺带容忍逗号、换行、[%evp] 这类残留"""
    tokens = re.split(r"[\s,]+", text.strip())
    return [t for t in tokens if t]


def parse_pgn(text):
    """解析类 PGN 文本

    只认主线 (带括号的变着整段忽略并记一条 warning)。着法可以写中文也可以写 ICCS,
    有 [Format] 就按它, 没写就逐个 token 自己判断; 中文着法按"轮谁走"反查坐标。
    {} 注释挂在它前面那一步上 (出现在任何着法之前的注释留给下一步)。

    :return: {"meta": {...}, "start_fen":..., "raw_moves": [{"iccs", "comment"?}],
              "comments": [...], "warnings": [...], "move_format": 'iccs'|'chinese'}
    """
    warnings = []
    tags = {name.lower(): value for name, value in PGN_TAG_RE.findall(text)}

    # 去掉标签段, 剩下的就是着法段
    body = PGN_TAG_RE.sub("", text)
    if "(" in body or ")" in body:
        warnings.append("棋谱里带括号的变着已忽略, 只导入了主线")
        body = re.sub(r"\([^()]*\)", " ", body)
    body = PGN_MOVE_NO_RE.sub(" ", body)

    start_fen = (tags.get("fen") or "").strip() or START_FEN
    try:
        fen_to_board(start_fen)
    except RuleError as e:
        warnings.append(f"[FEN] 标签不合法, 改用标准开局: {e}")
        start_fen = START_FEN

    move_format = (tags.get("format") or "").strip().lower()
    move_format = "chinese" if move_format.startswith("chin") else "iccs"

    grid, side = fen_to_board(start_fen)
    raw_moves, comments = [], []
    pending = None                # 出现在着法之前的注释, 留给下一步
    warnings_used = 0

    def scan(chunk):
        """把一段纯着法文本解成 ICCS 追加到 raw_moves"""
        nonlocal grid, side, pending, warnings_used
        for token in re.split(r"[\s,]+", chunk):
            if not token or token in RESULT_TOKENS:
                continue
            iccs = token if ICCS_MOVE_RE.match(token) else None
            if not iccs:
                if not _looks_like_chinese_move(token):
                    if warnings_used < 50:
                        warnings.append(f"看不懂的记号已跳过: {token!r}")
                        warnings_used += 1
                    continue
                iccs = chinese_to_iccs(token, board_to_fen(grid, side))
                if not iccs:
                    if warnings_used < 50:
                        warnings.append(f"中文着法 {token} 在当前局面下找不到对应走法, 已跳过")
                        warnings_used += 1
                    continue
            try:
                after, info = apply_iccs(grid, iccs, side=side)
            except RuleError as e:
                if warnings_used < 50:
                    warnings.append(f"{token} 跳过: {e}")
                    warnings_used += 1
                continue
            entry = {"iccs": info["iccs"]}
            if pending:
                entry["comment"] = pending
                pending = None
            raw_moves.append(entry)
            grid, side = after, ("b" if side == "w" else "w")

    pos = 0
    for match in PGN_COMMENT_RE.finditer(body):
        scan(body[pos:match.start()])
        comment = match.group(1).strip()
        comments.append(comment)
        if raw_moves and "comment" not in raw_moves[-1]:
            raw_moves[-1]["comment"] = comment    # 注释跟在着法后面, 说的是这一步
        else:
            pending = comment
        pos = match.end()
    scan(body[pos:])

    meta = {}
    for tag, field in (("game", "name"), ("event", "event"), ("site", "site"),
                       ("round", "round"), ("red", "red_name"), ("black", "black_name"),
                       ("opening", "opening"), ("ecco", "ecco"), ("result", "result")):
        value = (tags.get(tag) or "").strip()
        if value and value != "?":
            meta[field] = value
    date = (tags.get("date") or "").strip()
    if date and date != "?":
        meta["date"] = date.replace(".", "-").replace("/", "-")

    return {"meta": meta, "start_fen": start_fen, "raw_moves": raw_moves,
            "comments": comments, "warnings": warnings, "move_format": move_format}


def parse_json(text):
    """解析 json 格式; 兼容 storage.get_game() 的 {"game":..., "moves":[...]} 结构

    :return: {"meta", "start_fen", "raw_moves", "warnings"}
        raw_moves 是原始着法条目 (dict 或 ICCS 字符串), 由调用方重放后再合并评语
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuleError(f"JSON 解析失败: {e}") from e
    if isinstance(data, list):
        data = {"game": {}, "moves": data}
    if not isinstance(data, dict):
        raise RuleError("JSON 顶层应当是对象或数组")
    game = data.get("game") or {}
    if not isinstance(game, dict):
        raise RuleError("JSON 里的 game 应当是对象")
    raw_moves = data.get("moves") or []
    if not isinstance(raw_moves, list):
        raise RuleError("JSON 里的 moves 应当是数组")
    start_fen = (game.get("start_fen") or "").strip() or START_FEN
    return {"meta": {k: v for k, v in game.items() if v not in (None, "")},
            "start_fen": start_fen, "raw_moves": raw_moves, "warnings": []}


def detect_format(text):
    """猜格式: pgn / json / fenmoves / fen / iccs"""
    content = (text or "").strip()
    if not content:
        raise RuleError("内容是空的")
    # 先认 PGN: PGN 的标签段也以 [ 开头, 跟 JSON 数组长得像, 顺序不能反
    if PGN_TAG_RE.search(content):
        return "pgn"
    if content.startswith("{") or content.startswith("["):
        return "json"
    first = content.split("\n", 1)[0].split()
    if first and "/" in first[0] and FEN_BOARD_RE.match(first[0]):
        # 注意 FEN 自己就有 6 段, 所以不能按段数判断, 得看"去掉 FEN 之后还剩不剩着法"
        try:
            _fen, rest = _split_fen_and_moves(content)
        except RuleError:
            return "fen"
        return "fenmoves" if rest else "fen"
    # 没有标签段的着法文本: "1. 炮二平五 马8进7" / "炮二平五 马8进7" / "1. h2e2 h9g7"
    # 只要出现中文着法, 或者"带回合号的着法序列", 就按 PGN 处理 (不然只能当 ICCS, 中文就丢了)
    tokens = [t for t in re.split(r"[\s,]+", PGN_MOVE_NO_RE.sub(" ", content))
              if t and t not in RESULT_TOKENS]
    if any(_looks_like_chinese_move(t) for t in tokens if not ICCS_MOVE_RE.match(t)):
        return "pgn"
    if PGN_MOVE_NO_RE.search(content) and any(ICCS_MOVE_RE.match(t) for t in tokens):
        return "pgn"
    if first and ICCS_MOVE_RE.match(first[0]):
        return "iccs"
    raise RuleError("认不出这是什么格式 (既不是 FEN/ICCS, 也不是 PGN/JSON)")


EXTRA_MOVE_FIELDS = ("comment", "score_cp", "score_mate", "eval_by", "is_key")


def merge_move_extras(replayed, raw):
    """重放只负责算坐标/FEN; 原始条目里的评语、评分要并回来 (备份恢复不能丢评价)"""
    for move, original in zip(replayed, raw):
        if not isinstance(original, dict):
            continue
        for key in EXTRA_MOVE_FIELDS:
            if original.get(key) not in (None, ""):
                move[key] = original[key]


def import_content(text, fmt=None):
    """统一入口: 文本 + (可选)格式 -> 结构化的导入结果

    除了"只存局面"的 fen, 其余格式都会把着法逐个重放一遍, 所以返回的每一步都带
    中文着法、走子前后的 FEN、被吃子 —— 上层直接写库即可。

    :return: {"fmt", "start_fen", "meta", "moves": [...], "comments": [...],
              "warnings": [...]}
    """
    content = text or ""
    fmt = (fmt or "").strip().lower() or detect_format(content)
    if fmt not in FORMATS:
        raise RuleError(f"不支持的格式: {fmt!r}, 可选 {', '.join(FORMATS)}")

    if fmt == "pgn":
        parsed = parse_pgn(content)
        result = replay(parsed["start_fen"], [m["iccs"] for m in parsed["raw_moves"]])
        merge_move_extras(result["moves"], parsed["raw_moves"])
        result["fmt"] = fmt
        result["meta"] = parsed["meta"]
        result["comments"] = parsed["comments"]
        result["warnings"] = parsed["warnings"] + result["warnings"]
        return result

    if fmt == "json":
        parsed = parse_json(content)
        raw = parsed["raw_moves"]
        result = replay(parsed["start_fen"],
                        [m.get("iccs") if isinstance(m, dict) else m for m in raw])
        merge_move_extras(result["moves"], raw)
        result["fmt"] = fmt
        result["meta"] = parsed["meta"]
        result["comments"] = [m.get("comment") or "" for m in result["moves"]]
        result["warnings"] = parsed["warnings"] + result["warnings"]
        return result

    if fmt == "fen":
        fen = content.strip().split("\n")[0].strip()
        return {"fmt": fmt, "start_fen": fen, "meta": {}, "moves": [],
                "comments": [], "warnings": []}

    if fmt == "fenmoves":
        start_fen, seq = parse_fenmoves(content)
    else:
        start_fen, seq = START_FEN, parse_iccs(content)
    result = replay(start_fen, seq)
    result["fmt"] = fmt
    result["meta"] = {}
    result["comments"] = []
    return result
