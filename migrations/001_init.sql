-- 001_init.sql — Knowledge Vault initial schema (PLAN.md §4)
-- Additive migrations only after this file (Invariant: migrations after 001 are additive).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE projects (                       -- DATA-8: registry, auto-upserted on first save
  name TEXT PRIMARY KEY,
  domain TEXT,                                -- embedded|web|devops|ml|desktop|teaching|...
  repos TEXT[] NOT NULL DEFAULT '{}',         -- local repo paths mapped to this project
  description TEXT,
  archived BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sources (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('manual','chat','file','session','git')),
  uri TEXT NOT NULL DEFAULT '',
  project TEXT NOT NULL,
  content_hash TEXT NOT NULL,                -- sha256 of normalized content
  trust TEXT NOT NULL DEFAULT 'verified' CHECK (trust IN ('verified','unverified')),
  raw_path TEXT,                              -- zstd original under ~/kb/raw (OPS-4)
  ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (kind, uri, content_hash)            -- DATA-1 idempotency
);

CREATE TABLE items (
  id BIGSERIAL PRIMARY KEY,
  source_id BIGINT REFERENCES sources(id),
  project TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN
    ('note','decision','spec','snippet','bug','gotcha','pattern','runbook','qa','config','summary','report')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,                          -- immutable after insert (Invariant 1)
  tags TEXT[] NOT NULL DEFAULT '{}',           -- namespaced, see §2.1
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','superseded','archived','duplicate')),
  superseded_by BIGINT REFERENCES items(id),
  supersede_reason TEXT,
  meta JSONB NOT NULL DEFAULT '{}',            -- {quote, redactions, extractor, code_ref, env, ...}
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX items_proj_status_idx ON items(project, status);
CREATE INDEX items_tags_idx ON items USING gin(tags);

CREATE TABLE chunks (
  id BIGSERIAL PRIMARY KEY,
  item_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  ord INT NOT NULL,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,  -- DATA-6: 'simple' for Urdu+English+identifiers
  embedding vector(1024),
  embed_model TEXT,                            -- DATA-3
  UNIQUE(item_id, ord)
);
CREATE INDEX chunks_tsv_idx ON chunks USING gin(tsv);
CREATE INDEX chunks_vec_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE entities (
  id BIGSERIAL PRIMARY KEY,
  project TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN
    ('component','library','service','tool','hardware','api','repo','protocol','concept','person','decision_topic')),
  summary TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(project, name, kind)
);

CREATE TABLE relations (
  id BIGSERIAL PRIMARY KEY,
  project TEXT NOT NULL,
  src_kind TEXT NOT NULL CHECK (src_kind IN ('entity','item')),
  src_id BIGINT NOT NULL,
  rel TEXT NOT NULL CHECK (rel IN
    ('depends_on','part_of','supersedes','implements','mentions','derived_from','contradicts',
     'fixes','alternative_to')),                -- DATA-10: bug↔fix, ADR alternatives
  dst_kind TEXT NOT NULL CHECK (dst_kind IN ('entity','item')),
  dst_id BIGINT NOT NULL,
  evidence_item BIGINT REFERENCES items(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(src_kind, src_id, rel, dst_kind, dst_id)
);

CREATE TABLE access_log (
  id BIGSERIAL PRIMARY KEY,
  item_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  tool TEXT NOT NULL, query TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE review_queue (
  id BIGSERIAL PRIMARY KEY,
  item_id BIGINT REFERENCES items(id) ON DELETE CASCADE,
  reason TEXT NOT NULL CHECK (reason IN ('unverified_extraction','contradiction','dup_cluster')),
  detail JSONB NOT NULL DEFAULT '{}',
  resolved BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE session_checkpoints (            -- DATA-1 for growing .jsonl transcripts
  path TEXT PRIMARY KEY,
  byte_offset BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
