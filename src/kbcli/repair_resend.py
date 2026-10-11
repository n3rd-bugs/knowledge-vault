"""kbcli.repair resend-dups — retire the rows and items a session RESEND cost (wave S, S5).

Thin shell over :mod:`kbcore.resend_repair` (no SQL, Invariant 2), shaped on
:func:`kbcli.repair_shared._flow`: dry-run table -> confirm -> apply -> undo trail.
Owner-only; every write runs under the consolidation advisory lock so the nightly
cannot interleave a ``mark_group_extracted`` with a flip. A selector is REQUIRED.
Root cause KB 33783; design KB 33784. ``--replayed`` (tier 3, KB 49064) is the
byte-range repair of a same-session re-send (:mod:`kbcore.resend_repair_replay`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import typer

from kbcli.repair_shared import _clip, _flow, _guarded, _locked_apply, repair_app
from kbcore import resend_repair, scope
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.items import parse_instant
from kbcore.resend_repair import GroupPlan, RepairSelector

_HEADER = (
    f"{'session':8}  {'project':16}  {'rows':>5}  {'dup':>5}  {'pend':>5}  {'extr':>5}  "
    f"{'MB':>7}  {'canon(status,saved)':20}  {'items':>5}  {'archive':>7}  {'keep':>5}  decision"
)

_UNDO = """
undo / audit trail
  Nothing was deleted (Invariant 5). Rows flipped -> 'duplicate' carry meta.repair
  (prev status/error inside); items flipped active -> archived carry meta.archived
  (reason repair:resend-duplicate) + meta.repair; their open review rows were resolved.
  Bodies, trust, relations and duplicate_of/chunk_key (facts) are untouched.
    reverse   kb repair resend-dups --undo <same selector> [--apply]
    inspect   kb get <item-id>      queue: kb queue coverage --session <sid>""".strip()


def _table(plans: Sequence[GroupPlan], applied: bool) -> str:
    lines = [_HEADER, "-" * len(_HEADER)]
    for p in plans:
        canon = f"{p.canon_status},{p.canon_saved}" if p.canon_ids else "none,-"
        if applied and p.eligible:
            decision = f"FLIPPED {p.flipped} row(s); ARCHIVED {p.archived} item(s)"
        else:
            decision = p.decision
        lines.append(
            f"{p.session:8}  {_clip(p.project, 16):16}  {p.rows:>5}  {p.dup:>5}  {p.pend:>5}  "
            f"{p.extr:>5}  {p.dup_bytes / 1e6:>7.2f}  {_clip(canon, 20):20}  {p.items:>5}  "
            f"{len(p.archive_ids):>7}  {sum(p.keep.values()):>5}  {decision}"
        )
    return "\n".join(lines)


def _tally(plans: Sequence[GroupPlan]) -> str:
    rows = sum(p.rows for p in plans)
    dup = sum(p.dup for p in plans)
    pend = sum(p.pend for p in plans)
    parked = sum(p.parked for p in plans)
    extr = sum(p.extr for p in plans)
    unpaired = sum(p.unpaired for p in plans)
    items = sum(p.items for p in plans)
    archive = sum(len(p.archive_ids) for p in plans)
    keep = sum(sum(p.keep.values()) for p in plans)
    flip_b = sum(p.flip_bytes for p in plans)
    extr_b = sum(p.extr_bytes for p in plans)
    per_night = settings.kb_repair_mb_per_gpu_night
    parked_s = f", {parked} parked" if parked else ""
    unpaired_s = f", {unpaired} unpaired" if unpaired else ""
    return (
        f"{len(plans)} group(s): {rows} rows, {dup} duplicate ({pend} pending{parked_s}, "
        f"{extr} extracted{unpaired_s}); items: {items} candidate(s), {archive} archive, "
        f"{keep} keep\n"
        f"GPU-nights reclaimed: {resend_repair.gpu_nights(flip_b):.2f} "
        f"({flip_b / 1e6:.1f} MB not yet mined at {per_night} MB/night); "
        f"already spent on extracted duplicates: {resend_repair.gpu_nights(extr_b):.2f} "
        f"({extr_b / 1e6:.1f} MB)"
    )


def _read_rows_file(path: Path) -> list[int]:
    """Queue ids from the FIRST column of ``path`` (blank / ``#`` lines skipped)."""
    ids: list[int] = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        tok = re.split(r"[\s,;|]+", s)[0]
        try:
            ids.append(int(tok))
        except ValueError as e:
            raise InvalidOperation(f"{path}:{ln}: first column {tok!r} is not a queue id") from e
    if not ids:
        raise InvalidOperation(f"{path} lists no queue ids")
    return ids


def _backfill(apply: bool) -> None:
    plan = _guarded(lambda: resend_repair.backfill_keys(dry_run=True))
    typer.echo(
        f"{plan.groups} (session uri, content_hash) group(s): {len(plan.to_key)} canonical(s) "
        f"to key, {len(plan.to_mark)} later row(s) to mark duplicate_of (column writes only)"
    )
    if not plan.to_key and not plan.to_mark:
        typer.echo("nothing to backfill — every group is keyed.")
        return
    if not apply:
        typer.echo("dry run — nothing written. Re-run with --apply to write the keys.")
        return
    typer.confirm(
        f"Key {len(plan.to_key)} canonical(s) and mark {len(plan.to_mark)} duplicate(s)?",
        abort=True,
    )
    done = _locked_apply(lambda: resend_repair.backfill_keys(dry_run=False))
    typer.echo(f"keyed {done.keyed} canonical(s), marked {done.marked} later row(s).")


def _undo(sel: RepairSelector, apply: bool, fn=resend_repair.undo, rows: str = "") -> None:
    plan = _guarded(lambda: fn(sel, dry_run=True))
    typer.echo(
        f"undo: {len(plan.row_ids)} row(s) marked by resend-dups -> "
        f"{rows or 'previous status'}; "
        f"{len(plan.item_ids)} archived item(s) -> active (review rows re-opened)"
    )
    if not plan.row_ids and not plan.item_ids:
        typer.echo("nothing to undo for this selection.")
        if plan.stored_since is not None:  # replay: the key is the APPLY's --since
            stored = ", ".join(plan.stored_since) or "none"
            typer.echo(
                f"replay markers on this session carry --since: {stored} "
                "(re-run --undo with the --since the apply ran with)"
            )
        return
    if not apply:
        typer.echo("dry run — nothing written. Re-run with --apply to restore them.")
        return
    typer.confirm(
        f"Restore {len(plan.row_ids)} row(s) and {len(plan.item_ids)} item(s)?", abort=True
    )
    done = _locked_apply(lambda: fn(sel, dry_run=False))
    typer.echo(
        f"restored {done.rows_restored} row(s), {done.items_restored} item(s); "
        f"re-opened {done.reviews_reopened} review row(s). duplicate_of/chunk_key kept (facts)."
    )


@repair_app.command("resend-dups")
def repair_resend_dups(
    session: str | None = typer.Option(None, "--session", help="session id prefix"),
    project: str | None = typer.Option(None, "--project", help="rows under this project"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="rows created at/after (ISO, UTC); under --undo: the apply's marker time; "
        "under --replayed: the first re-sent row's created_at, and under --replayed --undo "
        "the SAME --since the replay apply ran with (its key, not a marker time)",
    ),
    all_: bool = typer.Option(False, "--all", help="every session duplicate group"),
    uri: str | None = typer.Option(None, "--uri", help="one exact session uri"),
    rows_file: Path | None = typer.Option(None, "--rows-file", help="queue ids, first column"),
    apply: bool = typer.Option(False, "--apply", help="perform the flips (default: dry run)"),
    backfill_keys: bool = typer.Option(
        False, "--backfill-keys", help="step 0: key every pre-020 (uri, hash) group"
    ),
    rows_only: bool = typer.Option(False, "--rows-only", help="stop after the row flips"),
    as_resend: bool = typer.Option(
        False, "--as-resend", help="owner assertion: pair the selected pending/parked rows"
    ),
    attribute_quotes: bool = typer.Option(
        False,
        "--attribute-quotes",
        help="tier 2: decide mixed groups by where each item's grounded quote lives",
    ),
    undo: bool = typer.Option(False, "--undo", help="reverse a previous apply (same selector)"),
    limit: int | None = typer.Option(
        None, "--limit", help="plan (and apply) only the first N groups by session uri"
    ),
    replayed: bool = typer.Option(
        False,
        "--replayed",
        help="tier 3 (KB 49064): a same-session re-send by byte range; needs --session and "
        "--since (the first re-sent row's created_at)",
    ),
) -> None:
    """Retire cross-project session RESENDS: later duplicate rows still pending ->
    'duplicate'; their items archived only behind the canonical gate (every canonical
    row extracted AND saved>0). Dry run by default; a selector is required."""
    _guarded(lambda: scope.require_owner("resend repair"))
    if backfill_keys:
        _backfill(apply)
        return
    sel = RepairSelector(
        session=session,
        project=project,
        since=_guarded(lambda: parse_instant(since, "--since")) if since else None,
        all=all_,
        uri=uri,
        row_ids=_guarded(lambda: _read_rows_file(rows_file)) if rows_file else None,
        as_resend=as_resend,
        rows_only=rows_only,
        attribute_quotes=attribute_quotes,
        limit=limit,
        replayed=replayed,
    )
    _guarded(lambda: resend_repair.require_selector(sel))
    if replayed:
        from kbcli.repair_resend_replay import run_replay

        run_replay(sel, apply=apply, undo=undo, undo_flow=_undo)
        return
    if undo:
        _undo(sel, apply)
        return
    _flow(
        apply=apply,
        dry_fn=lambda: resend_repair.plan(sel),
        apply_fn=lambda elig: resend_repair.apply(elig, sel),
        render=_table,
        tally=_tally,
        eligible_pred=lambda p: p.eligible,
        empty="no session duplicate groups match this selection",
        dry_hint="dry run — nothing written. Re-run with --apply to flip the rows/items above.",
        none_hint="nothing eligible — every group was kept by a guard (see decision).",
        prompt=lambda n: f"Flip rows and archive items in {n} group(s) as listed?",
        undo=_UNDO,
        hold_lock=True,
    )
