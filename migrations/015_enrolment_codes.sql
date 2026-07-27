-- 015_enrolment_codes.sql — one-time codes that let a NEW machine fetch its own
-- session-hook credential without a secret ever being pasted by hand.
-- ADDITIVE only (Invariant 5): one new table, nothing existing altered.
--
-- WHY a code instead of just handing over the token. kb_setup answers over the
-- claude.ai connector, so anything it returns lands in a chat transcript — and
-- the session hooks flush transcripts INTO the KB, which is then backed up
-- off-site. A live bearer travelling that path is durable in three places at
-- once. An enrolment code is single-use and short-lived, so once the machine has
-- redeemed it the transcript holds a dead string.
--
-- The code is a CREDENTIAL, so it follows the same rules as every other one here:
--   * only the SHA-256 of the plaintext is stored; the plaintext is shown once;
--   * redemption is a single atomic UPDATE ... WHERE redeemed_at IS NULL, so two
--     racing redemptions cannot both succeed (first wins, second is refused);
--   * expiry/redemption are TIMESTAMPTZ flips, never DELETEs (Invariant 5/7);
--   * invalid / expired / already-redeemed are INDISTINGUISHABLE to the caller
--     (the no-oracle contract) — the distinction is recorded server-side only.
--
-- Authority: a code NEVER carries more than the identity that requested it.
-- `grants_owner` is set only when an authenticated OWNER asked for it, and it is
-- the reason redemption can return the master credential — the owner already had
-- it. A grantee's code mints a fresh per-machine `kbg_` principal instead, so
-- every machine is revocable on its own.

CREATE TABLE IF NOT EXISTS enrolment_codes (
  id            BIGSERIAL PRIMARY KEY,
  code_hash     TEXT NOT NULL UNIQUE,      -- sha256 hex of the 'kbe_…' plaintext
  issued_to     TEXT NOT NULL,             -- audit label of the requesting identity
  grants_owner  BOOLEAN NOT NULL DEFAULT false,
  -- The requesting GRANTEE (NULL when the requester was the owner). The machine
  -- principal minted at redemption inherits exactly this grantee's grants.
  principal_id  BIGINT REFERENCES principals(id) ON DELETE CASCADE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,
  redeemed_at   TIMESTAMPTZ,
  redeemed_from TEXT,                      -- client address that redeemed it (audit)
  redeemed_note TEXT,                      -- machine label supplied at redemption
  -- The per-machine principal handed out by this redemption (NULL for an owner
  -- code, which returns the master, and NULL until redeemed).
  issued_principal_id BIGINT REFERENCES principals(id) ON DELETE SET NULL,
  -- An owner code has no requesting grantee, and vice versa: a row can never be
  -- a half-formed grant of authority.
  CHECK (NOT (grants_owner AND principal_id IS NOT NULL))
);

-- Redemption looks the code up by hash; expiry sweeps scan by expires_at.
CREATE INDEX IF NOT EXISTS enrolment_codes_expires_idx ON enrolment_codes(expires_at);
