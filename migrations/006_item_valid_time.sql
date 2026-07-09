-- 006_item_valid_time.sql — bitemporal VALID-TIME on items (WP1).
-- ADDITIVE only (Invariant 5): two nullable columns + one index, no constraint
-- changes.
--
-- Why: created_at/supersession track TRANSACTION time (when the KB learned a
-- fact). valid_from/valid_to add the orthogonal VALID-TIME axis — when the fact
-- was true in the world. Both default NULL (author/extractor-supplied metadata):
--   NULL valid_from = unbounded past (always been true)
--   NULL valid_to   = still true (open-ended)
-- A fact is "valid as of T" iff
--   (valid_from IS NULL OR valid_from <= T) AND (valid_to IS NULL OR valid_to > T)
-- (half-open interval: valid_to is exclusive). Search's as_of filter applies
-- exactly this predicate. The feature is inert unless as_of / the columns are
-- supplied. supersede() does NOT auto-close valid_to (deferred, by design).

ALTER TABLE items ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ;
ALTER TABLE items ADD COLUMN IF NOT EXISTS valid_to   TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS items_valid_idx ON items (valid_from, valid_to);
