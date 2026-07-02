"""kbcore.locks — cross-process file locks (§7.2 session serialization).

The session Stop hook can fire from several Claude Code processes at once; two
concurrent ``ingest_session`` runs over the same transcript would read
overlapping windows, insert duplicates, and race the checkpoint backwards.
``session_lock`` serializes them with an advisory ``fcntl.flock`` on a per-
transcript lock file (non-reentrant across open file descriptions), held for
the whole read → extract → ingest → checkpoint cycle.
"""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from kbcore.config import settings

_LOCK_SUBDIR = ".session-locks"


def session_lock_path(abs_path: str) -> Path:
    """Deterministic lock-file path for a transcript's absolute path."""
    key = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
    return settings.raw / _LOCK_SUBDIR / f"{key}.lock"


@contextmanager
def session_lock(abs_path: str) -> Iterator[None]:
    """Hold an exclusive cross-process lock for *abs_path* for the block."""
    lock_path = session_lock_path(abs_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — lifetime is the with-block below
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()
