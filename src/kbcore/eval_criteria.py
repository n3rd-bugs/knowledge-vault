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

THE ECHO RULE (E2: pre-registered by review #9, KB 40402; landed at review #10;
the helpers live in :mod:`kbcore.eval_echo`, re-exported here). For
``expect_substr`` ONLY, a hit whose title+snippet+body carries the case's
query (normalised, on word boundaries: :func:`is_echo`) KEEPS ITS SLOT — the
real satisfier below it still reads rank 2 — but never satisfies, and EVERY
such carrier in the top-K is reported (above and below the satisfier). Id
criteria are exempt: a pinned item that quotes its own query is a legitimate
source. A miss whose substrings only echo carriers hold reads CONTENT-ABSENT.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from kbcore import eval_echo
from kbcore.db import connect

# THE ESTABLISHED SEAM (review #10 fix round): the echo rule moved to
# kbcore.eval_echo at this file's ratchet; every name still resolves HERE.
from kbcore.eval_echo import ECHO_MIN_WORDS, _norm, echo_guarded, is_echo  # noqa: F401
from kbcore.items import get_item

# Positive criteria own the case's MRR rank and count toward n_positive.
POSITIVE_KEYS = ("expect_substr", "expect_item_id", "expect_item_id_raw")
CRITERIA_KEYS = (*POSITIVE_KEYS, "forbid_item_id")
CHAIN_DEPTH_CAP = 50  # supersession hops followed before giving up (cycle backstop)


@dataclass
class CriterionResult:
    """One criterion's verdict for one case (name, ok, human-readable detail).

    ``echo_skipped``: EVERY echo carrier in an ``expect_substr`` case's top-K
    (E2 echo rule), in rank order; always empty for the other criteria.
    """

    name: str
    ok: bool
    detail: str
    echo_skipped: list[int] = field(default_factory=list)


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


def _substr_rank(
    hits: Sequence, expected: list[str], query: str | None = None, echoes: list[int] | None = None
) -> int | None:
    """Legacy criterion: first rank whose title+snippet+FULL BODY has any substring.

    With ``query`` the echo rule applies (module docstring): a carrier keeps
    its slot — ``enumerate`` still counts it — but cannot satisfy. With
    ``echoes`` given, EVERY carrier among ``hits`` is appended in rank order:
    the scan runs on past the satisfier (same haystack per hit; the rank is
    unchanged) so carriers BELOW it are named too. A call without ``query`` is
    the pre-E2 criterion byte for byte — it stops at the first match.
    """
    rank: int | None = None
    tail = echoes is not None and isinstance(query, str) and echo_guarded(query)
    for i, hit in enumerate(hits, start=1):
        if rank is not None and not tail:
            break
        haystack = f"{hit.title}\n{hit.snippet}\n{get_item(hit.item_id).body}"
        if is_echo(query, haystack):
            if echoes is not None:
                echoes.append(int(hit.item_id))
            continue
        lowered = haystack.lower()
        if rank is None and any(sub.lower() in lowered for sub in expected):
            rank = i
    return rank


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
            pattern = eval_echo.like_pattern(sub)  # literal: no LIKE wildcard (fix-round H2)
            row = conn.execute(
                "SELECT 1 FROM items WHERE title ILIKE %s ESCAPE '!' OR body ILIKE %s ESCAPE '!' "
                "LIMIT 1",
                (pattern, pattern),
            ).fetchone()
            if row is not None:
                return True
    return False


def _held_by(expected: list[str], query: str | None) -> str:
    """The miss triage: the pre-E2 presence check, or the echo-aware one when armed."""
    if isinstance(query, str) and echo_guarded(query):
        return eval_echo.content_triage(expected, query)
    return "present" if _content_exists(expected) else "absent"


def substr_criterion(
    hits: Sequence, expected: list[str], k: int, query: str | None = None
) -> tuple[int | None, CriterionResult]:
    """``expect_substr`` verdict + the rank it claims (None on a miss).

    ``query`` (the case's ``q``) arms the echo rule; every carrier in the
    top-K rides the verdict as ``echo_skipped`` and its detail as ``echo
    skipped [ids]``, and a miss whose substrings only carriers hold is
    CONTENT-ABSENT ``(only echo carriers hold it)`` (``eval_echo.content_triage``).
    """
    echoes: list[int] = []
    rank = _substr_rank(hits, expected, query, echoes)
    if rank:
        detail = f"matched at rank {rank}"
    elif (held := _held_by(expected, query)) != "present":
        # Corpus problem, not retrieval: nothing in the corpus (bar carriers) matches.
        detail = (
            f"CONTENT-ABSENT: no corpus row contains any of {expected} (stale case or lost content)"
        )
        if held == "echo-only":
            detail += " (only echo carriers hold it)"
    else:
        detail = f"no substring of {expected} in top-{k}"
    if echoes:
        detail += f"; echo skipped {echoes}"
    return rank, CriterionResult("expect_substr", rank is not None, detail, echoes)


def fused_rank(case: dict, fused_ids: Sequence[int]) -> int | None:
    """1-based position of the case's first satisfier in a search's PRE-RERANK order.

    E2 telemetry (``eval_case_fused_rank``, order from ``kbcore.search_trace``)
    under the harness's ownership: the CHAIN HEAD for ``expect_item_id``, the
    id itself for ``expect_item_id_raw``, else a non-echo item whose title/body
    holds an expected substring (one batched read). None: no satisfier fused.
    """
    if "expect_item_id" in case:
        want = {_chain_head(int(case["expect_item_id"]))}
    elif "expect_item_id_raw" in case:
        want = {int(case["expect_item_id_raw"])}
    elif case.get("expect_substr") and fused_ids:
        want = _substr_satisfiers(case, list(fused_ids))
    else:
        return None
    return next((i for i, item_id in enumerate(fused_ids, start=1) if item_id in want), None)


def _substr_satisfiers(case: dict, ids: list[int]) -> set[int]:
    """The ids among ``ids`` that satisfy ``expect_substr`` (title/body, echo rule applied)."""
    expected = [str(s).lower() for s in case["expect_substr"]]
    with connect() as conn:
        rows = conn.execute("SELECT id, title, body FROM items WHERE id = ANY(%s)", (ids,))
        found = rows.fetchall()
    out: set[int] = set()
    for item_id, title, body in found:
        haystack = f"{title}\n{body or ''}"
        lowered = haystack.lower()
        if any(s in lowered for s in expected) and not is_echo(case.get("q"), haystack):
            out.add(int(item_id))
    return out


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
