"""A nightly run report's NESTED meta is the owner's (E2 fix-round review H1).

The report stores the run's whole counts dict as meta: per-case eval telemetry
keyed by golden query text, the ids / types / trust of items in projects the
reader may not see, fleet maps keyed by project names. A non-owner — a minted
``kbg_`` token with a kb-system read grant — reads that meta reduced to its BODY
scalars (kbcore.report_meta.body_scalars: what the indexed body says, plus the
``consolidation`` flag) on ``GET /api/items/{id}`` and ``GET /api/timeline``; the
owner still reads all of it, and no other item's meta changes. Synthetic query
text only; real kb_test DB.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from kbcore import principals
from kbcore.config import settings
from kbcore.ingest import ingest
from kbworker import run_report

TEST_TOKEN = "kbtest-" + "0" * 32
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}
Q = "which bracket torque survives the long synthetic soak"  # synthetic, never golden


@pytest.fixture()
def rest(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbrest.server import build_app

    return TestClient(build_app())


def _grantee() -> dict[str, str]:
    _pid, token = principals.create_principal("report-reader", kind="token", role="reader")
    principals.grant("report-reader", "kb-system", rw=False)
    return {"Authorization": f"Bearer {token}"}


def test_a_grantee_reads_a_run_reports_meta_as_its_body_scalars_only(rest):
    hidden = ingest("hw-lab", "note", "Bracket torque", "forty newton metres in a star pattern")
    counts = {
        "eval_status": "ok",
        "eval_hit_at_5": 0.95,
        "eval_embed_device": "auto",
        "eval_case_ranks": {Q: 1},
        "eval_case_top5": {Q: [hidden]},
        "eval_case_satisfier": {Q: hidden},
        "eval_case_above_satisfier": {Q: [[hidden, "note", "verified"]]},
        "eval_satisfier_ids": [hidden],
        "eval_case_persistent": [{"case": 1, "satisfier": hidden, "since": "2026-11-02"}],
        "fleet_by_box": {"hw-lab": {"box-a": 3}},
    }
    report = run_report._write_report(dict(counts))
    grantee = _grantee()
    assert rest.get(f"/api/items/{hidden}", headers=grantee).status_code == 404, "the wall"
    # The body's scalars only: the embed identity is a meta-only scalar (FB-1).
    scalars = {"consolidation": True, "eval_status": "ok", "eval_hit_at_5": 0.95}
    got = rest.get(f"/api/items/{report}", headers=grantee)
    assert got.status_code == 200 and got.json()["meta"] == scalars, "the body's facts only"
    assert Q not in got.text and "hw-lab" not in got.text
    timeline = rest.get("/api/timeline", params={"project": "kb-system"}, headers=grantee)
    (entry,) = [e for e in timeline.json() if e["item_id"] == report]
    assert entry["meta"] == scalars and Q not in timeline.text
    owner = rest.get(f"/api/items/{report}", headers=AUTH).json()["meta"]
    assert owner["eval_case_satisfier"] == {Q: hidden} and owner["fleet_by_box"]
    assert owner["eval_embed_device"] == "auto"
    owned = rest.get("/api/timeline", params={"project": "kb-system"}, headers=AUTH).json()
    assert Q in json.dumps([e["meta"] for e in owned if e["item_id"] == report])


def test_only_the_nightly_run_report_is_projected(rest):
    summary = ingest(
        "kb-system",
        "summary",
        "Summary - brackets",
        "the bracket notes, summarised",
        meta={"consolidation": True, "topic": "brackets", "source_ids": [7, 8]},
    )
    weekly = ingest(
        "kb-system", "report", "KB weekly report", "weekly", meta={"weekly_report": True, "x": [1]}
    )
    grantee = _grantee()
    assert rest.get(f"/api/items/{summary}", headers=grantee).json()["meta"]["source_ids"] == [7, 8]
    assert rest.get(f"/api/items/{weekly}", headers=grantee).json()["meta"]["x"] == [1]
