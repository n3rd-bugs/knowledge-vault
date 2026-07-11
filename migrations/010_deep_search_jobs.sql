-- 010_deep_search_jobs.sql — async deep-search job queue. ADDITIVE only.
--
-- kb_deep_search(wait=false) enqueues a 'pending' job and returns immediately; the
-- host worker (kb-worker, the only process with the GPU) claims it atomically, runs
-- kbcore.deepsearch.deep_search(), and stores the saved report item id + text in
-- `result`. This lets claude.ai enqueue-and-poll (kb_deep_search_status) instead of
-- blocking a 1-5 minute run past Anthropic's mcp-proxy ~100s timeout. It is explicit
-- user-invoked LLM work, so the GPU use is Invariant-8-compliant.
--
-- Why a dedicated table (not consolidation_requests): that table has no column for
-- the job INPUT (question/params), and its claim query would grab these rows for
-- consolidate.main(). A separate table keeps the two queues from colliding.

CREATE TABLE IF NOT EXISTS deep_search_jobs (
  id           BIGSERIAL PRIMARY KEY,
  question     TEXT NOT NULL,
  params       JSONB NOT NULL DEFAULT '{}',
  requested_by TEXT NOT NULL DEFAULT 'unknown',
  status       TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'failed')),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ,
  result       JSONB
);

-- Fast lookup of the claimable (pending) rows for the worker's SKIP LOCKED claim.
CREATE INDEX IF NOT EXISTS deep_search_jobs_pending_idx
  ON deep_search_jobs (id) WHERE status = 'pending';
