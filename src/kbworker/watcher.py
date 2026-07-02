"""kbworker.watcher — inbox watcher + ingest (Phase 3, PLAN.md §7.1).

Watches ``KB_INBOX`` (watchdog events + a 30 s poll fallback) and ingests each
dropped file through :func:`kbcore.ingest.ingest`. The original bytes are
archived zstd-compressed under ``KB_RAW`` (that copy is canonical), so the
inbox file is removed after a successful ingest — or after a duplicate no-op,
since the archive already holds it. The same loop runs the daily repo docs
scan over ``KB_GIT_REPOS`` (§7.5). Run: ``python -m kbworker.watcher``.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from kbcore.config import settings
from kbcore.ingest import archive_raw, ingest
from kbworker import reposcan
from kbworker.watcher_parsers import is_supported, parse

logger = logging.getLogger("kbworker.watcher")

POLL_INTERVAL = 30.0  # §7.1 poll fallback for events watchdog may miss
REPOSCAN_INTERVAL = 86_400.0  # daily docs scan over KB_GIT_REPOS (§7.5)
STABLE_DELAY = 0.5  # partial-write guard: wait for size to settle
STABLE_TRIES = 20  # up to ~10s for a slow write to finish before we give up

# Serialize processing so a watchdog event and the poll pass can't fight over
# the same file. Ingest is idempotent, so a race is harmless, but the lock
# keeps DB churn and log noise down.
_LOCK = threading.Lock()


def _project_and_uri(path: Path) -> tuple[str, str]:
    """Project = first-level subfolder under the inbox (else 'unsorted');
    uri = the inbox-relative path (§7.1)."""
    rel = path.resolve().relative_to(settings.inbox.resolve())
    parts = rel.parts
    project = parts[0] if len(parts) > 1 else "unsorted"
    return project, str(rel)


def process_file(path: Path) -> int | None:
    """Ingest one inbox file and remove it. Returns the item id, or None.

    This is the unit the tests drive directly. The archived copy under KB_RAW
    is canonical, so the inbox file is always removed afterwards — even when no
    item is created (duplicate, or no extractable text). In both of those cases
    the raw bytes are archived FIRST so a distinct original is never lost.
    """
    project, uri = _project_and_uri(path)
    parsed = parse(path)

    if not parsed.text.strip():
        # Scanned/image PDF, empty DOCX, blank file: an empty body yields a
        # chunk-less, search-invisible item. Archive the original and drop it
        # from the inbox instead of ingesting a ghost (review finding).
        archive_raw(project, parsed.raw)
        path.unlink(missing_ok=True)
        logger.warning("inbox %s: no extractable text; archived raw, not ingested", uri)
        return None

    item_id = ingest(
        project,
        parsed.type,
        title=path.name,
        body=parsed.text,
        kind="file",
        uri=uri,
        trust="verified",
        raw_content=parsed.raw,
        is_source=parsed.is_source,
    )
    if item_id is None:
        # Dedup no-op: ingest archived nothing this round, so archive here to
        # preserve a distinct original whose scrubbed body collided with a
        # previously ingested one (review finding).
        archive_raw(project, parsed.raw)
        logger.info("inbox %s: duplicate content (archived copy is canonical)", uri)
    else:
        logger.info("inbox %s: ingested item %s (project=%s)", uri, item_id, project)
    path.unlink(missing_ok=True)
    return item_id


def _wait_stable(path: Path) -> bool:
    """True only once two consecutive size reads match (partial-write guard).

    A still-growing file that never settles within STABLE_TRIES returns False,
    so the caller leaves it in the inbox for a later poll rather than ingesting
    a truncated body and unlinking mid-write (review finding: this used to
    return True after exhausting its tries).
    """
    last = -1
    for _ in range(STABLE_TRIES):
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last:
            return True
        last = size
        time.sleep(STABLE_DELAY)
    return False


def _safe_process(path: Path) -> None:
    """Process a path defensively — never let one bad file kill the watcher."""
    with _LOCK:
        try:
            if not path.is_file() or not is_supported(path):
                return
            if not _wait_stable(path):
                return
            process_file(path)
        except FileNotFoundError:
            return
        except Exception:  # noqa: BLE001 — a bad drop must not stop the loop
            logger.exception("failed to process inbox file %s", path)


def scan_inbox() -> None:
    """Poll pass: process every supported file currently in the inbox tree."""
    inbox = settings.inbox
    if not inbox.exists():
        return
    for path in sorted(inbox.rglob("*")):
        if path.is_file() and is_supported(path):
            _safe_process(path)


class _InboxHandler(FileSystemEventHandler):
    """Route watchdog create/modify/move events to the processor."""

    def _handle(self, path_str: str) -> None:
        path = Path(path_str)
        if is_supported(path):
            _safe_process(path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(event.dest_path)


def main() -> None:
    """Run the watcher loop: watchdog observer + poll fallback + daily reposcan."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    inbox = settings.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    logger.info("watching inbox %s (poll every %ss)", inbox, int(POLL_INTERVAL))

    observer = Observer()
    observer.schedule(_InboxHandler(), str(inbox), recursive=True)
    observer.start()

    last_reposcan: float | None = None  # None ⇒ due immediately at startup
    try:
        while True:
            scan_inbox()
            now = time.monotonic()
            if _reposcan_due(last_reposcan, now):
                _run_reposcan()
                last_reposcan = now
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def _reposcan_due(last: float | None, now: float) -> bool:
    """Whether a repo docs scan is due.

    Runs once promptly at startup (``last is None``), then every
    REPOSCAN_INTERVAL. Using an elapsed comparison instead of a wall/monotonic
    mismatch means a host with < 24h uptime still gets its first scan (review
    finding: ``last=0.0`` vs ``time.monotonic()`` deferred it for ~a day).
    """
    if last is None:
        return True
    return now - last >= REPOSCAN_INTERVAL


def _run_reposcan() -> None:
    """Daily docs scan over KB_GIT_REPOS; never let a bad repo stop the loop."""
    try:
        summaries = reposcan.scan_all()
    except Exception:  # noqa: BLE001
        logger.exception("repo docs scan failed")
        return
    for s in summaries:
        logger.info("reposcan %s: %s docs ingested (project=%s)", s.repo, s.saved, s.project)


if __name__ == "__main__":
    main()
