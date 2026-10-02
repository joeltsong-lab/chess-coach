-- 0001_endgame_study (down): 回滚残局研究迁移
--
-- 注意 1: category 是**有损回滚**。CHECK 收窄回原来的"无约束"没问题, 但
--   pattern / custom 这两个新分类在旧结构里没有对应值, 这里统一并回 'game',
--   否则回滚后这两类研究会从列表的默认筛选里消失。回滚前请自行确认。
-- 注意 2: 新加的 study_id / fen_text / rule_set / opening_position_id 四列会随表重建丢掉。
--   回滚前需要保留的话, 先导出 json (GET /api/games/<id>/export?fmt=json)。
-- 注意 3: 四张 endgame_* 表会整表删除, 分析结果与候选着法一并丢失。

-- ----------------------------------------------------------------------
-- 1. 新分类并回 game (有损)
-- ----------------------------------------------------------------------
UPDATE games SET category = 'game' WHERE category IN ('pattern', 'custom');

-- ----------------------------------------------------------------------
-- 2. 删掉 0001 建的表 (先删引用方, 再删被引用方)
-- ----------------------------------------------------------------------
DROP TABLE IF EXISTS endgame_position_moves;
DROP TABLE IF EXISTS endgame_candidates;
DROP TABLE IF EXISTS endgame_tasks;
DROP TABLE IF EXISTS endgame_positions;

DROP INDEX IF EXISTS idx_games_study;

-- ----------------------------------------------------------------------
-- 3. games 还原成 0001 之前的定义 (20 列, category 无 CHECK)
-- ----------------------------------------------------------------------
CREATE TABLE games_migrated (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT,
  category      TEXT DEFAULT 'game',
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
  updated_at    TEXT
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
