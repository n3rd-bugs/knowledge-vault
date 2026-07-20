-- 012_principal_roles.sql — role (capability-bundle) column for tiered web/tool access.
-- ADDITIVE only (Invariant 5): one new column with a default + CHECK; no row deleted,
-- no existing constraint changed, no data rewritten except the stated backfill.
--
-- Model (WEBUI_TIERED_ACCESS_PLAN §2): a principal's ROLE names the FEATURE bundle
-- that code expands into capabilities at resolution time (kbcore.scope.ROLE_CAPS —
-- roles-as-bundles keep `kb principal list` auditable; no caps join table, no second
-- cache-invalidation path). Project grants (principal_grants) keep gating WHICH ROWS
-- are visible inside a permitted feature — the two walls AND, they never substitute.
--
-- 'owner' is deliberately NOT a valid value: the owner is not a row (011 header) —
-- ownership stays credential-derived (master KB_TOKEN constant-time match /
-- allowlisted email), and kbcore.principals._build_grantee hardcodes is_owner=False,
-- so NO forged/injected DB row can ever resolve to owner. This frozen CHECK is the
-- escalation firewall.
--
-- 'reader' as the default is behavior-preserving: today's grantee surface is exactly
-- the search+browse feature set (every non-owner-only view/tool), so every existing
-- row keeps exactly the access it has.

ALTER TABLE principals
  ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'reader'
    CONSTRAINT principals_role_check
    CHECK (role IN ('guest', 'reader', 'contributor', 'reviewer'));

-- Backfill: an rw-grantee can kb_save TODAY (assert_writable is the only wall,
-- kbcore/scope.py), so seed those rows as 'contributor' — this migration never
-- silently revokes a working credential. Idempotent: only rows still at 'reader'
-- match, so a re-run against the migrated state is a no-op.
UPDATE principals SET role = 'contributor'
 WHERE role = 'reader'
   AND id IN (SELECT principal_id FROM principal_grants WHERE rw);
