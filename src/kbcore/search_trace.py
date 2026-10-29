"""kbcore.search_trace — opt-in capture of ONE search's PRE-RERANK fused order (E2).

Review #10 (E2-prime, the KB 40402 follow-up): the per-case diff could name a
slipped case but not WHERE its satisfier sat before the cross-encoder reordered
the window — the case-14 displacers and the case-48 candidate-generation miss
were attributable only through an out-of-tree replay that monkeypatched
``rerank.rerank_fused``. This is the in-tree instrument behind
``eval_case_fused_rank`` / ``eval_case_n_fused``: ``kbcore.eval_harness``
calls :func:`begin` before each golden search, ``kbcore.search`` calls
:func:`capture` with the fused candidates just before its rerank step (ONE line
there), and the harness reads the order back with :func:`end`.

The captured order is the reranker's OWN view of the candidates — sorted by
fused score, highest first, stable on ties (``rerank.rerank_fused``'s
``ordered``) — so a position here is the position the cross-encoder window
saw, and with rerank off it is the order ``_to_hits`` re-sorts (bar exact
score ties). Thread-local, like ``kbcore.rerank``'s participation tally:
writer and reader share the eval thread by construction, and a concurrent
server thread's search can neither write into nor read from an eval trace.

INERT unless a trace is active on the calling thread: a production search pays
one attribute read and allocates nothing. Capture never mutates the fused list
and never raises into ``search()`` — a measurement may not change what it
measures (asserted in tests/test_eval_plumbing.py).
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

_trace = threading.local()


def begin() -> None:
    """Arm this thread's trace for the next search; any earlier capture is dropped."""
    _trace.active = True
    _trace.ids = None


def capture(fused: Sequence[Any]) -> None:
    """Record ``fused``'s item ids in score order — a no-op unless a trace is active.

    ``fused`` is ``kbcore.search``'s ``[(item_id, score, snippet), ...]`` as it
    is handed to the rerank step. A malformed row records no order (None)
    rather than raising: telemetry may never fail a search.
    """
    if not getattr(_trace, "active", False):
        return
    try:
        _trace.ids = [int(t[0]) for t in sorted(fused, key=lambda t: -t[1])]
    except Exception:  # noqa: BLE001 — a measurement may never cost the search
        _trace.ids = None


def end() -> list[int] | None:
    """Disarm the trace; the captured ids, or None when no search reached capture."""
    ids = getattr(_trace, "ids", None)
    _trace.active = False
    _trace.ids = None
    return ids
