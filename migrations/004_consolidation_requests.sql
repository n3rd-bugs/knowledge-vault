-- 004_consolidation_requests.sql — on-demand nightly-run trigger. ADDITIVE only.
--
-- The web button / kb_consolidate MCP tool / `kb consolidate-now` CLI insert a
-- 'pending' row; the host worker (kb-worker, the only process with the GPU)
-- claims it atomically and runs consolidate.main(), recording the result. This
-- decouples the trigger surfaces from execution — nothing but the host worker
-- runs the extractor (Invariant 8), and the read-only web tier only causes a
-- REST write, never runs the job.

CREATE TABLE IF NOT EXISTS consolidation_requests (
  id           BIGSERIAL PRIMARY KEY,
  requested_by TEXT NOT NULL DEFAULT 'unknown',
  status       TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'running', 'done', 'failed')),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ,
  result       JSONB
);

-- Fast lookup of the (at most one) claimable row.
CREATE INDEX IF NOT EXISTS consolidation_requests_pending_idx
  ON consolidation_requests (id) WHERE status = 'pending';
