"""kbcore.resend_repair_models — the I/O shapes of ``kb repair resend-dups``.

Split from :mod:`kbcore.resend_repair` (ratchet). Pydantic models only — the
selector the CLI builds from its flags, the per-group dry-run line, and the two
plans (backfill, undo). They import nothing from the repair modules, so every one
of those can import them at the top without a cycle; ``kbcore.resend_repair``
re-exports them, which is the name callers and tests use.
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
