-- 020_ingest_queue_chunk_key.sql — server-side resend guard for transcript chunks
-- (flush proposal v2, KB 33784; root cause KB 33783). ADDITIVE only (CLAUDE.md rule 5):
-- nullable columns, indexes, the status CHECK widened by ONE value (the 013 move), and
-- one BOUNDED non-knowledge ledger. No row is rewritten.
--
-- Why: a session resumed after a clean SessionEnd re-sent its whole transcript from
-- byte 0 under whatever project the firing-time cwd resolved to, and the only guard was
-- UNIQUE (kind, project, content_hash) — so the resend was all new rows and a full GPU
-- re-extraction under the wrong project (891 MB of exact duplicate chunks in 90 days).
-- kbcore.queue_guard now dedupes transcript chunks by (session uri, content_hash)
-- regardless of project; this file gives it the columns and indexes it needs.

ALTER TABLE ingest_queue ADD COLUMN IF NOT EXISTS chunk_key TEXT;
-- The canonical row a later duplicate points at. Plain BIGINT, deliberately NO foreign
-- key: the owner repair backfills it over ~1,070 historical rows, and a FK would tie the
-- (never deleted, Inv 5) canonical's lifecycle to rows written under another project.
ALTER TABLE ingest_queue ADD COLUMN IF NOT EXISTS duplicate_of BIGINT;
-- Race guard for rows written AFTER 020. NULL on every pre-020 row on purpose: the live
-- table already holds cross-project duplicates, so a unique index over existing rows
-- could not build; `kb repair resend-dups --backfill-keys` stamps historical canonicals.
CREATE UNIQUE INDEX IF NOT EXISTS ingest_queue_chunk_key_uidx
  ON ingest_queue (chunk_key) WHERE chunk_key IS NOT NULL;
-- The guard's lookup: the 002 UNIQUE leads with (kind, project) and cannot serve a
-- project-less probe.
CREATE INDEX IF NOT EXISTS ingest_queue_content_hash_idx ON ingest_queue (content_hash);
-- Session-identity lookups spelled exactly as queue_groups.py spells the group key.
CREATE INDEX IF NOT EXISTS ingest_queue_session_uri_idx
  ON ingest_queue ((btrim(meta->>'source_uri'))) WHERE kind = 'session';
CREATE INDEX IF NOT EXISTS ingest_queue_duplicate_of_idx
  ON ingest_queue (duplicate_of) WHERE duplicate_of IS NOT NULL;
-- 'duplicate': the terminal status ONLY the owner repair sets on a still-pending resend.
-- Every existing row satisfies the new CHECK; _DRAINABLE (queue_groups), queue_depth and
-- requeue_failed/requeue_extracted ignore it by construction (they name their statuses).
ALTER TABLE ingest_queue DROP CONSTRAINT IF EXISTS ingest_queue_status_check;
ALTER TABLE ingest_queue ADD CONSTRAINT ingest_queue_status_check
  CHECK (status IN ('pending', 'extracted', 'failed', 'duplicate'));

-- Refused-resend ledger: ONE row per (principal, session uri), UPSERTed on every refusal,
-- so the nightly digest can name the box that keeps resending. Bounded by sessions x
-- principals (never by refused chunks), holds no content, is written as the SUBMITTER
-- (never on the canonical row, which may belong to another project's bearer), and is
-- never swept or deleted — it is not a knowledge store, so the rule-5 carve-out list
-- is unchanged.
CREATE TABLE IF NOT EXISTS ingest_resend_events (
  principal      TEXT        NOT NULL,
  uri            TEXT        NOT NULL,
  first_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  refused_rows   BIGINT      NOT NULL DEFAULT 0,
  refused_bytes  BIGINT      NOT NULL DEFAULT 0,
  last_kind      TEXT,
  last_project   TEXT,
  last_box       TEXT,
  last_canonical BIGINT,
  PRIMARY KEY (principal, uri)
);
CREATE INDEX IF NOT EXISTS ingest_resend_events_last_at_idx ON ingest_resend_events (last_at);
