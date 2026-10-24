"""kbworker.digest_recency_lines — the recency guard's quiet digest fact line.

Its own small module because kbworker.digest_lines sits at the 300-line cap.
Rendering only, like every digest line: both numbers come from the run's
counts (``auto_resolve_held`` / ``auto_resolve_held_open``, from
:data:`kbworker.evidence_step._EVIDENCE_STATS`), and a run that does not carry
them — or holds nothing — contributes NO line. Pure ASCII (ntfy to a phone).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _count(counts: Mapping[str, Any], key: str) -> int:
    value = counts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def auto_resolve_held_line(counts: Mapping[str, Any]) -> str | None:
    """``auto-resolve held: N (would retire the newer item)``, with ``+M tonight``
    when this run held new pairs — the open contradiction pairs awaiting the
    owner because evidence alone would have retired the NEWER observation."""
    held_open = _count(counts, "auto_resolve_held_open")
    tonight = _count(counts, "auto_resolve_held")
    if not held_open and not tonight:
        return None
    total = max(held_open, tonight)
    new = f"+{tonight} tonight; " if tonight else ""
    return f"auto-resolve held: {total} ({new}would retire the newer item)"
