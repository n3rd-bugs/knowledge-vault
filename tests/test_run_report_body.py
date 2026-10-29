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


def test_e2_adds_no_body_line(kb):
    """E2 final reviews FB-1 / F1: every scalar E2 introduced is search vocabulary no
    earlier report carried (key-name words, model, hosts, runtime), and one report
    lands every night — so all of them live in meta only (report_meta.body_scalars)."""
    counts = {
        "eval_mrr": 0.865,
        "eval_regression": False,
        "run_started_at": "2026-10-30T20:30:01+00:00",
        "eval_echo_skipped": 1,
        "eval_regression_epoch_closed": 35091,
        "eval_regression_epoch_closed_count": 2,
        "eval_persistence_status": "error",
        "eval_replay_telemetry_status": "error",
        "eval_embed_model": "probe-model:0.6b",
        "eval_embed_hosts_key": "http://10.9.8.7:11434,http://127.0.0.1:11434",
        "eval_embed_host": "http://10.9.8.7:11434",
        "eval_embed_fingerprint": "0123456789ab",
        "eval_embed_runtime": "ollama 0.40.11 probe-model:0.6b@790764642607",
        "eval_embed_device": "auto",
        "eval_floor_hit_at_5": 0.95,
        "eval_floor_mrr": 0.845,
        "eval_floors_at_defaults": True,
    }
    report_id = run_report._write_report(dict(counts))
    with connect() as conn:
        body, meta = conn.execute(
            "SELECT body, meta FROM items WHERE id = %s", (report_id,)
        ).fetchone()
    assert body.splitlines()[2:] == ["- eval_mrr: 0.865", "- eval_regression: False"]
    assert "probe-model" not in body and "10.9.8.7" not in body and "ollama" not in body
    assert {k: meta[k] for k in counts} == counts, "meta keeps every one of them"
