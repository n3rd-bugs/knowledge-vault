"""kbcore.contradiction_rejudge — owner re-judge of "no conflict" verdicts given blind (KB 55589).

Until the pair-judge prompt was fitted to the window (:mod:`kbcore.judge_fit`), a big pair
was judged blind two ways — a prompt over num_ctx lost its system prompt to Ollama's
truncation, and a prompt that fit could leave the seat's hidden thinking no room, so the
reply came back EMPTY (done_reason=length) — and either reply, read as False, was memorized
in ``contradiction_judgements``: the nightly never asks about that pair again at the
current embed model. Live on 2026-10-27: 159 of 8,231 judged pairs over the 8,904-token
ceiling (63 over 16,383 cl100k, certainly truncated), 19 of them memorized "no conflict"
with both items still active — all rm3 / rm3-simulation docs.

:func:`candidates` lists the memorized "no conflict" pairs checked before
:data:`~kbcore.judge_fit.FIT_EPOCH` (both items still active, not already recorded as a
contradiction) whose unfitted prompt was over :data:`~kbcore.judge_fit.LEGACY_CEILING`;
:func:`rejudge` asks the contradiction seat again with the fitted request —
:func:`kbcore.llm.items_contradict`, the nightly's own call — and queues a "yes" exactly
as the nightly does (:func:`record_contradiction`, idempotent, plus the ``judged_by``
stamp); from there it is an ordinary contradiction row, which the nightly evidence step
may auto-resolve after a stable two-night suggestion (the precision gate, when on, asks
this same fitted request and seed, so it confirms rather than re-checks — the owner keeps
a pair by dismissing or resolving its row first). The memorized row is never updated or
deleted (Invariant 5, migration 017): it stays the exclusion key, and a conflict found
lives on as its review row. Memorized TRUE verdicts are out of scope: they are already
queued (the one oversized one on 2026-10-27 was resolved long ago).

Owner-only (the pairs span projects) and GPU work (Invariant 8): an explicit owner run on
an idle slot. The consolidation lock is probed try-and-release before EVERY pair — never
held, or a nightly starting meanwhile would skip its whole night — and the run stops
cleanly once a consolidation run has started (it shares the slot for one pair at most:
up to two calls with :func:`kbcore.llm.items_contradict`'s re-ask).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from kbcore import judge_fit, llm, scope
from kbcore.config import settings
from kbcore.consolidation import item_pair_texts, record_contradiction
from kbcore.db import connect
from kbcore.errors import ExtractorUnavailable, InvalidOperation
from kbcore.review_judge_gate import per_pair_failure, stamp_judged_by

logger = logging.getLogger("kbcore.contradiction_rejudge")

# A pair that may still be re-judged: both items active, no 'contradicts' edge yet either way.
_OPEN_PAIR = """
    ia.status = 'active' AND ib.status = 'active'
    AND NOT EXISTS (
        SELECT 1 FROM relations r
        WHERE r.rel = 'contradicts' AND r.src_kind = 'item' AND r.dst_kind = 'item'
          AND ((r.src_id = ia.id AND r.dst_id = ib.id) OR (r.src_id = ib.id AND r.dst_id = ia.id))
    )
"""


@dataclass(frozen=True)
class Rejudge:
    """One memorized pair; ``judged``/``verdict``/``queued``/``error`` are set by
    :func:`rejudge` (``error`` names why the run stopped at this pair, or skipped it)."""

    a_id: int
    b_id: int
    project: str
    tokens: int  # the UNFITTED prompt, cl100k
    a_title: str
    b_title: str
    judged: bool = False
    verdict: bool | None = None
    queued: bool = False
    error: str = ""


def candidates() -> list[Rejudge]:
    """READ-ONLY: the memorized "no conflict" pairs a blind reply may have produced (module
    doc), lower id first. The byte prefilter (half the ceiling) is safe: a cl100k token is
    never shorter than one byte."""
    scope.require_owner("re-judging oversized contradiction pairs")
    ceiling = judge_fit.LEGACY_CEILING
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT cj.a_id, cj.b_id, ia.project, ia.title, ia.body, ib.title, ib.body
            FROM contradiction_judgements cj
            JOIN items ia ON ia.id = cj.a_id
            JOIN items ib ON ib.id = cj.b_id
            WHERE cj.embed_model = %s AND cj.verdict = false AND cj.checked_at < %s
              AND octet_length(ia.title) + octet_length(ia.body)
                + octet_length(ib.title) + octet_length(ib.body) > %s
              AND {_OPEN_PAIR}
            ORDER BY cj.a_id, cj.b_id
            """,
            (settings.kb_embed_model, judge_fit.FIT_EPOCH, ceiling // 2),
        ).fetchall()
    out: list[Rejudge] = []
    for a_id, b_id, project, a_title, a_body, b_title, b_body in rows:
        tokens = judge_fit.contradict_tokens(a_title, a_body, b_title, b_body)
        if tokens > ceiling:
            out.append(Rejudge(a_id, b_id, project, tokens, a_title, b_title))
    return out


def _still_open(a_id: int, b_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM items ia, items ib WHERE ia.id = %s AND ib.id = %s AND {_OPEN_PAIR}",
            (a_id, b_id),
        ).fetchone()
    return row is not None


def rejudge(
    pairs: Sequence[Rejudge], *, on_progress: Callable[[Rejudge], None] | None = None
) -> list[Rejudge]:
    """Ask the seat again, fitted, for each pair; queue each "yes" as the nightly does.

    Returns EVERY pair, so the owner always gets the table of what was done, and reports
    every pair it reaches through ``on_progress``: judged ones carry their verdict (None =
    the reply had none; reported, nothing written). A failure about ONE pair (a read
    timeout, an unusable reply — the gate's
    :func:`~kbcore.review_judge_gate.per_pair_failure`) is named on that pair and the run
    goes on — but two in a row mean the slot is wedged or contended, and stop it, as they
    trip the gate's pass breaker. An outage, a consolidation run taking the lock, or any
    other error stops it too, the reason named on the pair it hit and the rest left
    unjudged; only a failure of the lock probe before the first pair refuses the run
    outright (nothing has been judged or written yet). A pair closed meanwhile (an item
    retired, or the conflict recorded) is skipped. A re-run asks the remaining pairs
    again: same request and seeds, so the same answers.
    """
    scope.require_owner("re-judging oversized contradiction pairs")
    from kbcore.bench_judge import assert_gpu_slot_free  # the bench's try-and-release probe

    out: list[Rejudge] = []
    stop = ""
    failed_in_a_row = 0
    for n, pair in enumerate(pairs):
        if stop:
            out.append(pair)
            continue
        try:
            assert_gpu_slot_free()  # every pair: a nightly that starts waits on one pair
        except Exception as e:  # noqa: BLE001 — a held lock (InvalidOperation) or a dead DB
            if n == 0:
                raise  # nothing judged yet: refuse the run outright
            stop = f"stopped: {e}" if isinstance(e, InvalidOperation) else _named(e)
            done = replace(pair, error=stop)
        else:
            try:
                done = _rejudge_one(pair)
            except ExtractorUnavailable as e:
                failed_in_a_row += 1
                if per_pair_failure(e) and failed_in_a_row < 2:
                    done = replace(pair, error=f"judge failed on this pair: {e.__cause__!r}")
                else:
                    stop = f"stopped: judge unavailable: {e}"
                    done = replace(pair, error=stop)
            except Exception as e:  # noqa: BLE001 — keep the table; name what stopped the run
                logger.exception("re-judging pair %s/%s failed", pair.a_id, pair.b_id)
                stop = _named(e)
                done = replace(pair, error=stop)
            else:
                if done.verdict is not None:  # only a real answer clears it; a skip or a
                    failed_in_a_row = 0  # blank reply proves nothing about the slot
        out.append(done)
        if on_progress is not None:
            on_progress(done)
    return out


def _named(e: BaseException) -> str:
    return f"stopped: {e.__class__.__name__}: {e}"


def _rejudge_one(pair: Rejudge) -> Rejudge:
    if not _still_open(pair.a_id, pair.b_id):
        return replace(pair, error="closed meanwhile (skipped)")
    (a_title, a_body), (b_title, b_body) = item_pair_texts(pair.a_id, pair.b_id)
    verdict = llm.items_contradict(a_title, a_body, b_title, b_body)
    queued = bool(verdict) and record_contradiction(pair.a_id, pair.b_id)
    if queued:
        try:  # provenance (which judge queued it); bookkeeping, never a gate — as the nightly
            stamp_judged_by(pair.a_id, pair.b_id)
        except Exception:  # noqa: BLE001
            logger.warning("stamping the judge on pair %s/%s failed", pair.a_id, pair.b_id)
    return replace(pair, judged=True, verdict=verdict, queued=queued)
