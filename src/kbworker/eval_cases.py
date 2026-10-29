"""kbworker.eval_cases — per-case eval telemetry keyed by QUERY (review #9a EV-4).

E1's per-case diff ('which golden case slipped one quantum') had no instrument:
the harness's ``CaseResult`` list was consumed for ``eval_chain_resolved`` and
discarded, ``eval_top5_item_ids`` / ``eval_satisfier_ids`` are sorted DISTINCT
sets, and the rerank tally is a whole-replay count. So a +1.6-quanta night had
three indistinguishable decompositions and a -1.7 one five.

Two ADDED meta keys per replay, both keyed by the case's query text (never by
golden.yaml index — E1 rewrites the set and a diff by position would lie):

* ``eval_case_ranks``  — ``{query: rank | null}`` for the PRIMARY replay
  (``null`` = the positive criterion did not match in the top-K; a forbid-only
  case has no rank and reads ``null`` too);
* ``eval_case_rerank`` — ``{query: 'active' | 'fallback' | 'off'}``, the
  rerank participation of THAT case's search; and for the composition replay
  (the satisfier keys' producer, which the primary tally never described)
  ``eval_case_rerank_composition`` plus its rolled-up
  ``eval_rerank_tally_composition``.

Golden queries are not unique (five texts repeat today: the same question asked
with different criteria), so the second and later cases of one query text key
as ``'<query> #2'``, ``'<query> #3'`` … in golden order — the bare text stays
the first case's key, which is what a query-keyed diff wants. Pure functions
(bar :func:`replay_keys`' one fingerprint embed and its two identity GETs);
shape-tolerant so a stubbed ``evaluate()`` returning bare tuples costs nothing.

E2 (review #10) — :func:`replay_keys` adds, keyed EXACTLY like
``eval_case_ranks`` and minted in golden order (``kbworker.eval_persistence``
reads a case's 1-based index off that order): ``eval_case_passed`` (the full
verdict), ``eval_case_top5``, ``eval_case_satisfier`` (the hit at the claimed
rank), ``eval_case_fused_rank`` / ``eval_case_n_fused`` (the PRE-RERANK
order, kbcore.search_trace), ``eval_case_above_satisfier`` (``[id, type,
trust]`` per hit above it — review #9's summary-above-satisfier, widened) and
``eval_echo_skips`` (only cases with skips) and ``eval_case_fused_error`` (the
cases whose fused-rank read raised: their fused keys are OMITTED, never a null
that reads as "no satisfier fused"). Every dict stays in report META
(run_report writes scalars only to the body); the scalars it adds —
``eval_echo_skipped``, ``eval_embed_host`` (the serving RUNS, comma-joined),
``eval_embed_fingerprint`` and the probe's own ``eval_embed_fingerprint_host``
/ ``eval_embed_runtime`` (kbcore.embed_identity) / ``eval_embed_query_prefix_sha``
/ ``eval_embed_device`` — carry no query text. They stay out of the indexed
body too, like every scalar E2 added (kbcore.report_meta: their words are
search vocabulary a nightly report would keep adding), so read them from meta.
With rerank not 'active' a fused-vs-final gap may be an RRF tie artefact, and
with kb_collapse_alternatives on a folded twin shifts it on any case
(kbcore.eval_harness._telemetry).

QUERY-KEYED SINKS (review #10 fix F16): every dict keyed by golden query text
(:data:`QUERY_KEYED`, minted with :func:`case_key`) lives in report META only —
:func:`loggable` drops them from what reaches journald (the "consolidation
done" line, the one-shot's stdout) and from the on-demand result stored in
``consolidation_requests`` (kb_consolidate / the CLI / GET /api/consolidate).
:func:`replay_telemetry` is the sentinel's GUARDED fold: a broken producer
costs its own keys (``eval_replay_telemetry_status='error'``), never the
detector that runs right after it.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

logger = logging.getLogger("kbworker.eval_sentinel")
RERANK_OUTCOMES = ("active", "fallback", "off")
EMBED_HOST_RUNS_MAX = 6  # serving runs eval_embed_host names before a '+N' suffix
# The per-case dicts replay_keys mints (in this order), each {case_key: value}.
CASE_DICTS = (
    "eval_case_passed",
    "eval_case_top5",
    "eval_case_satisfier",
    "eval_case_fused_rank",
    "eval_case_n_fused",
    "eval_case_above_satisfier",
    "eval_echo_skips",
)
# EVERY counts key whose value carries golden query text (a case_key-keyed dict, or
# eval_case_fused_error's list of case keys) — META only, never a log line or a
# stored job result (:func:`loggable`). A new case_key producer MUST be listed here
# (tests/test_eval_loggable.py fails otherwise).
QUERY_KEYED = (
    "eval_case_ranks",
    "eval_case_rerank",
    *CASE_DICTS,
    "eval_case_fused_error",
    "eval_case_rerank_composition",  # kbworker.eval_composition
)


def case_key(query: str, seen: dict[str, Any]) -> str:
    """The key for ``query`` given the keys already taken: bare, then ``' #2'``, …"""
    if query not in seen:
        return query
    n = 2
    while f"{query} #{n}" in seen:
        n += 1
    return f"{query} #{n}"


def per_case_keys(results: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """``{'eval_case_ranks': {...}, 'eval_case_rerank': {...}}`` from CaseResults.

    Reads ``query`` / ``rank`` / ``rerank`` by attribute; an element without a
    string ``query`` (a stub tuple) is skipped, an unknown rerank value reads
    as ``'off'`` (an older harness that never noted one).
    """
    ranks: dict[str, int | None] = {}
    outcomes: dict[str, str] = {}
    for r in results:
        query = getattr(r, "query", None)
        if not isinstance(query, str):
            continue
        key = case_key(query, ranks)
        rank = getattr(r, "rank", None)
        ranks[key] = rank if isinstance(rank, int) and not isinstance(rank, bool) else None
        outcome = getattr(r, "rerank", None)
        outcomes[key] = outcome if outcome in RERANK_OUTCOMES else "off"
    return {"eval_case_ranks": ranks, "eval_case_rerank": outcomes}


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def loggable(counts: Mapping[str, Any]) -> dict[str, Any]:
    """``counts`` minus every :data:`QUERY_KEYED` key — what a log line or a stored
    job result may carry (module docstring). The report meta keeps everything."""
    return {k: v for k, v in counts.items() if k not in QUERY_KEYED}


def replay_telemetry(counts: dict[str, Any], results: Sequence[Any]) -> None:
    """Fold the primary replay's per-case keys into ``counts`` — each producer GUARDED.

    The regression detector runs right after this and reads ``eval_case_passed``,
    so a raising fold may never cost the measurement: the failed producer's keys
    are absent, the error is logged and ``eval_replay_telemetry_status`` reads
    ``'error'`` (scalar; absent on a clean fold).
    """
    for produce in (per_case_keys, replay_keys):
        try:
            counts.update(produce(results))
        except Exception:  # noqa: BLE001 — telemetry may never cost the measurement
            logger.warning(
                "replay telemetry %s failed; the detector still runs",
                produce.__name__,
                exc_info=True,
            )
            counts["eval_replay_telemetry_status"] = "error"


def _host_runs(runs: list[str]) -> str:
    """The serving runs, comma-joined; past :data:`EMBED_HOST_RUNS_MAX` a ``+N`` closes it."""
    shown = runs[:EMBED_HOST_RUNS_MAX]
    extra = len(runs) - len(shown)
    return ",".join(shown + ([f"+{extra}"] if extra else []))


def replay_keys(results: Iterable[Any]) -> dict[str, Any]:
    """The E2 per-case dicts + the replay's embed identity (module docstring).

    Reads the CaseResult telemetry fields by attribute (an element without a
    string ``query`` is skipped, a missing field reads empty). The fused keys
    appear only for a case whose search was traced and whose fused read did not
    raise (that case is listed in ``eval_case_fused_error`` instead).
    ``eval_embed_host`` is the serving hosts as RUNS in serving order — a host
    is appended when it differs from the last one (A, B, A reads ``A,B,A``; one
    host on a healthy night) — at most :data:`EMBED_HOST_RUNS_MAX`, then ``+N``;
    absent when no host was recorded.
    """
    out: dict[str, dict[str, Any]] = {name: {} for name in CASE_DICTS}
    runs: list[str] = []
    fused_error: list[str] = []
    for r in results:
        query = getattr(r, "query", None)
        if not isinstance(query, str):
            continue
        key = case_key(query, out["eval_case_passed"])
        rank = _int(getattr(r, "rank", None))
        ids = [int(i) for i in getattr(r, "top_ids", None) or []]
        out["eval_case_passed"][key] = bool(getattr(r, "passed", False))
        out["eval_case_top5"][key] = ids
        out["eval_case_satisfier"][key] = ids[rank - 1] if rank and 0 < rank <= len(ids) else None
        out["eval_case_above_satisfier"][key] = [list(a) for a in getattr(r, "above", None) or []]
        if (n_fused := _int(getattr(r, "n_fused", None))) is not None:
            out["eval_case_n_fused"][key] = n_fused
            out["eval_case_fused_rank"][key] = _int(getattr(r, "fused_rank", None))
        if getattr(r, "fused_error", False) is True:
            fused_error.append(key)
        if skipped := [int(i) for i in getattr(r, "echo_skipped", None) or []]:
            out["eval_echo_skips"][key] = skipped
        host = getattr(r, "embed_host", None)
        if isinstance(host, str) and host and (not runs or runs[-1] != host):
            runs.append(host)
    keys: dict[str, Any] = dict(out)
    keys["eval_echo_skipped"] = sum(len(v) for v in out["eval_echo_skips"].values())
    if fused_error:
        keys["eval_case_fused_error"] = fused_error
    if runs:
        keys["eval_embed_host"] = _host_runs(runs)
    keys.update(_fingerprint())
    return keys


def _fingerprint() -> dict[str, str]:
    """The probe's identity — ONE query embed; ``{}`` when that embed fails.

    ``eval_embed_fingerprint`` plus, when the serving host is known (a test
    double records none), ``eval_embed_fingerprint_host`` (the credential-free
    host that served THIS probe, which need not be the replay's) and
    ``eval_embed_runtime`` (kbcore.embed_identity, absent when unreadable).
    Beside them the probe's CONFIGURED inputs the epoch key never sees
    (fix-round review H4): ``eval_embed_query_prefix_sha`` (12-hex sha256 of
    the query prefix — never its text) and ``eval_embed_device``.
    """
    try:
        from kbcore import embed, embed_identity
        from kbcore.config import settings

        keys = {"eval_embed_fingerprint": embed.query_fingerprint()}
        host = embed.last_query_host()
        prefix = settings.kb_embed_query_prefix.encode("utf-8")
        keys["eval_embed_query_prefix_sha"] = hashlib.sha256(prefix).hexdigest()[:12]
        keys["eval_embed_device"] = str(settings.kb_embed_device)
    except Exception:  # noqa: BLE001 — identity telemetry may never cost the eval
        logger.warning("embed fingerprint unavailable tonight; the eval stands", exc_info=True)
        return {}
    if host:
        keys["eval_embed_fingerprint_host"] = host
        if (runtime := embed_identity.runtime(host)) is not None:
            keys["eval_embed_runtime"] = runtime
    return keys


def rerank_tally(outcomes: dict[str, str]) -> dict[str, int]:
    """``{'active', 'fallback', 'off'}`` counts over a per-case outcome map."""
    tally = dict.fromkeys(RERANK_OUTCOMES, 0)
    for outcome in outcomes.values():
        tally[outcome if outcome in RERANK_OUTCOMES else "off"] += 1
    return tally
