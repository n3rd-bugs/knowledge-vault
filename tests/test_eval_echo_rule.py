"""The E2 ECHO RULE (review #9 KB 40402, landed at review #10).

E1's "clean" nights were a golden echo: an item quoting a golden query verbatim
plus its expected substring satisfied the very case it described. Since E2,
for ``expect_substr`` only, such a hit KEEPS ITS SLOT (the real satisfier below
it reads rank 2) but can never satisfy; the harness and the composition rider
agree on it, and the skipped ids ride META only (never the report body).

Every query here is SYNTHETIC (golden-text hygiene): no real golden case is
quoted. Real kb_test DB; conftest's deterministic embeddings, and the fixtures
blind the vector leg so the keyword leg alone decides the order.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kbcore import db, eval_criteria, eval_echo, eval_harness, search_legs
from kbcore.ingest import ingest
from kbworker import eval_cases, eval_composition, eval_sentinel, eval_trust, run_report

pytestmark = pytest.mark.usefixtures("kb")

QUERY = "why does the widget calibration drift during the long soak"
EXPECT = ["median of three"]


def _blind(item_id: int) -> int:
    """NULL every chunk embedding so the keyword leg alone ranks the item."""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


def _seed(query: str = QUERY) -> dict[str, int]:
    """A carrier quoting ``query`` + the expected substring, and the real satisfier.

    The carrier holds EVERY query word, so it is the keyword leg's only AND
    match (rank 1); the satisfier shares a few words, so it arrives through
    the OR fallback (rank 2).
    """
    carrier = ingest(
        "kb-system",
        "gotcha",
        "Session wrap-up notes",
        f"Golden check: {query}. The answer says the routine takes the median of three reads.",
        kind="chat",
        uri="echo:carrier",
        trust="verified",
    )
    real = ingest(
        "hw-lab",
        "decision",
        "Widget calibration decision",
        "The widget calibration routine settles on the median of three reads.",
        kind="chat",
        uri="echo:real",
        trust="verified",
    )
    return {"carrier": _blind(carrier), "real": _blind(real)}


# --- 1. a carrier keeps its slot but cannot satisfy ----------------------------------


def test_a_carrier_quoting_query_and_substring_cannot_satisfy_and_keeps_its_slot():
    ids = _seed()
    results, hit5, mrr = eval_harness.evaluate(golden=[{"q": QUERY, "expect_substr": EXPECT}])
    (r,) = results
    assert r.top_ids[:2] == [ids["carrier"], ids["real"]], "fixture: the carrier ranks first"
    assert r.passed and r.rank == 2, "the real satisfier reads rank 2 — the carrier kept its slot"
    assert (hit5, mrr) == (1.0, 0.5)
    (verdict,) = r.criteria
    assert verdict.echo_skipped == [ids["carrier"]] == r.echo_skipped
    assert verdict.detail == f"matched at rank 2; echo skipped [{ids['carrier']}]"
    assert r.above[0][0] == ids["carrier"], "the carrier is what sat above the satisfier"
    # Without the query (the pre-E2 call) the criterion is byte-for-byte the old one.
    hits = eval_harness.search(QUERY, k=eval_harness.K, tool=None)
    assert eval_criteria._substr_rank(hits, EXPECT) == 1
    # The PRE-RERANK fused satisfier skips the carrier the same way.
    case = {"q": QUERY, "expect_substr": EXPECT}
    assert eval_criteria.fused_rank(case, [ids["carrier"], ids["real"]]) == 2 == r.fused_rank
    assert eval_criteria.fused_rank({**case, "expect_item_id": ids["carrier"]}, r.top_ids) == 1


def test_a_carrier_without_the_substring_is_still_reported_as_skipped():
    """An echo carrier is collected whatever it holds — the class E1 never saw."""
    carrier = _blind(
        ingest("kb-system", "note", "Golden notes", f"Asked today: {QUERY}.", uri="echo:bare")
    )
    real = _blind(
        ingest("hw-lab", "decision", "Widget calibration", "It takes the median of three reads.")
    )
    (r,), _h, _m = eval_harness.evaluate(golden=[{"q": QUERY, "expect_substr": EXPECT}])
    assert r.rank == 2 and r.top_ids[:2] == [carrier, real]
    assert r.echo_skipped == [carrier]


def test_a_miss_names_its_skipped_carriers_too():
    carrier = _seed()["carrier"]
    (r,), _h, _m = eval_harness.evaluate(golden=[{"q": QUERY, "expect_substr": ["zz-absent-zz"]}])
    assert not r.passed and r.rank is None
    assert r.criteria[0].detail.endswith(f"; echo skipped [{carrier}]")
    assert [a[0] for a in r.above] == r.top_ids, "no satisfier: the whole top-K is above it"


# --- 2. the guard: short and identifier-dominant queries are exempt -----------------


def test_short_and_identifier_dominant_queries_are_exempt():
    assert eval_criteria.ECHO_MIN_WORDS == 4
    assert not eval_criteria.echo_guarded("widget soak drift"), "3 words: too short to echo"
    assert eval_criteria.echo_guarded("widget calibration soak drift"), "4 prose words"
    # search's own identifier-dominance rule: identifiers are >= half the words.
    assert not eval_criteria.echo_guarded("ZWIDGET99 QXR4471 soak drift")
    assert eval_criteria.echo_guarded("QXR4471 widget calibration soak drift")
    assert not eval_criteria.echo_guarded("zq_bus_poll FrameCount LATCH relay")
    short = "widget soak drift"
    hay = f"notes: {short} — the median of three reads"
    assert not eval_criteria.is_echo(short, hay), "an exempt query never echoes"
    assert not eval_criteria.is_echo("ZWIDGET99 QXR4471 soak drift", "ZWIDGET99 QXR4471 soak drift")


def test_an_exempt_query_still_lets_a_verbatim_item_satisfy():
    short = "widget soak drift"
    item = _blind(ingest("hw-lab", "note", "Soak log", f"{short}: settles on the median of three"))
    (r,), _h, _m = eval_harness.evaluate(golden=[{"q": short, "expect_substr": EXPECT}])
    assert r.passed and r.rank == 1 and r.top_ids[0] == item
    assert r.echo_skipped == [] and "echo skipped" not in r.criteria[0].detail


# --- 3. id criteria are unaffected ---------------------------------------------------


def test_id_criteria_are_unaffected_by_the_echo_rule():
    ids = _seed()
    golden = [
        {"q": QUERY, "expect_item_id": ids["carrier"]},
        {"q": QUERY, "expect_item_id_raw": ids["carrier"]},
        {"q": QUERY, "forbid_item_id": 999_999},
    ]
    results, hit5, mrr = eval_harness.evaluate(golden=golden)
    assert [r.rank for r in results] == [1, 1, None], "a pinned item quoting its query counts"
    assert all(r.passed for r in results) and (hit5, mrr) == (1.0, 1.0)
    assert all(r.echo_skipped == [] for r in results)
    assert (
        eval_composition._matching_hits(golden[0], _hits(results[0]))[0].item_id == ids["carrier"]
    ), "the composition rider credits the pin too"


# --- 4. normalisation ----------------------------------------------------------------


def test_normalisation_folds_case_and_whitespace_runs_only():
    q = "Why does  the widget\tDRIFT on soak"
    assert eval_criteria._norm(q) == "why does the widget drift on soak"
    assert eval_criteria.is_echo(q, "log:\n  WHY does the\nwidget   drift on SOAK, again")
    assert not eval_criteria.is_echo(q, "why does the widget drift during the soak")
    assert not eval_criteria.is_echo(q, "why does the widget-drift on soak"), "punctuation counts"
    assert not eval_criteria.is_echo(None, q) and not eval_criteria.is_echo(42, q)


# --- 5. the composition rider agrees with the harness --------------------------------


def _hits(result) -> list:
    """The harness's top-K back as hits (re-searched: deterministic in this rig)."""
    hits = eval_harness.search(result.query, k=eval_harness.K, tool=None)
    assert [h.item_id for h in hits] == result.top_ids
    return hits


def test_matching_hits_agrees_with_the_harness(monkeypatch):
    ids = _seed()
    case = {"q": QUERY, "expect_substr": EXPECT}
    (r,), _h, _m = eval_harness.evaluate(golden=[case])
    matching = eval_composition._matching_hits(case, _hits(r))
    assert [h.item_id for h in matching] == [ids["real"]], "an echo carrier never satisfies"
    assert matching[0].item_id == r.top_ids[r.rank - 1], "the same first satisfier as the harness"
    monkeypatch.setattr(eval_trust, "verified_lift", lambda share, n: {})
    composition = eval_composition._trust_composition([case])
    assert composition["eval_satisfier_ids"] == [ids["real"]]
    # Stub hits whose rows CANNOT exist (TA-14: ids 1/2 were the seeded rows, so the
    # fallback never ran): matching degrades to title+snippet and decides the outcome.
    gone, also_gone = 10**9, 10**9 + 1
    assert eval_composition._body(gone) == "" == eval_composition._body(also_gone)
    stub = [
        SimpleNamespace(item_id=gone, title=f"re: {QUERY}", snippet="median of three", type="note"),
        SimpleNamespace(item_id=also_gone, title="median of three", snippet="", type="note"),
    ]
    assert [h.item_id for h in eval_composition._matching_hits(case, stub)] == [also_gone]
    title_only = [SimpleNamespace(item_id=gone, title="nothing", snippet="median of three")]
    assert [h.item_id for h in eval_composition._matching_hits(case, title_only)] == [gone]


# --- 6. the skips ride META only -----------------------------------------------------


def test_echo_skips_stay_out_of_the_report_body(monkeypatch):
    ids = _seed()
    golden = [{"q": QUERY, "expect_substr": EXPECT}, {"q": QUERY, "expect_item_id": ids["real"]}]
    monkeypatch.setattr(eval_sentinel.eval_harness, "load_golden", lambda golden_path=None: golden)
    monkeypatch.setattr(eval_sentinel.eval_epoch, "golden_hash", lambda path=None: "ec40ec40ec40")
    counts: dict = {}
    eval_sentinel.run_sentinel(counts)
    assert counts["eval_status"] == "ok"
    assert counts["eval_echo_skips"] == {QUERY: [ids["carrier"]]}, "only cases with skips"
    assert counts["eval_echo_skipped"] == 1
    assert ids["carrier"] not in counts["eval_satisfier_ids"], "the composition agrees"
    report_id = run_report._write_report(dict(counts))
    with db.connect() as conn:
        body, meta = conn.execute(
            "SELECT body, meta FROM items WHERE id = %s", (report_id,)
        ).fetchone()
    assert "eval_echo_skip" not in body, "the count is meta-only too: E2 adds no body line"
    assert QUERY not in body, "golden text is never indexed"
    assert meta["eval_echo_skips"] == {QUERY: [ids["carrier"]]} and meta["eval_echo_skipped"] == 1


# --- 7. review #10 fix round: carriers below, word boundaries, the triage ---------------


def _stub_hits(monkeypatch, rows: list[tuple[int, str, str]]) -> list:
    """Hits over ``(id, title, body)`` rows the criterion reads through a stubbed get_item."""
    bodies = {i: body for i, _t, body in rows}
    monkeypatch.setattr(
        eval_criteria, "get_item", lambda item_id, **k: SimpleNamespace(body=bodies[item_id])
    )
    return [SimpleNamespace(item_id=i, title=t, snippet="") for i, t, _b in rows]


def test_a_carrier_below_the_satisfier_is_named_and_the_rank_is_unchanged(monkeypatch):
    """F9 (harness probe inverted): the scan runs on past the satisfier for carriers."""
    hits = _stub_hits(
        monkeypatch,
        [
            (1, "real", "the routine takes the median of three reads"),
            (2, "notes", f"golden check: {QUERY}; median of three"),
            (3, "other", "unrelated"),
            (4, "more notes", f"asked again: {QUERY}"),
            (5, "also real", "a second source: the median of three reads"),  # TA2-1
        ],
    )
    echoes: list[int] = []
    assert eval_criteria._substr_rank(hits, EXPECT, QUERY, echoes) == 1, "the FIRST satisfier"
    assert echoes == [2, 4], "EVERY carrier in the top-K, in rank order"
    rank, verdict = eval_criteria.substr_criterion(hits, EXPECT, 5, QUERY)
    assert rank == 1 and verdict.detail == "matched at rank 1; echo skipped [2, 4]"
    assert eval_criteria._substr_rank(hits, EXPECT) == 1, "no query: the pre-E2 scan"


def test_the_harness_names_a_carrier_ranked_below_the_real_satisfier():
    # The real satisfier keeps its embedding (BOTH legs); the carrier is keyword-only.
    real = ingest("hw-lab", "decision", "Widget calibration drift during the long soak", "x")
    carrier = _blind(
        ingest("kb-system", "note", "Archive", f"Asked: {QUERY}. Answer: median of three.")
    )
    also = _blind(ingest("hw-lab", "note", "Soak notes", "the widget calibration drift, again"))
    golden = [{"q": QUERY, "expect_substr": ["widget calibration drift"]}]
    (r,), _h, _m = eval_harness.evaluate(golden=golden)
    assert r.top_ids.index(real) < r.top_ids.index(carrier), "fixture: the carrier is below"
    first = min(r.top_ids.index(real), r.top_ids.index(also)) + 1  # TA2-1: two real satisfiers
    assert r.rank == first and r.echo_skipped == [carrier], "the FIRST satisfier owns the rank"


def test_the_echo_match_is_anchored_on_word_boundaries():
    """F11: a phrase inside longer words at either end is NOT an echo; a quote is."""
    q = "how to flash the image"  # 5 prose words: guarded
    assert eval_criteria.echo_guarded(q)
    assert not eval_criteria.is_echo(q, "Show to flash the images onto the board")
    assert not eval_criteria.is_echo(q, "Show to flash the image onto the board"), "left end"
    assert not eval_criteria.is_echo(q, "how to flash the images onto the board"), "right end"
    assert eval_criteria.is_echo(q, 'Q: "How to flash the image?" — hold BOOT, then reset')
    assert eval_criteria.is_echo(q, "how to flash the image"), "the whole haystack"


def test_the_guard_and_norm_details_are_pinned():
    """TA-15: strip, search's MAX_ID_TOKENS cap, and the echo count are ids not cases."""
    assert eval_criteria.is_echo(f"  {QUERY}  ", QUERY), "the query is stripped first"
    nine_ids = " ".join(f"QX{i}" for i in range(1, 10))  # 9 distinct identifiers
    q = f"{nine_ids} the widget calibration drifts during every long soak"  # + 8 prose words
    assert len(search_legs._identifier_tokens(q)) == 9 > search_legs.MAX_ID_TOKENS
    assert eval_criteria.echo_guarded(q), (
        "8 capped identifiers of 17 words: prose, as search reads it"
    )
    r = eval_harness.CaseResult(QUERY, 1, True, top_ids=[1, 2, 3], echo_skipped=[2, 3])
    assert eval_cases.replay_keys([r])["eval_echo_skipped"] == 2, "ids, not cases"


def test_a_miss_held_only_by_echo_carriers_reads_content_absent():
    """F12: echo carriers are not content."""
    q = "why does the zephyr bracket loosen after the thermal soak"
    carrier = ingest(
        "kb-system", "note", "Session notes", f"Golden check: {q}. Answer: torque stripe."
    )
    (r,), _h, _m = eval_harness.evaluate(golden=[{"q": q, "expect_substr": ["torque stripe"]}])
    assert not r.passed and r.echo_skipped == [carrier]
    detail = r.criteria[0].detail
    assert detail.startswith("CONTENT-ABSENT") and "(only echo carriers hold it)" in detail
    assert detail.endswith(f"; echo skipped [{carrier}]")
    assert eval_echo.content_triage(["torque stripe"], q) == "echo-only"
    assert eval_echo.content_triage(["zz-absent-zz"], q) == "absent"
    # TA2-8 (B9): a later substring nobody holds never erases the carriers' note.
    assert eval_echo.content_triage(["torque stripe", "zz-absent-zz"], q) == "echo-only"
    # One real holder anywhere in the corpus: a retrieval miss, as ever.
    ingest("hw-lab", "note", "Bracket fix", "a torque stripe on every bracket bolt")
    assert eval_echo.content_triage(["torque stripe"], q) == "present"


def test_a_capped_triage_read_cannot_prove_only_carriers_hold_it(monkeypatch):
    q = "why does the zephyr bracket loosen after the thermal soak"
    ingest("kb-system", "note", "Session notes", f"Golden check: {q}. Answer: torque stripe.")
    assert eval_echo.content_triage(["torque stripe"], q) == "echo-only"
    monkeypatch.setattr(eval_echo, "CONTENT_SCAN_CAP", 1)
    assert eval_echo.content_triage(["torque stripe"], q) == "present", "bounded: no negative"


# --- 8. fix-round review: the triage reads substrings literally, and is bounded ---------

ZQ = "why does the zephyr bracket loosen after the thermal soak"  # synthetic, guarded


def test_the_triage_reads_a_substring_literally_never_as_a_like_pattern():
    """H2 (harness probe P6 inverted): '_' and '%' are LIKE wildcards and '\\' its escape,
    so a row that only MATCHED the pattern ('zephyr-gear' for 'zephyr_gear') counted as
    content and masked 'echo-only' — and a literal backslash went unfound. A holder is a
    row holding the substring LITERALLY, exactly as the harness's own scan reads it."""
    carrier = ingest(
        "kb-system", "note", "Session notes", f"Golden check: {ZQ}. Answer: zephyr_gear."
    )
    ingest("hw-lab", "note", "Gear log", "the zephyr-gear housing was swapped at load 90 percent")
    assert eval_echo.content_triage(["zephyr_gear"], ZQ) == "echo-only", "a near-miss holds nothing"
    assert eval_echo.content_triage(["load 90%"], ZQ) == "absent", "'%' is literal"
    ingest("hw-lab", "note", "Paths", "soak logs land in C:\\kb\\zephyr on the bench box")
    assert eval_echo.content_triage(["C:\\kb\\zephyr"], ZQ) == "present", "'\\' is literal"
    assert eval_criteria._content_exists(["C:\\kb\\zephyr"]), "the pre-E2 check agrees"
    assert not eval_criteria._content_exists(["load 90%"])
    (r,), _h, _m = eval_harness.evaluate(golden=[{"q": ZQ, "expect_substr": ["zephyr_gear"]}])
    assert not r.passed and r.echo_skipped == [carrier]
    detail = r.criteria[0].detail
    assert detail.startswith("CONTENT-ABSENT") and "(only echo carriers hold it)" in detail


def test_near_misses_never_fill_the_triage_cap_and_the_escape_is_literal(monkeypatch):
    """H2: the SQL itself is literal (``like_pattern``), so LIKE near-misses never fill the
    read cap — 2 near-misses + 1 carrier under a cap of 2 still read 'echo-only' — and
    '!', the ESCAPE character, is literal too."""
    ingest("kb-system", "note", "Session notes", f"Golden check: {ZQ}. Answer: zephyr_gear.")
    ingest("hw-lab", "note", "Gear log", "the zephyr-gear housing was swapped")
    ingest("hw-lab", "note", "Spares", "a zephyr gear spare sits on the shelf")
    monkeypatch.setattr(eval_echo, "CONTENT_SCAN_CAP", 2)
    assert eval_echo.content_triage(["zephyr_gear"], ZQ) == "echo-only", "the cap counts holders"
    ingest("hw-lab", "note", "Tags", "the zephyr!gear tag marks the spare")
    assert eval_echo.content_triage(["zephyr!gear"], ZQ) == "present", "'!' is literal"
    assert eval_criteria._content_exists(["zephyr!gear"])


def test_the_triage_read_is_bounded_per_substring(monkeypatch):
    """H3 (MA): at most CONTENT_SCAN_CAP rows leave the DB per substring."""
    for n in range(3):
        ingest("kb-system", "note", f"Notes {n}", f"Golden check {n}: {ZQ}. Answer: torque stripe.")
    monkeypatch.setattr(eval_echo, "CONTENT_SCAN_CAP", 2)
    fetched: list[int] = []
    real = eval_echo.connect

    class _Spy:
        def __init__(self) -> None:
            self._conn = real()

        def __enter__(self):
            self._inner = self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def execute(self, sql, params):
            rows = self._inner.execute(sql, params).fetchall()
            fetched.append(len(rows))
            return SimpleNamespace(fetchall=lambda: rows)

    monkeypatch.setattr(eval_echo, "connect", _Spy)
    assert eval_echo.content_triage(["torque stripe"], ZQ) == "present", "capped: no negative"
    assert fetched == [2], "LIMIT is the cap — never the whole match set"
