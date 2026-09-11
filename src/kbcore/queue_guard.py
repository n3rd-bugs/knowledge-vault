"""kbcore.queue_guard — the server-side resend guard's PURE helpers (wave S contract).

The scope predicate and the chunk key are fixed HERE first so the enqueue guard
(:mod:`kbcore.queue_enqueue`), the owner repair (:mod:`kbcore.resend_repair`) and
the fleet telemetry (:mod:`kbcore.fleet_flush`) agree byte for byte; the guard's
DB probes (find_prior, the extracted-anywhere and same-principal hash-xsession
checks, the ledger upsert) are added beside these by wave S item S2. Root cause
KB 33783; design KB 33784 (proposal_v2 Appendix F).
"""

from __future__ import annotations

import hashlib

# Bump ONLY with a repair that re-keys every stored row: the key is compared, never parsed.
CHUNK_KEY_VERSION = "session-chunk/1"


def is_transcript_chunk(kind: str, uri: str | None) -> bool:
    """The ONLY rows the guard touches: session chunks keyed ``session:<sid>``.

    ``uri`` is the value AFTER :func:`kbcore.queue_groups.normalise_source_uri`
    (a degenerate ``'session:'`` is already ``None`` -> False). ``doc://`` uris
    (project-qualified by construction), the local flush's ``<path>:<off>-<new>``
    uris and ``kind='memory'`` are excluded by this predicate, never by a list.
    """
    return kind == "session" and uri is not None and uri.startswith("session:")


def chunk_key(uri: str, content_hash: str) -> str:
    """Deterministic identity of ONE transcript chunk across projects and hook vintages.

    ``content_hash`` is the sha256 of the SCRUBBED text (ingest_queue keeps hashing
    that way), so two boxes redacting the same secret still agree on the key.
    """
    return hashlib.sha256(f"{CHUNK_KEY_VERSION}|{uri}|{content_hash}".encode()).hexdigest()
