-- 005_ingest_queue_attempts.sql — bounded self-healing retry for the drain.
-- ADDITIVE only (Invariant 5): one nullable-with-default column, no constraint
-- changes.
--
-- Why: a row that hit a NON-transient error was marked 'failed' and never
-- re-drained; because the dedupe UNIQUE(kind,project,content_hash) spans failed
-- rows, a re-submission silently deduped, so the content was stranded. With an
-- attempt counter the nightly drain re-tries a failed row a few times (a
-- genuinely transient DB hiccup / OOM recovers itself) and then gives up — so
-- the service self-heals without any operator requeue tool. Transient
-- extractor/embedder outages are handled separately (left 'pending', unlimited
-- retry until the model is back), so this counter only bounds real failures.

ALTER TABLE ingest_queue ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0;

-- Fast selection of the drain-eligible set (pending OR retryable-failed).
CREATE INDEX IF NOT EXISTS ingest_queue_drainable_idx
  ON ingest_queue (attempts) WHERE status IN ('pending', 'failed');
