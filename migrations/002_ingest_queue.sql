-- 002_ingest_queue.sql — durable queue of remote submissions awaiting nightly
-- extraction (Invariant 8: the 14B runs only in consolidation). ADDITIVE only.
--
-- Content is stored ALREADY SCRUBBED at enqueue time (this is a public endpoint —
-- raw-with-secrets must never persist). Extraction happens in the nightly drain;
-- survivors land as trust='unverified' items in the review queue (V4/SEC-3).

CREATE TABLE ingest_queue (
  id BIGSERIAL PRIMARY KEY,
  project TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('session','memory')),
  content TEXT NOT NULL,              -- ALREADY SCRUBBED at enqueue time
  content_hash TEXT NOT NULL,         -- sha256 of scrubbed content — idempotency
  meta JSONB NOT NULL DEFAULT '{}',   -- {repo,branch,commit,redactions,source_uri}
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','extracted','failed')),
  saved_count INT,                    -- items ingested when extracted
  rejected_count INT,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  UNIQUE (kind, project, content_hash) -- re-submit of identical content = no-op
);

CREATE INDEX ingest_queue_pending_idx ON ingest_queue(status) WHERE status='pending';
