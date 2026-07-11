-- 008_review_reason_search_regression.sql — allow the search-quality sentinel's
-- review reason. ADDITIVE only (Invariant 5): widens the review_queue.reason
-- allow-list; no data is touched, no capability removed.
--
-- Why: the nightly consolidation's regression sentinel (§8) enqueues a
-- 'search_regression' review row when hit@5/MRR drop vs the prior run, so the
-- owner sees it in `kb review list`. The reason CHECK from 001 permitted only
-- the three original reasons, so the INSERT would fail without this. Keeps the
-- DB CHECK in sync with kbcore.models.REVIEW_REASONS.

ALTER TABLE review_queue DROP CONSTRAINT IF EXISTS review_queue_reason_check;
ALTER TABLE review_queue ADD CONSTRAINT review_queue_reason_check
  CHECK (reason IN ('unverified_extraction', 'contradiction', 'dup_cluster', 'search_regression'));
