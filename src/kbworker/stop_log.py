"""kbworker.stop_log — the stand-down log object and the run instants beside it.

Split out of :mod:`kbworker.tail_ops` (review #9a INS-1/INS-2, 2026-09-06)
when the two instants pushed that file past the size cap. ``tail_ops`` (and
through it ``consolidate``) re-imports :data:`_STOP_LOG`, so every existing
``consolidate._STOP_LOG.clear()`` / equality pin keeps resolving to this ONE
shared object; ``tail_ops._note_stop`` is still the only writer of entries.

The per-step entries stay exactly ``{'reason', 'deferred'}`` — ten tests pin
them by equality and ``consolidate.main()`` copies them into
``counts['stopped']``. The instants are ATTRIBUTES of the log, never keys:

* ``started_at`` — stamped by :meth:`_StopLog.clear`, because
  ``consolidate.main()`` clears the log exactly once, at run start, and that
  call is the only per-run reset this package sees without editing
  consolidate itself (a separate module-level stamp would need its own);
* ``battery_at`` — the FIRST battery stand-down of the run
  (:meth:`_StopLog.note_reason`). The pause marker's ``'at'`` used to be the
  marker WRITE time at the run's tail — on Sep 5 that was 06:57, 2h44m after
  the grid had come back, and the digest's RESUME line named it as the pause.

:func:`pause_payload` turns both into the marker payload
``powerguard.record_battery_pause`` writes. UTC throughout (Invariant 7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class _StopLog(dict):
    """``{step: {'reason', 'deferred'}}`` plus the run's start / first battery instants."""

    started_at: datetime | None = None
    battery_at: datetime | None = None

    def clear(self) -> None:
        super().clear()
        self.started_at = datetime.now(UTC)
        self.battery_at = None

    def note_reason(self, reason: str) -> None:
        """Stamp ``battery_at`` on the run's FIRST battery stand-down; later ones keep it."""
        if reason == "battery" and self.battery_at is None:
            self.battery_at = datetime.now(UTC)


_STOP_LOG: _StopLog = _StopLog()


def battery_stand_down_at() -> datetime | None:
    """The run's FIRST battery stand-down instant (UTC); None when it never stood down."""
    return _STOP_LOG.battery_at


def run_started_at() -> datetime | None:
    """When the log was last cleared (UTC) — the run start; None in a bare process."""
    return _STOP_LOG.started_at


def pause_payload(deferred_total: int) -> dict[str, Any]:
    """The battery-pause marker's JSON payload (review #9a INS-1/2), ADDED keys only.

    ``'at'`` is the run's first battery stand-down (falling back to now when no
    step stood down through ``_note_stop`` — a caller-built ``counts``),
    ``'deferred'`` the total the caller summed, ``'recorded_at'`` the write
    instant the old ``'at'`` used to be, and ``'started'`` the run start when
    the log has one — the digest's RESUME line keys the night on it.
    """
    now = datetime.now(UTC)
    at = _STOP_LOG.battery_at or now
    payload: dict[str, Any] = {
        "at": at.isoformat(),
        "deferred": int(deferred_total),
        "recorded_at": now.isoformat(),
    }
    if _STOP_LOG.battery_at is not None and _STOP_LOG.started_at is not None:
        payload["started"] = _STOP_LOG.started_at.isoformat()
    return payload
