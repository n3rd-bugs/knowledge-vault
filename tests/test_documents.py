"""kbcore.documents — raw documents a session submits, and anchors into them.

The point of this feature is that raw material survives and stays citable, so
the tests are about what would make that false: a document that is not really
retained, a chunked upload that assembles wrong, an anchor that points at
nothing, and an upload id that escapes its directory.
"""

from __future__ import annotations

import hashlib

import pytest

from kbcore import documents, scope
from kbcore.errors import InvalidOperation, KBError, NotFound
from kbcore.ingest import ingest


@pytest.fixture()
def as_owner():
    with scope.use_principal(scope.OWNER):
        yield


# ------------------------------------------------------------------ retention


def test_an_inline_document_is_retained_byte_for_byte(kb, as_owner):
    raw = b"line one\nline two\n\xc3\xa9 unicode\n" * 40
    doc = documents.put_document("proj", "spec.md", content=raw)

    assert doc.bytes == len(raw)
    assert doc.sha256 == hashlib.sha256(raw).hexdigest()
    assert doc.raw_path, "no raw blob was archived"
    # The whole point: what comes back is what went in.
    assert documents.read_raw(doc.source_id) == raw


def test_the_registration_item_is_a_card_not_the_document(kb, as_owner):
    """A 100 MB body would bloat every index for content nobody searches by
    accident; the archive holds the document, the item describes it."""
    raw = b"x" * 50_000 + b"\nTAIL-MARKER\n"
    doc = documents.put_document("proj", "big.log", content=raw)

    from kbcore.items import get_item

    item = get_item(doc.item_id)
    assert len(item.body) < 10_000, "the item body duplicated the document"
    assert "big.log" in item.title
    assert "TAIL-MARKER" not in item.body, "the item body is not bounded"
    assert item.meta["document"]["sha256"] == doc.sha256
    # ...and the tail is still retrievable from the archive.
    assert b"TAIL-MARKER" in documents.read_raw(doc.source_id)


def test_a_document_is_replayable_by_reprocess(kb, as_owner):
    """Retention is only meaningful if a better pipeline can be run over it
    later, which is what the whole feature is for."""
    from kbcore import reprocess

    raw = b"the DRV8706 needs SPI mode 1; mode 0 reads zeros\n" * 50
    doc = documents.put_document("proj", "notes.txt", content=raw)
    ids = {
        r["source_id"] if isinstance(r, dict) else r.source_id
        for r in reprocess.reprocessable_sources(limit=500)
    }
    assert doc.source_id in ids, "a submitted document is not replayable"


# ------------------------------------------------------------------- chunking


def test_a_large_document_assembles_from_chunks_exactly(kb, as_owner):
    raw = bytes((i * 7) % 251 for i in range(300_000))
    up = "upload-abc"
    off = 0
    for i in range(0, len(raw), 40_000):
        piece = raw[i : i + 40_000]
        off = documents.stage_chunk(up, piece, offset=off)
    assert off == len(raw)

    doc = documents.put_document("proj", "dataset.bin", upload_id=up)
    assert doc.bytes == len(raw)
    assert documents.read_raw(doc.source_id) == raw


def test_a_chunk_at_the_wrong_offset_is_refused(kb, as_owner):
    """A gap must be an error, not a silently corrupt document."""
    documents.stage_chunk("upload-gap", b"aaaa", offset=0)
    with pytest.raises(InvalidOperation):
        documents.stage_chunk("upload-gap", b"bbbb", offset=999)


def test_a_retried_chunk_is_idempotent(kb, as_owner):
    documents.stage_chunk("upload-retry", b"aaaa", offset=0)
    documents.stage_chunk("upload-retry", b"bbbb", offset=4)
    # The network dropped the ack; the client resends the same chunk.
    assert documents.stage_chunk("upload-retry", b"bbbb", offset=4) == 8
    doc = documents.put_document("proj", "r.bin", upload_id="upload-retry")
    assert documents.read_raw(doc.source_id) == b"aaaabbbb"


def test_the_staging_id_cannot_escape_its_directory(kb, as_owner):
    for evil in ("../../etc/passwd", "..", "a/b", "", "x" * 200, "/abs"):
        with pytest.raises(InvalidOperation):
            documents.stage_chunk(evil, b"x", offset=0)


def test_an_oversized_inline_document_is_refused_with_the_alternative(kb, as_owner):
    with pytest.raises(InvalidOperation) as exc:
        documents.put_document("proj", "huge", content=b"x" * (documents.MAX_INLINE_BYTES + 1))
    assert "upload_id" in str(exc.value), "the refusal does not say how to send it instead"


# --------------------------------------------------------------------- anchors


def test_a_summary_can_anchor_into_a_region_of_a_document(kb, as_owner):
    raw = b"HEADER\n" + b"the interesting part is here\n" + b"tail\n" * 100
    doc = documents.put_document("proj", "log.txt", content=raw)
    start, end = 7, 7 + len(b"the interesting part is here\n")

    item_id = ingest(
        "proj",
        "gotcha",
        "Anchored finding",
        "Derived from a specific region of log.txt.",
        raw_ref={"source_id": doc.source_id, "start": start, "end": end, "line_start": 2},
    )
    from kbcore.items import get_item

    ref = get_item(item_id).meta["raw_ref"]
    assert ref["source_id"] == doc.source_id
    assert (ref["start"], ref["end"]) == (start, end)
    assert ref["line_start"] == 2
    # Following the citation returns exactly the cited bytes — which is what
    # makes the anchor evidence rather than decoration.
    assert documents.read_raw(doc.source_id, start=ref["start"], end=ref["end"]) == raw[start:end]


def test_an_anchor_that_points_at_nothing_is_refused(kb, as_owner):
    """An unverifiable anchor carries the authority of provenance without the
    substance, so it is rejected at ingest rather than found broken later."""
    raw = b"short document\n"
    doc = documents.put_document("proj", "s.txt", content=raw)

    for bad in (
        {"source_id": 999_999, "start": 0, "end": 5},  # no such source
        {"source_id": doc.source_id, "start": 0, "end": 10_000},  # past the end
        {"source_id": doc.source_id, "start": 10_000},  # starts past the end
        {"source_id": doc.source_id, "start": 5, "end": 2},  # inverted
        {"source_id": doc.source_id, "start": -1},  # negative
        {"start": 0, "end": 1},  # no source at all
    ):
        with pytest.raises((InvalidOperation, NotFound, KBError)):
            ingest("proj", "note", f"bad {bad}", "body", raw_ref=bad)


def test_an_anchor_cannot_read_across_a_project_wall(kb, as_owner):
    """The anchor must not become a way to reach another project's raw data."""
    doc = documents.put_document("secret-proj", "confidential.txt", content=b"top secret\n")
    outsider = scope.Principal(
        name="grantee", read_projects=frozenset({"other"}), write_projects=frozenset({"other"})
    )
    with scope.use_principal(outsider), pytest.raises((NotFound, KBError)):
        documents.read_raw(doc.source_id)


def test_reading_a_region_needs_the_source_to_have_raw(kb, as_owner):
    item_id = ingest("proj", "note", "plain", "no raw content behind this one")
    assert item_id is not None
    with pytest.raises(NotFound):
        documents.read_raw(999_999)


def test_describe_raw_reports_size_and_digest_without_transferring(kb, as_owner):
    raw = b"a\nb\nc\n"
    doc = documents.put_document("proj", "tiny.txt", content=raw)
    info = documents.describe_raw(doc.source_id)
    assert info["bytes"] == len(raw)
    assert info["lines"] == 3
    assert info["sha256"] == hashlib.sha256(raw).hexdigest()
    assert info["uri"] == doc.uri


# ------------------------------------------------------------------ authority


def test_writing_a_document_needs_write_access_to_the_project(kb, as_owner):
    reader = scope.Principal(name="reader", read_projects=frozenset({"proj"}))
    with scope.use_principal(reader), pytest.raises(KBError):
        documents.put_document("proj", "nope.txt", content=b"x")
