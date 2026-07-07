"""kbcore.compress — compressed transcript submission (large-context flushing).

Session-transcript JSONL compresses ~10x with gzip, so the flush hooks can send
``transcript_gz`` (base64-encoded gzip) instead of plaintext — a 1 GB session
becomes ~100 MB on the wire. Decompression happens here, at the trust boundary,
with hard guards: a compressed-size cap and a streamed decompression limit so a
zip bomb on the public endpoint cannot balloon memory (SEC).
"""

from __future__ import annotations

import base64
import binascii
import zlib

from kbcore.errors import InvalidOperation

# A row stores <=1 MB plaintext; gzip'd input beyond ~2 MB is either not a
# transcript chunk or not honest. Decompressed output is hard-capped just above
# the row limit — anything bigger is rejected mid-stream, never materialized.
MAX_COMPRESSED_BYTES = 2_000_000
MAX_DECOMPRESSED_BYTES = 1_100_000


def decode_transcript_gz(transcript_gz: str) -> str:
    """base64(gzip(text)) -> text, with size guards at every stage."""
    if len(transcript_gz) > MAX_COMPRESSED_BYTES * 4 // 3 + 8:
        raise InvalidOperation("compressed transcript exceeds the size limit")
    try:
        raw = base64.b64decode(transcript_gz, validate=True)
    except (binascii.Error, ValueError) as e:
        raise InvalidOperation(f"transcript_gz is not valid base64: {e}") from e
    if len(raw) > MAX_COMPRESSED_BYTES:
        raise InvalidOperation("compressed transcript exceeds the size limit")
    # wbits=47 accepts both gzip and zlib framing. Stream with max_length so a
    # zip bomb is rejected the moment it crosses the cap, not after expansion.
    d = zlib.decompressobj(wbits=47)
    try:
        out = d.decompress(raw, MAX_DECOMPRESSED_BYTES + 1)
    except zlib.error as e:
        raise InvalidOperation(f"transcript_gz is not valid gzip: {e}") from e
    if len(out) > MAX_DECOMPRESSED_BYTES or d.unconsumed_tail:
        raise InvalidOperation(
            f"decompressed transcript exceeds the {MAX_DECOMPRESSED_BYTES}-byte row limit"
        )
    return out.decode("utf-8", errors="replace")


def resolve_transcript(transcript: str | None, transcript_gz: str | None) -> str:
    """The submitted text from exactly one of the two forms."""
    if transcript_gz:
        if transcript:
            raise InvalidOperation("pass transcript OR transcript_gz, not both")
        return decode_transcript_gz(transcript_gz)
    if not transcript:
        raise InvalidOperation("a transcript (or transcript_gz) is required")
    return transcript
