-- 014_web_contribution_reason.sql — the 'web_contribution' review reason.
-- ADDITIVE only (Invariant 5): widens the review_queue.reason allow-list; no data
-- is touched, no reason removed (mirrors 008's widening for 'search_regression').
--
-- Why (A11-3 findings 5/6/16): a non-owner web save lands as a quarantined
-- (status='pending') unverified item ATOMICALLY with its review_queue row
-- (kbcore.web_write.save_for_review / supersede_for_review — one transaction). The
-- row carries reason='web_contribution' so reviewer triage can tell a human
-- WEB-authored contribution apart from an automatic 'unverified_extraction';
-- mislabeling it as the latter would corrupt triage semantics. Keeps the DB CHECK
-- in sync with kbcore.models.REVIEW_REASONS.

ALTER TABLE review_queue DROP CONSTRAINT IF EXISTS review_queue_reason_check;
ALTER TABLE review_queue ADD CONSTRAINT review_queue_reason_check
  CHECK (reason IN ('unverified_extraction', 'contradiction', 'dup_cluster',
                    'search_regression', 'web_contribution'));
