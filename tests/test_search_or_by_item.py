"""E3: the OR-coverage leg hands fusion one row per ITEM, not a window of chunk rows.

Its sort key is an item's coverage, so all of a long document's chunks sort together. Under a
LIMIT on chunk rows the first one or two documents took the whole leg and a short item holding
the answer never reached fusion (eval/EPOCHS.md, E3: golden cases 7 and 48).

Real DB in kb_test via the `kb` fixture. Chunks are inserted directly because these tests need
exact chunk counts and texts, which the chunker does not give. They carry no embedding, so
search() has only the keyword leg to go on.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from itertools import count

import psycopg
import pytest

from kbcore import db
from kbcore.search import SNIPPET_CHARS, _filters, _to_hits, search
from kbcore.search_legs import (
    CANDIDATES,
    KW_OR_FALLBACK_MIN,
    _keyword_leg,
    _or_coverage_rows,
)

pytestmark = pytest.mark.usefixtures("kb")

PROJECT = "or-by-item"
_hashes = count()


@contextmanager
def _seeding() -> Iterator[psycopg.Connection]:
    with db.transaction() as conn:
        for project in (PROJECT, f"{PROJECT}-other"):
            conn.execute(
                "INSERT INTO projects (name, domain) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (project, "embedded"),
            )
        yield conn


def _item(
    conn: psycopg.Connection,
    chunks: list[str],
    status: str = "active",
    project: str = PROJECT,
) -> int:
    """One item with these chunk texts in ord order, the highest ord inserted first so physical
    order can never agree with ord by accident."""
    item_id = conn.execute(
        "INSERT INTO items (project, type, title, body, status) "
        "VALUES (%s, 'note', %s, %s, %s) RETURNING id",
        (project, f"item {next(_hashes)}", " ".join(chunks), status),
    ).fetchone()[0]
    for ord_, text in reversed(list(enumerate(chunks))):
        conn.execute(
            "INSERT INTO chunks (item_id, ord, text, text_hash) VALUES (%s, %s, %s, %s)",
            (item_id, ord_, text, f"hash-{next(_hashes)}"),
        )
    return item_id


def _long_document() -> list[str]:
    """Covers qalpha, qbravo and qcharlie across its chunks, with no chunk holding all three,
    so the AND query misses it and its coverage sorts every one of its rows first."""
    terms = ("qalpha qbravo", "qbravo qcharlie", "qalpha qcharlie")
    return [f"{terms[n % 3]} part {n}" for n in range(50)]


def _wall(project: str | None = None) -> tuple[str, list]:
    return _filters(project, None, None, None, ["active"], None)


def _keyword_rows(query: str, project: str | None = None) -> list[tuple[int, str]]:
    with db.connect() as conn:
        return _keyword_leg(conn, query, *_wall(project))


def _or_rows(lexemes: list[str], project: str | None = None) -> list[tuple[int, str]]:
    with db.connect() as conn:
        return _or_coverage_rows(conn, lexemes, *_wall(project))


def _ids(rows: list[tuple[int, str]]) -> list[int]:
    return [item_id for item_id, _text in rows]


def test_a_long_document_no_longer_takes_the_whole_or_leg():
    with _seeding() as conn:
        long_item = _item(conn, _long_document())
        shorts = [_item(conn, [f"qbravo qcharlie short {n}"]) for n in range(6)]

    ids = _ids(_keyword_rows("qalpha qbravo qcharlie"))

    assert ids[0] == long_item, "the broadest coverage still sorts first"
    assert ids.count(long_item) == 1
    assert set(ids[1:]) == set(shorts), "under a chunk-row LIMIT these never reached fusion"


def test_a_short_item_behind_a_long_document_reaches_search():
    with _seeding() as conn:
        _item(conn, _long_document())
        answer = _item(conn, ["qbravo qcharlie the short item holding the answer"])

    assert answer in [h.item_id for h in search("qalpha qbravo qcharlie", k=10, tool=None)]


def test_the_or_query_itself_is_cut_at_candidates_items():
    with _seeding() as conn:
        long_item = _item(conn, _long_document())
        for n in range(CANDIDATES + 5):
            _item(conn, [f"qbravo qcharlie short {n}"])
        for n in range(150):  # keeps the lexemes under the df filter's N/3
            _item(conn, [f"filler {n}"])

    ids = _ids(_or_rows(["qalpha", "qbravo", "qcharlie"]))

    assert len(ids) == CANDIDATES
    assert len(set(ids)) == CANDIDATES, "RARE_ID_RANK_OFFSET needs distinct items in the window"
    assert ids[0] == long_item


def test_and_chunks_plus_or_items_are_still_cut_at_candidates():
    """Three AND rows inside ONE item plus 39 OR-only items would be 42 rows without the cut, and
    rank 42 scores below the 1/101 a rare-only identifier row gets."""
    with _seeding() as conn:
        _item(conn, [f"qalpha qbravo qcharlie all three {n}" for n in range(3)])
        for n in range(CANDIDATES + 5):
            _item(conn, [f"qbravo qcharlie short {n}"])
        for n in range(150):
            _item(conn, [f"filler {n}"])

    assert len(_keyword_rows("qalpha qbravo qcharlie")) == CANDIDATES


def test_an_item_is_represented_by_its_best_chunk():
    best = "qalpha qbravo side by side"
    with _seeding() as conn:
        item = _item(conn, ["qalpha alone first", best, "qbravo alone last"])

    assert _or_rows(["qalpha", "qbravo"]) == [(item, best)]


def test_an_items_chunk_is_chosen_over_the_kept_lexemes_not_the_dropped_ones():
    """'qthe' is over the df filter (20 of 30 items, above max(8, N/3) = 10), so it does not count
    toward coverage. It must not pick the chunk either: this one is what the reranker reads."""
    answer = "qrare answer here"
    with _seeding() as conn:
        item = _item(conn, ["qthe qthe qthe qthe qthe qthe boilerplate", answer])
        for n in range(19):
            _item(conn, [f"qthe chatter {n}"])
        for n in range(10):
            _item(conn, [f"filler {n}"])

    assert _or_rows(["qthe", "qrare"]) == [(item, answer)]


def test_dropped_lexemes_do_not_rank_chunks_or_count_toward_coverage():
    """Both chunks of the first item hold the kept lexeme: only a rank over the kept lexemes alone
    prefers the one with more of it. And an item holding the dropped lexeme gets no coverage for
    it, so the newer of two equal items comes first."""
    answer = "qrare qrare qrare answer"
    with _seeding() as conn:
        older = _item(conn, ["qrare qthe qthe qthe qthe qthe qthe qthe", answer])
        newer = _item(conn, [answer])
        for n in range(19):
            _item(conn, [f"qthe chatter {n}"])
        for n in range(10):
            _item(conn, [f"filler {n}"])

    rows = _or_rows(["qthe", "qrare"])

    assert rows == [(newer, answer), (older, answer)]


def test_a_lexeme_that_parses_to_nothing_does_not_break_the_query():
    with _seeding() as conn:
        item = _item(conn, ["qalpha qbravo words"])

    assert _ids(_or_rows(["qalpha", "&", "qbravo"])) == [item]


def test_equally_good_chunks_of_one_item_resolve_to_the_lowest_ord():
    with _seeding() as conn:
        item = _item(conn, [f"qalpha qbravo same words variant{n}" for n in range(10)])

    assert _or_rows(["qalpha", "qbravo"]) == [(item, "qalpha qbravo same words variant0")]


def test_items_tied_on_coverage_and_rank_come_back_newest_first():
    """Physical order is oldest first, so only the tie-break reverses it."""
    with _seeding() as conn:
        ties = [_item(conn, ["qalpha qbravo same words"]) for _ in range(5)]

    assert _ids(_or_rows(["qalpha", "qbravo"])) == ties[::-1]


def test_a_better_chunk_decides_between_items_of_equal_coverage():
    """`near` is the OLDER item, so the newest-first tie-break alone would put `far` first."""
    with _seeding() as conn:
        near = _item(conn, ["qalpha qbravo side by side"])
        far = _item(conn, ["qalpha alone here", "qbravo alone there"])

    assert _ids(_or_rows(["qalpha", "qbravo"])) == [near, far]


def test_and_matches_keep_their_ranks_ahead_of_or_only_items():
    """The OR order alone puts the other item first (equal coverage, a better chunk), so only
    the AND-first merge can put the AND match on top."""
    with _seeding() as conn:
        _item(conn, ["qalpha qbravo qalpha qbravo qalpha qbravo", "qcharlie elsewhere"])
        and_match = _item(conn, ["qalpha qbravo qcharlie all three in one chunk"])

    assert _ids(_or_rows(["qalpha", "qbravo", "qcharlie"]))[0] != and_match, "the premise"
    ids = _ids(_keyword_rows("qalpha qbravo qcharlie"))
    assert ids[0] == and_match
    assert ids.count(and_match) == 1, "an AND item is not listed again from the OR rows"


def test_the_or_fallback_trigger_counts_and_chunk_rows():
    """KW_OR_FALLBACK_MIN is defined on chunk rows: enough of them inside ONE item keep the
    strict leg, and an OR-only item stays out."""
    with _seeding() as conn:
        item = _item(conn, [f"qalpha qbravo row {n}" for n in range(KW_OR_FALLBACK_MIN)])
        _item(conn, ["qalpha something else"])

    rows = _keyword_rows("qalpha qbravo")

    assert set(_ids(rows)) == {item}
    assert len(rows) == KW_OR_FALLBACK_MIN, "the AND path is untouched: chunk rows, as before"


def test_a_lone_common_lexeme_is_selected_but_not_admitted():
    """N = 60: df(qcommon) = 12 is under the df filter's max(8, N/3) = 20, so it IS selected, and
    over the rare line max(8, N/100) = 8, so an item matching only it stays out."""
    with _seeding() as conn:
        lonely = _item(conn, ["qcommon solitary note"])
        both = _item(conn, ["qcommon qbravo together"])
        for n in range(10):
            _item(conn, [f"qcommon chatter {n}"])
        for n in range(48):
            _item(conn, [f"filler {n}"])

    ids = _ids(_or_rows(["qcommon", "qbravo"]))

    assert both in ids
    assert lonely not in ids


def test_a_rare_lexeme_outweighs_two_common_ones():
    """N = 60: ln(1 + 60/1) = 4.11 beats 2 ln(1 + 60/20) = 2.77, where a plain lexeme count would
    rank the other way."""
    with _seeding() as conn:
        rare = _item(conn, ["qrare anchor"])
        for n in range(20):
            _item(conn, [f"qcom1 qcom2 chatter {n}"])
        for n in range(39):
            _item(conn, [f"filler {n}"])

    assert _ids(_or_rows(["qrare", "qcom1", "qcom2"]))[0] == rare


def test_items_with_the_same_lexemes_tie_exactly_under_a_hash_plan():
    """Float addition depends on order, and the hash aggregates feed sum() in no fixed one. Each
    lexeme has its own df, hence its own weight: an ulp between these items would outrank the
    chunk rule. Rank must decide, and the older item holds the better chunk."""
    lexemes = ["qaa", "qbb", "qcc", "qdd", "qee"]
    with _seeding() as conn:
        items = [_item(conn, [" ".join(lexemes * (31 - n)), "filler words"]) for n in range(30)]
        for k, lex in enumerate(lexemes):
            for m in range(3 + 7 * k):
                _item(conn, [f"{lex} extra {k} {m}"])
        for n in range(150):
            _item(conn, [f"filler {n}"])

    with db.connect() as conn:
        conn.execute("SET LOCAL enable_sort = off")
        rows = _or_coverage_rows(conn, lexemes, *_wall())

    assert _ids(rows)[:30] == items, "strictly better chunks first: oldest first here"


def test_items_with_equal_weights_tie_exactly_across_different_lexemes():
    """N = 300: two lexeme sets with the same weights, summed in a different order under a sort on
    the lexeme. The older item holds the better chunk, so it must come first, not the ulp's pick."""
    with _seeding() as conn:
        y = _item(conn, ["qa qc qd side by side"])
        x = _item(conn, ["qa alone", "qb alone", "qc alone"])
        for n in range(7):
            _item(conn, [f"qa pad {n}"])
        for lex in ("qb", "qd", "qc"):
            for n in range(11):
                _item(conn, [f"{lex} pad {n}"])
        for n in range(258):
            _item(conn, [f"filler {n}"])

    assert _ids(_or_rows(["qa", "qb", "qc", "qd"])) == [y, x]


def test_the_cut_at_candidates_keeps_the_better_chunks():
    """Equal coverage, and more items than fit: the rank rule decides who reaches fusion."""
    with _seeding() as conn:
        better = [_item(conn, ["qalpha qbravo side by side"]) for _ in range(5)]
        for n in range(CANDIDATES + 5):
            _item(conn, [f"qalpha alone {n}", f"qbravo alone {n}"])
        for n in range(150):
            _item(conn, [f"filler {n}"])

    assert _ids(_or_rows(["qalpha", "qbravo"]))[:5] == better[::-1]


def test_the_cut_among_exact_ties_keeps_the_newest():
    with _seeding() as conn:
        ties = [_item(conn, ["qalpha qbravo same words"]) for _ in range(CANDIDATES + 5)]
        for n in range(150):
            _item(conn, [f"filler {n}"])

    assert _ids(_or_rows(["qalpha", "qbravo"])) == ties[::-1][:CANDIDATES]


def test_the_final_order_holds_under_a_hash_join():
    """The text join must not cost the order: without the outer ORDER BY a hash join follows
    chunks' physical order."""
    with _seeding() as conn:
        ties = [_item(conn, ["qalpha qbravo same words"]) for _ in range(8)]

    with db.connect() as conn:
        conn.execute("SET LOCAL enable_nestloop = off")
        conn.execute("SET LOCAL enable_mergejoin = off")
        rows = _or_coverage_rows(conn, ["qalpha", "qbravo"], *_wall())

    assert _ids(rows) == ties[::-1]


def test_a_query_of_only_common_lexemes_takes_the_all_common_fallback():
    """N = 30: every lexeme is over max(8, N/3) = 10, so `sel` is empty and `selected` falls back
    to the rarest few, for the coverage and for the kept tsquery alike."""
    with _seeding() as conn:
        hits = [_item(conn, [f"qthe qof chatter {n}"]) for n in range(20)]
        for n in range(10):
            _item(conn, [f"filler {n}"])

    assert set(_ids(_or_rows(["qthe", "qof"]))) == set(hits)


def test_an_item_whose_project_has_no_row_still_reaches_the_or_leg():
    """LEFT JOIN projects: the vector and identifier legs keep such an item, so this leg must."""
    with _seeding() as conn:
        item = _item(conn, ["qalpha qbravo orphan"], project=f"{PROJECT}-unregistered")

    assert _ids(_or_rows(["qalpha", "qbravo"])) == [item]


def test_the_or_leg_reaches_the_domain_filter():
    with _seeding() as conn:
        conn.execute(
            "INSERT INTO projects (name, domain) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (f"{PROJECT}-web", "web"),
        )
        mine = _item(conn, ["qalpha qbravo mine"])
        _item(conn, ["qalpha qbravo theirs"], project=f"{PROJECT}-web")

    with db.connect() as conn:
        rows = _or_coverage_rows(
            conn, ["qalpha", "qbravo"], *_filters(None, "embedded", None, None, ["active"], None)
        )

    assert _ids(rows) == [mine]


def test_the_or_leg_stays_inside_the_scope_and_status_wall():
    with _seeding() as conn:
        mine = _item(conn, ["qalpha qbravo mine"])
        _item(conn, ["qalpha qbravo someone else's"], project=f"{PROJECT}-other")
        _item(conn, ["qalpha qbravo retired"], status="superseded")

    assert _ids(_or_rows(["qalpha", "qbravo"], project=PROJECT)) == [mine]


def test_a_hit_with_no_chunk_snippet_still_previews_its_body():
    """_to_hits reads only as much body as the fallback snippet needs: same text, multibyte too."""
    body = "ہ" * (SNIPPET_CHARS + 50)
    with _seeding() as conn:
        item = conn.execute(
            "INSERT INTO items (project, type, title, body) VALUES (%s, 'note', 't', %s) "
            "RETURNING id",
            (PROJECT, body),
        ).fetchone()[0]

    hit = _to_hits([(item, 1.0, "")])[0]

    assert hit.snippet == body[:SNIPPET_CHARS]
