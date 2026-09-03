"""kbworker.summary_stamps — per-tag 'attempted' stamps for the summaries step (H8b).

Review #8 / panel #7's empty-body finding: a tag whose feeding set came back too
small (<3 sources) or whose summarizer call returned an EMPTY body was re-selected
every night. :func:`kbcore.consolidation.tags_needing_summary` derives need from
the DATA (items newer than the tag's own newest summary) and a failed attempt
writes no summary, so the tag's high-water never moved and the same summarizer
call was paid again the next night, forever. H8a removes the structural residue
(all-flagged tags) in SQL; this module handles what only the model can reveal —
an empty body — plus the race-only <3 case, by remembering the attempt.

THE STAMP IS A NON-KNOWLEDGE MARKER in ``KB_STATE_DIR`` (the AC-restore pause
marker's home), never a row: ``{"<project>\\t<tag>": {"at", "new_count", "why"}}``.
A stamped tag is skipped while its ``new_count`` has not GROWN past the stamped
one — i.e. until at least one new qualifying item arrives, the only thing that
can change what the summarizer sees — and a successful write CLEARS it (the
mark advances, the count resets, and a stale stamp would otherwise hide the tag).
Fail-open both ways: an unreadable or unwritable marker logs and never fails the
step; one retry per tag is the whole cost of losing it. Invariant 5 is untouched:
no knowledge row is written, flipped or deleted here, and the file holds no
content — only project/tag names, a count and a timestamp.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kbcore.config import settings

logger = logging.getLogger("kbworker.consolidate")

SUMMARY_STAMPS_FILE = "summary-attempts.json"

Stamps = dict[str, dict[str, Any]]


def stamp_path() -> Path:
    """``$KB_STATE_DIR/summary-attempts.json`` (tests point KB_STATE_DIR at tmp_path)."""
    return Path(settings.kb_state_dir).expanduser() / SUMMARY_STAMPS_FILE


def _key(project: str, tag: str) -> str:
    return f"{project}\t{tag}"


def load(path: Path | None = None) -> Stamps:
    """The stamps on disk, or ``{}`` — a missing or corrupt file is not a failure."""
    path = stamp_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("summary stamps at %s unreadable; treating every tag as untried", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save(stamps: Stamps, path: Path | None = None) -> None:
    path = stamp_path() if path is None else path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(stamps, sort_keys=True), encoding="utf-8")
        tmp.replace(path)  # atomic on POSIX, like the pause marker
    except OSError:
        logger.warning("summary stamps at %s not writable; the tag will simply be retried", path)


def skip(stamps: Stamps, project: str, tag: str, new_count: int) -> bool:
    """True when a prior attempt is on record and nothing new has arrived since."""
    entry = stamps.get(_key(project, tag))
    if not isinstance(entry, dict):
        return False
    try:
        return int(new_count) <= int(entry.get("new_count", 0))
    except (TypeError, ValueError):
        return False


def record(
    stamps: Stamps,
    project: str,
    tag: str,
    new_count: int,
    why: str,
    path: Path | None = None,
) -> None:
    """Remember that ``tag`` was tried at ``new_count`` fresh items and yielded nothing."""
    stamps[_key(project, tag)] = {
        "at": datetime.now(UTC).isoformat(),
        "new_count": int(new_count),
        "why": why,
    }
    _save(stamps, path)


def clear(stamps: Stamps, project: str, tag: str, path: Path | None = None) -> None:
    """Forget a tag's stamp once a summary was actually written for it."""
    if stamps.pop(_key(project, tag), None) is not None:
        _save(stamps, path)
