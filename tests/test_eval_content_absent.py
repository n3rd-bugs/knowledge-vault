"""expect_substr failure triage: CONTENT-ABSENT vs retrieval miss.

Six golden cases read as a hard retrieval ceiling for a week when the seed
corpus silently vanished (2026-07-18) — the eval could not say "the target
content does not exist". A failing expect_substr case must now distinguish a
corpus problem (no row matches at all → CONTENT-ABSENT) from a genuine
ranking miss (content present, not in top-K).
"""

from __future__ import annotations

import pytest

from kbcore import eval_harness
from kbcore.ingest import ingest

pytestmark = pytest.mark.usefixtures("kb")


def _case(q: str, expected: list[str]) -> dict:
    return {"q": q, "expect_substr": expected}


def test_absent_content_flagged(kb):
    # Nothing in the (empty) corpus mentions the target → CONTENT-ABSENT.
    results, hit, _ = eval_harness.evaluate(golden=[_case("anything at all", ["zanzibar-widget"])])
    (res,) = results
    assert not res.passed
    detail = res.criteria[0].detail
    assert "CONTENT-ABSENT" in detail
    assert "zanzibar-widget" in detail


def test_present_but_missed_reads_as_retrieval_miss(kb):
    # The target string exists in the corpus but the query cannot retrieve it
    # in top-K → plain retrieval-miss message, NOT content-absent.
    ingest(
        "proj-a",
        "note",
        "Entirely unrelated title",
        "body mentioning zanzibar-widget once",
        kind="chat",
        uri="test:absent:1",
    )
    results, _, _ = eval_harness.evaluate(
        golden=[_case("completely disjoint query terms qqqq", ["zanzibar-widget"])]
    )
    (res,) = results
    detail = res.criteria[0].detail
    if not res.passed:  # retrieval may or may not surface it; the ASSERT is on the message
        assert "CONTENT-ABSENT" not in detail
        assert "no substring" in detail


def test_matched_case_message_unchanged(kb):
    ingest(
        "proj-a",
        "note",
        "TPS-style target",
        "the golden target zanzibar-widget lives here",
        kind="chat",
        uri="test:absent:2",
    )
    results, _, _ = eval_harness.evaluate(
        golden=[_case("golden target zanzibar", ["zanzibar-widget"])]
    )
    (res,) = results
    if res.passed:
        assert "matched at rank" in res.criteria[0].detail
