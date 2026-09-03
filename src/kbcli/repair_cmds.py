"""kbcli.repair — owner-fired one-time repairs (thin shells, Invariant 2 — no SQL).

``twins`` (:mod:`kbcore.twin_repair`), ``summaries``/``judge-flagged``
(:mod:`kbcore.yield_hygiene`). Each defaults to a DRY RUN (table, writes nothing);
``--apply`` confirms y/N then status-flips/retags (never delete/promote) and prints the
undo trail. All three share :func:`_flow`.
"""

from __future__ import annotations

import typer

from kbcli.repair_shared import _clip, _flow, repair_app
from kbcore import twin_repair, yield_hygiene

# --- kb repair twins ---------------------------------------------------------
_HEADER = (
    f"{'dist':>7}  {'retire':>8}  {'keep':>8}  {'created':10}  "
    f"{'redactions':12}  {'decision':22}  title"
)

_UNDO = """
undo / audit trail
  Nothing was deleted (Invariant 5). Each retired copy keeps its body/tags/relations; only
  `status` flipped active -> duplicate, plus a `derived_from` edge — the nightly dedupe trail.
    inspect  kb get <retire-id>    walk  kb trace <keep-id>    re-check  kb repair twins
  Restoring one is a deliberate owner action on that row; re-running never un-retires.""".strip()


def _table(outcomes: list[twin_repair.RepairOutcome], applied: bool) -> str:
    lines = [_HEADER, "-" * len(_HEADER)]
    for o in outcomes:
        if o.retired:
            decision = "RETIRED"
        elif o.eligible:
            decision = "would retire" if not applied else "RETIRE FAILED"
        else:
            decision = f"skip: {o.reason}"
        p = o.pair
        lines.append(
            f"{p.distance:>7.4f}  {'#' + str(p.retire.item_id):>8}  "
            f"{'#' + str(p.keep.item_id):>8}  {p.retire.created_at.date().isoformat():10}  "
            f"{_clip(p.retire.fingerprint(), 12):12}  {decision:22}  {_clip(p.retire.title)}"
        )
    return "\n".join(lines)


def _tally(outcomes: list[twin_repair.RepairOutcome]) -> str:
    c = twin_repair.summarize(outcomes)
    skips = {k: v for k, v in c.items() if k not in ("pairs", "eligible", "retired")}
    tail = "; " + ", ".join(f"{k}={v}" for k, v in sorted(skips.items())) if skips else ""
    return f"{c['pairs']} candidate pair(s), {c['eligible']} eligible, {c['retired']} retired{tail}"


@repair_app.command("twins")
def repair_twins(
    max_dist: float = typer.Option(
        twin_repair.DEFAULT_MAX_DIST, "--max-dist", help="lead-chunk cosine distance ceiling"
    ),
    gateway: str = typer.Option(
        twin_repair.GATEWAY_PROJECT, "--gateway", help="project whose copies get retired"
    ),
    original: str = typer.Option(
        twin_repair.ORIGINAL_PROJECT, "--original", help="project that stays canonical"
    ),
    limit: int | None = typer.Option(None, "--limit", help="only the closest N pairs"),
    apply: bool = typer.Option(
        False, "--apply", help="perform the retirements (default: dry run, writes nothing)"
    ),
) -> None:
    """Retire GATEWAY copies of data-server originals (refuses verified, recently-read, or
    copies that are themselves canonical of other duplicates)."""

    def _dry() -> list:
        pairs = twin_repair.enumerate_twins(gateway, original, max_dist)
        return twin_repair.repair(pairs[: max(0, limit)] if limit is not None else pairs)

    _flow(
        apply=apply,
        dry_fn=_dry,
        apply_fn=lambda elig: twin_repair.repair([o.pair for o in elig], dry_run=False),
        render=_table,
        tally=_tally,
        eligible_pred=lambda o: o.eligible,
        empty=f"no {gateway}/{original} twins within distance {max_dist}",
        dry_hint="dry run — nothing written. Re-run with --apply to retire the copies above.",
        none_hint="nothing eligible to retire — every candidate was refused by a guard.",
        prompt=lambda n: f"Retire {n} {gateway} copy/copies as duplicates of {original}?",
        undo=_UNDO,
        hold_lock=True,
    )


# --- kb repair summaries -----------------------------------------------------
_SUM_HEADER = (
    f"{'item':>8}  {'created':10}  {'trust':10}  {'leaked tag':16}  {'decision':16}  corrected"
)

_SUM_UNDO = """
undo / audit trail
  Nothing deleted (Inv 5). Each noise summary flipped active -> superseded, pointing at a
  clean-titled trust='unverified' replacement (no promotion Inv 10; body untouched Inv 1).
    kb get <item-id> / <new-id> (summary / replacement);  re-check: kb repair summaries.
""".strip()


def _sum_table(outcomes: list[yield_hygiene.SummaryOutcome], applied: bool) -> str:
    lines = [_SUM_HEADER, "-" * len(_SUM_HEADER)]
    for o in outcomes:
        if o.superseded:
            decision = "RETAGGED"
        elif o.eligible:
            decision = "RETAG FAILED" if applied else "would retag"
        else:
            decision = f"skip: {o.reason}"
        it = o.item
        lines.append(
            f"{'#' + str(it.item_id):>8}  {it.created_at.date().isoformat():10}  "
            f"{it.trust:10}  {_clip(it.leaked_tag, 16):16}  {decision:16}  "
            f"{_clip(it.corrected_title)}"
        )
    return "\n".join(lines)


def _sum_tally(outcomes: list[yield_hygiene.SummaryOutcome]) -> str:
    c = yield_hygiene.summarize_summaries(outcomes)
    skips = {k: v for k, v in c.items() if k not in ("items", "eligible", "superseded")}
    tail = "; " + ", ".join(f"{k}={v}" for k, v in sorted(skips.items())) if skips else ""
    return f"{c['items']} summary(ies), {c['eligible']} eligible, {c['superseded']} retagged{tail}"


@repair_app.command("summaries")
def repair_summaries(
    apply: bool = typer.Option(
        False, "--apply", help="perform the retags (default: dry run, writes nothing)"
    ),
) -> None:
    """Retag ``Summary — lang:<tag>`` noise: supersede each with a clean-titled copy
    (refuses verified or recently-read; replacement stays unverified, keeps the lang tag)."""
    _flow(
        apply=apply,
        dry_fn=lambda: yield_hygiene.clean_summary_noise(dry_run=True),
        apply_fn=lambda _elig: yield_hygiene.clean_summary_noise(dry_run=False),
        render=_sum_table,
        tally=_sum_tally,
        eligible_pred=lambda o: o.eligible,
        empty="no 'Summary — lang:<tag>' noise summaries found",
        dry_hint="dry run — nothing written. Re-run with --apply to retag the summaries above.",
        none_hint="nothing eligible to retag — every candidate was refused by a guard.",
        prompt=lambda n: f"Retag {n} noise summary(ies) (supersede each with a clean-titled copy)?",
        undo=_SUM_UNDO,
        hold_lock=True,
    )


# --- kb repair judge-flagged -------------------------------------------------
_JUDGE_HEADER = f"{'queue':>8}  {'item':>8}  {'project':16}  {'decision':16}  title"

_JUDGE_NOTE = """
priority marker written
  Nothing resolved — rows stay OPEN, no item changed (Inv 5, no promotion); each row gained
  a detail->'priority' marker. review_triage.pending_triage sorts stamped rows first (key
  EXISTENCE, not a cast — the stamp is an object), so the next nightly triage batch takes
  these before the FIFO tail.
  Inspect: kb review list.   Re-check: kb repair judge-flagged.
""".strip()


def _judge_table(outcomes: list[yield_hygiene.JudgeOutcome], applied: bool) -> str:
    lines = [_JUDGE_HEADER, "-" * len(_JUDGE_HEADER)]
    for o in outcomes:
        if o.stamped:
            decision = "PRIORITIZED"
        elif o.reason == yield_hygiene.ALREADY_PRIORITIZED:
            decision = "skip: already"
        else:
            decision = "STAMP FAILED" if applied else "would prioritize"
        r = o.row
        lines.append(
            f"{'#' + str(r.queue_id):>8}  {'#' + str(r.item_id):>8}  "
            f"{_clip(r.project, 16):16}  {decision:16}  {_clip(r.title)}"
        )
    return "\n".join(lines)


def _judge_tally(outcomes: list[yield_hygiene.JudgeOutcome]) -> str:
    c = yield_hygiene.summarize_judge(outcomes)
    return (
        f"{c['rows']} judge-failed row(s), {c['pending']} to prioritize, "
        f"{c['stamped']} stamped, {c['already']} already marked"
    )


@repair_app.command("judge-flagged")
def repair_judge_flagged(
    apply: bool = typer.Option(
        False, "--apply", help="stamp the priority marker (default: dry run, writes nothing)"
    ),
) -> None:
    """Float secondary-judge-failed rows to the front of triage: stamps a
    `detail->'priority'` marker on each open row; never resolves a row or touches an item."""
    _flow(
        apply=apply,
        dry_fn=lambda: yield_hygiene.prioritize_judge_failed(dry_run=True),
        apply_fn=lambda _elig: yield_hygiene.prioritize_judge_failed(dry_run=False),
        render=_judge_table,
        tally=_judge_tally,
        eligible_pred=lambda o: o.reason != yield_hygiene.ALREADY_PRIORITIZED,
        empty="no open review rows over secondary-judge-failed items",
        dry_hint="dry run — nothing written. Re-run with --apply to stamp the priority marker.",
        none_hint="nothing to prioritize — every row already carries the marker.",
        prompt=lambda n: f"Stamp a front-of-triage priority marker on {n} row(s)?",
        undo=_JUDGE_NOTE,
    )


# The cross-project twin linker lives in its own module (ratchet); importing it
# here registers `kb repair cross-twins` onto repair_app so main.py's single
# `import repair_cmds` still wires every repair command.
from kbcli import repair_audit as _repair_audit  # noqa: E402,F401
from kbcli import repair_crosstwins as _repair_crosstwins  # noqa: E402,F401
