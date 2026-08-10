"""kbworker.prep — extraction INPUT: window budgets, fitting, job assembly, batching.

Split from ``kbworker.consolidate`` (mechanical extraction, 2026-08-04) — the
ratchet's worst file at 1,276 lines. Everything here is the pure "turn queue
groups into context-sized extraction jobs" machinery; the drain loop that calls
it stays in ``consolidate``, which re-exports these names so existing call sites
and test monkeypatches keep resolving through ``consolidate``'s globals.

Same logger name as the parent on purpose: these lines have always appeared in
journald under ``kbworker.consolidate`` and grep recipes depend on that.
"""

from __future__ import annotations

import logging
from typing import Any

from kbcore import ingest_queue
from kbcore.config import settings
from kbworker import sessions
from kbworker.cohort import _COHORT_STATS

logger = logging.getLogger("kbworker.consolidate")

# --- extraction input budget -------------------------------------------------
# Ollama truncates a chat prompt to num_ctx/2 tokens and keeps only the first
# n_keep (4) tokens of the front, so an oversized job loses the SYSTEM PROMPT and
# the model is handed an instruction-less tail slice. Measured 2026-07-31: a
# 1,034,082-token prompt was cut to 8,194 tokens ("truncating input prompt" x13
# in journald) — SILENTLY, because Ollama returns 200 with a plausible-looking
# empty extraction. Assembled group text is therefore split into windows that
# fit, and :func:`_warn_if_oversized` is the tripwire that keeps this class of
# failure loud if anything ever bypasses the split again.
EXTRACT_PROMPT_OVERHEAD_TOKENS = 1024  # system prompt + JSON schema + output headroom
EXTRACT_BYTES_PER_TOKEN = 3.5  # FIRST-PASS sizing only — enforcement is by real token count
MIN_WINDOW_BYTES = 4_000  # floor, so a mis-set num_ctx cannot produce absurd windows
# cl100k (the vendored counter) is not qwen3's tokenizer. On ordinary prose/code
# the two are close; the margin absorbs the residual mismatch so a window that
# counts as fitting under cl100k cannot overflow the qwen ceiling.
TOKENIZER_SAFETY = 1.15


def window_budget_bytes() -> int:
    """FIRST-PASS byte sizing for windows, derived from num_ctx.

    A byte estimate alone is NOT a guarantee: dense content runs far below the
    assumed bytes/token (measured with the vendored tokenizer: base64 1.23,
    hex 1.47, log tables 1.95, diffs 2.14 — all under 3.5), so a budget-full
    byte window can still decode to 2x the token ceiling. Bytes give cheap,
    line-boundary-friendly splits; :func:`_fit_windows` then ENFORCES the token
    ceiling with a real count and re-splits any window that exceeds it.
    """
    usable = int(settings.kb_llm_num_ctx) // 2 - EXTRACT_PROMPT_OVERHEAD_TOKENS
    return max(MIN_WINDOW_BYTES, int(usable * EXTRACT_BYTES_PER_TOKEN))


def window_budget_tokens() -> int:
    """Tokens ONE extraction call may carry: num_ctx/2 minus prompt overhead.

    num_ctx/2 is Ollama's prompt ceiling (measured: 8,194 tokens at num_ctx
    16384, "truncating input prompt" in journald when exceeded, keep=4 so the
    SYSTEM PROMPT is what truncation drops).
    """
    return max(512, int(settings.kb_llm_num_ctx) // 2 - EXTRACT_PROMPT_OVERHEAD_TOKENS)


def _token_ceiling() -> int:
    """The cl100k count a window must stay under to fit the qwen budget."""
    return max(256, int(window_budget_tokens() / TOKENIZER_SAFETY))


def _fit_windows(text: str) -> list[str]:
    """Split assembled text into windows GUARANTEED to fit one extraction call.

    Two passes. Byte windows first (line-boundary aware, cheap); then every
    window is measured with the real tokenizer, and any that exceeds the ceiling
    — dense content like base64, hex dumps or diff hunks runs as low as 1.2
    bytes/token — is re-split by TOKEN SLICES, which cannot overshoot by
    construction. The verifier reproduced exactly this failure against the
    byte-only version: a "budget-full" 25 kB window decoding to ~20k tokens.
    """
    from kbcore.chunk import _enc, count_tokens

    ceiling = _token_ceiling()
    out: list[str] = []
    for w in sessions._text_windows(text, window_budget_bytes()):
        if not w.strip():
            continue
        if count_tokens(w) <= ceiling:
            out.append(w)
            continue
        toks = _enc.encode(w, disallowed_special=())
        for i in range(0, len(toks), ceiling):
            piece = _enc.decode(toks[i : i + ceiling])
            if piece.strip():
                out.append(piece)
    return out or [""]


def _warn_if_oversized(key: str, text: str, budget_tokens: int | None = None) -> bool:
    """TRIPWIRE: a job that Ollama will truncate must never be silent again.

    Measures REAL tokens (the byte estimate was the bug — dense content runs at
    1.2-2.1 bytes/token and sailed under a byte check while overflowing the
    context). After :func:`_fit_windows` this should never fire; it exists for
    whatever bypasses the split next.
    """
    from kbcore.chunk import count_tokens

    budget = window_budget_tokens() if budget_tokens is None else budget_tokens
    tokens = count_tokens(text)
    if tokens <= budget:
        return False
    logger.warning(
        "TRUNCATION RISK: job %r submits %d tokens (measured, cl100k) against the "
        "%d-token budget from num_ctx=%d — Ollama truncates to num_ctx/2 and the "
        "SYSTEM PROMPT is what it drops first",
        key,
        tokens,
        budget,
        settings.kb_llm_num_ctx,
    )
    return True


def _resume_windows(row: dict[str, Any], fp: str, n_windows: int) -> tuple[set[int], int, int]:
    """``(windows already extracted, saved so far, rejected so far)`` for a group.

    Policy here, validation in :mod:`kbcore.queue_checkpoint`: the kill switch and
    the telemetry are the DRAIN's business, the fingerprint match and the
    malformed-value handling are the store's. Empty on a single-window group
    (nothing to resume TO), and empty whenever the fingerprint says the assembled
    text has moved since the checkpoint was written.
    """
    if not settings.kb_drain_window_checkpoint or n_windows < 2:
        return set(), 0, 0
    done, saved, rejected = ingest_queue.resume_windows(row.get("meta"), fp, n_windows)
    if done:
        # .get(), not []: _assemble_jobs is also called directly (unit tests, an
        # owner-invoked run) where the stats dict was never primed, and a KeyError
        # here would fail a group that is extracting perfectly well.
        for stat, n in (("cohort_groups_resumed", 1), ("cohort_windows_resumed", len(done))):
            _COHORT_STATS[stat] = _COHORT_STATS.get(stat, 0) + n
        logger.warning(
            "resuming group %s: %d of %d window(s) already extracted on an earlier run "
            "(%d item(s) saved so far) — NOT re-extracting them",
            row.get("id"),
            len(done),
            n_windows,
            saved,
        )
    return done, saved, rejected


def _assemble_jobs(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One JOB per CONTEXT-SIZED WINDOW of each coalesced group's assembled text.

    COALESCED BY SESSION. The network path windows RAW JSONL at 900 KB while the
    local path assembles to text first, so the same session arrives as ~42x more
    rows over MCP/REST (measured: 114 MB of JSONL is 2.7 MB of conversation —
    350 rows vs 9 for a 300 MB session). Extracting each raw window separately
    spent the whole 200-row nightly budget on one session and fed the 14B mostly
    JSON scaffolding. Concatenating a session's rows and assembling ONCE restores
    the local path's behaviour. Row order is the hook's byte order (it submits
    sequential offsets), so plain concatenation reconstitutes the delta.

    RE-WINDOWED (2026-08-01), which is what ``ingest_queue.pending_groups``
    already promised and this function did not do: coalescing 350 raw rows into
    one job produced ONE prompt of megabytes, and Ollama silently truncated it to
    num_ctx/2 — dropping the system prompt with it (see
    :func:`window_budget_bytes`). The assembled text is therefore split through
    the same :func:`kbworker.sessions._text_windows` machinery the local flush
    path uses, at a budget derived from num_ctx instead of the queue's 1 MB row
    cap, and each window becomes its own job.

    Every window of a group shares the group's ids, uri, extra_meta and detail —
    only ``key`` (the cohort key, which must stay unique so a batch cannot hand a
    job its neighbour's prefetch) and ``detail.window`` differ. The group's rows
    flip status ONCE, after the LAST window lands (``cohort._drain_job``), so a
    group is still all-or-nothing.

    The cohort phases and the ingest pass share this ONE assembly, so the text a
    judge saw is provably the text stored as the item's ``raw`` (it used to be
    assembled twice, in two places). Per-group isolation as everywhere else in
    the drain: a group that will not assemble is marked failed, never fatal.

    CHECKPOINTED (2026-08-10, owner decision). Windows a previous attempt already
    extracted are SKIPPED — no job is emitted — and their totals are restored into
    the shared state, so the final ``mark_group_extracted`` books the truth across
    every night the group took. Safe because a checkpointed window is one whose
    ``extract_and_ingest`` RETURNED: its items are stored, so re-running it only
    pays the 14B to produce duplicates that dedupe to nothing (Invariant 3). ROW
    FLIPS are untouched — still all-or-nothing — and the skip is only ever trusted
    against a fingerprint of this exact text (:mod:`kbcore.queue_checkpoint`).
    """
    jobs: list[dict[str, Any]] = []
    for group in groups:
        rows = group["rows"]
        row = rows[0]
        ids = [r["id"] for r in rows]
        try:
            meta = row["meta"] or {}
            text = sessions.assemble_text("".join(r["content"] for r in rows), row["kind"])
            windows = _fit_windows(text)
            # Fingerprint the WINDOWS (content + geometry), never the pre-split
            # text: the split depends on num_ctx, so a knob change must
            # invalidate the checkpoint (reviewed-out CRITICAL, wfulfrf33).
            fp = ingest_queue.window_fingerprint(windows)
            resumed, saved, rejected = _resume_windows(row, fp, len(windows))
            # ONE state per group, shared by its window jobs: the queue flip and
            # the failure taxonomy are group-wide, the extraction calls are not.
            state: dict[str, Any] = {
                "ids": ids,
                # A resumed window is COMPLETED work: it decrements `left` and
                # restores its yield, so the group still flips exactly once, when
                # the last OUTSTANDING window lands.
                "left": len(windows) - len(resumed),
                "saved": saved,
                "rejected": rejected,
                "done": False,
                "fp": fp,
                "windows_done": sorted(resumed),
                # A single-window group has nothing to resume to, so checkpointing
                # it would be an UPDATE per drained note for no benefit.
                "checkpoint": bool(settings.kb_drain_window_checkpoint and len(windows) > 1),
            }
            uri = meta.get("source_uri") or f"queue:{row['kind']}:{row['id']}"
            for i, window in enumerate(windows):
                if i in resumed:
                    continue  # extracted on an earlier run; its items are stored
                # Keyed off len(windows), never the emitted count, so a resumed
                # window keeps the same cohort key and detail it had last night.
                key = f"{group['uri']}#w{i}" if len(windows) > 1 else group["uri"]
                _warn_if_oversized(key, window)
                jobs.append(
                    {
                        "key": key,  # cohort key: unique per SUBMISSION
                        "group": state,  # shared: the queue rows flip once, together
                        "ids": ids,
                        "window": i,  # this window's index in the GROUP (checkpoint)
                        "project": row["project"],
                        "uri": uri,
                        "branch": meta.get("branch"),
                        "text": window,
                        # FIX 6 (audit 2026-07-26): a queued session/memory window
                        # anchors no FILE — repo/commit ride the item meta as
                        # provenance instead of a phantom {"path": "."} code_ref.
                        "detail": {
                            "ingest_queue_id": row["id"],
                            "source": row["kind"],
                            **({"coalesced_rows": len(ids)} if len(ids) > 1 else {}),
                            **({"window": i, "windows": len(windows)} if len(windows) > 1 else {}),
                        },
                        "extra_meta": sessions._session_provenance(
                            meta.get("repo"), meta.get("commit")
                        ),
                    }
                )
        except Exception as e:  # noqa: BLE001 — one bad submission must not abort the drain
            logger.exception("assembling ingest_queue group %s failed", ids)
            for rid in ids:
                ingest_queue.mark_failed(rid, f"{type(e).__name__}: {e}")
    return jobs


def _batch_jobs(jobs: list[dict[str, Any]], cohort: int) -> list[list[dict[str, Any]]]:
    """Slice jobs into cohort batches, bounded by BOTH job count and total bytes.

    The count is the throughput bound (per-job cost is dominated by model loading,
    so jobs/hour is what sizes a cohort). The byte budget is purely an OOM GUARD:
    a cohort holds every job's assembled text in memory at once, so one unusually
    large transcript must not be able to make a full-sized cohort exhaust RAM.

    A job larger than the whole budget is still taken, ALONE. Skipping it would
    return an empty batch and the drain would make no progress — one oversized row
    would wedge the queue permanently, which is a worse failure than a big cohort.
    """
    budget = max(1, int(settings.kb_drain_cohort_bytes))
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for job in jobs:
        size = len(job.get("text") or "")
        if current and (len(current) >= cohort or used + size > budget):
            batches.append(current)
            current, used = [], 0
        if not current and size > budget:
            logger.warning(
                "cohort: job %r is %.1f MB, over the %.1f MB budget — taken alone",
                job.get("key"),
                size / 1e6,
                budget / 1e6,
            )
        current.append(job)
        used += size
    if current:
        batches.append(current)
    return batches
