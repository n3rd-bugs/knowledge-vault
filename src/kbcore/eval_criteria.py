"""kbcore.eval_criteria — the golden-set criteria: keys, validation, verdicts.

THE ESTABLISHED SEAM (mechanical split, E1 2026-09-16, KB 31857 review #8 H5):
``kbcore.eval_harness`` sat at 298/300 when the ``expect_item_id_raw``
criterion landed, so every per-criterion verdict builder and the helpers they
lean on (chain-head resolution, the corpus-presence triage, the substring
rank) moved HERE. ``eval_harness`` re-exports every name — ``_chain_head``,
``_content_exists``, ``_substr_rank``, ``CHAIN_DEPTH_CAP``, ``CriterionResult``
— so tests and ``kbworker.eval_composition`` keep resolving through the
harness. What stays in the harness is the search replay itself (``search`` is a
patched test seam there) and the metric fold.

Criteria (each returns its ``CriterionResult`` and, for the positive ones, the
MRR rank it claims):

* ``expect_substr`` — ANY-of over title+snippet+FULL BODY (legacy);
* ``expect_item_id`` — the pin resolved through ``items.superseded_by`` to the
  LIVE chain head (review #5 ret-eval-1: a doc-refresh supersede is telemetry,
  not a red night);
* ``expect_item_id_raw`` — the item ITSELF, never resolved: the transaction-time
  axis (review #8 H5, KB 31857), where a superseded revision is asserted
  PRESENT under ``kwargs: {include_superseded: true}``. Resolving it would make
  the case assert the head and silently stop testing the flag. Mutually
  exclusive with ``expect_item_id`` — a case pins one id, resolved OR raw;
* ``expect_rank_le`` — the positive match's rank must be <= R;
* ``forbid_item_id`` — the raw id must be absent (deliberately NEVER resolved:
  resolving it to the head would make every superseded-revision guard fail
  against itself).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kbcore.db import connect
from kbcore.items import get_item

# Positive criteria own the case's MRR rank and count toward n_positive.
POSITIVE_KEYS = ("expect_substr", "expect_item_id", "expect_item_id_raw")
CRITERIA_KEYS = (*POSITIVE_KEYS, "forbid_item_id")
CHAIN_DEPTH_CAP = 50  # supersession hops followed before giving up (cycle backstop)


@dataclass
class CriterionResult:
    """One criterion's verdict for one case (name, ok, human-readable detail)."""

    name: str
    ok: bool
    detail: str


def has_positive(case: dict) -> bool:
    """Does the case carry a positive criterion (MRR basis, n_positive member)?"""
    return any(k in case for k in POSITIVE_KEYS)


def validate_case(case: dict) -> None:
    """Reject a malformed case BEFORE any search runs (the harness must stay
    trustworthy: a case that tests nothing must raise, never pass)."""
    query = case.get("q")
    if not any(k in case for k in CRITERIA_KEYS):
        raise ValueError(f"golden case {query!r}: needs at least one of {CRITERIA_KEYS}")
    if "expect_item_id" in case and "expect_item_id_raw" in case:
        raise ValueError(
            f"golden case {query!r}: expect_item_id and expect_item_id_raw are mutually "
            "exclusive (a case pins one id, chain-resolved OR raw)"
        )
    if "expect_rank_le" in case and not has_positive(case):
        raise ValueError(f"golden case {query!r}: expect_rank_le needs a positive criterion")


def _substr_rank(hits: Sequence, expected: list[str]) -> int | None:
    """Legacy criterion: first rank whose title+snippet+FULL BODY has any substring."""
    for i, hit in enumerate(hits, start=1):
        haystack = f"{hit.title}\n{hit.snippet}\n{get_item(hit.item_id).body}".lower()
        if any(sub.lower() in haystack for sub in expected):
            return i
    return None


def _chain_head(item_id: int) -> int:
    """Follow ``items.superseded_by`` (single pointer) to the LIVE chain head.

    Review #5 ret-eval-1: both 08-18/19 red nights were doc-refresh supersedes
    moving a pinned revision id out from under its golden case — the case's
    intent ("the CURRENT revision of this knowledge is retrievable") is exactly
    the chain head, so the harness resolves the pin instead of failing on
    honest churn. Cycle-guarded and depth-capped (a cycle or a >50-hop chain
    returns the last sound id rather than looping); a missing id resolves to
    itself and fails the criterion downstream exactly as before. SCOPE: this
    covers ONLY the explicit-supersede mode — a re-captured richer twin has no
    ``superseded_by`` edge to walk and needs a stable-substr case instead
    (the 858/488 class; see eval/golden.yaml's audit notes).
    """
    head = item_id
    seen = {head}
    with connect() as conn:
        for _ in range(CHAIN_DEPTH_CAP):
            row = conn.execute("SELECT superseded_by FROM items WHERE id = %s", (head,)).fetchone()
            nxt = row[0] if row else None
            if nxt is None or nxt in seen:
                return head
            seen.add(nxt)
            head = int(nxt)
    return head


def _content_exists(expected: list[str]) -> bool:
    """Does ANY corpus row (any status) contain one of the expected substrings?

    Failure triage for expect_substr misses: a rank-miss with matching content
    present is a RETRIEVAL problem; with zero matching rows it is a CORPUS
    problem (target content gone/stale golden case) — a distinction that went
    undiagnosed for a week when the seed corpus silently vanished (2026-07-18:
    six "embedder-independent" golden failures were all absent content; the
    eval read as a hard ~0.76 ceiling until `make seed` restored 1.00/0.90).
    """
    with connect() as conn:
        for sub in expected:
            pattern = f"%{sub}%"
            row = conn.execute(
                "SELECT 1 FROM items WHERE title ILIKE %s OR body ILIKE %s LIMIT 1",
                (pattern, pattern),
            ).fetchone()
            if row is not None:
                return True
    return False


def substr_criterion(
    hits: Sequence, expected: list[str], k: int
) -> tuple[int | None, CriterionResult]:
    """``expect_substr`` verdict + the rank it claims (None on a miss)."""
    rank = _substr_rank(hits, expected)
    if rank:
        detail = f"matched at rank {rank}"
    elif not _content_exists(expected):
        # Corpus problem, not retrieval: nothing in the corpus matches at all.
        detail = (
            f"CONTENT-ABSENT: no corpus row contains any of {expected} (stale case or lost content)"
        )
    else:
        detail = f"no substring of {expected} in top-{k}"
    return rank, CriterionResult("expect_substr", rank is not None, detail)


def item_id_criterion(
    hit_ids: list[int], pinned: int, k: int
) -> tuple[int | None, CriterionResult, bool]:
    """``expect_item_id`` verdict: the CHAIN-RESOLVED pin must be in the top-K.

    Returns (rank, verdict, chain_resolved) — the third flag feeds
    ``CaseResult.chain_resolved`` / the sentinel's ``eval_chain_resolved``.
    """
    want = _chain_head(pinned)
    chain_resolved = want != pinned
    label = f"item {want}"
    if chain_resolved:
        label += f" (chain-resolved {pinned}->{want})"
    rank = hit_ids.index(want) + 1 if want in hit_ids else None
    detail = f"{label} at rank {rank}" if rank else f"{label} not in top-{k}"
    return rank, CriterionResult("expect_item_id", rank is not None, detail), chain_resolved


def item_id_raw_criterion(
    hit_ids: list[int], pinned: int, k: int
) -> tuple[int | None, CriterionResult]:
    """``expect_item_id_raw`` verdict: the item ITSELF in the top-K, unresolved."""
    rank = hit_ids.index(pinned) + 1 if pinned in hit_ids else None
    detail = (
        f"raw item {pinned} at rank {rank}" if rank else f"raw item {pinned} absent from top-{k}"
    )
    return rank, CriterionResult("expect_item_id_raw", rank is not None, detail)


def rank_le_criterion(rank: int | None, bound: int) -> CriterionResult:
    """``expect_rank_le`` verdict over the positive criterion's claimed rank."""
    ok = rank is not None and rank <= bound
    detail = f"rank {rank if rank is not None else 'MISS'} vs bound {bound}"
    return CriterionResult("expect_rank_le", ok, detail)


def forbid_criterion(hit_ids: list[int], bad: int, k: int) -> CriterionResult:
    """``forbid_item_id`` verdict on the RAW id (never chain-resolved)."""
    ok = bad not in hit_ids
    detail = (
        f"item {bad} absent from top-{k}"
        if ok
        else f"forbidden item {bad} surfaced at rank {hit_ids.index(bad) + 1}"
    )
    return CriterionResult("forbid_item_id", ok, detail)
