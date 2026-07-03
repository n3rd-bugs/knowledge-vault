-- 003_project_public.sql — the public/cloud allow-list (Phase 6 safety gate).
-- ADDITIVE + idempotent. Everything starts PRIVATE (no backfill): a public-only
-- instance (KB_PUBLIC_ONLY=true) sees ONLY projects explicitly flagged public.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS public BOOLEAN NOT NULL DEFAULT false;
