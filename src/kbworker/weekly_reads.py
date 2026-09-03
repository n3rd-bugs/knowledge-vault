"""kbworker.weekly_reads — the weekly's Wave H instrument READS (review #8 H9).

Two fail-open, one-query readers split out of :mod:`kbworker.weekly` (which
sat at 300 after they landed): the typed-edge totals — the first caller of
``kbcore.save_nudge.typed_edge_counts``'s recorded contract, widened to
``alternative_to`` — and the calibration percentiles. ``weekly`` re-imports
both names (the established seam). Each returns ``None`` after ONE warning
when its query fails: an instrument may never fail the weekly that carries it,
and ``None`` renders as ``n/a`` (kbworker.weekly_instruments).
"""

from __future__ import annotations

import logging
from typing import Any

from kbcore import save_nudge
from kbcore.db import connect
from kbworker.weekly_instruments import WEEKLY_EDGE_RELS
from kbworker.weekly_render import WINDOW_DAYS

logger = logging.getLogger("kbworker.weekly")


def _typed_edge_totals() -> dict[str, int] | None:
    """:func:`kbcore.save_nudge.typed_edge_counts` over :data:`WEEKLY_EDGE_RELS`
    (H9b) — the checkpoint the weekly persists and diffs, promoted_total-style."""
    try:
        return save_nudge.typed_edge_counts(rels=WEEKLY_EDGE_RELS)
    except Exception:  # noqa: BLE001 — rendered n/a instead
        logger.warning("typed-edge totals unavailable for the weekly", exc_info=True)
        return None


def _calibration_stats(days: int = WINDOW_DAYS) -> dict[str, Any] | None:
    """``{n, p10, p50, p90}`` of the window's calibration top-scores (H9d).

    ONE grouped query over ``subtype='calibration'`` rows that carry a score
    (fallback-mode rows have none) — the thin-floor re-tune's evidence beside
    ``kb_miss_relevance_floor``. Lives here for now because kbcore.search_misses
    is another package's file this wave; it moves there (``calibration_stats``)
    when that module is next open.
    """
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT count(*), "
                "percentile_cont(0.1) WITHIN GROUP (ORDER BY top_score), "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY top_score), "
                "percentile_cont(0.9) WITHIN GROUP (ORDER BY top_score) "
                "FROM search_misses WHERE subtype = 'calibration' AND top_score IS NOT NULL "
                "  AND at >= now() - make_interval(days => %s)",
                (int(days),),
            ).fetchone()
    except Exception:  # noqa: BLE001 — rendered n/a instead
        logger.warning("calibration stats unavailable for the weekly", exc_info=True)
        return None
    n, p10, p50, p90 = row
    return {"n": int(n), "p10": p10, "p50": p50, "p90": p90}
