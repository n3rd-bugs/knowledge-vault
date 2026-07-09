-- 007_relations_dst_idx.sql — index in-edge neighbor lookups (WP2 graph expansion).
-- ADDITIVE only (Invariant 5): one index, no schema/constraint changes.
--
-- Why: graph._neighbors' in-edge lookup (WHERE dst_kind = %s AND dst_id = %s)
-- has no supporting index and seq-scans `relations` today. Out-edges already
-- ride the UNIQUE(src_kind, src_id, rel, dst_kind, dst_id) prefix
-- (src_kind, src_id), so only the reverse (dst) direction needs this. n-hop BFS
-- from a search hit does one in-edge lookup per frontier node, so the scan cost
-- compounds without it.

CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst_kind, dst_id);
