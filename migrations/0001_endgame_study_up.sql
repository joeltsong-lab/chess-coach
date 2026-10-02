-- 0001_endgame_study: 残局研究 (endgame study)
--
-- 内容:
--   1. 重建 games 表: 加 4 个新列 + category 的 CHECK 约束
--   2. 新建 endgame_positions / endgame_candidates / endgame_position_moves / endgame_tasks
--
-- 为什么 games 要重建而不是 ALTER TABLE ADD COLUMN:
--   SQLite 的 ALTER TABLE 只能加列, 加不了 CHECK 约束, 也给不了非恒定的 DEFAULT。
--   这里走 sqlite.org 官方推荐的"重建表"流程, 由 migrate.py 在事务外先关掉
--   foreign_keys 并打开 legacy_alter_table (否则 ALTER ... RENAME 会把 moves 表里
--   指向 games 的外键改写成指向临时表名)。
--
-- category 取值: 规格只要求 game/endgame/pattern/custom, 但库里已有
--   opening / study / imported 三类历史数据, 收窄会让旧行违约,
--   因此这里并集为 8 个值 —— 旧数据无需改写, 现有筛选逻辑不受影响。
--
-- updated_at 的默认值用 datetime('now','localtime'): 应用写入的一直是本地时间
--   字符串, 用 UTC 的 datetime('now') 会让两种来源的时间混在一起排不对序。

-- ----------------------------------------------------------------------
-- 1. 重建 games
-- ----------------------------------------------------------------------
CREATE TABLE games_migrated (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT,
  category      TEXT DEFAULT 'game'
                CHECK (category IN ('game', 'opening', 'endgame', 'study',
                                    'imported', 'pattern', 'custom')),
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
  updated_at    TEXT DEFAULT (datetime('now', 'localtime')),
  -- ↓ 0001 新增
  study_id            INTEGER,
  fen_text            TEXT,
  rule_set            TEXT DEFAULT 'chinese_2020_analysis',
  opening_position_id INTEGER
);

INSERT INTO games_migrated
  (id, name, category, start_fen, current_fen, side_to_move, result, status,
   red_name, black_name, event, site, date, round, opening, ecco, tags, note,
   created_at, updated_at)
SELECT
   id, name, category, start_fen, current_fen, side_to_move, result, status,
   red_name, black_name, event, site, date, round, opening, ecco, tags, note,
   created_at, updated_at
FROM games;

DROP TABLE games;
ALTER TABLE games_migrated RENAME TO games;

CREATE INDEX IF NOT EXISTS idx_games_category_updated ON games(category, updated_at);
CREATE INDEX IF NOT EXISTS idx_games_tags ON games(tags);
CREATE INDEX IF NOT EXISTS idx_games_study ON games(study_id);

-- ----------------------------------------------------------------------
-- 2. 分析结果 (一个局面 + 一套分析配置 = 一行)
--    唯一键把"深度/MultiPV/规则/时限"都算进去, 保证不同配置的结果不会互相覆盖。
--    engine_version / nnue_file 用 '' 而不是 NULL: SQLite 的唯一索引里
--    NULL 互不相等, 留 NULL 会让同一配置重复插入。
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endgame_positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  ply           INTEGER NOT NULL,
  fen           TEXT NOT NULL,
  position_hash TEXT NOT NULL,
  to_move       TEXT NOT NULL CHECK (to_move IN ('r', 'b')),
  repetition_rule TEXT NOT NULL DEFAULT 'chinese_2020_analysis',
  engine_version TEXT NOT NULL DEFAULT '',
  nnue_file     TEXT NOT NULL DEFAULT '',
  depth         INTEGER NOT NULL DEFAULT 0,
  seldepth      INTEGER,
  movetime_ms   INTEGER NOT NULL DEFAULT 0,
  multipv       INTEGER NOT NULL DEFAULT 0,
  rule_aware    INTEGER NOT NULL DEFAULT 0,
  bestmove      TEXT,
  best_pv_uci   TEXT,
  score_type    TEXT CHECK (score_type IN ('cp', 'mate', 'draw', 'unknown')),
  score_value   INTEGER,
  normalized_win REAL,
  result        TEXT CHECK (result IN ('win', 'loss', 'draw',
                                       'unknown', 'needs_rule_review')),
  result_for    TEXT CHECK (result_for IN ('r', 'b')),
  mate_plies    INTEGER,
  draw_type     TEXT,
  rule_review_status TEXT NOT NULL DEFAULT 'none'
                CHECK (rule_review_status IN ('none', 'pending', 'human_verified', 'override')),
  confidence    TEXT NOT NULL DEFAULT 'preliminary'
                CHECK (confidence IN ('preliminary', 'stable', 'rule_review_needed', 'verified')),
  analysis_duration_ms INTEGER,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  UNIQUE (game_id, ply, fen, engine_version, repetition_rule,
          depth, movetime_ms, multipv)
);

CREATE INDEX IF NOT EXISTS idx_eg_pos_game_ply ON endgame_positions(game_id, ply);
CREATE INDEX IF NOT EXISTS idx_eg_pos_hash ON endgame_positions(position_hash);
CREATE INDEX IF NOT EXISTS idx_eg_pos_game_hash ON endgame_positions(game_id, position_hash);

-- ----------------------------------------------------------------------
-- 3. 候选着法 (MultiPV 每一路一行)
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endgame_candidates (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id    INTEGER NOT NULL REFERENCES endgame_positions(id) ON DELETE CASCADE,
  rank           INTEGER NOT NULL,
  multipv_index  INTEGER,
  move_uci       TEXT NOT NULL,
  from_square    TEXT,
  to_square      TEXT,
  promotion      TEXT,
  score_type     TEXT CHECK (score_type IN ('cp', 'mate', 'draw', 'unknown')),
  score_value    INTEGER,
  mate_plies     INTEGER,
  pv_uci         TEXT,
  pv_san         TEXT,
  visited_nodes  INTEGER,
  is_key_move    INTEGER NOT NULL DEFAULT 0,
  is_user_variation INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  UNIQUE (position_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_eg_cand_position ON endgame_candidates(position_id, rank);

-- ----------------------------------------------------------------------
-- 4. 局面 <-> 走法 的关联 (只引用 moveId, 关键步复用现有 moves.is_key 语义)
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endgame_position_moves (
  position_id INTEGER NOT NULL REFERENCES endgame_positions(id) ON DELETE CASCADE,
  move_id     INTEGER NOT NULL REFERENCES moves(id) ON DELETE CASCADE,
  role        TEXT NOT NULL CHECK (role IN ('key_move', 'critical_line', 'refutation')),
  PRIMARY KEY (position_id, move_id, role)
);

CREATE INDEX IF NOT EXISTS idx_eg_pos_moves_move ON endgame_position_moves(move_id);

-- ----------------------------------------------------------------------
-- 5. 分析任务 (状态机 + 取消 + 进度)
--    API 里的 taskId 必须能跨请求存活, 所以状态要落库。
--    endgame_positions 只存"分析结果", 任务的中间态放在这里,
--    这样同一局面可以用不同 depth/multipv/规则反复分析而互不覆盖。
-- ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS endgame_tasks (
  id            TEXT PRIMARY KEY,              -- taskId (uuid4 hex)
  study_id      INTEGER NOT NULL,              -- 研究根对局 id
  game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  ply           INTEGER NOT NULL,
  fen           TEXT NOT NULL,
  position_hash TEXT NOT NULL,
  mode          TEXT NOT NULL DEFAULT 'standard'
                CHECK (mode IN ('quick', 'standard', 'precise', 'root_moves')),
  depth         INTEGER NOT NULL,
  movetime_ms   INTEGER NOT NULL,
  multipv       INTEGER NOT NULL,
  search_moves  TEXT,                          -- 逗号分隔的 uci 着法; NULL = 不限制
  rule_options  TEXT,                          -- JSON: 规则层开关(不是引擎选项)
  repetition_rule TEXT NOT NULL DEFAULT 'chinese_2020_analysis',
  priority      INTEGER NOT NULL DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'queued', 'running', 'aggregating',
                                  'completed', 'failed', 'cancelled', 'superseded')),
  progress_json TEXT,                          -- 最新一条 info 的摘要
  partial_best  TEXT,                          -- 未跑完时的当前最佳着法
  position_id   INTEGER REFERENCES endgame_positions(id) ON DELETE SET NULL,
  error_code    TEXT,
  message       TEXT,
  engine_version TEXT,
  nnue_file     TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
  started_at    TEXT,
  finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_eg_tasks_game ON endgame_tasks(game_id, ply);
CREATE INDEX IF NOT EXISTS idx_eg_tasks_status ON endgame_tasks(status, priority DESC, created_at);
