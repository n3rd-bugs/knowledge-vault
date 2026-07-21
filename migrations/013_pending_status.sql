-- 013_pending_status.sql — the 'pending' quarantine status for web-authored items.
-- ADDITIVE only (Invariant 5): widens the items.status allow-list by dropping and
-- recreating the SAME named CHECK constraint with one extra value. No row is
-- touched, no existing status value removed, no data rewritten — every existing
-- 'active'/'superseded'/'archived'/'duplicate' row still satisfies the new CHECK.
--
-- Why (A11-3 finding 6 — indirect prompt-injection channel): a NON-owner web save
-- must be quarantined from RETRIEVAL until the OWNER promotes it, or forged content
-- is served to every agent kb_search the moment it lands (before any human review).
-- 'pending' is that quarantine state: a web-authored item lands
-- status='pending' + trust='unverified' + a review_queue row, is ABSENT from the
-- default search status set (["active"], kbcore.search — one chokepoint, all legs),
-- and is flipped to 'active' only by review.promote (owner-only). reject archives it.
--
-- The 001 inline column CHECK is auto-named <table>_<column>_check = items_status_check
-- (same convention 008 used for review_queue_reason_check). Drop-if-exists then
-- re-add under that exact name so the widened constraint fully replaces the old one.

ALTER TABLE items DROP CONSTRAINT IF EXISTS items_status_check;
ALTER TABLE items ADD CONSTRAINT items_status_check
  CHECK (status IN ('active', 'superseded', 'archived', 'duplicate', 'pending'));
