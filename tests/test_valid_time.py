"""WP1 — bitemporal VALID-TIME on items.

Covers the interval round-trip through get_item, the search `as_of` valid-time
filter (half-open, valid_to exclusive), and the shared instant parser. Real DB
in kb_test; Ollama mocked by conftest (CLAUDE.md: real-DB tests, mock only
Ollama). Hits are made deterministic with a shared keyword ("widgetflux") so the
keyword leg matches every item regardless of the fake embedding vectors.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import get_item, parse_instant
from kbcore.search import search
from kbcore.supersede import supersede

pytestmark = pytest.mark.usefixtures("kb")

T = datetime(2026, 6, 1, tzinfo=UTC)  # the as_of instant under test
JAN = datetime(2026, 1, 1, tzinfo=UTC)
DEC = datetime(2026, 12, 31, tzinfo=UTC)
MAR = datetime(2026, 3, 1, tzinfo=UTC)
SEP = datetime(2026, 9, 1, tzinfo=UTC)


def _ingest(title: str, *, valid_from=None, valid_to=None) -> int:
    """Ingest a note sharing the 'widgetflux' keyword; distinct title ⇒ no dedupe."""
    item_id = ingest(
        project="valid-time",
        type="note",
        title=title,
        body=f"The widgetflux protocol note for the {title} case.",
        valid_from=valid_from,
        valid_to=valid_to,
    )
    assert item_id is not None
    return item_id


def _four() -> dict[str, int]:
    return {
        "covering": _ingest("covering", valid_from=JAN, valid_to=DEC),  # covers T
        "ended": _ingest("ended", valid_to=MAR),  # valid_to <= T
        "future": _ingest("future", valid_from=SEP),  # valid_from > T
        "unbounded": _ingest("unbounded"),  # NULL/NULL — always
    }


# --- interval round-trip ------------------------------------------------------


def test_interval_roundtrips_through_get_item():
    item = get_item(_ingest("covering", valid_from=JAN, valid_to=DEC))
    assert item.valid_from == JAN
    assert item.valid_to == DEC


def test_null_interval_is_the_default():
    item = get_item(_ingest("open-ended"))
    assert item.valid_from is None
    assert item.valid_to is None


# --- as_of search filter ------------------------------------------------------


def test_as_of_keeps_only_facts_valid_at_the_instant():
    ids = _four()
    got = {h.item_id for h in search("widgetflux", as_of=T)}
    assert ids["covering"] in got  # interval covers T
    assert ids["unbounded"] in got  # NULL bounds are unbounded
    assert ids["ended"] not in got  # ended before T (valid_to <= T)
    assert ids["future"] not in got  # not yet valid at T (valid_from > T)


def test_as_of_unset_applies_no_valid_time_filter():
    ids = _four()
    got = {h.item_id for h in search("widgetflux")}
    assert got == set(ids.values())  # identical to pre-WP1 behaviour


def test_valid_to_is_exclusive_and_valid_from_is_inclusive():
    ends_at_t = _ingest("ends-at-t", valid_from=JAN, valid_to=T)
    starts_at_t = _ingest("starts-at-t", valid_from=T)
    got = {h.item_id for h in search("widgetflux", as_of=T)}
    assert ends_at_t not in got  # half-open: valid_to == T is excluded
    assert starts_at_t in got  # valid_from == T is included


def test_search_hit_carries_the_interval():
    item_id = _ingest("covering", valid_from=JAN, valid_to=DEC)
    hit = next(h for h in search("widgetflux") if h.item_id == item_id)
    assert hit.valid_from == JAN
    assert hit.valid_to == DEC


# --- the shared instant parser ------------------------------------------------


def test_parse_instant_accepts_date_and_timestamp():
    assert parse_instant("2026-06-01", "as_of") == T
    assert parse_instant("2026-06-01T12:30:00+00:00", "as_of") == datetime(
        2026, 6, 1, 12, 30, tzinfo=UTC
    )
    # A naive timestamp is assumed UTC (Invariant 7).
    assert parse_instant("2026-06-01T12:30:00", "as_of") == datetime(2026, 6, 1, 12, 30, tzinfo=UTC)


def test_parse_instant_rejects_garbage():
    with pytest.raises(InvalidOperation):
        parse_instant("not-a-date", "as_of")


# --- interval validation (Task 2) ---------------------------------------------


def test_ingest_rejects_inverted_interval():
    with pytest.raises(InvalidOperation, match="strictly before"):
        _ingest("inverted", valid_from=DEC, valid_to=MAR)


def test_ingest_rejects_empty_interval():
    # from == to is the empty half-open interval [T, T) — matches no as_of.
    with pytest.raises(InvalidOperation, match="strictly before"):
        _ingest("empty", valid_from=T, valid_to=T)


def test_search_as_of_naive_datetime_is_normalized_to_utc():
    # search() coerces a naive as_of at the read point (Invariant 7 symmetry).
    ids = _four()
    naive = datetime(2026, 6, 1)  # tzinfo is None
    got = {h.item_id for h in search("widgetflux", as_of=naive)}
    assert ids["covering"] in got and ids["unbounded"] in got
    assert ids["ended"] not in got and ids["future"] not in got


# --- content-hash dedupe vs a differing interval (Task 3) ---------------------


def test_same_content_different_interval_is_a_distinct_assertion():
    a = ingest("dedupe-vt", "note", "proto", "Widget uses proto v1.")
    b = ingest("dedupe-vt", "note", "proto", "Widget uses proto v1.", valid_to=T)
    assert a is not None and b is not None and a != b, "an interval makes it a new assertion"


def test_interval_less_resave_still_dedupes():
    a = ingest("dedupe-vt2", "note", "c", "body c")
    b = ingest("dedupe-vt2", "note", "c", "body c")
    assert a is not None and b is None, "interval-less idempotency unchanged (hard rule 3)"


def test_identical_interval_resave_dedupes():
    a = ingest("dedupe-vt3", "note", "d", "body d", valid_from=JAN, valid_to=DEC)
    b = ingest("dedupe-vt3", "note", "d", "body d", valid_from=JAN, valid_to=DEC)
    assert a is not None and b is None, "same content + same interval still dedupes"


# --- supersede valid-time carry / override / auto-close (Task 1) ---------------


def test_supersede_new_body_inherits_the_old_interval():
    old = _ingest("bounded fact", valid_from=JAN, valid_to=DEC)
    new = supersede(old, new_body="corrected bounded fact", reason="typo")
    repl = get_item(new)
    assert repl.valid_from == JAN and repl.valid_to == DEC, "correction carries the interval"


def test_supersede_explicit_override_replaces_the_interval():
    old = _ingest("v1 fact", valid_from=JAN, valid_to=DEC)
    new = supersede(old, new_body="v2 fact", reason="new window", valid_from=MAR, valid_to=SEP)
    repl = get_item(new)
    assert repl.valid_from == MAR and repl.valid_to == SEP


def test_supersede_explicit_new_start_auto_closes_the_open_old_window():
    old = _ingest("service on host Y", valid_from=JAN)  # still true (valid_to NULL)
    supersede(old, new_body="service on host Z", reason="moved", valid_from=T)
    assert get_item(old).valid_to == T, "the old fact's open window is closed at the new start"


def test_supersede_new_item_path_closes_with_the_new_items_start():
    old = _ingest("running v1", valid_from=JAN)  # open
    newer = _ingest("running v2", valid_from=SEP)  # carries its own start
    supersede(old, new_item_id=newer, reason="upgraded")
    assert get_item(old).valid_to == SEP, "old window closed at the successor's valid_from"
    assert get_item(newer).valid_from == SEP, "the successor item is untouched"


def test_supersede_plain_correction_does_not_auto_close():
    old = _ingest("still true", valid_from=JAN)  # open, no new start supplied
    supersede(old, new_body="still true (typo fixed)", reason="typo")
    assert get_item(old).valid_to is None, "a plain correction leaves the window open"


def test_supersede_rejects_valid_time_on_the_new_item_path():
    old = _ingest("x")
    newer = _ingest("y")
    with pytest.raises(InvalidOperation, match="new_body path"):
        supersede(old, new_item_id=newer, reason="r", valid_from=T)


def test_supersede_override_inverted_interval_is_rejected():
    old = _ingest("z", valid_from=JAN)
    with pytest.raises(InvalidOperation, match="strictly before"):
        supersede(old, new_body="z2", reason="r", valid_from=DEC, valid_to=MAR)


def test_supersede_new_start_before_old_start_is_rejected():
    old = _ingest("started march", valid_from=MAR)  # open
    with pytest.raises(InvalidOperation, match="precedes"):
        supersede(old, new_body="started earlier?", reason="bad", valid_from=JAN)


# --- supersede EQUAL-start same-window takeover (finding 0) --------------------


def test_supersede_equal_start_new_body_is_a_same_window_takeover():
    # Restating the UNCHANGED start is a same-window correction, NOT an error: the
    # window is unchanged so no auto-close (equal start would be an empty interval).
    old = _ingest("v1 same window", valid_from=JAN)  # open
    new = supersede(old, new_body="v2 same window", reason="restated", valid_from=JAN)
    assert get_item(old).valid_to is None, "equal start leaves the old window unchanged"
    assert get_item(old).status == "superseded"
    assert get_item(new).valid_from == JAN


def test_supersede_equal_start_new_item_path_is_a_same_window_takeover():
    # The dead-end from the finding: a pre-saved successor sharing the old fact's
    # true start could not supersede an open predecessor. Equality now skips the
    # auto-close instead of raising 'precedes'.
    old = _ingest("running v1", valid_from=JAN)  # open
    newer = _ingest("running v2", valid_from=JAN)  # same true start
    supersede(old, new_item_id=newer, reason="corrected")
    assert get_item(old).valid_to is None, "same-start successor leaves the window open"
    assert get_item(old).status == "superseded"
    assert get_item(old).superseded_by == newer


# --- supersede valid_to-only closes the old open window (finding 1) ------------


def test_supersede_valid_to_only_closes_the_old_open_window():
    old = _ingest("service running", valid_from=JAN)  # open (valid_to NULL)
    new = supersede(old, new_body="service stopped", reason="ended", valid_to=T)
    assert get_item(old).valid_to == T, "valid_to-only closes the old open window at T"
    repl = get_item(new)
    assert repl.valid_from == JAN and repl.valid_to == T, "replacement carries [JAN, T)"


def test_supersede_valid_to_only_fixes_inverted_visibility_past_the_end():
    # Before the fix the old row stayed OPEN while the correction was closed, so an
    # as_of past T returned the STALE superseded fact and dropped the correction.
    old = _ingest("valid-to fact", valid_from=JAN)  # open, matches 'widgetflux'
    supersede(old, new_body="ended fact", reason="ended", valid_to=T)
    got = {h.item_id for h in search("widgetflux", as_of=SEP, include_superseded=True)}
    assert old not in got, "the closed old window no longer reports as valid past its end"


# --- content_hash: injective interval fold + interval-less stability ----------


def test_interval_fold_is_not_delimiter_injectable():
    iso = JAN.isoformat()
    # A: a crafted body that ENDS with a plaintext 'valid:...' suffix, NO interval.
    a = ingest("inject", "note", "proto", f"Widget uses proto v1.\n\nvalid:{iso}..")
    # B: the plain body carrying a GENUINE interval.
    b = ingest("inject", "note", "proto", "Widget uses proto v1.", valid_from=JAN)
    assert a is not None and b is not None and a != b, "no collision — the fold is injective"


def test_content_hash_interval_less_is_stable_and_unfolded():
    import hashlib

    from kbcore.ingest import content_hash

    expected = hashlib.sha256(b"t\n\nb").hexdigest()
    assert content_hash("t", "b") == expected, "interval-less hash unchanged (Invariant 3)"
    assert content_hash("t", "b", valid_from=JAN) != expected, "an interval yields a distinct key"


def test_supersede_twice_second_is_rejected():
    # Exercises the FOR UPDATE re-read + status guard (finding 4): a second
    # supersede of an already-superseded row is rejected, not a silent double-mint.
    old = _ingest("lockme", valid_from=JAN)
    supersede(old, new_body="first correction", reason="one")
    with pytest.raises(InvalidOperation, match="already superseded"):
        supersede(old, new_body="second correction", reason="two")
