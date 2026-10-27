"""kb repair rejudge-oversized — the owner re-judge of pre-fit blind "no conflict" verdicts.

KB 55589. Real DB in kb_test (conftest ``kb``); Ollama is never called — ``llm._chat`` is
stubbed, so the REAL ``items_contradict`` builds (and fits) the request under test. The
memorized rows' ``checked_at`` is set explicitly around ``FIT_EPOCH`` so the suite does not
depend on the day it runs.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
from datetime import timedelta

import httpx
import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcli.repair_rejudge import _r_tally
from kbcore import consolidation, contradiction_rejudge, db, judge_fit, llm, scope
from kbcore.chunk import count_tokens
from kbcore.config import settings
from kbcore.errors import ExtractorUnavailable, InvalidOperation
from kbcore.ingest import ingest
from kbcore.supersede import supersede

replace_ = dataclasses.replace

SEAT = "seat:27b"
PRE = judge_fit.FIT_EPOCH - timedelta(days=3)
POST = judge_fit.FIT_EPOCH + timedelta(days=1)
_N = itertools.count()


def _spec(words: int = 3500) -> int:
    """One item; two 3,500-word specs make a pair over the 8,904-token legacy ceiling."""
    n = next(_N)
    body = " ".join(f"w{n}x{i}" for i in range(words))
    return ingest("rm3", "spec", f"spec {n}", body, uri=f"rm3:spec:{n}")


def _memorize(a: int, b: int, verdict: bool = False, at=PRE, model: str | None = None) -> None:
    lo, hi = min(a, b), max(a, b)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO contradiction_judgements (a_id, b_id, embed_model, verdict, checked_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (lo, hi, model or settings.kb_embed_model, verdict, at),
        )


def _memory() -> list[tuple]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT a_id, b_id, embed_model, verdict, checked_at FROM contradiction_judgements "
            "ORDER BY a_id, b_id, embed_model"
        ).fetchall()


def _queued() -> list[dict]:
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT detail FROM review_queue WHERE reason = 'contradiction' AND NOT resolved "
                "ORDER BY id"
            ).fetchall()
        ]


@pytest.fixture()
def seat(monkeypatch) -> dict:
    """The stubbed seat: ``answer`` is the reply, or an exception to raise; every request
    is captured (messages + kwargs)."""
    state: dict = {"answer": '{"contradict": true}', "sent": []}

    def chat(messages, **kw):
        state["sent"].append((messages, kw))
        answer = state["answer"]
        if isinstance(answer, BaseException):
            raise answer
        return answer(len(state["sent"])) if callable(answer) else answer

    monkeypatch.setattr(settings, "kb_contradiction_judge_model", SEAT)
    monkeypatch.setattr(llm, "_chat", chat)
    return state


def test_candidates_are_exactly_the_pre_fit_oversized_no_conflict_pairs(kb):
    target = (_spec(), _spec())
    _memorize(*target)
    small = (
        ingest("rm3", "note", "a", "x is 5", uri="s1"),
        ingest("rm3", "note", "b", "x", uri="s2"),
    )
    _memorize(*small)  # fits: its "no" was a real one
    _memorize(*(_spec(), _spec()), verdict=True)  # already queued; the gate re-judges it
    _memorize(*(_spec(), _spec()), at=POST)  # judged by the fitted judge
    _memorize(*(_spec(), _spec()), model="previous-embedder")  # another model's memory
    gone = (_spec(), _spec())
    _memorize(*gone)
    supersede(gone[0], new_item_id=small[0], reason="test: retired")
    known = (_spec(), _spec())
    _memorize(*known)
    consolidation.record_contradiction(known[1], known[0])  # recorded, reverse direction
    mid = (_spec(600), _spec(600))  # over the byte prefilter, under the token ceiling
    _memorize(*mid)
    texts = consolidation.item_pair_texts(*mid)
    assert sum(len(t.encode()) for pair in texts for t in pair) > judge_fit.LEGACY_CEILING // 2
    assert judge_fit.contradict_tokens(*texts[0], *texts[1]) < judge_fit.LEGACY_CEILING

    (only,) = contradiction_rejudge.candidates()
    assert (only.a_id, only.b_id, only.project) == (*target, "rm3")
    assert only.tokens == judge_fit.contradict_tokens(
        *consolidation.item_pair_texts(*target)[0], *consolidation.item_pair_texts(*target)[1]
    )
    assert only.tokens > judge_fit.LEGACY_CEILING
    assert not only.judged and only.verdict is None


def test_a_conflict_found_is_queued_like_the_nightly_and_the_memory_is_untouched(kb, seat):
    a, b = _spec(), _spec()
    _memorize(a, b)
    before = _memory()

    (done,) = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())

    assert (done.judged, done.verdict, done.queued, done.error) == (True, True, True, "")
    ((messages, kw),) = seat["sent"]
    assert sum(count_tokens(m["content"]) for m in messages) <= judge_fit.judge_ceiling()
    assert "tokens omitted ...]" in messages[1]["content"]
    assert kw["model"] == SEAT
    (detail,) = _queued()
    assert (detail["a_id"], detail["b_id"]) == (a, b)
    assert detail["judged_by"] == {"model": SEAT, "think": None}
    assert _memory() == before, "Invariant 5: the memorized row is never updated or deleted"
    assert contradiction_rejudge.candidates() == [], "a recorded conflict is not re-asked"


@pytest.mark.parametrize(
    ("answer", "verdict"), [('{"contradict": false}', False), ("not json", None), ("", None)]
)
def test_a_no_or_a_blank_reply_writes_nothing(kb, seat, answer, verdict):
    a, b = _spec(), _spec()
    _memorize(a, b)
    seat["answer"] = answer
    (done,) = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())
    assert (done.judged, done.verdict, done.queued) == (True, verdict, False)
    assert _queued() == []
    assert len(contradiction_rejudge.candidates()) == 1, "still a candidate for a re-run"


def test_an_outage_stops_the_run_but_returns_the_table(kb, seat):
    pairs = [(_spec(), _spec()) for _ in range(3)]
    for p in pairs:
        _memorize(*p)

    def second_one_fails(n: int) -> str:
        if n == 2:
            raise ExtractorUnavailable("ollama restarting")
        return '{"contradict": false}'

    seat["answer"] = second_one_fails
    done = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())
    assert [d.judged for d in done] == [True, False, False]
    assert done[1].error == "stopped: judge unavailable: ollama restarting"
    assert done[2].error == "" and not done[2].judged
    assert len(seat["sent"]) == 2, "nothing is asked after the outage"


def test_a_pair_closed_meanwhile_is_skipped(kb, seat):
    a, b = _spec(), _spec()
    _memorize(a, b)
    found = contradiction_rejudge.candidates()
    supersede(a, new_item_id=_spec(10), reason="test: retired between dry run and apply")
    (done,) = contradiction_rejudge.rejudge(found)
    assert done.error == "closed meanwhile (skipped)" and not done.judged
    assert seat["sent"] == []


def test_the_run_is_refused_while_a_consolidation_run_holds_the_lock(kb, seat):
    from kbworker.consolidate import ADVISORY_KEY

    _memorize(_spec(), _spec())
    found = contradiction_rejudge.candidates()
    with db.advisory_lock(ADVISORY_KEY) as held:
        assert held
        with pytest.raises(InvalidOperation, match="advisory lock"):
            contradiction_rejudge.rejudge(found)
    assert seat["sent"] == []


def test_a_consolidation_run_starting_mid_run_stops_it_and_nothing_is_held(kb, seat):
    """The lock is probed before EVERY pair and never held: a nightly that starts while the
    repair runs waits on one call at most, and the table of what was done comes back."""
    from kbworker.consolidate import ADVISORY_KEY

    for _ in range(3):
        _memorize(_spec(), _spec())
    nightly = contextlib.ExitStack()

    def a_nightly_starts_during_the_first_call(n: int) -> str:
        if n == 1:
            with db.advisory_lock(ADVISORY_KEY) as free:
                assert free, "the repair must not hold the lock while it judges"
            nightly.enter_context(db.advisory_lock(ADVISORY_KEY))
        return '{"contradict": false}'

    seat["answer"] = a_nightly_starts_during_the_first_call
    with nightly:
        done = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())
    assert [d.judged for d in done] == [True, False, False]
    assert done[1].error.startswith("stopped:") and "advisory lock" in done[1].error
    assert done[2].error == "" and len(seat["sent"]) == 1


def test_a_failure_about_one_pair_is_named_and_the_run_goes_on(kb, seat):
    for _ in range(3):
        _memorize(_spec(), _spec())

    def the_second_pair_times_out(n: int) -> str:
        if n == 2:
            try:
                raise httpx.ReadTimeout("600 s")
            except httpx.ReadTimeout as e:  # how llm._chat wraps a per-pair timeout
                raise ExtractorUnavailable("extraction call failed: ReadTimeout") from e
        return '{"contradict": false}'

    seat["answer"] = the_second_pair_times_out
    done = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())
    assert [d.judged for d in done] == [True, False, True]
    assert done[1].error.startswith("judge failed on this pair")


def test_two_failures_in_a_row_mean_a_wedged_slot_and_stop_the_run(kb, seat):
    for _ in range(4):
        _memorize(_spec(), _spec())

    def every_call_times_out(n: int) -> str:
        if n == 1:
            return '{"contradict": false}'
        try:
            raise httpx.ReadTimeout("600 s")
        except httpx.ReadTimeout as e:
            raise ExtractorUnavailable("extraction call failed: ReadTimeout") from e

    seat["answer"] = every_call_times_out
    seen: list = []
    done = contradiction_rejudge.rejudge(
        contradiction_rejudge.candidates(), on_progress=seen.append
    )
    assert [d.judged for d in done] == [True, False, False, False]
    assert done[1].error.startswith("judge failed on this pair")
    assert done[2].error.startswith("stopped: judge unavailable") and done[3].error == ""
    assert seen == done[:3], "every pair reached is reported, failed and stopped ones too"


@pytest.mark.parametrize(
    ("script", "want"),
    [
        # a real answer between two failures clears the count: both named, the run goes on
        (["fail", "ok", "fail", "ok"], ["judge failed", True, "judge failed", True]),
        # a skipped pair proves nothing about the slot: the second failure stops the run
        (["fail", "closed", "fail", "ok"], ["judge failed", "skipped", "stopped", None]),
        # neither does a blank reply
        (["fail", "blank", "fail", "ok"], ["judge failed", True, "stopped", None]),
    ],
)
def test_only_a_real_answer_resets_the_two_in_a_row_count(kb, seat, script, want):
    pairs = [(_spec(), _spec()) for _ in script]
    for p in pairs:
        _memorize(*p)
    found = contradiction_rejudge.candidates()
    for step, pair in zip(script, found, strict=True):
        if step == "closed":
            supersede(pair.a_id, new_item_id=_spec(10), reason="test: retired meanwhile")
    replies = iter(
        r
        for step in script
        if step != "closed"
        for r in {"fail": ["timeout"], "ok": ['{"contradict": false}'], "blank": ["", ""]}[step]
    )

    def answer(_n: int) -> str:
        reply = next(replies)
        if reply == "timeout":
            try:
                raise httpx.ReadTimeout("600 s")
            except httpx.ReadTimeout as e:
                raise ExtractorUnavailable("extraction call failed: ReadTimeout") from e
        return reply

    seat["answer"] = answer
    done = contradiction_rejudge.rejudge(found)
    assert [_outcome(d) for d in done] == want


def _outcome(d: contradiction_rejudge.Rejudge) -> bool | str | None:
    """True for a judged pair, else the kind of error it carries (None: not reached)."""
    if d.judged:
        return True
    for prefix, kind in (("closed", "skipped"), ("judge failed", "judge failed")):
        if d.error.startswith(prefix):
            return kind
    return "stopped" if d.error.startswith("stopped") else None


def test_a_db_failure_in_the_lock_probe_after_the_first_pair_keeps_the_table(kb, seat, monkeypatch):
    import psycopg

    from kbcore import bench_judge

    for _ in range(2):
        _memorize(_spec(), _spec())
    probes = iter([None, psycopg.OperationalError("the server closed the connection")])

    def probe() -> None:
        if isinstance(err := next(probes), BaseException):
            raise err

    monkeypatch.setattr(bench_judge, "assert_gpu_slot_free", probe)
    done = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())
    assert done[0].judged and done[1].error.startswith("stopped: OperationalError")


@pytest.mark.parametrize("seam", ["record_contradiction", "stamp_judged_by"])
def test_a_write_failure_stops_the_run_but_a_stamp_failure_does_not(kb, seat, monkeypatch, seam):
    for _ in range(2):
        _memorize(_spec(), _spec())

    def boom(*_a):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(contradiction_rejudge, seam, boom)
    done = contradiction_rejudge.rejudge(contradiction_rejudge.candidates())
    if seam == "record_contradiction":  # the table survives a non-KBError
        assert done[0].error == "stopped: RuntimeError: db hiccup"
        assert not done[1].judged and done[1].error == ""
    else:  # provenance only, isolated exactly as the nightly isolates it
        assert [(d.judged, d.queued, d.error) for d in done] == [(True, True, "")] * 2


def test_the_tally_accounts_for_every_pair():
    base = contradiction_rejudge.Rejudge(1, 2, "p", 9000, "a", "b")
    rows = [
        replace_(base, judged=True, verdict=True, queued=True),
        replace_(base, judged=True, verdict=True, queued=False),  # recorded meanwhile
        replace_(base, judged=True, verdict=False),
        replace_(base, judged=True, verdict=None),
        replace_(base, error="judge failed on this pair: ReadTimeout()"),
        replace_(base, error="closed meanwhile (skipped)"),
        replace_(base, error="stopped: judge unavailable: down"),
        base,
    ]
    tally = _r_tally(rows)
    assert "8 'no conflict' pair(s)" in tally
    assert "1 queued as conflicts, 1 no conflict, 1 no verdict" in tally
    assert "1 already recorded, 1 judge failed on the pair, 1 closed meanwhile" in tally
    assert tally.endswith("; 2 not judged (stopped: judge unavailable: down)")


def test_both_surfaces_are_owner_only(kb):
    grantee = scope.Principal(name="g", read_projects=frozenset({"rm3"}))
    with scope.use_principal(grantee):
        with pytest.raises(InvalidOperation, match="owner only"):
            contradiction_rejudge.candidates()
        with pytest.raises(InvalidOperation, match="owner only"):
            contradiction_rejudge.rejudge([])


def test_cli_dry_run_writes_nothing_and_apply_queues(kb, seat):
    a, b = _spec(), _spec()
    _memorize(a, b)
    runner = CliRunner()

    dry = runner.invoke(app, ["repair", "rejudge-oversized"])
    assert dry.exit_code == 0, dry.output
    assert f"#{a}" in dry.output and "re-ask" in dry.output
    assert "dry run — nothing judged or written" in dry.output
    assert seat["sent"] == [] and _queued() == []

    applied = runner.invoke(app, ["repair", "rejudge-oversized", "--apply"], input="y\n")
    assert applied.exit_code == 0, applied.output
    assert "QUEUED" in applied.output
    assert "1 queued as conflicts, 0 no conflict, 0 no verdict" in applied.output
    assert len(_queued()) == 1

    again = runner.invoke(app, ["repair", "rejudge-oversized"])
    assert again.exit_code == 0 and "nothing to do" in again.output
