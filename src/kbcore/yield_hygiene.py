"""kbcore.yield_hygiene — owner-fired cleanup of two recovery-residue classes.

Owner-built, owner-fired, status-flip / retag only — nothing deleted (Inv 5), nothing
promoted (Inv 10); verified / recent-access guards REUSED from :mod:`kbcore.twin_repair`.

1. Summary-noise retag. A bare ``lang:`` topic LEAKED the namespace into a
   ``f"Summary — {topic}"`` title. :func:`clean_summary_noise` mints a clean-titled
   ``trust='unverified'`` replacement (no promotion; ``lang:`` tag kept in ``tags``) and
   RETIRES the original via ``supersede`` — NOT the ``new_body`` path, which would mint a
   ``verified`` replacement (a promotion).
2. Judge-flagged prioritization. :func:`prioritize_judge_failed` STAMPS the open review
   rows of ``verified_by='secondary-judge-failed'`` items with a ``detail->'priority'``
   marker (NOT the ``'triage'`` key — that would HIDE the row from
   ``review_triage.pending_triage``) and never resolves them. Inert until
   ``pending_triage`` orders by it — a one-line ORDER BY change the CLI flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import ingest
from kbcore.supersede import supersede
from kbcore.twin_repair import (
    ACCESS_WINDOW_DAYS,
    OK,
    REFUSE_INACTIVE,
    REFUSE_RECENT_ACCESS,
    REFUSE_VERIFIED,
)

SUMMARY_TITLE_PREFIX = "Summary — "  # exact create_summary prefix (U+2014 em dash)
NOISE_NAMESPACE = "lang"  # only this namespace leaked into titles (measured residue)
SUMMARY_REASON = "yield-hygiene: retag leaked lang tag out of summary title"
RETAG_META_KEY = "retag_of"  # replacement -> original: crash/re-run idempotency anchor

JUDGE_FAILED_LABEL = "secondary-judge-failed"
TRIAGE_REASON = "unverified_extraction"
PRIORITY_KEY = "priority"  # NOT 'triage' — pending_triage EXCLUDES rows keyed 'triage'
ALREADY_PRIORITIZED = "already_prioritized"


@dataclass(frozen=True)
class SummaryNoiseItem:
    """One active ``Summary — lang:<tag>`` summary and its computed clean title."""

    item_id: int
    project: str
    leaked_tag: str
    corrected_title: str
    trust: str
    created_at: datetime
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryOutcome:
    """One summary's decision; ``superseded`` is never True on a dry run."""

    item: SummaryNoiseItem
    eligible: bool
    superseded: bool
    reason: str
    new_item_id: int | None = None


_NOISE_SQL = """
SELECT i.id, i.project, i.title, i.tags, i.created_at, COALESCE(s.trust, 'verified')
FROM items i LEFT JOIN sources s ON s.id = i.source_id
WHERE i.type = 'summary' AND i.status = 'active' AND i.title LIKE %(pat)s
ORDER BY i.id
"""


def enumerate_summary_noise() -> list[SummaryNoiseItem]:
    """READ-ONLY, owner-only: active ``Summary — lang:<tag>`` summaries, oldest first
    (clean title strips the namespace: ``lang:python`` -> ``Summary — python``)."""
    scope.require_owner("summary-noise repair")
    with connect() as conn:
        rows = conn.execute(
            _NOISE_SQL, {"pat": f"{SUMMARY_TITLE_PREFIX}{NOISE_NAMESPACE}:%"}
        ).fetchall()
    out: list[SummaryNoiseItem] = []
    for r in rows:
        leaked = r[2][len(SUMMARY_TITLE_PREFIX) :]  # e.g. 'lang:python'
        value = leaked.partition(":")[2]
        if not value:
            continue
        out.append(
            SummaryNoiseItem(
                item_id=int(r[0]),
                project=r[1],
                leaked_tag=leaked,
                corrected_title=f"{SUMMARY_TITLE_PREFIX}{value}",
                trust=r[5],
                created_at=r[4],
                tags=list(r[3] or []),
            )
        )
    return out


_GUARD_SQL = """
SELECT i.status, COALESCE(s.trust, 'verified'),
       EXISTS (SELECT 1 FROM access_log al
               WHERE al.item_id = i.id AND al.ts >= now() - make_interval(days => %(days)s))
FROM items i LEFT JOIN sources s ON s.id = i.source_id
WHERE i.id = %(id)s
"""


def _summary_refusal(conn: psycopg.Connection, item_id: int) -> str | None:
    """Refusal code, or None when safe — the SAME guards as ``twin_repair._refusal``
    (verified, incl. sourceless via ``COALESCE``; or read within :data:`ACCESS_WINDOW_DAYS`)."""
    row = conn.execute(_GUARD_SQL, {"id": item_id, "days": ACCESS_WINDOW_DAYS}).fetchone()
    if row is None:
        raise NotFound(f"item {item_id} does not exist")
    status, trust, accessed_recently = row
    if status != "active":
        return REFUSE_INACTIVE
    if trust == "verified":
        return REFUSE_VERIFIED
    if accessed_recently:
        return REFUSE_RECENT_ACCESS
    return None


def _mint_clean_summary(item: SummaryNoiseItem) -> int:
    """Mint (or reuse, by :data:`RETAG_META_KEY`) the clean-titled ``trust='unverified'``
    replacement — reuse makes a re-run after a mint/supersede crash idempotent. NEW uri."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM items WHERE project = %s AND type = 'summary' "
            "AND status = 'active' AND meta ->> %s = %s",
            (item.project, RETAG_META_KEY, str(item.item_id)),
        ).fetchone()
        if existing is not None:
            return int(existing[0])
        row = conn.execute(
            "SELECT body, COALESCE(meta, '{}'::jsonb) FROM items WHERE id = %s", (item.item_id,)
        ).fetchone()
    if row is None:
        raise NotFound(f"item {item.item_id} does not exist")
    body, meta = row
    carried = {k: v for k, v in (meta or {}).items() if k != "redactions"}
    carried[RETAG_META_KEY] = item.item_id
    carried["yield_hygiene"] = "summary_noise_retag"
    ts = datetime.now(UTC)
    new_id = ingest(
        item.project,
        "summary",
        item.corrected_title,
        body,
        kind="chat",
        uri=f"summary:{item.project}:{item.leaked_tag}:retag:{ts.isoformat()}",
        trust="unverified",
        tags=list(item.tags),
        meta=carried,
    )
    if new_id is None:  # unreachable with the timestamped uri, but be explicit
        raise InvalidOperation(f"retag replacement for item {item.item_id} collided")
    return new_id


def clean_summary_noise(*, dry_run: bool = True) -> list[SummaryOutcome]:
    """Retag the noise summaries via supersede. OWNER-ONLY.

    ``dry_run=True`` (default) writes NOTHING; ``dry_run=False`` mints an unverified
    clean-titled replacement and supersedes the original (active -> superseded, no
    delete/body-edit/promotion; per-item isolation on a race).
    """
    scope.require_owner("summary-noise repair")
    outcomes: list[SummaryOutcome] = []
    for item in enumerate_summary_noise():
        with connect() as conn:
            reason = _summary_refusal(conn, item.item_id)
        if reason is not None:
            outcomes.append(SummaryOutcome(item, False, False, reason))
            continue
        if dry_run:
            outcomes.append(SummaryOutcome(item, True, False, OK))
            continue
        try:
            new_id = _mint_clean_summary(item)
            supersede(item.item_id, new_item_id=new_id, reason=SUMMARY_REASON)
        except (InvalidOperation, NotFound):  # raced non-active after the guard read
            outcomes.append(SummaryOutcome(item, False, False, REFUSE_INACTIVE))
            continue
        outcomes.append(SummaryOutcome(item, True, True, OK, new_item_id=new_id))
    return outcomes


def summarize_summaries(outcomes: list[SummaryOutcome]) -> dict[str, int]:
    """Counts by decision — ``eligible``/``superseded`` plus one key per refusal."""
    counts: dict[str, int] = {"items": len(outcomes), "eligible": 0, "superseded": 0}
    for o in outcomes:
        counts["eligible"] += int(o.eligible)
        counts["superseded"] += int(o.superseded)
        if o.reason != OK:
            counts[o.reason] = counts.get(o.reason, 0) + 1
    return counts


@dataclass(frozen=True)
class JudgeFailedRow:
    """One open ``unverified_extraction`` row over a judge-failed item."""

    queue_id: int
    item_id: int
    project: str
    title: str
    already_prioritized: bool


@dataclass(frozen=True)
class JudgeOutcome:
    """One row's decision; ``stamped`` is never True on a dry run."""

    row: JudgeFailedRow
    stamped: bool
    reason: str


_JUDGE_SQL = """
SELECT rq.id, rq.item_id, i.project, i.title, (COALESCE(rq.detail, '{}'::jsonb) ? %(pkey)s)
FROM review_queue rq JOIN items i ON i.id = rq.item_id
WHERE rq.resolved = false AND rq.reason = %(reason)s AND i.status = 'active'
  AND i.meta -> 'extraction_pipeline' ->> 'verified_by' = %(label)s
ORDER BY rq.id
"""


def enumerate_judge_failed() -> list[JudgeFailedRow]:
    """READ-ONLY, owner-only: open review rows over ``secondary-judge-failed`` items."""
    scope.require_owner("judge-flagged prioritization")
    with connect() as conn:
        rows = conn.execute(
            _JUDGE_SQL,
            {"pkey": PRIORITY_KEY, "reason": TRIAGE_REASON, "label": JUDGE_FAILED_LABEL},
        ).fetchall()
    return [JudgeFailedRow(int(r[0]), int(r[1]), r[2], r[3], bool(r[4])) for r in rows]


def _stamp_priority(queue_id: int) -> bool:
    """Merge the priority marker into an OPEN, unstamped row's ``detail`` (jsonb ||) — row
    stays open, item untouched. False if it resolved/was stamped meanwhile."""
    stamp = {
        PRIORITY_KEY: {
            "reason": JUDGE_FAILED_LABEL,
            "source": "yield_hygiene",
            "at": datetime.now(UTC).isoformat(),
        }
    }
    with transaction() as conn:
        row = conn.execute(
            "UPDATE review_queue SET detail = COALESCE(detail, '{}'::jsonb) || %s "
            "WHERE id = %s AND resolved = false AND NOT (COALESCE(detail, '{}'::jsonb) ? %s) "
            "RETURNING id",
            (Jsonb(stamp), queue_id, PRIORITY_KEY),
        ).fetchone()
    return row is not None


def prioritize_judge_failed(*, dry_run: bool = True) -> list[JudgeOutcome]:
    """Stamp a front-of-triage priority marker on judge-failed rows. OWNER-ONLY.

    ``dry_run=False`` stamps ``detail->'priority'`` on each not-yet-marked row (never
    resolves/touches an item); INERT until ``pending_triage`` orders by it (CLI flags it).
    """
    scope.require_owner("judge-flagged prioritization")
    outcomes: list[JudgeOutcome] = []
    for row in enumerate_judge_failed():
        if row.already_prioritized:
            outcomes.append(JudgeOutcome(row, False, ALREADY_PRIORITIZED))
            continue
        if dry_run:
            outcomes.append(JudgeOutcome(row, False, OK))
            continue
        stamped = _stamp_priority(row.queue_id)
        outcomes.append(JudgeOutcome(row, stamped, OK if stamped else ALREADY_PRIORITIZED))
    return outcomes


def summarize_judge(outcomes: list[JudgeOutcome]) -> dict[str, int]:
    """Counts — ``rows`` total, ``pending`` (would-stamp), ``stamped``, ``already``."""
    counts = {"rows": len(outcomes), "pending": 0, "stamped": 0, "already": 0}
    for o in outcomes:
        counts["stamped"] += int(o.stamped)
        if o.reason == ALREADY_PRIORITIZED:
            counts["already"] += 1
        else:
            counts["pending"] += 1
    return counts
