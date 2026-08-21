-- 019_search_miss_relevance.sql — relevance-based search-miss instrument
-- (review #5 val-1 / ret-miss-1 / ops-2 / strat-1; extends migration 018).
-- ADDITIVE only (CLAUDE.md rule 5): three new nullable-or-defaulted columns on
-- the existing telemetry table, no changes to any other object.
--
-- Why: the Wave-E instrument only saw EMPTY result sets, and the vector leg has
-- no similarity floor — any non-degenerate corpus returns *something* — so the
-- zero-hit count read 0 while agents were still walking away with irrelevant
-- answers. Each row now carries a subtype:
--
--   'empty'       — the Wave-E zero-hit miss (filter-emptied searches; the
--                   proven hook mechanics). Existing rows backfill to this via
--                   the column DEFAULT.
--   'thin'        — the search RETURNED results but the top answer was judged
--                   irrelevant: the rerank sidecar scored it below
--                   KB_MISS_RELEVANCE_FLOOR, or (sidecar absent) the top fused
--                   candidate had no keyword/identifier-leg contribution.
--                   Recorded only while the floor dial is ON (> 0).
--   'calibration' — floor dial OFF (the calibration-week default): the would-be
--                   top relevance score is recorded per sidecar-scored search so
--                   a week of real data sizes the floor. NOT a miss — excluded
--                   from every miss count/table by default.
--
-- top_score: the sidecar's top-1 relevance score when it scored this search
-- (NULL on the sidecar-absent path — fused RRF scores are a different scale and
-- deliberately never enter this column: a fused-score floor is unreachable by
-- construction, review #5 guard). top_legs: leg provenance of the top hit
-- ('vector', 'vector+keyword', ...; NULL for 'empty' rows, which have no hits).
--
-- The migration-018 retention carve-out covers these columns unchanged: still
-- telemetry, still swept by kbcore.search_misses.sweep_old after 90 days.

ALTER TABLE search_misses
  ADD COLUMN IF NOT EXISTS subtype TEXT NOT NULL DEFAULT 'empty';
ALTER TABLE search_misses
  ADD COLUMN IF NOT EXISTS top_score DOUBLE PRECISION;
ALTER TABLE search_misses
  ADD COLUMN IF NOT EXISTS top_legs TEXT;
