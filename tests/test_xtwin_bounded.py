"""Wave C (review #3 P2, KB 19663): the cross-project twin enumeration is a
BOUNDED, INDEX-ASSISTED scan, not the old O(N^2) self-join.

Pins the rewrite's contract:
* a full probe finds the SAME pairs, in the same closest-first order, as the old
  join did on a corpus the join could finish (the small-corpus equivalence);
* ``probe_limit`` bounds how many items are KNN-probed (newest first) and the
  returned coverage says N-of-M honestly — a bounded run never masquerades as
  exhaustive (the no-silent-caps rule), and the CLI prints the line;
* ``cap`` truncates to the CLOSEST pairs AFTER the scan (the judge budget), and
  0 stays the kill switch that probes nothing.

Real DB in kb_test; embeddings forced to exact cosines via the helpers shared
with tests/test_twin_crossproject.py; the 14B judge (llm.chat) is the only mock.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import llm, twin_crossproject
from tests.test_twin_crossproject import PA, PB, _judge_yes, _pair

runner = CliRunner()


# --- the bounded scan --------------------------------------------------------


def test_full_probe_matches_the_old_join_contract(kb):
    """Same pairs, same order, as the O(N^2) join produced on a small corpus."""
    far = _pair("bx-far", "unrelated capture", sim=0.80)
    near = _pair("bx-near", "the drain deadline gotcha", sim=0.99)
    mid = _pair("bx-mid", "the rerank sidecar gotcha", sim=0.96)

    pairs, cov = twin_crossproject.probe_candidates(threshold=0.93)

    assert [{p.a.item_id, p.b.item_id} for p in pairs] == [set(near), set(mid)], (
        "closest first; the 0.80 pair is below the floor"
    )
    seen = {i for p in pairs for i in (p.a.item_id, p.b.item_id)}
    assert far[0] not in seen and far[1] not in seen
    assert pairs[0].similarity == pytest.approx(0.99, abs=1e-6)
    assert all(p.a.item_id < p.b.item_id for p in pairs), "each unordered pair once"
    assert cov.exhaustive and cov.probed == cov.eligible == 6, (
        "a full probe visits every eligible item and says so"
    )


def test_probe_limit_bounds_the_scan_and_the_coverage_says_so(kb):
    _old = _pair("bx-old", "an old twin pair", sim=0.99)
    new = _pair("bx-new", "a new twin pair", sim=0.98)  # highest ids = newest

    pairs, cov = twin_crossproject.probe_candidates(threshold=0.93, probe_limit=1)

    assert [{p.a.item_id, p.b.item_id} for p in pairs] == [set(new)], (
        "the bounded scan probes newest-first"
    )
    assert (cov.probed, cov.eligible, cov.exhaustive) == (1, 4, False)
    assert "probed 1 of 4" in cov.render() and "BOUNDED" in cov.render(), (
        "a bounded run must never read as exhaustive"
    )


def test_cap_truncates_to_the_closest_pairs_after_the_scan(kb):
    """cap bounds the JUDGE budget (closest pairs kept), never which items are
    probed — so it keeps the old join's 'the two closest are kept' contract."""
    _pair("bx-p1", "gotcha one", sim=0.99)
    _pair("bx-p2", "gotcha two", sim=0.97)
    lo = _pair("bx-p3", "gotcha three", sim=0.95)

    pairs, cov = twin_crossproject.probe_candidates(threshold=0.93, cap=2)

    assert len(pairs) == 2 and cov.exhaustive, "capped output, uncapped (and honest) scan"
    kept = {i for p in pairs for i in (p.a.item_id, p.b.item_id)}
    assert lo[0] not in kept and lo[1] not in kept, "the farthest pair is dropped by the cap"


def test_kill_switch_probes_nothing(kb):
    _pair("bx-kill", "a twin", sim=0.99)

    pairs, cov = twin_crossproject.probe_candidates(threshold=0.93, cap=0)

    assert pairs == [] and (cov.probed, cov.eligible) == (0, 0)


def test_project_filters_bound_the_probe_side(kb):
    ab = _pair("bx-ab", "pair between A and B", sim=0.99)
    ac = _pair("bx-ac", "pair between A and C", sim=0.98, pb="third")

    both, _cov = twin_crossproject.probe_candidates(PA, PB, threshold=0.93)
    assert [{p.a.item_id, p.b.item_id} for p in both] == [set(ab)], (
        "a two-project run pins pairs to that unordered pair"
    )

    touching, cov = twin_crossproject.probe_candidates(project_a="third", threshold=0.93)
    assert [{p.a.item_id, p.b.item_id} for p in touching] == [set(ac)]
    assert cov.eligible == 1, "a one-project run probes only that project's items"


def test_knn_parameter_plumbs_through(kb):
    pair = _pair("bx-knn", "a twin", sim=0.99)

    pairs, _cov = twin_crossproject.probe_candidates(threshold=0.93, knn=1)

    assert [{p.a.item_id, p.b.item_id} for p in pairs] == [set(pair)]


# --- the CLI coverage line ---------------------------------------------------


def test_cli_prints_the_coverage_line(kb, monkeypatch):
    _pair("bx-cli", "the drain deadline gotcha", sim=0.98)
    monkeypatch.setattr(llm, "chat", _judge_yes)

    result = runner.invoke(app, ["repair", "cross-twins", "--threshold", "0.93"])

    assert result.exit_code == 0, result.output
    assert "coverage: probed 2 of 2 eligible item(s) (exhaustive)" in result.output


def test_cli_marks_a_bounded_run_as_bounded(kb, monkeypatch):
    _pair("bx-cli2", "the drain deadline gotcha", sim=0.98)
    monkeypatch.setattr(llm, "chat", _judge_yes)

    result = runner.invoke(
        app, ["repair", "cross-twins", "--threshold", "0.93", "--probe-limit", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "probed 1 of 2" in result.output and "BOUNDED" in result.output, (
        "the no-silent-caps line: a bounded run says so on the surface the owner reads"
    )
