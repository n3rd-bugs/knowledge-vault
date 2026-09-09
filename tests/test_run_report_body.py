"""The nightly report's BODY carries scalar counts only (2026-09-09 regression).

The report item is chunked and embedded like any other item. Wave H.1 added
per-case eval telemetry keyed by the golden QUERY TEXT to the counts, and the
body writer dumped every key — so each nightly report contained all 52 golden
queries verbatim and, four reports later, the reports themselves filled the
top-5 for every golden query (hit@5 0.981 -> 0.923, MRR 0.927 -> 0.854, a
test-set leak, not a retrieval regression). Nested values stay in meta.
"""

from __future__ import annotations

from kbcore.db import connect
from kbworker import run_report


def test_report_body_omits_nested_telemetry_but_meta_keeps_it(kb):
    counts = {
        "queued_drained": 3,
        "eval_mrr": 0.9269,
        "eval_case_ranks": {"service dies at boot but starts fine manually afterwards": 1},
        "eval_case_rerank": {"service dies at boot but starts fine manually afterwards": "active"},
        "eval_top5_item_ids": [1, 2, 3],
        "eval_rerank_tally": {"active": 51, "fallback": 0, "off": 1},
        "stopped": {},
    }
    report_id = run_report._write_report(dict(counts))
    with connect() as conn:
        body, meta = conn.execute(
            "SELECT body, meta FROM items WHERE id = %s", (report_id,)
        ).fetchone()
    assert "- queued_drained: 3" in body and "- eval_mrr: 0.9269" in body
    assert "service dies at boot" not in body, "golden query text must never be indexed"
    assert "eval_case_ranks" not in body and "eval_top5_item_ids" not in body
    assert meta["eval_case_ranks"] == counts["eval_case_ranks"], "meta keeps the telemetry"
    assert meta["eval_rerank_tally"]["active"] == 51
