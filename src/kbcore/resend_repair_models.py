"""kbcore.resend_repair_models — the I/O shapes of ``kb repair resend-dups``.

Split from :mod:`kbcore.resend_repair` (ratchet). Pydantic models only — the
selector the CLI builds from its flags, the per-group dry-run line, the two
plans (backfill, undo) and the tier-3 replay plan. They import nothing from the
repair modules, so every one of those can import them at the top without a
cycle; ``kbcore.resend_repair`` re-exports them, which is the name callers and
tests use.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator


class RepairSelector(BaseModel):
    """The owner's selection (CLI flags). One of the selector fields is REQUIRED."""

    model_config = ConfigDict(extra="forbid")

    session: str | None = None  # sid prefix -> uri starts with 'session:<prefix>'
    project: str | None = None
    since: datetime | None = None  # rows created at/after
    all: bool = False
    uri: str | None = None  # exact session uri
    row_ids: list[int] | None = None  # --rows-file (first column = queue id)
    as_resend: bool = False  # owner assertion: pair pending/parked rows by (uri, hash)
    rows_only: bool = False  # stop after step 1
    attribute_quotes: bool = False  # tier 2 on mixed groups
    limit: int | None = None  # groups (bounds the planning work, not just the output)
    replayed: bool = False  # tier 3 (KB 49064): --session + --since only, see resend_repair_replay

    @field_validator("since")
    @classmethod
    def _since_is_aware(cls, v: datetime | None) -> datetime | None:
        """Invariant 7 at the kbcore boundary: a naive instant is UTC (parse_instant's rule)."""
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    def is_set(self) -> bool:
        return (
            bool(self.session or self.project or self.since or self.all or self.uri)
            or self.row_ids is not None
        )


class GroupPlan(BaseModel):
    """One dry-run line: the ``(project, session uri)`` duplicate group."""

    model_config = ConfigDict(extra="forbid")

    project: str
    uri: str
    rows: int  # every row of (project, uri)
    dup: int  # rows that are (or will be) marked duplicate_of
    pend: int  # duplicate rows still 'pending' -> flip
    extr: int  # duplicate rows 'extracted' -> kept
    parked: int = 0  # --as-resend: parked-failed rows -> flip
    unpaired: int = 0  # --as-resend: rows with no canonical -> keep
    dup_bytes: int = 0  # every duplicate row of the group
    flip_bytes: int = 0  # the rows this run would flip (GPU-nights reclaimed)
    extr_bytes: int = 0  # duplicate rows already extracted (GPU-nights spent)
    flip: dict[int, int] = {}  # row id -> canonical id
    dup_ids: list[int] = []  # every duplicate row of the group: existing marks + this run's flips
    canon_ids: list[int] = []
    canon_status: str = "none"  # extracted | pending | failed | duplicate | mixed | none
    canon_saved: int = 0
    canon_project: str | None = None
    items: int = 0
    archive_ids: list[int] = []
    keep: dict[str, int] = {}
    reserved: bool = False
    flipped: int = 0  # after apply
    archived: int = 0

    @property
    def session(self) -> str:
        return self.uri.removeprefix("session:")[:8]

    @property
    def eligible(self) -> bool:
        return not self.reserved and bool(self.flip or self.archive_ids)

    @property
    def decision(self) -> str:
        if self.reserved:
            return "keep: reserved project"
        parts: list[str] = []
        if self.flip:
            parts.append(f"flip {len(self.flip)} row(s)")
        if self.unpaired:
            parts.append(f"keep: no canonical ({self.unpaired})")
        if self.archive_ids:
            parts.append(f"archive {len(self.archive_ids)} item(s)")
        for reason, n in sorted(self.keep.items()):
            parts.append(f"keep: {reason} ({n})")
        return "; ".join(parts) or "nothing to do"


class BackfillPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groups: int
    to_key: dict[int, str] = {}  # canonical id -> chunk_key
    to_mark: dict[int, int] = {}  # later id -> canonical id
    skipped: int = 0  # groups left alone: every row already marked / keyed row is a duplicate
    keyed: int = 0
    marked: int = 0


class UndoPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_ids: list[int] = []
    item_ids: list[int] = []
    rows_restored: int = 0
    items_restored: int = 0
    reviews_reopened: int = 0
    # Replay undo only (None elsewhere): when the (uri, since) key matched nothing,
    # the --since values the uri's replay markers DO carry (rows and items).
    stored_since: list[str] | None = None


class ReplayProject(BaseModel):
    """One project's line of the tier-3 replay dry run (items of the session under it)."""

    model_config = ConfigDict(extra="forbid")

    candidates: int = 0
    archive_ids: list[int] = []
    keep_recalled: int = 0  # access_log hit (automated brief/turn-start recall included)
    keep_successor: int = 0  # another item's superseded_by (or a folded duplicate) points here
    keep_reprocess: int = 0  # meta.reprocessed_from: an owner `kb reprocess` minted it
    keep_no_quote: int = 0
    keep_new_content: int = 0  # quote (raw or scrubbed form) found in a NEW row's text
    archived: int = 0  # after apply


class ReplayPlan(BaseModel):
    """The tier-3 replay plan for ONE session uri (``kbcore.resend_repair_replay``)."""

    model_config = ConfigDict(extra="forbid")

    uri: str
    since: datetime  # the instant the re-send began (owner-supplied)
    covered_end: int  # C: max(offset + bytes) over stamped rows created before ``since``
    pre_rows: int = 0
    pre_status: dict[str, int] = {}  # the gate: every pre-since row extracted | duplicate
    pre_stamped: int = 0
    pre_unstamped_rows: int = 0  # the seed-era prefix (no meta.flush)
    pre_unstamped_bytes: int = 0
    first_stamped_offset: int = 0
    prefix_short: bool = False  # CHECK: the unstamped prefix cannot hold [0, first stamp)
    gaps: int = 0  # holes in [0, C) the stamped ranges leave
    gap_bytes: int = 0
    r_ids: list[int] = []  # R: re-sent rows fully inside [0, C), touching no gap
    r_bytes: int = 0
    r_status: dict[str, int] = {}
    r_in_gap: int = 0  # re-sent rows touching a stamp gap -> moved to NEW (gap -> NEW)
    r_undrained: int = 0  # R rows still pending or retryable failed: --apply refused
    w0: datetime | None = None  # min(created_at) over R — no item of R is older
    first_done: datetime | None = None  # min(processed_at) over R
    w1: datetime | None = None  # max(processed_at) over R — no item of R is newer
    new_ids: list[int] = []  # NEW: rows outside R whose extraction ran at/after W0
    new_bytes: int = 0
    mark_ids: list[int] = []  # R rows that will gain the replay marker
    marked_before: int = 0  # R rows already carrying a replay marker
    other_marker: int = 0  # R rows carrying another repair's marker (left alone)
    projects: dict[str, ReplayProject] = {}
    rows_marked: int = 0  # after apply

    @property
    def sid8(self) -> str:
        return self.uri.removeprefix("session:")[:8]

    @property
    def archive_count(self) -> int:
        return sum(len(p.archive_ids) for p in self.projects.values())

    @property
    def eligible(self) -> bool:
        return bool(self.archive_count or self.mark_ids)

    @property
    def apply_refusals(self) -> list[str]:
        """Why ``--apply`` must refuse this plan (empty: it may run). The dry run
        still prints the whole plan; only the write is withheld."""
        out: list[str] = []
        if self.r_undrained:
            out.append(
                f"{self.r_undrained} R row(s) still pending or retryable -- re-run after the drain"
            )
        if self.prefix_short:
            out.append(
                "CHECK unstamped prefix: bytes below the first stamp may never have been "
                "sent, so a re-send there is not provably a duplicate"
            )
        return out
