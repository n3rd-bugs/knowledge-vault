"""kbcore.resend_repair_replay_gate — the replay's pre-``since`` VERDICTS (wave C.2).

Split from :mod:`kbcore.resend_repair_replay_rows` (ratchet; review 2, 2026-10-11),
which re-exports every name here. Read-only, no SQL.

* The GATE: every pre-``since`` row must be ``extracted`` or ``duplicate``, else the
  plan is refused — re-extracting never-mined bytes is not a duplicate. Each
  blocking row is tagged (:func:`gate_options`): ``wait`` (the drain still reaches
  it), ``parked`` (resend residue an owner parked as failed — ``error LIKE
  'parked %'``, :data:`kbcore.resend_repair_rows.PARKED_LIKE` — settled by
  ``--as-resend``, NEVER requeued: that would re-extract a re-send; ``--as-resend``
  settles only a row with an exact ``(uri, content_hash)`` twin, so the hint names
  the owner's path for an UNPAIRED one — the gate reads no content hashes, so it
  cannot tell the two apart itself) or ``requeue``
  (genuinely failed for good; ``requeue_failed`` re-opens failed rows in EVERY
  project, which the hint says).
* The prefix NOTE: the unstamped-prefix CHECK is one-sided (a SHORT prefix refuses
  ``--apply``); a prefix over ``1 + PREFIX_TOLERANCE`` of the first stamp is only a
  NOTE with its ratio (:func:`prefix_note`) — a re-sent prefix, whose size proves
  nothing about ``[0, first stamp)``. rm1's bd162e9f: 570 rows / 484.5 MB under a
  first stamp at 221,030,428 (2.19x).
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from kbcore.errors import InvalidOperation
from kbcore.ingest_queue import MAX_ATTEMPTS
from kbcore.resend_repair_models import ReplayPlan

if TYPE_CHECKING:
    from kbcore.resend_repair_replay_rows import Row

GATE_SHOWN = 20  # gate refusal: rows listed
GATE_OK = frozenset({"extracted", "duplicate"})
PREFIX_TOLERANCE = 0.10  # the unstamped prefix may fall short of the first stamp by 10%


def _tag(r: Row) -> str:
    """``wait`` (the drain still reaches it) / ``parked`` / ``requeue``."""
    return "wait" if r.undrained else "parked" if r.parked else "requeue"


def gate(uri: str, pre: list[Row]) -> dict[str, int]:
    """Status counts of the pre-``since`` rows; REFUSES (listing them) when any is not
    ``extracted``/``duplicate`` — re-extracting never-mined bytes is not a duplicate."""
    status: dict[str, int] = {}
    for r in pre:
        status[r.status] = status.get(r.status, 0) + 1
    bad = [r for r in pre if r.status not in GATE_OK]
    if bad:
        listed = "\n".join(
            f"  {r.id}  {r.project}  {r.status}  {r.created_at.isoformat()}  {_tag(r)}"
            for r in bad[:GATE_SHOWN]
        )
        more = f"\n  +{len(bad) - GATE_SHOWN} more" if len(bad) > GATE_SHOWN else ""
        raise InvalidOperation(
            f"replay refused: {len(bad)} row(s) of {uri} created before --since are not "
            "extracted/duplicate — a re-extraction of never-mined bytes is not a duplicate. "
            f"{gate_options(bad, uri)}:\n{listed}{more}"
        )
    return status


def gate_options(bad: list[Row], uri: str = "") -> str:
    """The owner's real ways past the gate, per tag (:func:`_tag`): wait for the
    drain; settle PARKED resend residue as duplicates with ``--as-resend`` (never a
    requeue — that would re-extract a re-send; it settles only rows with an exact
    ``(uri, content_hash)`` twin, and an unpaired one is the owner's call by id,
    which the hint spells out); requeue only the genuinely failed
    rows — there is no CLI for :func:`kbcore.queue_repair.requeue_failed`, so the
    exact call is named, with what else it re-opens."""
    out: list[str] = []
    tags = [_tag(r) for r in bad]
    if wait := tags.count("wait"):
        out.append(f"{wait} 'wait': pending or retryable — re-run after the nightly drain")
    if parked := tags.count("parked"):
        sid = uri.removeprefix("session:") or "<sid>"
        out.append(
            f"{parked} 'parked': resend residue parked as failed — NEVER requeue it (that "
            "re-extracts a re-send); settle it as a duplicate first: kb repair resend-dups "
            f"--session {sid} --as-resend (dry run, then --apply), then re-run. --as-resend "
            "settles ONLY a parked row with an exact (uri, content_hash) twin (or a "
            "duplicate_of mark already on it); its dry run counts any other 'unpaired' and "
            "leaves it failed, and this gate refuses on it again. An unpaired parked row is "
            "the OWNER's call, by queue id: if its bytes were never mined it is not residue "
            "— requeue it via kbcore.queue_repair.requeue_failed (that re-opens EVERY failed "
            "row in EVERY project, so only after --as-resend --apply settled the paired "
            "ones) and re-run after the drain; if another row mined them (a re-send cut on "
            "another boundary: no hash twin) no repair settles it — it stays parked and this "
            "session is not replayed"
        )
    terminal = [r for r, t in zip(bad, tags, strict=True) if t == "requeue"]
    if terminal:
        done = [r.processed_at for r in terminal if r.processed_at is not None]
        since = min(done).astimezone(UTC).isoformat() if len(done) == len(terminal) else None
        arg = f"since=d.fromisoformat('{since}'), " if since else ""
        scope_ = f"processed at/after {since}" if since else "in the queue"
        also = ", parked residue included — settle the 'parked' rows first" if parked else ""
        out.append(
            f"{len(terminal)} 'requeue': failed for good (attempts >= {MAX_ATTEMPTS}) — "
            'requeue, then re-run after the drain: uv run python -c "from datetime import '
            "datetime as d; from kbcore import ingest_queue as q; "
            f"print(len(q.requeue_failed({arg}reason='resend replay gate')))\" "
            f"(it requeues EVERY failed row in EVERY project {scope_}{also})"
        )
    return "; ".join(out)


def prefix_note(p: ReplayPlan) -> str | None:
    """The one-sided CHECK's other side: an unstamped prefix over ``1 +
    PREFIX_TOLERANCE`` of the first stamp is a NOTE with its ratio, never a refusal."""
    if not p.pre_unstamped_rows or p.first_stamped_offset <= 0:
        return None
    ratio = p.pre_unstamped_bytes / p.first_stamped_offset
    if ratio <= 1.0 + PREFIX_TOLERANCE:
        return None
    return (
        f"NOTE     unstamped prefix {p.pre_unstamped_bytes / 1e6:.1f} MB / first stamp at "
        f"{p.first_stamped_offset:,} = ratio {ratio:.2f} (over 1 + {PREFIX_TOLERANCE:.0%}): "
        "a re-sent prefix (an earlier incident stored those bytes more than once), so its "
        "size cannot show that every byte below the first stamp was sent -- the CHECK passes "
        "on size alone and --apply treats [0, first stamp) as covered on the owner's word"
    )
