"""kbworker.digest_recency_lines — the recency guard's quiet digest fact line.

Its own small module because kbworker.digest_lines sits at the 300-line cap.
Rendering only, like every digest line: both numbers come from the run's
counts (``auto_resolve_held`` / ``auto_resolve_held_open``, from
:data:`kbworker.evidence_step._EVIDENCE_STATS`), and a run that does not carry
them — or holds nothing — contributes NO line. Pure ASCII (ntfy to a phone).
The precision gate's line and the contradiction step's ``judged pairs`` line
(with its KB 55589 clauses) live here for the same reason.
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


def judge_held_line(counts: Mapping[str, Any]) -> str | None:
    """``auto-resolve held by the judge: N (X no conflict, Y no verdict; +M tonight)`` —
    the open pairs the precision gate (kbcore.review_judge_gate) kept from being
    retired, by kind; pairs whose no-verdict is still being retried, and an
    unreachable judge, are their own clauses (neither is a hold)."""
    no_conflict = _count(counts, "auto_resolve_judge_held_open")
    no_verdict = _count(counts, "auto_resolve_judge_failed_open")
    tonight = _count(counts, "auto_resolve_judge_held") + _count(
        counts, "auto_resolve_judge_failed"
    )
    retry = _count(counts, "auto_resolve_judge_retry")
    down = _count(counts, "auto_resolve_gate_unavailable")
    parts = []
    if no_conflict or no_verdict or tonight:
        new = f"; +{tonight} tonight" if tonight else ""
        parts.append(
            f"auto-resolve held by the judge: {no_conflict + no_verdict} "
            f"({no_conflict} no conflict, {no_verdict} no verdict{new})"
        )
    if retry:
        parts.append(f"precision gate: {retry} retrying (no verdict yet)")
    if down:
        parts.append(f"precision gate: judge unreachable, {down} deferred")
    return "; ".join(parts) or None


def judged_line(counts: Mapping[str, Any]) -> str | None:
    """``judged pairs: N`` exactly as the digest always printed it (absent when the run
    carries no count), plus ``(T trimmed, V no verdict, R resting)`` when the contradiction
    step fitted oversized prompts, got replies without a verdict, or left out pairs resting
    after an earlier no-verdict (KB 55589) — those rest a week and are then asked again, so
    an R that never clears is a set of pairs worth a look."""
    judged = counts.get("contradiction_pairs_judged")
    if judged is None:
        return None
    clauses = [
        f"{n} {what}"
        for key, what in (
            ("contradiction_pairs_trimmed", "trimmed"),
            ("contradiction_pairs_no_verdict", "no verdict"),
            ("contradiction_pairs_resting", "resting"),
        )
        if (n := _count(counts, key))
    ]
    return f"judged pairs: {judged}" + (f" ({', '.join(clauses)})" if clauses else "")
