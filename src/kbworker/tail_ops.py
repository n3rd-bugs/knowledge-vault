"""kbworker.tail_ops — the LLM-tail consolidation steps and the stand-down log.

Split from ``kbworker.consolidate`` (mechanical extraction, 2026-08-04). These
are the per-project tail steps (dedupe, summaries, contradictions, doc-refresh)
plus the shared stop bookkeeping (``_STOP_LOG`` / ``_note_stop``) they and the
drain both write to. ``consolidate`` re-exports every name here so existing call
sites, tests, and ``tail_steps``'s lazy ``consolidate._note_stop`` wrapper keep
working; the dicts are shared BY REFERENCE and mutated in place, never rebound.

The evidence step and its patched cap (``EVIDENCE_PAIR_CAP``) stay in
``consolidate`` — tests monkeypatch the cap on that module and the read must
keep resolving there. Same logger name as the parent on purpose.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from kbcore import consolidation, llm
from kbcore.config import settings
from kbcore.review import close_contradiction_rows_for
from kbcore.supersede import same_uri_active_pairs, supersede
from kbworker import summary_stamps

logger = logging.getLogger("kbworker.consolidate")

CONTRA_LOW, CONTRA_HIGH = 0.80, 0.95

# Contradiction pairs judged this run, across projects — the budget is shared
# (settings.kb_contradiction_pair_cap) and PER RUN, not per night (a resumed fragment
# gets a fresh allotment; day-keyed caps are Wave I), so it has to be counted somewhere
# both _contradictions and the report can see. Reset per run, like _COHORT_STATS.
_CONTRA_STATS: dict[str, int] = {"judged": 0}

# "deadline" / "battery" / None — see ``consolidate.should_stop``.
StopCheck = Callable[[], "str | None"]

# How often a per-item loop asks ``consolidate.should_stop``. The answer is
# cached (_STOP_CACHE_SECONDS) so the cost is a modulo, not a SQLite read; the
# bound that matters is the OVERSHOOT — at most this many more LLM calls after
# the grid drops or the deadline passes.
STOP_CHECK_EVERY = 5

# Why each step stood down, and how much it left behind. Persisted in the run
# report (counts["stopped"]) — a run that quietly did a third of its work is
# indistinguishable from a broken one unless it SAYS why it stopped.
_STOP_LOG: dict[str, dict[str, Any]] = {}


def _note_stop(step: str, reason: str, deferred: int) -> None:
    """Record + log that ``step`` stood down, and how much it deferred.

    The wording is deliberate (review #6): the old "nothing is lost" was FALSE
    for the summaries step while its need was judged against a run mark that
    ``_write_report`` advanced unconditionally — a stood-down step's unreached
    tags were silently dropped. Every step's remaining work is now re-derived
    from the queue/data on the next eligible run, and the log claims exactly
    that, no more.

    THE NUMBER IS THE STEP'S HONEST REMAINING WORK, in the step's own unit
    (review #8): for the drain, still-pending queue ROWS that already existed at
    run start (H6 — a submission landing mid-run is not 'deferred'); for
    summaries, tags this run would actually have WRITTEN (H8 — the all-flagged
    residue and stamped no-yield tags are excluded); for contradictions and
    evidence, candidate pairs. Callers pass exactly what the next eligible run
    will find waiting, so the pause marker, the report and the digest agree.
    """
    prior = _STOP_LOG.get(step, {}).get("deferred", 0)
    _STOP_LOG[step] = {"reason": reason, "deferred": prior + deferred}
    logger.warning(
        "step %r pausing (%s): %d unit(s) of remaining work deferred — the next "
        "eligible run re-derives its remaining work and resumes here",
        step,
        reason,
        deferred,
    )


def _canonical_clusters(pairs: list[tuple[int, int, float]]) -> list[set[int]]:
    """Union-find the dedupe pairs into clusters of mutually-similar items."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for a, b, _ in pairs:
        union(a, b)
    clusters: dict[int, set[int]] = {}
    for node in parent:
        clusters.setdefault(find(node), set()).add(node)
    return [c for c in clusters.values() if len(c) > 1]


def _dedupe(project: str) -> int:
    """Step 1: collapse near-dupes to one canonical (oldest verified, else oldest)."""
    pairs = consolidation.duplicate_pairs(project, settings.kb_dup_threshold)
    marked = 0
    for cluster in _canonical_clusters(pairs):
        ranks = consolidation.item_ranks(list(cluster))
        canonical = min(cluster, key=lambda i: ranks[i])
        for dup in cluster:
            if dup != canonical and consolidation.mark_duplicate(dup, canonical):
                marked += 1
    return marked


def _summaries(since: datetime | None, stop: StopCheck | None = None) -> int:
    """Step 2: (re)write topic summaries for tags with >=3 items past their own summary.

    ``stop`` is the run's stand-down guard (``consolidate.should_stop``), asked
    every :data:`STOP_CHECK_EVERY` tags because each tag is a summarizer call.
    Tags not reached are simply re-selected next run — LOSSLESSLY, since review
    #6: need is derived per tag from the DATA (items newer than the tag's own
    newest summary, :func:`kbcore.consolidation.tags_needing_summary`), not from
    the run mark ``_write_report`` used to advance even over a stood-down step.
    ``since`` rides along for the call/patch surface only.

    TODO == WRITABLE WORK (review #8 H8). The selection applies the same content
    exclusion the feeding query does (H8a, in SQL), and a tag a prior attempt
    could not summarize — too few sources, or an EMPTY body from the model — is
    stamped (:mod:`kbworker.summary_stamps`) and skipped until new items arrive
    for it (H8b). So the 'deferred' number a stand-down reports is tags this run
    would actually have written, never a residue re-tried nightly, and a no-yield
    tag costs exactly one summarizer call per change in its feeding set.
    """
    written = 0
    stamps = summary_stamps.load()
    selected = consolidation.tags_needing_summary(since)
    todo = [t for t in selected if not summary_stamps.skip(stamps, *t)]
    if len(selected) != len(todo):
        logger.info(
            "summaries: %d stamped tag(s) skipped — a prior attempt yielded nothing and "
            "no new item has arrived for them since",
            len(selected) - len(todo),
        )
    for n, (project, tag, new_count) in enumerate(todo):
        if stop is not None and n % STOP_CHECK_EVERY == 0 and (reason := stop()):
            _note_stop("summaries_written", reason, len(todo) - n)
            break
        sources = consolidation.active_items_with_tag(project, tag)
        if len(sources) < 3:
            summary_stamps.record(stamps, project, tag, new_count, "too_few_sources")
            continue
        body = llm.summarize_items(tag, sources)
        if not body.strip():
            summary_stamps.record(stamps, project, tag, new_count, "empty_body")
            continue
        consolidation.create_summary(project, tag, body, [s["id"] for s in sources])
        summary_stamps.clear(stamps, project, tag)
        written += 1
    return written


def _contradictions(project: str, stop: StopCheck | None = None, cap: int | None = None) -> int:
    """Step 3: judge near-but-not-dup pairs; queue confirmed conflicts (never auto-supersede).

    One LLM call per pair, so this is one of the three tail steps that used to
    run past the deadline unbounded: ``stop`` is asked every
    :data:`STOP_CHECK_EVERY` pairs and ``cap`` bounds the pairs judged (the
    caller's REMAINING share of the per-RUN budget). Stopping loses nothing — a
    pair that is still near-but-not-dup is re-offered by the same query next run,
    and ``record_contradiction`` is idempotent for the ones already judged.

    Every verdict — overwhelmingly "no contradiction" — is remembered in
    ``contradiction_judgements`` so the next night does not re-pay for it
    (:func:`kbcore.consolidation.record_pair_judgement`; ~90% of this step's cost
    was re-judged negatives). It is recorded AFTER the contradiction is queued, so
    a kill between the two loses the memory rather than the finding: the pair is
    simply judged once more next run.
    """
    queued = 0
    pairs = consolidation.contradiction_pairs(project, CONTRA_LOW, CONTRA_HIGH, limit=cap)
    for n, (a_id, b_id, _sim) in enumerate(pairs):
        if stop is not None and n % STOP_CHECK_EVERY == 0 and (reason := stop()):
            _note_stop("contradictions_queued", reason, len(pairs) - n)
            break
        (a_title, a_body), (b_title, b_body) = consolidation.item_pair_texts(a_id, b_id)
        verdict = llm.items_contradict(a_title, a_body, b_title, b_body)
        if verdict and consolidation.record_contradiction(a_id, b_id):
            queued += 1
        try:
            consolidation.record_pair_judgement(a_id, b_id, verdict)
        except Exception:  # noqa: BLE001 — the memory is an optimisation, never a gate
            # Same rule as the cohort path: an optimisation that can fail the
            # step is not one. A pair whose memory did not land is simply judged
            # again next run — the cost of the old behaviour, not a wrong answer.
            logger.warning("recording the judgement for pair %s/%s failed", a_id, b_id)
        _CONTRA_STATS["judged"] += 1
    return queued


def _contradiction_pair_cap(now: datetime | None = None) -> int:
    """Tonight's contradiction-pair budget — day-aware (review #6), the sibling of
    ``consolidate._evidence_pair_cap`` for the step before it.

    Unbounded days (``kbcore.drainsched.is_unbounded_day`` — the single "box is
    free tonight" definition) keep the full ``kb_contradiction_pair_cap`` (500,
    the catch-up bound); deadline days bind at ``min(cap,
    kb_contradiction_pair_cap_weekday)`` — measured 129-153 s/pair, so the
    154-pair nights spent ~21 binding minutes past what 40 covers, growing with
    ingest. Convergence-safe: judged-pair memory persists across nights
    (migration 017) and the candidate query is sim-DESC, so a capped night
    spends its budget on the closest pairs and never re-pays judged ones. This
    cap is also what bounds the re-judging wave after any embedder swap. ``min``
    keeps BOTH kill switches (0 in the main knob kills everywhere; 0 in the
    weekday knob is weekday-only). ``now`` is for day-pinned tests.
    """
    from kbcore.drainsched import is_unbounded_day

    cap = int(settings.kb_contradiction_pair_cap)
    if is_unbounded_day(now):
        return cap
    return min(cap, int(settings.kb_contradiction_pair_cap_weekday))


def _contradictions_all(
    projects: list[str], stop: StopCheck | None = None, cap: int | None = None
) -> int:
    """Step 3 across every project under ONE shared per-RUN pair budget.

    The budget is per RUN (a resumed fragment gets a fresh allotment; day-keyed
    caps are Wave I), not per project, so a single large project cannot
    consume the whole GPU window; projects are taken in order and whatever is not
    reached is offered again next run (nothing is written, nothing is lost).
    The default budget is day-aware (:func:`_contradiction_pair_cap`); an
    explicit ``cap`` overrides it, and ``cap`` of 0 is the kill switch — no pair
    is judged at all.
    """
    _CONTRA_STATS["judged"] = 0
    budget = _contradiction_pair_cap() if cap is None else int(cap)
    queued = 0
    for project in projects:
        remaining = budget - _CONTRA_STATS["judged"]
        if remaining <= 0:
            logger.info("contradiction judging hit the per-run cap of %d pairs", budget)
            break
        queued += _contradictions(project, stop, remaining)
    return queued


def _doc_refresh(projects: list[str]) -> int:
    """Step 0b: retire stale doc revisions + close their moot contradiction rows.

    Backlog counterpart of the scan-time hook (supersede_doc_refresh): any
    same-uri active pairs that predate the hook get resolved here — BEFORE
    dedupe (which would mislabel a revision pair 'duplicate') and before the
    contradiction scan wastes LLM calls on them. Per-pair isolation.
    """
    resolved = 0
    for project in projects:
        for stale, newest in same_uri_active_pairs(project):
            try:
                supersede(stale, new_item_id=newest, reason="auto: newer revision of same document")
                close_contradiction_rows_for(stale, newest)
                resolved += 1
            except Exception:  # noqa: BLE001 — one bad pair must not abort the sweep
                logger.exception("doc-refresh sweep failed for %s -> %s", stale, newest)
    return resolved
