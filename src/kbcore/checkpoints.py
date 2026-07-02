"""kbcore.checkpoints — per-transcript byte-offset checkpoints (DATA-1, §7.2).

Growing ``.jsonl`` session transcripts are re-read only from the last processed
byte offset, so a re-run over an unchanged file is a no-op and appended lines
are the only new work. All SQL lives here; kbworker calls these functions
(Invariant 2).
"""

from __future__ import annotations

from kbcore.db import connect, transaction


def get_checkpoint(path: str) -> int:
    """Return the last processed byte offset for *path*, or 0 if never seen."""
    with connect() as conn:
        row = conn.execute(
            "SELECT byte_offset FROM session_checkpoints WHERE path = %s", (path,)
        ).fetchone()
    return int(row[0]) if row else 0


def set_checkpoint(path: str, byte_offset: int) -> None:
    """Record that *path* has been processed up to ``byte_offset``."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO session_checkpoints (path, byte_offset, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (path) DO UPDATE
              SET byte_offset = EXCLUDED.byte_offset, updated_at = now()
            """,
            (path, byte_offset),
        )
