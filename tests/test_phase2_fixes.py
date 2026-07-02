"""Regression tests for the Phase 2 adversarial-review findings."""

from __future__ import annotations

import pytest

from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest
from kbcore.items import timeline
from kbmcp.server import _parse_links, _validate_runtime_config

# --- startup guards (SEC-1) ---------------------------------------------------


def _with(monkeypatch, **overrides):
    for k, v in overrides.items():
        monkeypatch.setattr(settings, k, v)


def test_refuses_default_token(monkeypatch):
    _with(monkeypatch, kb_token="change-me-long-random")
    with pytest.raises(SystemExit, match="KB_TOKEN"):
        _validate_runtime_config()


def test_refuses_empty_and_short_tokens(monkeypatch):
    for bad in ("", "short"):
        _with(monkeypatch, kb_token=bad)
        with pytest.raises(SystemExit, match="KB_TOKEN"):
            _validate_runtime_config()


def test_refuses_wildcard_binds(monkeypatch):
    _with(monkeypatch, kb_token="x" * 32)
    for bad in ("", "0.0.0.0", "::", "*"):
        _with(monkeypatch, kb_bind=bad)
        with pytest.raises(SystemExit, match="KB_BIND"):
            _validate_runtime_config()


def test_accepts_good_config(monkeypatch):
    _with(monkeypatch, kb_token="x" * 32, kb_bind="127.0.0.1")
    _validate_runtime_config()  # no raise


# --- kb_save link validation ---------------------------------------------------


def test_parse_links_rejects_bad_rel_and_missing_id():
    with pytest.raises(ValueError, match="unknown relation"):
        _parse_links([{"rel": "causes", "item_id": 1}])
    with pytest.raises(ValueError, match="item_id"):
        _parse_links([{"rel": "fixes"}])
    assert _parse_links([{"rel": "fixes", "item_id": "7"}]) == [("fixes", 7)]
    assert _parse_links(None) == []


# --- timeline ------------------------------------------------------------------


@pytest.mark.usefixtures("kb")
def test_timeline_to_date_is_inclusive_of_the_day():
    item_id = ingest(project="tlp", type="note", title="today item", body="created today")
    today = timeline("tlp")[0].created_at.date().isoformat()
    ids = [i.item_id for i in timeline("tlp", to_date=today)]
    assert item_id in ids, "to_date=<creation day> must include that day's items"


@pytest.mark.usefixtures("kb")
def test_timeline_rejects_garbage_dates_with_domain_error():
    with pytest.raises(InvalidOperation, match="YYYY-MM-DD"):
        timeline("tlp", from_date="not-a-date")
    with pytest.raises(InvalidOperation, match="YYYY-MM-DD"):
        timeline("tlp", to_date="2026-13-45")


@pytest.mark.usefixtures("kb")
def test_timeline_returns_most_recent_when_over_limit():
    ids = [
        ingest(project="tlp", type="note", title=f"tl item {i}", body=f"timeline body {i}")
        for i in range(5)
    ]
    got = timeline("tlp", limit=2)
    assert [i.item_id for i in got] == ids[-2:], "most recent N, ascending order"


@pytest.mark.usefixtures("kb")
def test_timeline_limit_is_clamped():
    ingest(project="tlp", type="note", title="one", body="single item")
    assert len(timeline("tlp", limit=-5)) == 1  # clamped to >= 1, no psycopg error
