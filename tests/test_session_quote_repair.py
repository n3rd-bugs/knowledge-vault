"""Pipeline v2 WP-B1 — quote repair wired into extract_and_ingest (real DB, kb_test).

An exact quote ingests as before; a near-miss quote ingests with the LOCATED
verbatim span plus a meta.quote_repair audit record (ratio + scrubbed model
quote); fabricated content still rejects (SEC-3). kb_quote_repair_min_ratio=0
is the kill switch. Ingest stays idempotent with repair active (hard rule 3).
The extractor is passed as a stub (mock only Ollama, per CLAUDE.md).
"""

from __future__ import annotations

import pytest

from kbcore import db
from kbcore.config import settings
from kbcore.items import get_item
from kbworker.sessions import extract_and_ingest

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _tmp_raw(tmp_path, monkeypatch):
    """Keep raw archives off the real ~/kb during tests."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


SPAN = (
    "Decision: run the DRV8706 SPI bus in mode 1 (CPOL=0, CPHA=1), because mode 0 "
    "clocks data on the wrong edge and every register read returns zeros."
)
TEXT = (
    "We spent the afternoon on the DRV8706 SPI bring-up.\n\n"
    + SPAN
    + "\n\nAfter the change the fault register finally reported 0x00 as expected."
)
EXACT_QUOTE = "every register read returns zeros"
# Near-miss: a lightly-mangled copy of a real transcript span (comma and final
# period dropped) — fails the exact check, repairs at ratio >= 0.95.
NEAR_MISS = SPAN.replace("(CPOL=0, CPHA=1)", "(CPOL=0 CPHA=1)").replace("zeros.", "zeros")
FABRICATED = "the moon is made of green cheese and telepathy"


def _extractor(text: str) -> list[dict]:
    return [
        {
            "type": "decision",
            "title": "exact item",
            "body": "grounded exactly",
            "quote": EXACT_QUOTE,
            "tags": ["protocol:spi"],
        },
        {
            "type": "gotcha",
            "title": "near-miss item",
            "body": "grounded after repair",
            "quote": NEAR_MISS,
            "tags": [],
        },
        {
            "type": "note",
            "title": "fabricated item",
            "body": "invented claim",
            "quote": FABRICATED,
            "tags": [],
        },
    ]


def _session_items() -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT i.id, i.title FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session'"
        ).fetchall()
    return {title: get_item(item_id) for item_id, title in rows}


def _counts() -> tuple[int, int, int]:
    with db.connect() as conn:
        items = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        sources = conn.execute("SELECT count(*) FROM sources").fetchone()[0]
        queue = conn.execute("SELECT count(*) FROM review_queue").fetchone()[0]
    return items, sources, queue


def test_exact_repaired_and_fabricated_paths():
    assert NEAR_MISS not in TEXT, "sanity: the near-miss genuinely fails the exact check"
    saved, rejected = extract_and_ingest(TEXT, "proj", "test://wpb1/main", extractor=_extractor)
    assert saved == 2, "exact + repaired near-miss ingest"
    assert rejected == 1, "fabricated content still rejects (SEC-3)"

    items = _session_items()
    assert set(items) == {"exact item", "near-miss item"}

    # Exact quote: stored as today, no repair record.
    exact = items["exact item"]
    assert exact.meta["quote"] == EXACT_QUOTE
    assert "quote_repair" not in exact.meta

    # Near-miss: the stored quote is the LOCATED verbatim span (true punctuation,
    # a real substring of the transcript), never the model's drifted version.
    near = items["near-miss item"]
    assert near.meta["quote"] in TEXT
    assert "(CPOL=0, CPHA=1)" in near.meta["quote"]
    repair = near.meta["quote_repair"]
    assert repair["ratio"] >= 0.95
    assert repair["model_quote"] == NEAR_MISS
    assert near.trust == "unverified"

    with db.connect() as conn:
        queue = conn.execute("SELECT detail FROM review_queue").fetchall()
    assert len(queue) == 2
    assert all(detail["rejected_count"] == 1 for (detail,) in queue)


def test_kill_switch_min_ratio_zero_rejects_near_miss(monkeypatch):
    monkeypatch.setattr(settings, "kb_quote_repair_min_ratio", 0.0)
    saved, rejected = extract_and_ingest(TEXT, "proj", "test://wpb1/kill", extractor=_extractor)
    assert saved == 1, "only the exact quote survives with repair disabled"
    assert rejected == 2, "near-miss counts as rejected exactly as before WP-B1"
    assert set(_session_items()) == {"exact item"}


def test_model_quote_with_secret_is_scrubbed():
    # SEC-2: a secret inside the (hallucinated) MODEL quote must not become
    # searchable via meta.quote_repair.model_quote either.
    fake = "ghp_" + "0" * 36
    secret_span = (
        f"Gotcha: the CI runner leaked the token {fake} into the build log; "
        "rotate it and scrub the archived logs before publishing anything."
    )
    secret_text = "Some prose before the incident.\n" + secret_span + "\nSome prose after."
    secret_near_miss = secret_span.replace("log; ", "log, ").replace("anything.", "anything")
    assert secret_near_miss not in secret_text

    def extractor(text: str) -> list[dict]:
        return [
            {
                "type": "gotcha",
                "title": "leaked token",
                "body": "rotate the leaked CI runner credential",
                "quote": secret_near_miss,
                "tags": [],
            }
        ]

    saved, rejected = extract_and_ingest(
        secret_text, "proj", "test://wpb1/secret", extractor=extractor
    )
    assert (saved, rejected) == (1, 0)

    (item,) = _session_items().values()
    repair = item.meta["quote_repair"]
    assert fake not in repair["model_quote"]
    assert "[REDACTED:github_token]" in repair["model_quote"]
    # The final quote (the located span) is scrubbed as before.
    assert fake not in item.meta["quote"]
    assert "[REDACTED:github_token]" in item.meta["quote"]


def test_ingest_is_idempotent_with_repair_active():
    # CLAUDE.md hard rule 3: same window re-processed → identical uri + identical
    # (deterministically repaired) content → sources (kind, uri, content_hash)
    # dedupe makes the second run a no-op. No duplicate items, no new review rows.
    uri = "test://wpb1/idem"
    s1 = extract_and_ingest(TEXT, "proj", uri, extractor=_extractor)
    assert s1 == (2, 1)
    before = _counts()

    s2 = extract_and_ingest(TEXT, "proj", uri, extractor=_extractor)
    assert s2 == (0, 1), "second run saves nothing (dedupe), re-counts the same reject"
    assert _counts() == before, "no duplicate items/sources/review rows"
