"""kbworker.run_report — host telemetry, the timed step runner and the run report.

Split from :mod:`kbworker.consolidate` (mechanical move, 2026-09-03, review #8
H0) to buy that file room under its size ratchet; nothing here changed.
``consolidate`` re-imports every name (main() and the tests call them as
``consolidate.<name>``), and the logger keeps the ``kbworker.consolidate`` name
because ``_run_step``'s ">= 60 s" WARNING is a journald recipe.
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kbcore import consolidation
from kbcore.config import settings
from kbcore.ingest import ingest

logger = logging.getLogger("kbworker.consolidate")


def _disk_pct() -> float:
    """Percent used on the filesystem holding KB_RAW."""
    p = settings.raw
    while not p.exists() and p != p.parent:
        p = p.parent
    usage = shutil.disk_usage(p if p.exists() else Path("/"))
    return round(usage.used / usage.total * 100, 1)


def _host_memory(meminfo: Path = Path("/proc/meminfo")) -> tuple[float, float]:
    """(swap % used, MemAvailable in GiB) — ONE procfs read, beside disk_pct.

    Swap hit 14G/16G on 2026-08-21 with zero telemetry anywhere (review #5
    ops-3). Framing per the verifier: cold-page parking under the consolidate
    cgroup plus Ollama loads, NOT imminent OOM — 43Gi MemAvailable at the peak —
    so this is telemetry, never an alarm. The digest renders a positive-only
    line when swap tops 80% (pkg-reporting); both raw numbers ride every run
    report so the trend is answerable from data. ``meminfo`` is a parameter for
    hermetic tests only.
    """
    fields: dict[str, int] = {}
    for line in meminfo.read_text(encoding="ascii", errors="replace").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0])  # kB
    swap_total = fields.get("SwapTotal", 0)
    swap_pct = (
        round((swap_total - fields.get("SwapFree", 0)) / swap_total * 100, 1) if swap_total else 0.0
    )
    return swap_pct, round(fields.get("MemAvailable", 0) / 1048576, 1)


def _rejection_rate() -> float | None:
    rejected, saved = consolidation.extraction_stats()
    total = rejected + saved
    return round(rejected / total, 3) if total else None


def _rerank_probe_step(counts: dict[str, Any]) -> None:
    """Tail step (review #5 ops-4): ask the rerank sidecar to prove it can score.

    ``kbcore.rerank.probe`` (pkg-search) POSTs a fixed two-document probe and
    returns ``{'ok': bool, 'latency_ms': int|None}``, itself fail-open. Guarded
    with ``hasattr`` + try/except so this tree imports and runs even before that
    helper lands: no probe, or no ``kb_rerank_url`` configured, means no keys and
    therefore no digest line (positive-only). A red eval night's meta must say
    which ranking system was measured — on 08-18 nothing ruled the sidecar in or
    out, and it demonstrably parks ~272MB in swap, so first-touch timeouts are
    armed; ``'failed'`` here is the attribution, in meta instead of journald.
    """
    from kbcore import rerank

    if not (settings.kb_rerank_url or "").strip() or not hasattr(rerank, "probe"):
        return
    try:
        result = rerank.probe() or {}
        counts["rerank_probe"] = "ok" if result.get("ok") else "failed"
        counts["rerank_probe_ms"] = result.get("latency_ms")
    except Exception:  # noqa: BLE001 — fail-open like every tail step
        logger.exception("rerank probe failed")
        counts["rerank_probe"] = "failed"
        counts["rerank_probe_ms"] = None


def _run_step(counts: dict[str, Any], key: str, fn) -> None:
    """Run one step, isolating failure: log and leave the count at 0.

    Also TIMES it. A pass ran for over an hour producing nothing, and there was
    no way to say which step consumed it without guessing — the same blind spot
    that hid a 40% CPU fallback and an inert cohort path. Durations land in the
    run report (counts["step_secs"]), which is persisted in
    consolidation_requests.result, so "where did the hour go" is answerable from
    data on the next run rather than by instrumenting after the fact.
    """
    t0 = time.monotonic()
    try:
        counts[key] = fn()
    except Exception:  # noqa: BLE001 — one step must not abort the pass
        logger.exception("consolidation step %r failed", key)
    finally:
        secs = round(time.monotonic() - t0, 1)
        counts.setdefault("step_secs", {})[key] = secs
        # WARNING so it survives the unit's default level: an INFO line here was
        # invisible in journald and cost hours of archaeology.
        if secs >= 60:
            logger.warning("consolidation step %r took %.0fs", key, secs)


def _write_report(counts: dict[str, Any]) -> int:
    ts = datetime.now(UTC)
    # BODY = scalar counts ONLY (review #9a follow-up, 2026-09-09). The body is
    # chunked + embedded like any item; nested telemetry (per-case ranks keyed by
    # the golden QUERY TEXT, id lists, tallies) belongs in meta alone — dumping
    # it here put all 52 golden queries verbatim into every nightly report, and
    # four reports then outranked the real answers (hit@5 0.981 -> 0.923).
    body = "Nightly consolidation report\n\n" + "\n".join(
        f"- {k}: {v}" for k, v in counts.items() if not isinstance(v, dict | list | tuple)
    )
    return ingest(
        "kb-system",
        "report",
        f"Consolidation report {ts.date().isoformat()}",
        body,
        kind="chat",
        uri=f"report:{ts.isoformat()}",
        trust="verified",
        meta={"consolidation": True, **counts},
    )
