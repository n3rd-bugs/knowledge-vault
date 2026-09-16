"""Golden-schema extension tests (measurement foundation for ranking work):
expect_item_id / expect_rank_le / forbid_item_id, per-case kwargs forwarding
(boost_project / expand / as_of), and access_log usage mining (`kb eval mine`).

Real DB in the isolated kb_test schema via the `kb` fixture; Ollama is mocked by
conftest (deterministic embeddings). The keyword leg + exact-identifier bonus
carry relevance, so rank assertions here are deterministic: a two-leg keyword
match always outscores a vector-only straggler (2/(60+r) vs 1/(60+r)), and the
0.05 project boost exceeds any possible RRF gap (max ~0.033).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

from kbcore import db, eval_criteria, eval_harness, supersede
from kbcore.ingest import ingest
from kbworker import eval_composition, eval_sentinel, eval_trust

pytestmark = pytest.mark.usefixtures("kb")

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _log(item_id: int, tool: str, query: str | None, ts: datetime) -> None:
    """Seed an access_log row with a controlled timestamp (test setup only)."""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, query, ts) VALUES (%s, %s, %s, %s)",
            (item_id, tool, query, ts),
        )


# --- expect_item_id ----------------------------------------------------------


def test_expect_item_id_pass():
    target = ingest(project="alpha", type="note", title="widget calibration", body="ZWIDGET99 gain")
    ingest(project="alpha", type="note", title="pasta recipe", body="Boil water, add salt.")

    case = {"q": "ZWIDGET99", "expect_item_id": target}
    results, hit5, mrr = eval_harness.evaluate(golden=[case])

    r = results[0]
    assert r.passed and r.rank == 1
    assert [(c.name, c.ok) for c in r.criteria] == [("expect_item_id", True)]
    assert hit5 == 1.0 and mrr == 1.0


def test_expect_item_id_fail_when_item_out_of_top_k():
    # 6 keyword matches guarantee the non-matching item falls outside top-5:
    # every two-leg match scores >= 1/66 + 1/67 > the straggler's max 1/61.
    for i in range(6):
        ingest(project="alpha", type="note", title=f"widget note {i}", body=f"ZWIDGET99 detail {i}")
    stray = ingest(project="alpha", type="note", title="pasta recipe", body="Boil water, add salt.")

    results, hit5, mrr = eval_harness.evaluate(golden=[{"q": "ZWIDGET99", "expect_item_id": stray}])

    r = results[0]
    assert not r.passed and r.rank is None
    assert [(c.name, c.ok) for c in r.criteria] == [("expect_item_id", False)]
    assert hit5 == 0.0 and mrr == 0.0


# --- forbid_item_id ----------------------------------------------------------


def test_forbid_item_id_fails_case_when_id_surfaces():
    ingest(project="alpha", type="note", title="widget calibration", body="ZWIDGET99 gain")
    stray = ingest(project="alpha", type="note", title="pasta recipe", body="Boil water, add salt.")

    # Only 2 items → both are in top-5; the substr criterion passes but the
    # forbidden id surfacing must fail the CASE (and thus hit@5), while MRR
    # still records the positive match's rank.
    case = {"q": "ZWIDGET99", "expect_substr": ["ZWIDGET99"], "forbid_item_id": stray}
    results, hit5, mrr = eval_harness.evaluate(golden=[case])

    r = results[0]
    assert not r.passed and r.rank == 1
    verdicts = {c.name: c.ok for c in r.criteria}
    assert verdicts == {"expect_substr": True, "forbid_item_id": False}
    forbid = next(c for c in r.criteria if c.name == "forbid_item_id")
    assert f"forbidden item {stray} surfaced" in forbid.detail
    assert hit5 == 0.0 and mrr == 1.0


def test_forbid_only_case_passes_and_does_not_dilute_mrr():
    for i in range(6):
        ingest(project="alpha", type="note", title=f"widget note {i}", body=f"ZWIDGET99 detail {i}")
    stray = ingest(project="alpha", type="note", title="pasta recipe", body="Boil water, add salt.")

    results, hit5, mrr = eval_harness.evaluate(golden=[{"q": "ZWIDGET99", "forbid_item_id": stray}])

    r = results[0]
    assert r.passed and r.rank is None
    assert hit5 == 1.0
    assert mrr == 0.0, "forbid-only cases carry no positive criterion — excluded from MRR"


def test_case_without_any_criterion_is_rejected():
    with pytest.raises(ValueError, match="needs at least one of"):
        eval_harness.evaluate(golden=[{"q": "anything"}])


# --- kwargs forwarding -------------------------------------------------------


def test_case_kwargs_reach_search_and_tool_stays_none(monkeypatch):
    ingest(project="alpha", type="note", title="widget calibration", body="ZWIDGET99 gain")
    captured: dict = {}
    real_search = eval_harness.search

    def spy(query, **kwargs):
        captured.update(kwargs)
        return real_search(query, **kwargs)

    monkeypatch.setattr(eval_harness, "search", spy)
    case = {
        "q": "ZWIDGET99",
        "expect_substr": ["ZWIDGET99"],
        "kwargs": {"boost_project": "beta", "expand": False, "as_of": "2026-01-02T03:04:05+00:00"},
    }
    results, _, _ = eval_harness.evaluate(golden=[case])

    assert results[0].criteria[0].ok
    assert captured["boost_project"] == "beta"
    assert captured["hops"] == 0, "expand=false maps to hops=0 exactly like the MCP surface"
    assert captured["as_of"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert captured["k"] == eval_harness.K
    assert captured["tool"] is None, "eval reads are NOT usage — must never hit access_log"


def test_boost_project_kwarg_flips_ranking():
    # B first (older loses the equal-score recency tie-break), denser-match A
    # second: unboosted A ranks 1. The 0.05 boost exceeds any RRF gap, so
    # boost_project=beta puts B at rank 1 — the case passes ONLY when the boost
    # actually reaches search().
    b_id = ingest(
        project="beta",
        type="note",
        title="beta config note",
        body="QBOOST7 appears once among many other filler words about the beta configuration.",
    )
    ingest(
        project="alpha",
        type="note",
        title="alpha driver note",
        body="QBOOST7 QBOOST7 QBOOST7 driver.",
    )

    case = {
        "q": "QBOOST7",
        "expect_item_id": b_id,
        "expect_rank_le": 1,
        "kwargs": {"boost_project": "beta"},
    }
    boosted, hit5, _ = eval_harness.evaluate(golden=[case])
    assert boosted[0].passed and boosted[0].rank == 1 and hit5 == 1.0

    control = {k: v for k, v in case.items() if k != "kwargs"}
    unboosted, hit5, _ = eval_harness.evaluate(golden=[control])
    assert not unboosted[0].passed and unboosted[0].rank == 2, "without the boost B is rank 2"
    rank_le = next(c for c in unboosted[0].criteria if c.name == "expect_rank_le")
    assert not rank_le.ok and "rank 2 vs bound 1" in rank_le.detail
    assert hit5 == 0.0


def test_as_of_kwarg_filters_valid_time_end_to_end():
    expired = ingest(
        project="alpha",
        type="note",
        title="widget v1 limits",
        body="ZWIDGET99 old current limits.",
        valid_to=datetime(2025, 1, 1, tzinfo=UTC),
    )
    golden = [
        # Back-dated as_of: the expired fact was still valid → must be found.
        {"q": "ZWIDGET99", "expect_item_id": expired, "kwargs": {"as_of": "2024-06-01"}},
        # After valid_to: the expired fact must NOT surface.
        {"q": "ZWIDGET99", "forbid_item_id": expired, "kwargs": {"as_of": "2025-06-01"}},
    ]
    results, hit5, _ = eval_harness.evaluate(golden=golden)
    assert [r.passed for r in results] == [True, True]
    assert hit5 == 1.0


def test_unknown_case_kwarg_is_rejected():
    with pytest.raises(ValueError, match="unknown kwargs"):
        eval_harness.evaluate(
            golden=[{"q": "x", "expect_substr": ["x"], "kwargs": {"boost": "typo"}}]
        )


def test_filter_kwargs_reach_search(monkeypatch):
    """The precision-hardening extension: type/domain/tags forward straight
    through to the same-named search() filters (no translation)."""
    ingest(project="alpha", type="decision", title="widget pick", body="ZWIDGET99 chosen")
    captured: dict = {}
    real_search = eval_harness.search

    def spy(query, **kwargs):
        captured.update(kwargs)
        return real_search(query, **kwargs)

    monkeypatch.setattr(eval_harness, "search", spy)
    case = {
        "q": "ZWIDGET99",
        "expect_substr": ["ZWIDGET99"],
        "kwargs": {"type": "decision", "domain": "embedded", "tags": ["hw:x"]},
    }
    eval_harness.evaluate(golden=[case])

    assert captured["type"] == "decision"
    assert captured["domain"] == "embedded"
    assert captured["tags"] == ["hw:x"]
    assert captured["tool"] is None, "eval reads never hit access_log"


def test_type_filter_narrows_the_result_end_to_end():
    """type= actually filters: a decision and a note share a token; type='decision'
    returns only the decision, so an expect_item_id case passes ONLY with it."""
    decision = ingest(
        project="alpha", type="decision", title="widget choice", body="ZWIDGET99 pick"
    )
    note = ingest(project="alpha", type="note", title="widget aside", body="ZWIDGET99 note")

    d_case = {"q": "ZWIDGET99", "expect_item_id": decision, "kwargs": {"type": "decision"}}
    d_res, d_hit, _ = eval_harness.evaluate(golden=[d_case])
    assert d_res[0].passed and d_hit == 1.0

    # the note is filtered OUT of a decision-only search
    n_case = {"q": "ZWIDGET99", "expect_item_id": note, "kwargs": {"type": "decision"}}
    n_res, n_hit, _ = eval_harness.evaluate(golden=[n_case])
    assert not n_res[0].passed and n_hit == 0.0


# --- legacy compatibility ----------------------------------------------------


def test_legacy_substr_only_metrics_unchanged():
    ingest(project="alpha", type="note", title="widget calibration", body="ZWIDGET99 gain")
    golden = [
        {"q": "ZWIDGET99", "expect_substr": ["zwidget99"]},  # case-insensitive, as before
        {"q": "nonexistent-token-xyzzy", "expect_substr": ["no-such-substring"]},
    ]
    results, hit5, mrr = eval_harness.evaluate(golden=golden)
    assert results[0].passed and results[0].rank == 1
    assert not results[1].passed and results[1].rank is None
    assert hit5 == 0.5 and mrr == 0.5  # identical to the pre-extension harness


# --- expect_item_id_raw (E1, review #8 H5, KB 31857) --------------------------


def _superseded_pair() -> tuple[int, int]:
    """An OLD revision (status superseded) and the live head that replaced it."""
    old = ingest(project="alpha", type="note", title="widget limits v1", body="ZOLDREV77 legacy.")
    new = supersede.supersede(old, new_body="ZNEWREV88 current limits.", reason="revised")
    return old, new


def test_expect_item_id_raw_asserts_the_superseded_revision_itself_present():
    """Transaction time: the OLD id, unresolved, is retrievable WITH the flag —
    the rank is reported and the pin is NOT chain-resolved."""
    old, _new = _superseded_pair()
    case = {"q": "ZOLDREV77", "expect_item_id_raw": old, "kwargs": {"include_superseded": True}}
    results, hit5, mrr = eval_harness.evaluate(golden=[case])

    r = results[0]
    assert r.passed and r.rank == 1 and r.chain_resolved is False
    assert [(c.name, c.ok) for c in r.criteria] == [("expect_item_id_raw", True)]
    assert r.criteria[0].detail == f"raw item {old} at rank 1"
    assert hit5 == 1.0 and mrr == 1.0, "the raw pin owns the case: counted in n_positive"


def test_expect_item_id_raw_fails_without_include_superseded():
    old, _new = _superseded_pair()
    case = {"q": "ZOLDREV77", "expect_item_id_raw": old}
    results, hit5, mrr = eval_harness.evaluate(golden=[case])

    r = results[0]
    assert not r.passed and r.rank is None and r.chain_resolved is False
    assert r.criteria[0].detail == f"raw item {old} absent from top-{eval_harness.K}"
    assert hit5 == 0.0 and mrr == 0.0


def test_expect_item_id_still_chain_resolves_to_the_head():
    old, new = _superseded_pair()
    results, _, _ = eval_harness.evaluate(golden=[{"q": "ZNEWREV88", "expect_item_id": old}])

    r = results[0]
    assert r.passed and r.chain_resolved is True
    assert f"chain-resolved {old}->{new}" in r.criteria[0].detail


def test_expect_item_id_raw_honours_expect_rank_le():
    old, _new = _superseded_pair()
    case = {
        "q": "ZOLDREV77",
        "expect_item_id_raw": old,
        "expect_rank_le": 1,
        "kwargs": {"include_superseded": True},
    }
    results, hit5, _ = eval_harness.evaluate(golden=[case])
    assert results[0].passed and hit5 == 1.0
    assert {c.name: c.ok for c in results[0].criteria} == {
        "expect_item_id_raw": True,
        "expect_rank_le": True,
    }


def test_both_id_criteria_in_one_case_is_rejected_naming_the_case():
    case = {"q": "which-pin", "expect_item_id": 1, "expect_item_id_raw": 1}
    with pytest.raises(ValueError, match="'which-pin'.*mutually exclusive"):
        eval_harness.evaluate(golden=[case])
    assert "expect_item_id_raw" in eval_harness._CRITERIA_KEYS
    assert eval_criteria.has_positive({"expect_item_id_raw": 1})


def _stub_hit(item_id: int, title: str, type_: str = "note", trust: str = "unverified"):
    return SimpleNamespace(item_id=item_id, title=title, snippet="", type=type_, trust=trust)


def test_composition_matching_credits_the_raw_id_as_satisfier(monkeypatch):
    """The satisfier rider scores a raw pin on the id ITSELF (no chain walk):
    the raw case is satisfied by hit 2 — 'verified' — and the ids name it."""
    hits = [_stub_hit(1, "Gamma summary", "summary"), _stub_hit(2, "Gamma note", trust="verified")]
    matched = eval_composition._matching_hits({"expect_item_id_raw": 2}, hits)
    assert [h.item_id for h in matched] == [2]
    assert eval_composition._matching_hits({"expect_item_id_raw": 3}, hits) == []

    monkeypatch.setattr(eval_sentinel, "search", lambda q, k, tool: list(hits))
    monkeypatch.setattr(eval_trust, "verified_lift", lambda share, n: {})
    golden = [
        {"q": "gamma", "expect_item_id_raw": 2},  # satisfied by the verified note
        {"q": "gamma", "expect_item_id_raw": 3},  # absent from the replay: unsatisfied
        {"q": "gamma", "forbid_item_id": 3},  # forbid-only: skipped
    ]
    result = eval_composition._trust_composition(golden)
    assert result["eval_satisfier_status"] == "ok"
    assert result["eval_satisfier_types"] == {
        "summary": 0,
        "verified": 1,
        "unverified": 0,
        "unsatisfied": 1,
    }
    assert result["eval_satisfier_ids"] == [2]
    assert result["eval_summary_only_cases"] == 0


# --- usage mining (`kb eval mine`) -------------------------------------------


def test_mine_emits_confirmed_candidate_and_ignores_unfollowed():
    followed = ingest(project="p", type="note", title="UART fix note", body="ring buffer fix")
    lonely = ingest(project="p", type="note", title="lonely note", body="never opened")
    _log(followed, "mcp.kb_search", "uart byte loss fix", T0)
    _log(lonely, "mcp.kb_search", "unfollowed query", T0)
    _log(followed, "mcp.kb_get", None, T0 + timedelta(minutes=3))

    cands = eval_harness.mine_candidates()

    assert [(c["q"], c["expect_item_id"]) for c in cands] == [("uart byte loss fix", followed)]
    assert cands[0]["confirmations"] == 1
    assert cands[0]["title"] == "UART fix note"


def test_mine_respects_window_and_direction():
    item = ingest(project="p", type="note", title="windowed note", body="window body")
    _log(item, "mcp.kb_search", "windowed query", T0)
    _log(item, "mcp.kb_get", None, T0 - timedelta(minutes=1))  # BEFORE the search: never counts
    _log(item, "mcp.kb_get", None, T0 + timedelta(minutes=30))  # outside the 15-min default

    assert eval_harness.mine_candidates() == []
    widened = eval_harness.mine_candidates(window_minutes=60)
    assert [(c["q"], c["confirmations"]) for c in widened] == [("windowed query", 1)]


def test_mine_ignores_gets_for_other_items():
    searched = ingest(project="p", type="note", title="searched note", body="searched body")
    other = ingest(project="p", type="note", title="other note", body="other body")
    _log(searched, "mcp.kb_search", "cross item query", T0)
    _log(other, "mcp.kb_get", None, T0 + timedelta(minutes=2))

    assert eval_harness.mine_candidates() == []


def test_kb_eval_mine_cli_prints_yaml_stubs_and_never_writes_golden():
    from typer.testing import CliRunner

    from kbcli.main import app

    item = ingest(project="p", type="note", title="UART fix note", body="ring buffer fix")
    _log(item, "mcp.kb_search", "uart byte loss fix", T0)
    _log(item, "mcp.kb_get", None, T0 + timedelta(minutes=3))
    golden_before = eval_harness.GOLDEN_PATH.read_bytes()

    result = CliRunner().invoke(app, ["eval", "mine"])

    assert result.exit_code == 0, result.output
    # The stub output must parse as YAML into ready-to-review golden cases.
    cases = yaml.safe_load(result.output)
    assert cases == [{"q": "uart byte loss fix", "expect_item_id": item}]
    assert eval_harness.GOLDEN_PATH.read_bytes() == golden_before, "mine must never write golden"


def test_kb_eval_mine_cli_empty_log():
    from typer.testing import CliRunner

    from kbcli.main import app

    result = CliRunner().invoke(app, ["eval", "mine"])
    assert result.exit_code == 0
    assert "no candidates" in result.output
