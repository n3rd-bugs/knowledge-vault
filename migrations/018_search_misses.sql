-- 018_search_misses.sql — zero-hit search telemetry (review #4 Wave E, KB 21332).
-- ADDITIVE only (Invariant 5): one new table + one index, no changes to any
-- existing object.
--
-- Why: the KB could not see its own gaps. A search that returned NOTHING left
-- no trace, so the demand side — what agents ask for and never get — was
-- invisible while the supply side (access_log) was fully instrumented.
-- kbcore.search records every zero-hit query here (kbcore.search_misses.record;
-- tool != NULL only — the eval harness replays golden queries with tool=None,
-- the same exemption the access log applies, so eval replays never pollute
-- demand telemetry). Read back by the nightly digest ('search misses: N') and
-- the weekly trend report's top-misses table (kbworker.weekly).
--
-- RETENTION CARVE-OUT (CLAUDE.md rule 5, in the spirit of the enrolment-code
-- and upload-staging sweeps): this table is TELEMETRY, not knowledge — rows
-- carry no item content and would otherwise grow forever, so the weekly pass
-- MAY prune rows older than 90 days (kbcore.search_misses.sweep_old). Never
-- extend this carve-out to items/sources/chunks/relations/review_queue/
-- ingest_queue.

CREATE TABLE IF NOT EXISTS search_misses (
  id BIGSERIAL PRIMARY KEY,
  query TEXT NOT NULL,
  project_scope TEXT,
  tool TEXT NOT NULL,
  at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The digest's 24h count, the weekly's 7d window and the retention sweep all
-- scan by time.
CREATE INDEX IF NOT EXISTS search_misses_at_idx ON search_misses (at);
