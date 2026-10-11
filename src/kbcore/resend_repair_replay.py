"""kbcore.resend_repair_replay — tier 3 of ``kb repair resend-dups``: the REPLAY (wave C.2).

Incident KB 49064: a session resumed from ANOTHER directory leaves two transcript
files under one session id; both share the hook's checkpoint, the sweep read the
stale copy as ``shrank`` and re-sent it from byte 0 — 411 rows / 282 MB, 408 of
them entirely inside bytes the session had ALREADY sent. Tiers 1-2 are blind to
it: the re-sent chunks have new boundaries (no hash twin), they sit in the SAME
project (no cross-project group), and every re-extracted item's quote is also in
the originals (tier 2 keeps them all as 'ambiguous').

The replay decides by BYTE RANGE, as an OWNER assertion over ONE session
(``--session <prefix> --replayed --since <ISO>``, ``since`` = the first re-sent
row's ``created_at``). It reads flush stamps offline for an owner-fired repair;
nothing at INGEST ever refuses, trims or range-checks on a stamp (proposal_v2
amendment 3) — this module is not on that path.

* C — the covered end: ``max(offset + bytes)`` over stamped rows of the uri (any
  project) created before ``since``. GATE: every pre-``since`` row is
  ``extracted`` or ``duplicate``, else the plan is refused — re-extracting bytes
  that were never mined is not a duplicate.
* R — rows created at/after ``since`` whose stamped range ends at or below C and
  touches no stamp gap (a gap row moves to NEW: ``gap -> NEW``).
* W0 — ``min(created_at)`` over R. Not ``min(processed_at)``: that is stamped when
  a coalesced GROUP completes (``mark_group_extracted``), possibly nights after
  its first checkpointed windows minted items; no item of R predates R's first row.
* W1 — ``max(processed_at)`` over R: every item R minted predates its group's
  completion, so nothing created after W1 can be R's (the still-flushing session's
  later items are never exposed).
* NEW — rows outside R that may have minted items at/after W0: processed at/after
  W0 (a row's items predate its ``processed_at``), still undrained (pending or
  retryable: a checkpointed partial), or a gap row. A few MB — the full history
  is never searched.
* Candidates — active, ``trust='unverified'`` items of the uri in ANY project
  (re-routed rows land in kb-system), not summaries/reports, created in
  ``[W0, W1]``. KEPT when recalled (``access_log`` — automated brief/turn-start
  recall hits included: the safe direction), when it is a SUCCESSOR (another
  item's ``superseded_by`` points at it — the nightly evidence auto-resolve and
  ``apply_suggestion`` may retire a pre-W0 item INTO an R paraphrase; archiving
  that would leave the old fact superseded by an archived item — or a
  ``'duplicate'`` was folded into it), when an owner ``kb reprocess`` minted it
  (``meta.reprocessed_from``), when the quote is empty, or when the quote occurs
  verbatim in the ASSEMBLED text of a NEW row — per row and per project
  concatenated the way the drain joins a (project, uri) group — in its raw OR its
  scrubbed form (the drain stores ``scrub(quote)`` after grounding the unscrubbed
  one, and a secret visible only in decoded text escapes the enqueue scrub);
  otherwise ARCHIVED: the quote can only have come from R's bytes, mined before
  the re-send.

``--apply`` REFUSES (the dry run still prints the plan) while an R row is still
pending or retryable — the drain would mint its items after the archive — and
while the unstamped-prefix CHECK is up (:mod:`kbcore.resend_repair_replay_rows`).

Apply: candidates go through :func:`kbcore.resend_repair_items.archive_items`
(reason :data:`REPLAY_REASON`, review rows resolved in the same transaction), in
batches — which re-checks the successor keep at WRITE time (an item that became a
successor between the dry run and the prompt is kept, counted and logged); R rows
keep their STATUS and gain ``meta.repair = {mode: 'replay', covered_end, since, at,
by}`` (audit; a row already carrying another repair's
marker is left alone). ``--undo`` with the same selector reverses exactly that
run (:mod:`kbcore.resend_repair_undo`). Nothing is deleted (Inv 5); bodies and
trust are never touched. The row-side reads (gate, C, R, W0, NEW) live in
:mod:`kbcore.resend_repair_replay_rows`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from psycopg.types.json import Jsonb

from kbcore import resend_repair_replay_rows as rows_step
from kbcore import resend_repair_undo as undo_step
from kbcore import scope
from kbcore.db import connect, transaction
from kbcore.errors import InvalidOperation
from kbcore.resend_repair import REPAIR_BY, now_iso
from kbcore.resend_repair_items import SUCCESSOR_SQL, archive_items
from kbcore.resend_repair_models import RepairSelector, ReplayPlan, ReplayProject, UndoPlan
from kbcore.scrub import scrub

logger = logging.getLogger("kbcore.resend_repair")

REPLAY_REASON = "repair:resend-replay"
REPLAY_MODE = undo_step.REPLAY_MODE
ARCHIVE_BATCH = 500  # items per archive transaction
_OTHER_FLAGS = {
    "project": "--project",
    "all": "--all",
    "uri": "--uri",
    "row_ids": "--rows-file",
    "as_resend": "--as-resend",
    "rows_only": "--rows-only",
    "attribute_quotes": "--attribute-quotes",
    "limit": "--limit",
}

_CANDIDATES_SQL = f"""
SELECT i.id, i.project, i.meta->>'quote',
       EXISTS (SELECT 1 FROM access_log a WHERE a.item_id = i.id) AS recalled,
       {SUCCESSOR_SQL.format(t="i")} AS successor,
       (i.meta ? 'reprocessed_from') AS reprocessed
  FROM items i JOIN sources s ON s.id = i.source_id
 WHERE s.kind = 'session' AND s.uri = %(uri)s
   AND i.status = 'active' AND s.trust = 'unverified'
   AND i.type NOT IN ('summary', 'report')
   AND i.created_at >= %(w0)s
   AND (%(w1)s::timestamptz IS NULL OR i.created_at <= %(w1)s::timestamptz)
 ORDER BY i.id
"""


def check_selector(sel: RepairSelector) -> tuple[str, datetime]:
    """``--replayed`` is ONE session (``--session``) and the re-send instant
    (``--since``), nothing else. Returns ``(session uri, since)``."""
    if not (sel.session or "").strip():
        raise InvalidOperation("--replayed needs --session <sid prefix> (exactly one session)")
    if sel.since is None:
        raise InvalidOperation(
            "--replayed needs --since <ISO UTC>: the created_at of the first re-sent row"
        )
    extra = [flag for f, flag in _OTHER_FLAGS.items() if getattr(sel, f) not in (None, False)]
    if extra:
        raise InvalidOperation(
            f"--replayed takes --session and --since only, not {', '.join(extra)}"
        )
    return rows_step.resolve_uri(sel.session or ""), sel.since


def plan(sel: RepairSelector) -> ReplayPlan:
    """Dry run for ONE session (module docstring). Writes nothing; refuses on the gate."""
    scope.require_owner("resend repair")
    uri, since = check_selector(sel)
    with connect() as conn:
        rows = rows_step.load_rows(conn, uri)
        p, hs = rows_step.coverage(uri, since, rows)
        rows_step.fill_resent(p, rows, hs)
        if p.w0 is not None:
            _plan_items(conn, p, rows_step.new_texts(conn, p.new_ids))
    return p


def _plan_items(conn, p: ReplayPlan, texts: list[str]) -> None:
    """Per candidate: recalled / successor / reprocess / no quote / quote in a NEW
    row's text (raw or scrubbed) -> keep; else archive."""
    scrubbed = [scrub(t)[0] for t in texts]
    for iid, project, quote, recalled, successor, reprocessed in conn.execute(
        _CANDIDATES_SQL, {"uri": p.uri, "w0": p.w0, "w1": p.w1}
    ).fetchall():
        pp = p.projects.setdefault(str(project), ReplayProject())
        pp.candidates += 1
        q = (quote or "").strip()
        if recalled:
            pp.keep_recalled += 1
        elif successor:
            pp.keep_successor += 1
        elif reprocessed:
            pp.keep_reprocess += 1
        elif not q:
            pp.keep_no_quote += 1
        elif any(q in t for t in texts) or any(q in t for t in scrubbed):
            pp.keep_new_content += 1
        else:
            pp.archive_ids.append(int(iid))


def check_apply(p: ReplayPlan) -> None:
    """Refuse ``--apply`` on a plan the stamps cannot vouch for (nothing written)."""
    if reasons := p.apply_refusals:
        raise InvalidOperation("replay --apply refused: " + "; ".join(reasons))


def apply(p: ReplayPlan) -> ReplayPlan:
    """Mark R (status kept) then archive the planned items in batches of
    :data:`ARCHIVE_BATCH`, each batch one transaction with its review rows."""
    scope.require_owner("resend repair")
    check_apply(p)
    since_iso = p.since.isoformat()
    if p.mark_ids:
        marker = {
            "mode": REPLAY_MODE,
            "covered_end": p.covered_end,
            "since": since_iso,
            "at": now_iso(),
            "by": REPAIR_BY,
        }
        with transaction() as conn:
            cur = conn.execute(
                """
                UPDATE ingest_queue
                   SET meta = coalesce(meta, '{}'::jsonb)
                              || jsonb_build_object('repair', %(m)s::jsonb)
                 WHERE id = ANY(%(ids)s::bigint[])
                   AND NOT (coalesce(meta, '{}'::jsonb) ? 'repair')
                """,
                {"m": Jsonb(marker), "ids": list(p.mark_ids)},
            )
            p.rows_marked = cur.rowcount or 0
    archived = {"source_uri": p.uri, "covered_end": p.covered_end, "since": since_iso}
    repair = {"mode": REPLAY_MODE, "covered_end": p.covered_end, "since": since_iso}
    for project, pp in sorted(p.projects.items()):
        kept = 0
        for i in range(0, len(pp.archive_ids), ARCHIVE_BATCH):
            batch = pp.archive_ids[i : i + ARCHIVE_BATCH]
            res = archive_items(batch, reason=REPLAY_REASON, archived=archived, repair=repair)
            pp.archived += res.archived
            kept += res.kept_successor
        logger.info(
            "resend-dups replay %s/%s since %s: archived %d of %d planned item(s); "
            "%d kept at write time (successor)",
            project,
            p.sid8,
            since_iso,
            pp.archived,
            len(pp.archive_ids),
            kept,
        )
    logger.info("resend-dups replay %s: marked %d re-sent row(s)", p.sid8, p.rows_marked)
    return p


def undo(sel: RepairSelector, *, dry_run: bool = True) -> UndoPlan:
    """Reverse exactly the replay run keyed by ``(session uri, since)``: items back to
    active with the review rows that run resolved re-opened; the rows' marker dropped."""
    scope.require_owner("resend repair")
    uri, since = check_selector(sel)
    plan_ = UndoPlan(
        row_ids=undo_step.replay_row_candidates(uri, since),
        item_ids=undo_step.replay_item_candidates(uri, since),
    )
    if not plan_.row_ids and not plan_.item_ids:
        plan_.stored_since = undo_step.replay_since_values(uri)
    if dry_run:
        return plan_
    plan_.items_restored, plan_.reviews_reopened = undo_step.undo_items(plan_.item_ids)
    plan_.rows_restored = undo_step.undo_replay_rows(plan_.row_ids)
    return plan_
