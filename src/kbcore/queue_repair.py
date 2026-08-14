"""kbcore.queue_repair — the two OWNER repairs that move ingest_queue rows back
into reach of the nightly drain.

Split from ``kbcore.ingest_queue`` (mechanical move, 2026-08-10): that file sat
at 651 lines against a 626 ratchet baseline, i.e. exactly at its ceiling, and the
per-window extraction checkpoint needed room. The baseline only ever moves DOWN
(CLAUDE.md), so the cut was made where it is cleanest — these two functions share
no state with the queue machinery, are called by no automatic path, and
``ingest_queue`` re-exports both names, which is the established seam every
caller and test already uses.

Same logger name as the parent on purpose: these lines have always appeared in
journald under ``kbcore.ingest_queue`` and grep recipes depend on that.

Invariant 5 is what makes both repairs possible: automation flips ``status`` and
never deletes, so when a verdict turns out to be wrong the content is still
there to re-judge.
"""

from __future__ import annotations

import logging
from datetime import datetime

from kbcore.db import transaction
from kbcore.errors import InvalidOperation
from kbcore.queue_checkpoint import CHECKPOINT_KEY

logger = logging.getLogger("kbcore.ingest_queue")


def requeue_failed(*, since: datetime | None = None, reason: str = "") -> list[int]:
    """Return ``failed`` rows to ``pending`` so a fixed pipeline can retry them.

    A row is marked ``failed`` by a policy judgement — "this content cannot be
    extracted" — but that judgement is only as good as the classifier that made
    it. When a TRANSIENT fault has been misclassified as content-deterministic,
    the rows it condemned are not bad content; they are casualties of a bug, and
    they sit at a terminal status no automatic path will ever revisit.

    Concretely (2026-07-29): an embed ReadTimeout was classified as permanent,
    so a zombie consolidation retired 12 rows on a fault fixed hours earlier.
    Invariant 5 is what makes this recoverable — automation flips ``status`` and
    never deletes, so the content was intact and only the verdict was wrong.

    ``attempts`` is RESET, deliberately: the previous attempts were spent against
    a broken classifier, and carrying them forward would exhaust
    :data:`kbcore.ingest_queue.MAX_ATTEMPTS` against a pipeline that can now
    succeed. ``deferrals`` is reset WITH it (2026-08-14): the owner's requeue is
    a fresh grant of BOTH budgets. Without this, the documented repair for a
    deferral-cap escalation was a NO-OP — the row came back 'pending' still
    carrying deferrals at :data:`kbcore.ingest_queue.DEFERRAL_CAP`, so its first
    re-defer escalated it straight back to 'failed' (review #3 P2, KB 19663).
    ``requeue_count`` is left alone — it guards the re-arm budget, a different
    concern.

    ANY PER-WINDOW CHECKPOINT IS KEPT (2026-08-10). These rows never completed,
    so resuming at the first not-done window is exactly the point of the feature:
    the repair is about the VERDICT, not about the extraction progress, and the
    fingerprint still guards the skip. (``requeue_extracted`` is the opposite
    case and clears it — see there.)

    This is an OWNER repair, never automation: nothing calls it on a schedule,
    and it widens no automatic path (Invariant 5's carve-out is about non-knowledge
    stores; this only moves knowledge rows BACK into reach).
    """
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE ingest_queue
               -- deferrals = 0 alongside attempts: the owner's requeue is a fresh
               -- grant of BOTH budgets (see docstring; review #3 P2).
               SET status = 'pending', attempts = 0, deferrals = 0, processed_at = NULL,
                   error = CASE
                             WHEN %(reason)s = '' THEN error
                             ELSE left(
                               coalesce(error, '') || ' | requeued: ' || %(reason)s, 2000)
                           END
             WHERE status = 'failed'
               AND (%(since)s::timestamptz IS NULL OR processed_at >= %(since)s)
            RETURNING id
            """,
            {"since": since, "reason": reason},
        ).fetchall()
    ids = [r[0] for r in rows]
    logger.info("requeued %d failed row(s) to pending: %s", len(ids), reason or "(no reason given)")
    return ids


def requeue_extracted(
    *,
    ids: list[int] | None = None,
    project: str | None = None,
    since: datetime | None = None,
    reason: str = "",
) -> list[int]:
    """Return ZERO-YIELD ``extracted`` rows to ``pending`` for a supervised retry.

    ``status='extracted'`` normally means "this content has been mined and the
    knowledge is in the KB". It does NOT mean that when the extraction saved
    nothing: those rows are terminal, no automatic path will ever revisit them,
    and their content — retained in full, because automation only ever flips
    ``status`` (Invariant 5) — has produced no knowledge at all.

    That is the state the coalescing bug left behind. 137 rows across six
    projects were concatenated into cross-project mega-extractions (an empty
    session id made every submission share one grouping key), and the extractions
    saved nothing. The rows were then stamped 'extracted' with
    ``saved_count = 0`` — indistinguishable, to every query in the system, from
    content that was genuinely mined and yielded nothing.

    SCOPE IS THE SAFETY PROPERTY. Only rows whose GROUP produced ZERO items can be
    re-opened: a row that produced knowledge would, if re-drained, duplicate it.
    Because :func:`kbcore.ingest_queue.mark_group_extracted` books a group's total
    on ONE row and 0 on the rest, "zero" is judged over the whole (project,
    source_uri) group. And a selector is REQUIRED — ``ids``, a ``project``, or a
    ``since`` cutoff — so this cannot become an accidental whole-table replay of
    every empty extraction the KB has ever done.

    THE PER-WINDOW CHECKPOINT IS CLEARED (2026-08-10). A group re-opened here is
    one an owner has deliberately decided to MINE AGAIN, so it must start at
    window 0 — resuming past windows whose (zero) yield is the very thing being
    retried would re-run the repair into the same nothing. Completion already
    strips the key in the same transaction as the flip, so this is belt and
    braces for any row that reached 'extracted' by another path.

    Like :func:`requeue_failed` this is an OWNER REPAIR: nothing calls it on a
    schedule, it deletes nothing, it only moves knowledge rows back into reach,
    and it records ``reason`` on the row so a status flip is never mistaken for a
    bug. ``attempts`` is reset for the same reason as there — the previous
    attempts were spent against a broken grouping key, not against bad content.
    """
    if ids is None and project is None and since is None:
        raise InvalidOperation(
            "requeue_extracted needs a selector (ids, project or since) — refusing "
            "to re-open every zero-yield extraction in the queue"
        )
    with transaction() as conn:
        rows = conn.execute(
            """
            UPDATE ingest_queue
               SET status = 'pending', attempts = 0, deferrals = 0, processed_at = NULL,
                   meta = coalesce(meta, '{}'::jsonb) - %(cpkey)s::text,
                   error = CASE
                             WHEN %(reason)s = '' THEN error
                             ELSE left(
                               coalesce(error, '') || ' | requeued (0 items saved): '
                               || %(reason)s, 2000)
                           END
             WHERE status = 'extracted'
               AND coalesce(saved_count, 0) = 0
               -- GROUP-AWARE zero-yield: a per-row 0 re-opens only when NO row in
               -- its (project, source_uri) group produced anything (else re-mining
               -- a productive session duplicates it); a degenerate uri has none.
               AND NOT EXISTS (
                   SELECT 1
                     FROM ingest_queue sib
                    WHERE sib.status = 'extracted'
                      AND coalesce(sib.saved_count, 0) > 0
                      AND sib.project = ingest_queue.project
                      AND btrim(coalesce(sib.meta->>'source_uri', '')) <> ''
                      AND btrim(coalesce(sib.meta->>'source_uri', ''))
                            !~ '^[^:]*:[/[:space:]]*$'
                      AND btrim(coalesce(sib.meta->>'source_uri', ''))
                            = btrim(coalesce(ingest_queue.meta->>'source_uri', ''))
               )
               AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s::bigint[]))
               AND (%(project)s::text IS NULL OR project = %(project)s)
               AND (%(since)s::timestamptz IS NULL OR processed_at >= %(since)s)
            RETURNING id
            """,
            {
                "ids": list(ids) if ids is not None else None,
                "project": project,
                "since": since,
                "reason": reason,
                "cpkey": CHECKPOINT_KEY,
            },
        ).fetchall()
    out = [r[0] for r in rows]
    logger.info(
        "requeued %d zero-yield extracted row(s) to pending: %s",
        len(out),
        reason or "(no reason given)",
    )
    return out
