"""kbcore.eval_harness — golden-set retrieval eval (hit@5 + MRR), shared logic.

Single source for the eval used by BOTH the `make eval` CLI (eval/run_eval.py)
and the nightly regression sentinel (kbworker.eval_sentinel).

Golden case schema (all criteria optional, at least one required; additive):
  q:              the query string (required)
  expect_substr:  [..] — pass if ANY substring appears in title+snippet+body of
                  a top-K hit (legacy criterion, unchanged)
  expect_item_id: N — pass if that item id appears in the top-K
  expect_rank_le: R — the positive match's rank must be <= R (catches "still
                  found, but demoted" ranking regressions that hit@K cannot see)
  forbid_item_id: N — FAIL if that id appears in the top-K
  kwargs:         {boost_project, expand, as_of} forwarded to search() so those
                  ranking knobs are exercised by the eval (expand is the public
                  tri-state bool mapped to hops exactly like the MCP surface)

Metrics: hit@5 counts CASES PASSED (all present criteria ok) / all cases. MRR
averages 1/rank over cases carrying a positive criterion (expect_substr or
expect_item_id) — forbid-only cases never dilute it. For an all-`expect_substr`
set both metrics are bit-identical to the pre-extension harness.

GPU courtesy (Invariant 8): this replays REAL searches → bge-m3 embed calls only;
kbcore.search has no extractor path, so the 14B is never invoked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from kbcore import search_expand
from kbcore.db import connect
from kbcore.items import get_item, parse_instant
from kbcore.search import search

# Repo root is two levels up from src/kbcore/eval_harness.py (mirrors config.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = _REPO_ROOT / "eval" / "golden.yaml"
K = 5

# The only search() knobs a golden case may forward. A misspelled key raises
# instead of silently testing nothing (the harness must stay trustworthy).
ALLOWED_CASE_KWARGS = frozenset({"boost_project", "expand", "as_of"})
_CRITERIA_KEYS = ("expect_substr", "expect_item_id", "forbid_item_id")


@dataclass
class CriterionResult:
    """One criterion's verdict for one case (name, ok, human-readable detail)."""

    name: str
    ok: bool
    detail: str


@dataclass
class CaseResult:
    """Per-case outcome: MRR rank, overall pass, and every criterion's verdict."""

    query: str
    rank: int | None  # positive-criterion match rank (MRR basis); None if unmatched
    passed: bool  # ALL present criteria ok → feeds hit@5
    criteria: list[CriterionResult] = field(default_factory=list)


def load_golden(golden_path: Path | None = None) -> list[dict]:
    """Parse the golden set. An ABSENT file yields ``[]`` so the sentinel can
    record eval=skipped instead of erroring."""
    path = golden_path or GOLDEN_PATH
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _parse_as_of(value: object) -> datetime:
    """Coerce a YAML ``as_of`` (str, bare date, or datetime) into a datetime.

    yaml.safe_load already turns unquoted ISO dates into date/datetime objects;
    quoted ones stay strings and go through the shared valid-time parser.
    search() normalizes any naive result to UTC (Invariant 7).
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        return parse_instant(value, "kwargs.as_of")
    raise ValueError(f"kwargs.as_of must be an ISO date/timestamp, got {value!r}")


def _search_kwargs(case: dict) -> dict:
    """Validate and translate a case's ``kwargs`` into search() arguments."""
    raw = case.get("kwargs") or {}
    unknown = set(raw) - ALLOWED_CASE_KWARGS
    if unknown:
        raise ValueError(
            f"golden case {case.get('q')!r}: unknown kwargs {sorted(unknown)} "
            f"(allowed: {sorted(ALLOWED_CASE_KWARGS)})"
        )
    kwargs: dict = {}
    if "boost_project" in raw:
        kwargs["boost_project"] = raw["boost_project"]
    if "expand" in raw:
        # Same tri-state mapping as the MCP kb_search surface (bool|None → hops).
        kwargs["hops"] = search_expand.hops_from_expand(raw["expand"])
    if "as_of" in raw:
        kwargs["as_of"] = _parse_as_of(raw["as_of"])
    return kwargs


def _substr_rank(hits, expected: list[str]) -> int | None:
    """Legacy criterion: first rank whose title+snippet+FULL BODY has any substring."""
    for i, hit in enumerate(hits, start=1):
        haystack = f"{hit.title}\n{hit.snippet}\n{get_item(hit.item_id).body}".lower()
        if any(sub.lower() in haystack for sub in expected):
            return i
    return None


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


def _evaluate_case(case: dict) -> CaseResult:
    query = case["q"]
    if not any(k in case for k in _CRITERIA_KEYS):
        raise ValueError(f"golden case {query!r}: needs at least one of {_CRITERIA_KEYS}")
    # tool=None → NO access_log write. The nightly eval replays the golden
    # queries every consolidation; logging those synthetic reads used to stamp
    # every top-5 golden-hit item 'recently used' one day before archive_stale's
    # 180-day cutoff, permanently exempting it from archiving and growing
    # access_log ~125 rows/night. Eval reads are not usage (review finding).
    hits = search(query, k=K, tool=None, **_search_kwargs(case))
    hit_ids = [h.item_id for h in hits]
    criteria: list[CriterionResult] = []
    rank: int | None = None

    if "expect_substr" in case:
        expected = case["expect_substr"]
        rank = _substr_rank(hits, expected)
        if rank:
            detail = f"matched at rank {rank}"
        elif not _content_exists(expected):
            # Corpus problem, not retrieval: nothing in the corpus matches at all.
            detail = (
                f"CONTENT-ABSENT: no corpus row contains any of {expected} "
                "(stale case or lost content)"
            )
        else:
            detail = f"no substring of {expected} in top-{K}"
        criteria.append(CriterionResult("expect_substr", rank is not None, detail))

    if "expect_item_id" in case:
        want = case["expect_item_id"]
        item_rank = hit_ids.index(want) + 1 if want in hit_ids else None
        detail = f"item {want} at rank {item_rank}" if item_rank else f"item {want} not in top-{K}"
        criteria.append(CriterionResult("expect_item_id", item_rank is not None, detail))
        rank = item_rank  # the id criterion is the precise one — it owns the MRR rank

    if "expect_rank_le" in case:
        bound = case["expect_rank_le"]
        if not any(k in case for k in ("expect_substr", "expect_item_id")):
            raise ValueError(f"golden case {query!r}: expect_rank_le needs a positive criterion")
        ok = rank is not None and rank <= bound
        detail = f"rank {rank if rank is not None else 'MISS'} vs bound {bound}"
        criteria.append(CriterionResult("expect_rank_le", ok, detail))

    if "forbid_item_id" in case:
        bad = case["forbid_item_id"]
        ok = bad not in hit_ids
        detail = (
            f"item {bad} absent from top-{K}"
            if ok
            else f"forbidden item {bad} surfaced at rank {hit_ids.index(bad) + 1}"
        )
        criteria.append(CriterionResult("forbid_item_id", ok, detail))

    return CaseResult(query, rank, all(c.ok for c in criteria), criteria)


def evaluate(
    golden: list[dict] | None = None, golden_path: Path | None = None
) -> tuple[list[CaseResult], float, float]:
    """Returns (per-case CaseResult list, hit@5, MRR).

    ``golden`` may be preloaded (the sentinel loads once to tell an empty set
    from a real run); otherwise it is read from ``golden_path`` / GOLDEN_PATH.
    hit@5 = passed cases / all cases. MRR = mean reciprocal rank over cases with
    a positive criterion (identical to the legacy metrics for all-substr sets).
    """
    cases = golden if golden is not None else load_golden(golden_path)
    results = [_evaluate_case(case) for case in cases]
    n = len(results)
    n_positive = sum(1 for case in cases if "expect_substr" in case or "expect_item_id" in case)
    hit_at_5 = sum(1 for r in results if r.passed) / n if n else 0.0
    reciprocal = sum(1.0 / r.rank for r in results if r.rank is not None)
    mrr = reciprocal / n_positive if n_positive else 0.0
    return results, hit_at_5, mrr


# --- usage mining: real search→get confirmations as candidate golden cases ----

_SEARCH_TOOL_LIKE = "%kb_search%"
_GET_TOOL_LIKE = "%kb_get%"


def mine_candidates(window_minutes: int = 15, limit: int = 50) -> list[dict]:
    """Mine access_log for golden-case candidates: a kb_search hit that the user
    then opened via kb_get within ``window_minutes`` is real evidence that
    (query → item) was a good retrieval. READ-ONLY, parameterized; never writes.

    Returns dicts: {q, expect_item_id, title, confirmations, first_searched,
    last_confirmed}, most-confirmed first. Un-followed searches (no later
    kb_get for the same item inside the window) never appear.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.query, s.item_id, i.title,
                   COUNT(DISTINCT g.id) AS confirmations,
                   MIN(s.ts) AS first_searched,
                   MAX(g.ts) AS last_confirmed
            FROM access_log s
            JOIN access_log g
              ON g.item_id = s.item_id
             AND g.tool LIKE %(get_like)s
             AND g.ts > s.ts
             AND g.ts <= s.ts + make_interval(mins => %(window)s)
            JOIN items i ON i.id = s.item_id
            WHERE s.tool LIKE %(search_like)s
              AND s.query IS NOT NULL AND s.query <> ''
            GROUP BY s.query, s.item_id, i.title
            ORDER BY confirmations DESC, last_confirmed DESC
            LIMIT %(limit)s
            """,
            {
                "get_like": _GET_TOOL_LIKE,
                "search_like": _SEARCH_TOOL_LIKE,
                "window": window_minutes,
                "limit": limit,
            },
        ).fetchall()
    return [
        {
            "q": query,
            "expect_item_id": item_id,
            "title": title,
            "confirmations": confirmations,
            "first_searched": first_searched,
            "last_confirmed": last_confirmed,
        }
        for query, item_id, title, confirmations, first_searched, last_confirmed in rows
    ]
