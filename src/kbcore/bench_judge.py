"""kbcore.bench_judge — READ-ONLY agreement probe for the pair-judge seat (review #8 H3f/H4).

Three ad-hoc benches in two months gated every model adoption, and the 08-30 raw
JSON did not survive the reboot. This is the repeatable version: it re-asks the
UNMODIFIED pair-judge prompts — :func:`kbcore.twin_judge.same_fact_messages` (the
nonce-fenced same-fact question) and :data:`kbcore.llm._CONTRADICT_SYSTEM` — over
pairs whose label is already known (:mod:`kbcore.bench_pairs`: the incumbent's
stored verdicts, or the live ``alternative_to`` edges), once per candidate
``model x think``, and scores agreement, judged-rate, FP/FN and seconds per pair
through the REAL request path (KB 30815: prove thinking-OFF through the actual
API call, never from a benchmark harness).

MECHANICAL READ-ONLY GUARANTEES (bench-gate rule, wave_h_guards):
* every query runs on a connection Postgres holds READ ONLY (``bench_pairs._read_only``);
* neither bench module imports a write chokepoint (``transaction``, ``graph.link``,
  ``ingest``, ``supersede``, ``record_*``, ``put_document``) — ``tests/
  test_bench_judge.py`` walks both ASTs and fails the import if one appears;
* the consolidation advisory lock is PROBED try-and-release and never held
  (:func:`assert_gpu_slot_free`): the key is schema-scoped inside
  ``db.advisory_lock`` (a literal ``pg_try_advisory_lock(42)`` never sees it), and
  HOLDING it would make the nightly skip the whole night.
Archiving results is the CLI's job (``kb bench judge --archive``), behind an
explicit flag, so this module stays pure. Rule 8: explicit user-invoked GPU use,
and only on an idle slot — never beside the nightly or a resume run.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kbcore import llm, scope

# THE SEAM: the pair sources were split into bench_pairs for the ratchet and are
# re-imported here because this module owns the feature — the CLI and the tests
# reach every name as ``bench_judge.<name>``.
from kbcore.bench_pairs import (  # noqa: F401
    BAND_HIGH,
    BAND_LOW,
    KIND_CONTRADICT,
    KIND_SAME_FACT,
    KINDS,
    LINKER_EPOCH,
    SAMPLE_SEED,
    BenchPair,
    _read_only,
    band_negatives,
    edge_pairs,
    file_pairs,
    judged_pairs,
    load_pairs,
)
from kbcore.config import settings
from kbcore.db import advisory_lock
from kbcore.errors import ExtractionFailed, ExtractorUnavailable, InvalidOperation, NotFound
from kbcore.twin_judge import parse_verdict, same_fact_messages

logger = logging.getLogger("kbcore.bench_judge")

THINK_MODES: dict[str, list[bool | None]] = {
    "on": [True],
    "off": [False],
    "both": [True, False],
    "default": [None],  # key omitted — the incumbent's request shape
}


@dataclass(frozen=True)
class Verdict:
    """One candidate answer. ``verdict=None`` is NO usable verdict (unjudged), which is
    a different fact from a negative answer and is scored as judged-rate, not FN."""

    a_id: int
    b_id: int
    kind: str
    label: bool
    verdict: bool | None
    rationale: str
    secs: float


# --- judging (the unmodified prompts, through llm._chat) ---------------------


def _pair_rows(a_id: int, b_id: int) -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """``((project, title, body), (project, title, body))`` for the two items."""
    with _read_only() as conn:
        rows = conn.execute(
            "SELECT id, project, title, body FROM items WHERE id = ANY(%s)", ([a_id, b_id],)
        ).fetchall()
    by_id = {r[0]: (r[1], r[2], r[3]) for r in rows}
    if a_id not in by_id or b_id not in by_id:
        raise NotFound(f"item pair {a_id}/{b_id} not found")
    return by_id[a_id], by_id[b_id]


def contradict_messages(a_title: str, a_body: str, b_title: str, b_body: str) -> list[dict]:
    """``llm.items_contradict``'s exact request (its parity is pinned by test)."""
    return [
        {"role": "system", "content": llm._CONTRADICT_SYSTEM},
        {"role": "user", "content": f"Item A: {a_title}\n{a_body}\n\nItem B: {b_title}\n{b_body}"},
    ]


def contradict_verdict(content: str) -> bool:
    """``llm.items_contradict``'s answer coercion, verbatim (parity pinned by test):
    ambiguous/unparseable ⇒ False; stringified bools coerced explicitly."""
    try:
        data = llm.parse_model_json(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    val = data.get("contradict")
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return bool(val)


def judge_pair(pair: BenchPair, *, model: str, think: bool | None) -> Verdict:
    """Ask ONE candidate config the unmodified question for ``pair``, timed through the
    real request path. Same per-content seed as production (``llm._draw_seed``)."""
    (a_proj, a_title, a_body), (b_proj, b_title, b_body) = _pair_rows(pair.a_id, pair.b_id)
    if pair.kind == KIND_SAME_FACT:
        messages = same_fact_messages(a_proj, a_title, a_body, b_proj, b_title, b_body)
    else:
        messages = contradict_messages(a_title, a_body, b_title, b_body)
    seed = llm._draw_seed(a_title, a_body, b_title, b_body)
    t0 = time.perf_counter()
    try:
        content = llm._chat(messages, json_format=True, model=model, seed=seed, think=think)
    except (ExtractorUnavailable, ExtractionFailed) as e:
        secs = time.perf_counter() - t0
        rationale = f"judge unavailable: {e.__class__.__name__}"
        return Verdict(pair.a_id, pair.b_id, pair.kind, pair.label, None, rationale, secs)
    secs = time.perf_counter() - t0
    if pair.kind == KIND_SAME_FACT:
        same, judged, rationale = parse_verdict(content)
        verdict: bool | None = same if judged else None
    else:
        verdict, rationale = contradict_verdict(content), ""
    return Verdict(pair.a_id, pair.b_id, pair.kind, pair.label, verdict, rationale, secs)


# --- scoring + the run -------------------------------------------------------


def score(verdicts: Sequence[Verdict]) -> dict[str, Any]:
    """Agreement is over JUDGED pairs only; unjudged ones lower ``judged_rate`` instead
    (fail-closed is a refusal, not a wrong answer). ``first_call_s`` carries the cold
    load; ``cold_load_s`` is its excess over the median."""
    n = len(verdicts)
    judged = [v for v in verdicts if v.verdict is not None]
    secs = [v.secs for v in verdicts]
    median = statistics.median(secs) if secs else 0.0
    return {
        "pairs": n,
        "judged": len(judged),
        "judged_rate": (len(judged) / n) if n else 0.0,
        "agreement": (sum(1 for v in judged if v.verdict == v.label) / len(judged))
        if judged
        else 0.0,
        "tp": sum(1 for v in judged if v.verdict and v.label),
        "tn": sum(1 for v in judged if not v.verdict and not v.label),
        "fp": sum(1 for v in judged if v.verdict and not v.label),
        "fn": sum(1 for v in judged if not v.verdict and v.label),
        "s_per_pair_median": median,
        "s_per_pair_mean": (sum(secs) / n) if n else 0.0,
        "total_s": sum(secs),
        "first_call_s": secs[0] if secs else 0.0,
        "cold_load_s": max(0.0, secs[0] - median) if len(secs) > 1 else 0.0,
    }


def think_word(think: bool | None) -> str:
    return "default" if think is None else ("on" if think else "off")


def run_bench(
    models: Sequence[str],
    thinks: Sequence[bool | None],
    pairs: Sequence[BenchPair],
    *,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Judge every pair once per ``model x think``; return the full results document
    (pairs, per-config scores and verdicts) the CLI writes and may archive. OWNER-ONLY:
    the pairs span projects, and the run holds the GPU."""
    scope.require_owner("the pair-judge bench")
    configs: list[dict[str, Any]] = []
    for model in models:
        for think in thinks:
            verdicts: list[Verdict] = []
            for i, pair in enumerate(pairs, 1):
                v = judge_pair(pair, model=model, think=think)
                verdicts.append(v)
                if on_progress is not None:
                    on_progress(f"{model} think={think_word(think)} {i}/{len(pairs)} {v.secs:.1f}s")
            configs.append(
                {
                    "model": model,
                    "think": think,
                    "score": score(verdicts),
                    "verdicts": [asdict(v) for v in verdicts],
                }
            )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "incumbent": settings.kb_extract_model,
        "pair_judge_model": settings.pair_judge_model,
        "contradiction_judge_model": settings.contradiction_judge_model,
        "embed_model": settings.kb_embed_model,
        "pairs": [asdict(p) for p in pairs],
        "configs": configs,
    }


def summary_lines(result: dict[str, Any]) -> list[str]:
    """One line per config — the shape pasted into the enabling commit message."""
    lines = []
    for c in result["configs"]:
        s = c["score"]
        lines.append(
            f"{c['model']:<20} think={think_word(c['think']):<7} "
            f"agreement {s['agreement']:.3f} ({s['judged']}/{s['pairs']} judged)  "
            f"FP {s['fp']} FN {s['fn']}  {s['s_per_pair_median']:.2f} s/pair (median)  "
            f"first call {s['first_call_s']:.1f} s"
        )
    return lines


def write_results(result: dict[str, Any], out_dir: Path) -> Path:
    """``<out_dir>/judge-<UTC stamp>.json`` — the raw evidence the 08-30 bench lost."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"judge-{stamp}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return path


def assert_gpu_slot_free() -> None:
    """Refuse to start while a consolidation run (nightly or resume) holds the advisory
    lock — TRY-AND-RELEASE: the probe is dropped the instant it answers, never held for
    the run (holding it would make the nightly skip the night)."""
    from kbworker.consolidate import ADVISORY_KEY  # lazy: kbcore never imports kbworker at load

    with advisory_lock(ADVISORY_KEY) as acquired:
        if not acquired:
            raise InvalidOperation(
                "a consolidation run holds the advisory lock — the bench never shares the "
                "GPU slot with the nightly or a resume run; retry when it finishes"
            )
    # The with-block released the probe; nothing is held from here on.
