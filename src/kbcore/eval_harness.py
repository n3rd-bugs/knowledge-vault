"""kbcore.eval_harness — golden-set retrieval eval (hit@5 + MRR), shared logic.

Single source for the eval used by BOTH the `make eval` CLI (eval/run_eval.py)
and the nightly regression sentinel (kbworker.eval_sentinel).

Golden case schema (all criteria optional, at least one required; additive):
  q:              the query string (required)
  expect_substr:  [..] — pass if ANY substring appears in title+snippet+body of
                  a top-K hit (legacy criterion, unchanged)
  expect_item_id: N — pass if that item id appears in the top-K. The pin is
                  resolved through items.superseded_by to the LIVE chain head
                  first (cycle-guarded, depth-capped — see
                  eval_criteria._chain_head), so a doc-refresh supersede is
                  telemetry, not a red night. forbid_item_id is deliberately
                  NEVER resolved.
  expect_item_id_raw: N — pass if item N ITSELF appears in the top-K, with NO
                  chain-head resolution (E1, review #8 H5, KB 31857): the
                  transaction-time axis, where a superseded revision is
                  asserted PRESENT under kwargs include_superseded. Owns the
                  case exactly like expect_item_id (rank, MRR, n_positive,
                  expect_rank_le); CaseResult.chain_resolved stays False.
                  A case carrying BOTH id criteria is a ValueError.
  expect_rank_le: R — the positive match's rank must be <= R (catches "still
                  found, but demoted" ranking regressions that hit@K cannot see)
  forbid_item_id: N — FAIL if that id appears in the top-K
  kwargs:         {boost_project, expand, as_of, include_superseded, type, domain,
                  tags} forwarded to search() so those ranking/filter knobs are
                  exercised by the eval (expand is the public tri-state bool mapped
                  to hops exactly like the MCP surface; type/domain/tags map
                  straight through to the same-named filters, letting a case pin a
                  single artefact class; as_of and include_superseded are the two
                  BITEMPORAL axes — valid time and transaction time — so a case can
                  assert a fact as-of an instant or reach a superseded revision;
                  review #8 H5, the E1 prerequisite)

Metrics: hit@5 counts CASES PASSED (all present criteria ok) / all cases. MRR
averages 1/rank over cases carrying a positive criterion (expect_substr,
expect_item_id or expect_item_id_raw) — forbid-only cases never dilute it. For
an all-`expect_substr` set both metrics are bit-identical to the pre-extension
harness.

MRR QUANTUM (review #8, KB 31857): because MRR is a mean of 1/rank over the
positive-criterion cases, ONE case slipping rank 1 -> 2 moves it by exactly
(1 - 1/2) / n_positive = 0.5/n_positive — 52 positive-criterion cases before E1
(2026-09-16, quantum 0.0096), 59 after (61 cases in all; the forbid-only guards
are not in n_positive) — or an equivalent decomposition (two cases slipping
2 -> 3 and 3 -> 4 read the same). Adding positive cases changes the quantum,
so the floor is re-anchored and the WoW baseline reset in the same eval-epoch
commit. The 08-29 step 0.9365 -> 0.9269 was exactly one quantum and coincided
with 661 new summaries + doc-refresh churn (22/22 top-5 ids swapped): corpus
composition, not a ranking regression. A single-quantum move is read PER CASE
at the eval epoch (the per-case diff names the slipped case), never as drift.

GPU courtesy (Invariant 8): this replays REAL searches → bge-m3 embed calls only;
kbcore.search has no extractor path, so the 14B is never invoked here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from kbcore import eval_criteria, rerank, search_expand

# THE ESTABLISHED SEAMS (mechanical splits at this file's 300-line ratchet):
# usage mining moved to kbcore.eval_mine (review #5 Wave F) and the criterion
# verdict builders + chain-head helpers to kbcore.eval_criteria (E1, review #8
# H5). Every caller keeps reaching them here — ``eval_harness.mine_candidates``,
# ``eval_harness._chain_head`` (kbworker.eval_composition, the suite) — same
# pattern as kbworker.tail_steps/digest.
from kbcore.eval_criteria import (  # noqa: F401
    CHAIN_DEPTH_CAP,
    CriterionResult,
    _chain_head,
    _content_exists,
    _substr_rank,
)
from kbcore.eval_mine import mine_candidates  # noqa: F401
from kbcore.items import parse_instant
from kbcore.search import search

# Repo root is two levels up from src/kbcore/eval_harness.py (mirrors config.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = _REPO_ROOT / "eval" / "golden.yaml"
K = 5

# The only search() knobs a golden case may forward. A misspelled key raises
# instead of silently testing nothing (the harness must stay trustworthy).
# type/domain/tags let a case exercise search()'s filters so precision cases can
# pin a query to a single artefact class (e.g. type='decision', tags=['hw:x']).
ALLOWED_CASE_KWARGS = frozenset(
    {"boost_project", "expand", "as_of", "include_superseded", "type", "domain", "tags"}
)
_CRITERIA_KEYS = eval_criteria.CRITERIA_KEYS


@dataclass
class CaseResult:
    """Per-case outcome: MRR rank, overall pass, and every criterion's verdict."""

    query: str
    rank: int | None  # positive-criterion match rank (MRR basis); None if unmatched
    passed: bool  # ALL present criteria ok → feeds hit@5
    criteria: list[CriterionResult] = field(default_factory=list)
    chain_resolved: bool = False  # expect_item_id followed superseded_by to a live head
    rerank: str = "off"  # this case's rerank participation: active | fallback | off (#9a)


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
    if "include_superseded" in raw:
        # Transaction-time axis (review #8 H5): a bitemporal case may reach the
        # superseded revisions default search rightly hides. Coerced to bool so
        # a YAML 'yes'/1 cannot smuggle a non-bool into search().
        kwargs["include_superseded"] = bool(raw["include_superseded"])
    # type/domain/tags map straight through to the identically-named search()
    # filters (no translation, unlike expand→hops / as_of→datetime).
    for key in ("type", "domain", "tags"):
        if key in raw:
            kwargs[key] = raw[key]
    return kwargs


def _evaluate_case(case: dict) -> CaseResult:
    query = case["q"]
    eval_criteria.validate_case(case)  # malformed cases raise BEFORE the search
    # tool=None → NO access_log write. The nightly eval replays the golden
    # queries every consolidation; logging those synthetic reads used to stamp
    # every top-5 golden-hit item 'recently used' one day before archive_stale's
    # 180-day cutoff, permanently exempting it from archiving and growing
    # access_log ~125 rows/night. Eval reads are not usage (review finding).
    hits = search(query, k=K, tool=None, **_search_kwargs(case))
    outcome = rerank.last_outcome()  # per-case: search() notes exactly one outcome per call
    hit_ids = [h.item_id for h in hits]
    criteria: list[CriterionResult] = []
    rank: int | None = None
    chain_resolved = False

    if "expect_substr" in case:
        rank, verdict = eval_criteria.substr_criterion(hits, case["expect_substr"], K)
        criteria.append(verdict)

    # An id criterion is the precise one — it owns the MRR rank over a substr
    # match. validate_case guarantees at most one of the two is present.
    if "expect_item_id" in case:
        rank, verdict, chain_resolved = eval_criteria.item_id_criterion(
            hit_ids, case["expect_item_id"], K
        )
        criteria.append(verdict)
    if "expect_item_id_raw" in case:
        rank, verdict = eval_criteria.item_id_raw_criterion(hit_ids, case["expect_item_id_raw"], K)
        criteria.append(verdict)

    if "expect_rank_le" in case:
        criteria.append(eval_criteria.rank_le_criterion(rank, case["expect_rank_le"]))

    if "forbid_item_id" in case:
        criteria.append(eval_criteria.forbid_criterion(hit_ids, case["forbid_item_id"], K))

    return CaseResult(query, rank, all(c.ok for c in criteria), criteria, chain_resolved, outcome)


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
    n_positive = sum(1 for case in cases if eval_criteria.has_positive(case))
    hit_at_5 = sum(1 for r in results if r.passed) / n if n else 0.0
    reciprocal = sum(1.0 / r.rank for r in results if r.rank is not None)
    mrr = reciprocal / n_positive if n_positive else 0.0
    return results, hit_at_5, mrr
