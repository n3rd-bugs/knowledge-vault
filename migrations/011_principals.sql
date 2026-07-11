-- 011_principals.sql — per-project sharing with dedicated credentials.
-- ADDITIVE only (Invariant 5): three new objects + one nullable column, no
-- constraint changes to existing tables, no data rewritten.
--
-- Model: the OWNER is NOT a row. The master KB_TOKEN and the allowlisted web/
-- OAuth emails (KB_WEB_ALLOWED_EMAILS) resolve to an IMPLICIT owner principal with
-- unrestricted access — exactly today's behavior. A `principals` row is a GRANTEE
-- with an explicit, walled project set. Zero principals rows ⇒ the system behaves
-- byte-identically to before this migration (backward compat is a hard requirement).
--
--   principals      — one credential-bearing identity (a `kbg_` bearer token, hash
--                     stored, OR a Google email for the claude.ai connector / web UI).
--   principal_grants — which projects that identity may read, and whether it may write.
--   access_log.principal — audit: the grantee NAME that performed each read (NULL =
--                     owner/legacy rows; kept a plain TEXT, not an FK, so the audit
--                     log stays append-simple and a deleted principal never breaks it).
--
-- Revocation/expiry are TIMESTAMPTZ flips (Invariant 5/7 — never a DELETE). Secrets
-- are NEVER stored: only the SHA-256 hex of a token's secret is persisted, and the
-- plaintext is shown exactly once at creation.

CREATE TABLE IF NOT EXISTS principals (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,        -- handle / audit label: 'alice-laptop', 'ci-agent'
  kind        TEXT NOT NULL CHECK (kind IN ('token','google')),
  secret_hash TEXT UNIQUE,                 -- sha256 hex of the 'kbg_…' secret (kind='token'); NULL for google
  email       TEXT UNIQUE,                 -- lowercased verified Google email (kind='google'); NULL for token
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at  TIMESTAMPTZ,                 -- NULL = no expiry; past ⇒ the credential no longer authenticates
  revoked_at  TIMESTAMPTZ,                 -- flip to revoke; next request stops authenticating (≤ cache TTL for tokens)
  note        TEXT,
  -- A token principal carries a secret_hash and no email; a google principal is the
  -- mirror. Enforced so a row can never be a half-formed credential.
  CHECK ((kind = 'token')  = (secret_hash IS NOT NULL)),
  CHECK ((kind = 'google') = (email IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS principal_grants (
  id           BIGSERIAL PRIMARY KEY,
  principal_id BIGINT NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
  project      TEXT NOT NULL,              -- no FK: a grant may pre-date the project's first save
                                           -- (projects auto-upsert on ingest); revocation is a
                                           -- principals-level flip, not a per-grant delete.
  rw           BOOLEAN NOT NULL DEFAULT false,  -- read-only unless true (write implies read)
  granted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (principal_id, project)
);
CREATE INDEX IF NOT EXISTS principal_grants_principal_idx ON principal_grants(principal_id);

-- Audit attribution (additive, NULL = owner/legacy). NAME, not FK: keep the log
-- append-only-simple and independent of the principals table's lifecycle.
ALTER TABLE access_log ADD COLUMN IF NOT EXISTS principal TEXT;
