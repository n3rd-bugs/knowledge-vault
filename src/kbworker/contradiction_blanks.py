"""kbworker.contradiction_blanks — the contradiction step's memory of pairs that gave NO verdict.

A reply without a verdict is never memorized as an answer (KB 55589), and its request and
seeds are deterministic, so a pair that blanks tonight blanks again tomorrow at the same
rank (the candidate query is sim DESC). Left alone, a block of such pairs at the head of the
queue would take the first slots of every night — and, being most of the first pairs judged,
trip the no-verdict breaker so that nothing else is judged at all (round-2 review). This
module remembers them: a pair that blanked is SKIPPED for :data:`RETRY_DAYS` (asked again
after that, in case the seat or the prompt changed — after every fresh pair, a few per run),
it never counts toward the breaker, and a pair that answers is cleared.

THE STAMP IS A NON-KNOWLEDGE MARKER in ``KB_STATE_DIR`` (summary_stamps' home and pattern):
``{"<a_id>\\t<b_id>": {"at", "nights", "embed_model", "seat"}}``. A stamp counts only for the
embed model and seat that wrote it — a re-embed or a new seat makes every pair fresh again.
Fail-open both ways: an unreadable or unwritable marker logs and never fails the step; the
cost of losing it is one night of the old order. Invariant 5 is untouched: no knowledge row
is written, flipped or deleted here, and the file holds only ids, models and timestamps.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kbcore.config import settings

logger = logging.getLogger("kbworker.consolidate")

BLANKS_FILE = "contradiction-blanks.json"
RETRY_DAYS = 7  # a blank pair rests a week (a re-embed or a new seat frees it at once)
# A stamp is written mid-run and read at the start of a later run: the slack keeps "a week"
# at seven nightly runs, not eight.
_REST = timedelta(days=RETRY_DAYS) - timedelta(hours=12)
#: Due retries one run may spend (after every fresh pair): a seat that stays broken under
#: the same name must not grow its nightly bill week by week (round-3 review).
RETRIES_PER_RUN = 5
# Every retry refreshes a live blank's stamp, so one this old belongs to a pair that is no
# longer offered (an item retired, the pair answered elsewhere) — or to another embed model
# or seat — and is dropped on the next write: the file stays bounded.
PRUNE_DAYS = 4 * RETRY_DAYS

Stamps = dict[str, dict[str, Any]]


def stamp_path() -> Path:
    """``$KB_STATE_DIR/contradiction-blanks.json`` (tests point KB_STATE_DIR at tmp_path)."""
    return Path(settings.kb_state_dir).expanduser() / BLANKS_FILE


def _key(a_id: int, b_id: int) -> str:
    lo, hi = (a_id, b_id) if a_id <= b_id else (b_id, a_id)
    return f"{lo}\t{hi}"


def _ours(entry: Any) -> bool:
    """A stamp written under the current embed model and contradiction seat."""
    return (
        isinstance(entry, dict)
        and entry.get("embed_model") == settings.kb_embed_model
        and entry.get("seat") == settings.contradiction_judge_model
    )


def load() -> Stamps:
    """The stamps on disk, or ``{}`` — a missing or corrupt file is not a failure."""
    try:
        data = json.loads(stamp_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("contradiction blanks at %s unreadable; every pair is fresh", stamp_path())
        return {}
    return data if isinstance(data, dict) else {}


def _at(entry: Any) -> datetime | None:
    """The stamp's aware timestamp, or None for anything unreadable (fail-open) — including
    a time in the future (a stamp written by a clock that ran fast must not park a pair)."""
    try:
        at = datetime.fromisoformat(str(entry.get("at")))
    except (AttributeError, TypeError, ValueError):
        return None
    if at.tzinfo is None or at > datetime.now(UTC) + timedelta(hours=1):
        return None
    return at


def _save(stamps: Stamps) -> None:
    horizon = datetime.now(UTC) - timedelta(days=PRUNE_DAYS)
    for key in [k for k, e in stamps.items() if not _ours(e) or (_at(e) or horizon) <= horizon]:
        del stamps[key]
    path = stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(stamps, sort_keys=True), encoding="utf-8")
        tmp.replace(path)  # atomic on POSIX, like the pause marker
    except OSError:
        logger.warning("contradiction blanks at %s not writable; the pair is simply re-asked", path)


def known(stamps: Stamps, a_id: int, b_id: int) -> bool:
    """True when this pair already blanked under the current embed model and seat."""
    return _ours(stamps.get(_key(a_id, b_id)))


def resting(stamps: Stamps, a_id: int, b_id: int, now: datetime | None = None) -> bool:
    """True while a known blank is inside its :data:`RETRY_DAYS` rest: skip it this run."""
    entry = stamps.get(_key(a_id, b_id))
    at = _at(entry) if _ours(entry) else None
    return at is not None and (now or datetime.now(UTC)) - at < _REST


def select(
    stamps: Stamps, pairs: list[tuple[int, int, float]], cap: int | None
) -> tuple[list[tuple[int, int, float]], int]:
    """``(pairs to judge, how many rest)`` from the step's candidates: resting pairs are left
    out; every FRESH pair comes first, in the query's order, so the breaker sees fresh
    replies before any retry; then at most :data:`RETRIES_PER_RUN` due retries; all within
    ``cap``. The caller over-fetches by ``len(stamps)``, so resting pairs never cost a slot."""
    due = [p for p in pairs if not resting(stamps, p[0], p[1])]
    fresh = [p for p in due if not known(stamps, p[0], p[1])]
    retries = [p for p in due if known(stamps, p[0], p[1])][:RETRIES_PER_RUN]
    return (fresh + retries)[:cap], len(pairs) - len(due)


def record(stamps: Stamps, a_id: int, b_id: int) -> None:
    """Remember a reply without a verdict for this pair (one more night of it)."""
    prior = stamps.get(_key(a_id, b_id))
    try:
        nights = int(prior.get("nights", 0)) + 1 if _ours(prior) else 1
    except (OverflowError, TypeError, ValueError):
        nights = 1
    stamps[_key(a_id, b_id)] = {
        "at": datetime.now(UTC).isoformat(),
        "nights": nights,
        "embed_model": settings.kb_embed_model,
        "seat": settings.contradiction_judge_model,
    }
    _save(stamps)


def clear(stamps: Stamps, a_id: int, b_id: int) -> None:
    """Forget a pair once the seat gave it a real verdict."""
    if stamps.pop(_key(a_id, b_id), None) is not None:
        _save(stamps)
