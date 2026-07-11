-- 009_ingest_queue_defer_requeue.sql — bound the two unbounded queue loops.
-- ADDITIVE only (Invariant 5): two nullable-with-default counters, no constraint
-- changes.
--
-- Why: two paths could burn the nightly window forever.
--  1) A payload-DETERMINISTIC 5xx / mid-response transport crash is classified
--     'transient' and DEFERRED (row stays 'pending', attempts untouched), so it
--     re-drained every night with no ceiling. `deferrals` counts consecutive
--     defers; once it reaches the code-side DEFERRAL_CAP (>> MAX_ATTEMPTS) the
--     drain escalates the row to 'failed' so the attempts cap can retire it.
--  2) Resubmitting content whose only row is TERMINALLY failed re-armed it to
--     'pending'/attempts=0 unboundedly (a recurring poison payload). `requeue_count`
--     bounds those re-arms; past the code-side cap enqueue reports the row
--     permanently failed instead of re-opening it.
-- Both are status/counter flips only (Invariant 1/5). The counters default 0 so
-- existing rows and idempotency are untouched.

ALTER TABLE ingest_queue ADD COLUMN IF NOT EXISTS deferrals     INT NOT NULL DEFAULT 0;
ALTER TABLE ingest_queue ADD COLUMN IF NOT EXISTS requeue_count INT NOT NULL DEFAULT 0;
