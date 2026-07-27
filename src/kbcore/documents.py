"""kbcore.documents — raw documents a session submits for reference.

A session can already hand over its TRANSCRIPT. This is the other half: the
artefacts a session produced or read — a spec it wrote, a config it debugged, a
log or dataset it pulled from the workspace — kept in raw form so that

  * a summary can point AT a region of one (``raw_ref`` on the item's meta),
  * and the whole thing can be replayed through a better pipeline later
    (``kb reprocess`` already does this for every source that has a raw blob).

Design, following the owner's decision (2026-07-27):

* ARCHIVE ALWAYS, EXTRACT ON REQUEST. Submitting a document is cheap and
  GPU-free: the bytes are archived, a source row records them, and one
  registration item makes the document addressable. Mining it into knowledge
  items happens only when the caller asks (``extract=True``), so a dataset or a
  dump costs nothing and never fills the review queue.
* SMALL INLINE, LARGE CHUNKED. A document under the row cap arrives in one
  call. A larger one is staged on disk chunk by chunk and finalised in one
  step, reusing the offset discipline the transcript flush already uses —
  nothing is held in memory but the current chunk.

The registration ITEM is a card, not the document: name, size, digest and a
bounded preview. The document itself lives in the raw archive, which is what
``raw_ref`` addresses and what a future reprocess reads. Putting a 100 MB body
into an item would bloat every search index for content nobody searches by
accident.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import zstandard

from kbcore import scope
from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation, NotFound

log = logging.getLogger("kbcore.documents")

# Matches the per-row content cap the ingest queue enforces, so a document that
# arrives inline can always be stored as one unit.
MAX_INLINE_BYTES = 900_000
# A staged upload that nobody finalises is abandoned scratch, not data.
STAGING_TTL_HOURS = 24
# How much of the document goes into the registration item's body. Enough to
# recognise it in a search result; never enough to duplicate the archive.
PREVIEW_BYTES = 2_000

_UPLOAD_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_NAME_RE = re.compile(r"\A[^\x00-\x1f]{1,200}\Z")


@dataclass(frozen=True)
class StoredDocument:
    """What the caller gets back: the addresses the document now has."""

    item_id: int | None
    source_id: int
    uri: str
    bytes: int
    sha256: str
    raw_path: str
    extracted: bool


def _staging_dir() -> Path:
    d = settings.raw / ".uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _staging_path(upload_id: str) -> Path:
    """Resolve a staging file, refusing anything that escapes the directory.

    ``upload_id`` comes from a caller, so it is validated against a strict
    pattern AND the resolved path is confirmed to sit inside the staging dir —
    a name like ``../../etc/passwd`` must never become a write target.
    """
    if not _UPLOAD_ID_RE.match(upload_id or ""):
        raise InvalidOperation("upload_id must be 1-64 chars of [A-Za-z0-9._-]")
    d = _staging_dir()
    path = (d / f"{upload_id}.part").resolve()
    if path.parent != d.resolve():
        raise InvalidOperation("invalid upload_id")
    return path


def stage_chunk(upload_id: str, data: bytes, *, offset: int) -> int:
    """Append one chunk of a large document. Returns the new staged size.

    The offset is the caller's assertion of where this chunk belongs, and it
    must match what has actually been staged. That makes a retried chunk a
    no-op instead of a duplicate, and a missing chunk an error instead of a
    silently corrupt document — the same guarantee the transcript flush gets
    from its per-session offset file.
    """
    path = _staging_path(upload_id)
    have = path.stat().st_size if path.exists() else 0
    if offset != have:
        if offset < have and offset + len(data) <= have:
            return have  # already staged: an idempotent retry, not an error
        raise InvalidOperation(
            f"chunk offset {offset} does not continue the upload (staged {have} bytes)"
        )
    with path.open("ab") as fh:
        fh.write(data)
    return path.stat().st_size


def discard_upload(upload_id: str) -> None:
    _staging_path(upload_id).unlink(missing_ok=True)


def sweep_staging(older_than_hours: int = STAGING_TTL_HOURS) -> int:
    """Remove abandoned uploads. Returns how many went."""
    import time

    cutoff = time.time() - older_than_hours * 3600
    n = 0
    for p in _staging_dir().glob("*.part"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:  # a concurrent finalise removed it; not an error
            continue
    return n


def _preview(raw: bytes) -> str:
    text = raw[:PREVIEW_BYTES].decode("utf-8", errors="replace")
    # Never cut mid-line in the preview: a half line reads as corruption.
    if len(raw) > PREVIEW_BYTES and "\n" in text:
        text = text.rsplit("\n", 1)[0]
    return text


def put_document(
    project: str,
    name: str,
    *,
    content: bytes | None = None,
    upload_id: str | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    extract: bool = False,
) -> StoredDocument:
    """Store a raw document and make it addressable.

    Either ``content`` (inline, up to :data:`MAX_INLINE_BYTES`) or ``upload_id``
    (a previously staged upload of any size). Writing is gated by the caller's
    own authority — a document is knowledge material, so the same project write
    rule applies as to anything else.
    """
    scope.assert_writable(project)
    if not _NAME_RE.match(name or ""):
        raise InvalidOperation("name must be 1-200 characters and contain no control characters")
    if (content is None) == (upload_id is None):
        raise InvalidOperation("pass exactly one of content or upload_id")

    if content is not None:
        if len(content) > MAX_INLINE_BYTES:
            raise InvalidOperation(
                f"document is {len(content)} bytes; inline is capped at {MAX_INLINE_BYTES}. "
                "Stage it in chunks with upload_id instead."
            )
        raw = content
        staged: Path | None = None
    else:
        staged = _staging_path(str(upload_id))
        if not staged.exists():
            raise NotFound("no staged upload with that id")
        raw = staged.read_bytes()
    if not raw:
        raise InvalidOperation("document is empty")

    digest = hashlib.sha256(raw).hexdigest()
    uri = f"doc://{project}/{name}"

    # The registration item is a CARD: what this document is, not what it says.
    # The bytes live in the archive, which is what raw_ref addresses.
    lines = raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)
    body_parts = [
        f"Raw document `{name}` submitted for reference.",
        f"{len(raw):,} bytes, {lines:,} lines, sha256 {digest}.",
    ]
    if note:
        body_parts.append(note.strip())
    body_parts.append("\nPreview (first bytes; the full document is in the raw archive):\n")
    body_parts.append(_preview(raw))
    body = "\n".join(body_parts)

    from kbcore.ingest import ingest

    item_id = ingest(
        project,
        "note",
        f"doc: {name}",
        body,
        kind="file",
        uri=uri,
        trust="verified",  # the submission itself is a fact, not an extracted claim
        tags=list(tags or []),
        meta={"document": {"name": name, "bytes": len(raw), "sha256": digest, "lines": lines}},
        raw_content=raw,  # <- archived zstd under KB_RAW by ingest_in
        is_source=True,
    )

    with connect() as conn:
        row = conn.execute(
            "SELECT id, raw_path FROM sources WHERE kind='file' AND uri=%s "
            "ORDER BY id DESC LIMIT 1",
            (uri,),
        ).fetchone()
    if row is None:  # pragma: no cover - ingest always records a source
        raise InvalidOperation("document was stored but no source row was recorded")
    source_id, raw_path = int(row[0]), row[1]

    if staged is not None:
        staged.unlink(missing_ok=True)

    extracted = False
    if extract:
        extracted = _extract_document(project, uri, raw)

    log.info(
        "document stored: %s (%d bytes, source=%s, extract=%s)", uri, len(raw), source_id, extract
    )
    return StoredDocument(
        item_id=item_id,
        source_id=source_id,
        uri=uri,
        bytes=len(raw),
        sha256=digest,
        raw_path=raw_path or "",
        extracted=extracted,
    )


def _extract_document(project: str, uri: str, raw: bytes) -> bool:
    """Queue the document for the same drain that handles session transcripts.

    Extraction is never done inline: it is 14B work (Invariant 8), and the
    caller of a document upload is waiting on an HTTP request.
    """
    from kbcore import ingest_queue

    text = raw.decode("utf-8", errors="replace")
    res = ingest_queue.enqueue(project, text, kind="session", uri=uri)
    return res.id is not None


# --------------------------------------------------------------------- reading


def read_raw(source_id: int, *, start: int = 0, end: int | None = None) -> bytes:
    """Read a byte region of a source's archived raw content.

    This is what makes ``raw_ref`` anchors worth recording: a summary can say
    which bytes it came from, and this returns exactly those bytes so the claim
    can be checked against the original rather than trusted.

    Readability is enforced by the caller's project scope, so an anchor can
    never be used to read across a project wall.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT project, raw_path FROM sources WHERE id = %s", (source_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"no source {source_id}")
    project, raw_path = row[0], row[1]
    scope.assert_readable(project, f"no source {source_id}")
    if not raw_path:
        raise NotFound(f"source {source_id} has no archived raw content")
    data = _read_blob(Path(raw_path))

    if start < 0 or (end is not None and end < start):
        raise InvalidOperation("invalid range")
    return data[start:end] if end is not None else data[start:]


def _read_blob(path: Path) -> bytes:
    if not path.exists():
        raise NotFound("the archived raw content is missing from the raw store")
    blob = path.read_bytes()
    if path.suffix == ".zst":
        return zstandard.ZstdDecompressor().decompress(blob, max_output_size=1 << 31)
    return blob


def describe_raw(source_id: int) -> dict[str, Any]:
    """Size and digest of a source's raw content, without transferring it."""
    with connect() as conn:
        row = conn.execute(
            "SELECT project, raw_path, uri, kind, ingested_at FROM sources WHERE id = %s",
            (source_id,),
        ).fetchone()
    if row is None:
        raise NotFound(f"no source {source_id}")
    scope.assert_readable(row[0], f"no source {source_id}")
    if not row[1]:
        raise NotFound(f"source {source_id} has no archived raw content")
    data = _read_blob(Path(row[1]))
    return {
        "source_id": source_id,
        "project": row[0],
        "uri": row[2],
        "kind": row[3],
        "bytes": len(data),
        "lines": data.count(b"\n") + (0 if data.endswith(b"\n") else 1),
        "sha256": hashlib.sha256(data).hexdigest(),
        "ingested_at": row[4].isoformat() if row[4] else None,
    }
