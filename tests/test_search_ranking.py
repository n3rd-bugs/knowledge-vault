"""Ranking rework tests: rank-preserving bonuses (PART A) + candidate legs (PART B).

PART A — boost_project is MULTIPLICATIVE (BOOST_FACTOR) and the exact-identifier
bonus is additive-but-capped (~one rank-1 RRF increment): a strong cross-project
rank-1 answer can no longer be buried under a boosted-project tail hit, ties
still break toward the boosted project, and the expansion-neighbor clamp holds
AFTER the new arithmetic.

PART B — keyword leg gains an OR fallback (fires only when AND yields <
KW_OR_FALLBACK_MIN rows) and a third identifier candidate leg (word-boundary
symbol match under the same scope wall) feeds RRF, so bare symbols / pasted
error strings outside the vector top-N become reachable.

Real DB in kb_test via the `kb` fixture; Ollama mocked by conftest.
"""

from __future__ import annotations

import pytest

from kbcore import db, principals, scope, search_legs
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.search import (
    BOOST_FACTOR,
    EXACT_ID_BONUS,
    EXACT_ID_BONUS_CAP,
    _apply_bonuses,
    _filters,
    search,
)
from kbcore.search_legs import RRF_K

pytestmark = pytest.mark.usefixtures("kb")


def _blind(item_id: int) -> int:
    """NULL the item's current-model embedding so the vector leg skips it."""
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET embedding = NULL WHERE item_id = %s", (item_id,))
    return item_id


def _make_public(*projects: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE projects SET public = TRUE WHERE name = ANY(%s)", (list(projects),))


def _open_where():
    return _filters(None, None, None, None, ["active"], None)


# --- PART A: rank-preserving bonus arithmetic ---------------------------------


def test_foreign_rank1_beats_boosted_tail_hit():
    """(a) A cross-project item at raw rank 1 outranks a boosted-project item at
    raw rank ~10 — the exact inversion the old flat +0.05 boost caused."""
    foreign = ingest("other-proj", "note", "Foreign exact answer", "the canonical answer body")
    local = ingest("mine", "note", "Local tail note", "a weakly related local note")
    rank1 = 2.0 / (RRF_K + 1)  # two-leg rank-1 raw fused score
    tail = 1.0 / (RRF_K + 10)  # single-leg rank-10 straggler
    with db.connect() as conn:
        out = _apply_bonuses(conn, [(foreign, rank1, "a"), (local, tail, "b")], "mine", [], {})
    scores = {i: s for i, s, _ in out}
    assert scores[local] == pytest.approx(tail * BOOST_FACTOR), "boost is multiplicative"
    assert scores[foreign] == rank1, "unboosted score untouched"
    assert scores[foreign] > scores[local], "strong foreign hit still wins"


def test_boost_does_not_bury_cross_project_exact_answer():
    """(a) end-to-end: boost_project set, yet the foreign exact answer stays #1."""
    target = ingest(
        "other-proj", "gotcha", "ZQUERY42 root cause", "ZQUERY42 fails when the clock gate is off."
    )
    for i in range(3):
        ingest("mine", "note", f"local filler {i}", f"unrelated local filler body number {i}")
    hits = search("ZQUERY42", boost_project="mine")
    assert hits and hits[0].item_id == target, "cross-project exact answer floats over the boost"
    assert hits[0].project == "other-proj"


def test_exact_tie_breaks_toward_boosted_project():
    """(b) At an exact raw tie the boosted project wins — the boost's whole job."""
    a = ingest("other-proj", "note", "tied A", "tied body a")
    b = ingest("mine", "note", "tied B", "tied body b")
    s = 2.0 / (RRF_K + 1)
    with db.connect() as conn:
        out = _apply_bonuses(conn, [(a, s, ""), (b, s, "")], "mine", [], {})
    scores = {i: sc for i, sc, _ in out}
    assert scores[b] > scores[a], "tie breaks toward the boosted project"


def test_boost_effect_on_rank1_stays_below_one_rrf_increment():
    """(c) Guard: neither bonus can move a rank-1 score by a full rank-1 RRF
    increment — the arithmetic stays rank-preserving by construction."""
    increment = 1.0 / (RRF_K + 1)
    max_raw = 3.0 / (RRF_K + 1)  # rank-1 in all three legs
    assert (BOOST_FACTOR - 1.0) * max_raw < increment
    assert EXACT_ID_BONUS_CAP <= increment
    assert EXACT_ID_BONUS < EXACT_ID_BONUS_CAP


def test_exact_id_bonus_stacking_is_capped():
    """(c) 8 matched identifier tokens add EXACT_ID_BONUS_CAP total, not 8x."""
    tokens = [f"ZTOK{i}X" for i in range(8)]
    many = ingest("p", "note", "many symbols", " ".join(tokens))
    one = ingest("p", "note", "one symbol", "only ZTOK0X appears here")
    base = 1.0 / (RRF_K + 1)
    with db.connect() as conn:
        out = _apply_bonuses(conn, [(many, base, ""), (one, base, "")], None, tokens, {})
    scores = {i: s for i, s, _ in out}
    assert scores[many] - base == pytest.approx(EXACT_ID_BONUS_CAP), "total capped"
    assert scores[one] - base == pytest.approx(EXACT_ID_BONUS), "single match adds one bonus"


def test_neighbor_clamp_holds_after_boost_and_bonus():
    """(d) A pure expansion neighbor stays STRICTLY below its vouching seed even
    when boost x bonus stacking would arithmetically lift it past the seed."""
    seed = ingest("workshop", "bug", "seed bug", "the seed bug body")
    nbr = ingest("mine", "note", "ZNBR9 reference", "ZNBR9 neighbor body")
    seed_score = 2.0 / (RRF_K + 1)
    decayed = seed_score * 0.5  # 1-hop expansion decay
    # Un-clamped: decayed * BOOST_FACTOR + EXACT_ID_BONUS_CAP > seed_score.
    assert decayed * BOOST_FACTOR + EXACT_ID_BONUS_CAP > seed_score
    fused = [(seed, seed_score, "s"), (nbr, decayed, "")]
    with db.connect() as conn:
        out = _apply_bonuses(conn, fused, "mine", ["ZNBR9"], {nbr: seed})
    scores = {i: s for i, s, _ in out}
    assert scores[nbr] < scores[seed], "clamp holds after the new bonus arithmetic"
    assert scores[seed] == seed_score


# --- PART B: keyword OR-fallback + identifier candidate leg -------------------


def test_or_fallback_fires_only_when_and_yields_few_rows():
    """(e) OR rerun only below KW_OR_FALLBACK_MIN AND-matching rows; AND matches
    keep their ranks ahead of OR-only rows; a single COMMON lexeme is noise."""
    where, params = _open_where()
    full = ingest("p", "note", "all terms", "qalpha qbravo qcharlie base note")
    partial = ingest("p", "note", "two terms", "qalpha qbravo partial note")
    lonely = ingest("p", "note", "one term", "qalpha solitary note")
    # Make 'qalpha' a COMMON lexeme (df > the rare-singleton threshold of 8) so
    # a qalpha-only match is inadmissible noise rather than a rare topic anchor.
    for i in range(9):
        ingest("p", "note", f"background {i}", f"qalpha background chatter number {i}")
    with db.connect() as conn:
        rows = search_legs._keyword_leg(conn, "qalpha qbravo qcharlie", where, params)
    ids = [i for i, _ in rows]
    assert full in ids and partial in ids, "AND yielded <5 rows, so the OR fallback fires"
    assert ids[0] == full, "the full AND match ranks ahead of the OR-only match"
    assert lonely not in ids, "a lone common-lexeme match stays out of the OR leg"
    for i in range(4):
        ingest("p", "note", f"filler {i}", f"qalpha qbravo qcharlie filler number {i}")
    with db.connect() as conn:
        rows = search_legs._keyword_leg(conn, "qalpha qbravo qcharlie", where, params)
    ids = [i for i, _ in rows]
    assert partial not in ids, "AND yields >=5 rows — the OR fallback must not fire"
    assert full in ids


def test_or_fallback_admits_a_rare_single_lexeme_anchor():
    """(e) The one term that matters: an item matching ONLY a rare lexeme
    ('wireguard' when the corpus says 'tunnels' not 'tunnel') is still admitted."""
    where, params = _open_where()
    anchor = ingest("p", "note", "vpn plan", "qwireguard mesh with several tunnels planned")
    ingest("p", "note", "other doc", "deployment chatter qtopology deployment notes")
    q = "qwireguard tunnel deployment qtopology"
    with db.connect() as conn:
        rows = search_legs._keyword_leg(conn, q, where, params)
    assert anchor in [i for i, _ in rows], "rare-lexeme single match is admitted to the OR leg"


def test_or_fallback_skips_single_word_queries():
    """(e) A lone symbol never falls back to an OR soup of its lexeme parts —
    reaching it is the identifier leg's job."""
    where, params = _open_where()
    ingest("p", "note", "parts only", "the qdelta and qecho parts appear separately")
    with db.connect() as conn:
        rows = search_legs._keyword_leg(conn, "qdelta_qecho", where, params)
    assert rows == [], "phrase-AND misses and no OR fallback fires for one word"


def test_multiline_pasted_error_string_finds_target():
    """(f) A pasted traceback (which AND-fails: extra tokens) still retrieves the
    gotcha holding the error string, via OR fallback + identifier leg."""
    target = ingest(
        "p",
        "gotcha",
        "UndefinedColumn tsv crash",
        "psycopg.errors.UndefinedColumn: column c.tsv does not exist"
        " — the chunks migration was skipped.",
    )
    ingest("p", "note", "unrelated a", "completely unrelated body about gardening")
    ingest("p", "note", "unrelated b", "another unrelated body about cooking")
    q = (
        "Traceback (most recent call last):\n"
        '  File "src/kbcore/search.py", line 204, in _keyword_leg\n'
        "psycopg.errors.UndefinedColumn: column c.tsv does not exist"
    )
    hits = search(q)
    assert hits and hits[0].item_id == target


def test_bare_symbol_beyond_vector_reach_surfaces_via_identifier_leg():
    """(g) A symbol living only inside a file-path tsv token (tsquery-invisible)
    on a vector-blinded item is reachable ONLY through the identifier leg."""
    target = ingest(
        "p", "note", "driver fix location", "Fix lives in /src/drv/spi_write.c line 88."
    )
    for i in range(3):
        ingest("p", "note", f"vector filler {i}", f"filler body number {i} about nothing")
    _blind(target)  # vector leg cannot return it; kw leg misses the path token
    hits = search("spi_write")
    ids = [h.item_id for h in hits]
    assert target in ids, "identifier leg makes the bare symbol reachable"
    assert hits[0].item_id == target, "and it outranks vector-only stragglers"


def test_id_dominant_classification():
    """The identifier leg fires for symbol lookups, not for prose with an acronym."""
    assert search_legs._id_dominant(
        "drv_spi_write", search_legs._identifier_tokens("drv_spi_write")
    )
    assert search_legs._id_dominant(
        "TPS55288 I2C output voltage", search_legs._identifier_tokens("TPS55288 I2C output voltage")
    )
    q = "SPI mode polarity problem motor driver"
    assert not search_legs._id_dominant(q, search_legs._identifier_tokens(q))
    assert not search_legs._id_dominant("plain prose query", [])


def test_prose_query_does_not_pull_identifier_only_matches():
    """A prose query mentioning an acronym must not surface an item reachable
    only through the identifier leg (the leg is gated to id-dominant queries).
    The lurker's acronym hides inside a path token, so only the id leg's regex
    could ever reach it — if the gate were dropped, this test would catch it."""
    lurker = ingest("p", "note", "acronym lurker", "Wiring notes live at /src/SPI/ctrl.c only.")
    ingest("p", "gotcha", "real answer", "motor driver polarity problem notes with SPI details")
    _blind(lurker)  # invisible to the vector leg; the path lexeme hides it from tsquery
    ids = [h.item_id for h in search("SPI mode polarity problem motor driver")]
    assert lurker not in ids, "identifier leg must stay quiet on prose queries"


def test_identifier_part_prefix_fallback_reaches_related_symbols():
    """A full-symbol miss retries with word-prefix parts: drv_spi_write reaches
    an item that only mentions DRV8706 and its SPI write path."""
    target = ingest(
        "p", "gotcha", "motor driver register notes", "The DRV8706 SPI register write path notes."
    )
    weak = ingest("p", "note", "lesser match", "you write things down here")
    _blind(target)
    _blind(weak)
    where, params = _open_where()
    with db.connect() as conn:
        rows = search_legs._identifier_leg(conn, ["drv_spi_write"], where, params)
    ids = [i for i, _ in rows]
    assert target in ids, "part-prefix fallback surfaces the DRV8706 item"
    assert ids[0] == target, "3-part coverage outranks the 1-part match"
    hits = search("drv_spi_write")
    assert hits and hits[0].item_id == target, "end-to-end: the symbol query finds it"


def test_identifier_leg_never_leaks_withheld_projects():
    """(h) Grant wall: the new leg runs under the same WHERE — a withheld
    project's item never surfaces for a grantee (test_no_oracle pattern)."""
    ia = ingest("a", "note", "driver fix location a", "Fix lives in /src/drv/spi_write.c line 88.")
    ib = ingest("b", "note", "driver fix location b", "Also fixed in /src/drv/spi_write.c line 99.")
    _blind(ia)
    _blind(ib)
    owner_ids = {h.item_id for h in search("spi_write", k=10)}
    assert {ia, ib} <= owner_ids, "owner sees both via the identifier leg"
    principals.create_principal("reader", kind="token")
    principals.grant("reader", "a", rw=False)
    g = principals.resolve_name("reader")
    assert g is not None
    with scope.use_principal(g):
        ids = {h.item_id for h in search("spi_write", k=10)}
    assert ia in ids, "granted project stays visible"
    assert ib not in ids, "identifier leg must not leak a withheld project"


def test_identifier_leg_respects_public_only_wall(monkeypatch):
    """(h) Public-only wall: same guarantee on a public-facing instance."""
    ia = ingest("pub", "note", "driver fix location", "Fix lives in /src/drv/spi_write.c line 88.")
    ib = ingest("priv", "note", "private fix location", "Also in /src/drv/spi_write.c line 99.")
    _blind(ia)
    _blind(ib)
    _make_public("pub")
    monkeypatch.setattr(settings, "kb_public_only", True)
    ids = {h.item_id for h in search("spi_write", k=10)}
    assert ia in ids, "public item found via the identifier leg"
    assert ib not in ids, "private item withheld under public-only"
