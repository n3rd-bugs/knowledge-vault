"""kbcore.boot — unclean-shutdown detection via a persistent boot marker.

A tiny, dependency-light helper the boot-edge units use to tell "the box came
back from a clean stop" from "the box lost power mid-run". It is deliberately
PURE FILESYSTEM: at boot the Postgres container and the network may both still
be down (the very situation being detected), so a DB- or network-backed signal
would be blind exactly when it is needed. It lives in its own module rather than
in kbcore.jobs because jobs.py sits on the 300-line size-ratchet ceiling.

The mechanism is the classic dirty-flag:

* a marker file records the CURRENT boot's id while the machine is up;
* a clean stop removes it (``record_clean_shutdown`` from the unit's ExecStop);
* a crash / power loss leaves it behind, still holding the PREVIOUS boot's id.

So at the next boot ``consume_boot_marker`` finds a marker whose id differs from
the running boot's id — that, and only that, is an unclean shutdown. Storing the
boot id (not merely presence) makes the check idempotent within a boot: a second
invocation sees its own id and stays silent, so the unit can never emit two
pushes for one recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kbcore.config import settings

_BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_MARKER_NAME = "unclean-shutdown.marker"
_MESSAGE_PREFIX = "BOOT: recovered from an unclean shutdown at "


def default_marker_path() -> Path:
    """Where the marker lives on a real deployment.

    Under the KB data root (beside ``raw/`` and ``backups/``) — persistent disk
    that survives a reboot, never ``/tmp`` (tmpfs here, which would wipe the flag
    every boot and blind the check). Derived from ``settings.raw`` so it follows
    a ``KB_RAW`` override instead of hardcoding a path (CLAUDE.md: config via
    kbcore.config).
    """
    return settings.raw.parent / "state" / _MARKER_NAME


def _current_boot_id() -> str:
    """The kernel's per-boot random id; ``''`` if unreadable (non-Linux/tests)."""
    try:
        return _BOOT_ID_PATH.read_text().strip()
    except OSError:
        return ""


def consume_boot_marker(
    marker: Path | None = None,
    *,
    boot_id: str | None = None,
    now: datetime | None = None,
) -> str | None:
    """Arm the marker for THIS boot; return a recovery message iff the prior
    shutdown was unclean, else ``None``.

    Unclean == the marker survived from a previous boot (its stored id differs
    from the running boot id). A fresh/clean history (no marker) and a repeat
    call within the same boot (id already matches) both return ``None`` — the
    latter is what makes "exactly one push per boot" hold even if the unit is
    invoked twice. This only READS and WRITES a flag file; it never deletes or
    mutates knowledge (Invariant 5 is not in scope, but nothing here touches the DB).
    """
    marker = marker or default_marker_path()
    boot_id = boot_id if boot_id is not None else _current_boot_id()
    previous = marker.read_text().strip() if marker.exists() else None

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(boot_id)  # arm: this boot now owns the flag

    if previous is None or previous == boot_id:
        return None
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    return f"{_MESSAGE_PREFIX}{stamp}"


def record_clean_shutdown(marker: Path | None = None) -> None:
    """Disarm the marker (a clean stop's ExecStop). Absence == clean history."""
    marker = marker or default_marker_path()
    marker.unlink(missing_ok=True)


def _main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "clean":
        record_clean_shutdown()
        return 0
    # 'check': arm the marker and print the recovery line (empty when clean) so
    # the shell wrapper only has to decide push-or-not on a non-empty stdout.
    message = consume_boot_marker()
    if message:
        print(message)
    return 0


if __name__ == "__main__":  # pragma: no cover — driven by the unit, not by pytest
    import sys

    raise SystemExit(_main(sys.argv))
