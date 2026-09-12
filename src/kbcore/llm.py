"""kbcore.llm — Ollama /api/chat extractor client (PLAN.md §7.2, Invariant 8).

The nightly 14B extractor turns a session transcript into a list of grounded
knowledge items. ``keep_alive`` comes from ``settings.kb_llm_keep_alive`` (FINITE
seconds, never -1 — CLAUDE.md rule 8, corrected 2026-07-29). Transport failures →
:class:`ExtractorUnavailable`; malformed JSON is recovered / re-asked / salvaged in
:mod:`kbcore.llm_json` (incident 2026-09-12). Traces stay in logs.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable

# Documented seam: callers/tests resolve llm.parse_model_json / llm.JSON_COUNTERS.
from kbcore.llm_json import JSON_COUNTERS, parse_model_json, recover_items  # noqa: F401

# Extractor sampling: pin ONLY a fixed seed — determinism-for-retry comes from the
# seed, not from greedy decoding. Ollama's seed makes sampling reproducible at
# temperature > 0, so a row re-drained after partial progress (a crash mid-batch
# left N items committed) re-extracts the SAME titles/bodies and content-hash
# dedupe collapses the retry (Invariant 3). temperature=0 was DROPPED: the default
# extractor qwen3:14b runs in thinking mode, whose model card forbids greedy
# decoding (it degrades quality and can loop) — so we leave temperature at the
# model-card default and rely on the seed alone (review finding; KB item 313 was
# benchmarked at default sampling, which this restores).
EXTRACT_SEED = 42


def _draw_seed(*parts: str) -> int:
    """Deterministic per-content sampling seed for the nightly judge paths.

    The SAME inputs reproduce the SAME draw on every nightly re-judge, so a
    compatible-but-similar pair judged 'false' stays false forever instead of
    accumulating a per-night probability of a spurious 'true' (the one-way
    contradiction ratchet — review finding); DIFFERENT pairs get different seeds
    (no shared-RNG bias). Reproducible at temperature > 0 via Ollama's seed, so no
    greedy decoding is needed.
    """
    digest = hashlib.sha256("\x00".join(parts).encode()).digest()
    return EXTRACT_SEED + int.from_bytes(digest[:4], "big")


def _classify_extractor_error(e: httpx.HTTPError) -> Exception:
    """Split an httpx error into connectivity (defer) vs content (bounded) failure.

    Connectivity → :class:`ExtractorUnavailable` (Ollama down/restarting, model not
    pulled/loaded, a 5xx hiccup): the drain leaves the row pending and retries.
    Content/timeout → :class:`ExtractionFailed` (a per-row ReadTimeout on the long
    budget, or a 4xx shaped by this request): the drain bounds it with the attempts
    cap so a poison row can't retry every night forever (queue self-heal taxonomy).
    """
    msg = f"extraction call to {settings.ollama_url} failed: {e.__class__.__name__}: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body = (e.response.text or "").lower()
        model_missing = status == 404 or ("model" in body and "not found" in body)
        if not model_missing and 400 <= status < 500:
            return ExtractionFailed(msg)  # bad request shaped by this row's content
        return ExtractorUnavailable(msg)  # model-not-loaded / 5xx → transient
    if isinstance(e, httpx.TimeoutException) and not isinstance(e, httpx.ConnectTimeout):
        return ExtractionFailed(msg)  # read timeout: THIS row is too heavy — bound it
    return ExtractorUnavailable(msg)  # connect errors and other transport failures


# The 14B can take a while on first-token; generous read timeout, tight connect.
_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


def _keep_alive() -> int:
    """Per-call keep_alive from config (read at call time so tests/env can flip)."""
    return settings.kb_llm_keep_alive


# The prompt demands the grounded schema from §7.2. Every item MUST carry a
# ``quote`` that is a verbatim substring of the transcript — the caller
# re-validates and drops anything that is not (SEC-3 / ARCH-2). We only *ask*
# here; grounding is enforced downstream, never trusted from the model.
SYSTEM_PROMPT = (
    "You extract durable software-engineering knowledge from a chat transcript.\n"
    "Return STRICT JSON only, shaped exactly like:\n"
    '{"items": [{"type": "...", "title": "...", "body": "...", '
    '"quote": "<verbatim substring of the transcript>", "tags": ["lang:...", "hw:..."]}]}\n\n'
    "Rules:\n"
    "- type is one of: decision, note, qa, snippet, gotcha, bug, spec, pattern, "
    "runbook, config.\n"
    "- Capture decisions made, gotchas hit, reusable snippets, and bug/fix pairs. "
    "Skip greetings, tool output, and chit-chat.\n"
    "- quote MUST be copied character-for-character from the transcript (a real "
    "substring). If you cannot ground an item in an exact quote, omit that item.\n"
    "- title is one line; body is self-contained prose.\n"
    "- tags are lowercase, hyphenated, namespaced (lang:, hw:, protocol:, "
    "framework:, tool:, os:).\n"
    '- Return {"items": []} if there is nothing worth keeping.'
)


def extract_items(
    transcript_text: str,
    *,
    model: str | None = None,
    keep_alive: int | None = None,
) -> list[dict[str, Any]]:
    """Call the extractor on a transcript and return the raw item dicts.

    The returned dicts are UNVALIDATED — the caller must verify each ``quote``
    is a verbatim substring of ``transcript_text`` before trusting the item.

    ``model`` / ``keep_alive`` are additive overrides for the pipeline-v2
    ensemble (WP-B2): extra extractors run with ``keep_alive=0`` so only the
    primary stays resident (Invariant 8). Both default to the settings-driven
    behavior — zero change when not passed.
    """
    if not transcript_text.strip():
        return []
    payload: dict[str, Any] = {
        "model": model or settings.kb_extract_model,
        "format": "json",
        "keep_alive": _keep_alive() if keep_alive is None else keep_alive,
        "stream": False,
        # num_ctx MUST be explicit: Ollama's runtime default (4K) silently
        # truncates the transcript and caps output mid-JSON (done_reason=
        # length) — measured grounding collapse, bench 2026-07-18.
        "options": {"seed": EXTRACT_SEED, "num_ctx": settings.kb_llm_num_ctx},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript_text},
        ],
    }

    def ask_again(hint: str) -> str:
        # The ONE re-ask (llm_json.recover_items): same model/keep_alive/num_ctx, seed+1.
        retry = {
            **payload,
            "options": {**payload["options"], "seed": EXTRACT_SEED + 1},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT + hint},
                payload["messages"][1],
            ],
        }
        return _extract_call(retry)

    return recover_items(payload["model"], _extract_call(payload), ask_again)


def _extract_call(payload: dict[str, Any]) -> str:
    """POST one extractor request; return the reply content (unparsed). Transport
    errors split via :func:`_classify_extractor_error`; a reply without a STRING
    ``message.content`` is content-deterministic (attempts-bounded, never deferred)."""
    try:
        resp = httpx.post(f"{settings.ollama_url}/api/chat", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise _classify_extractor_error(e) from e
    try:
        content = resp.json()["message"]["content"]
        if not isinstance(content, str):  # null/number/list: never a raw TypeError
            raise TypeError(f"message.content is {type(content).__name__}")
        return content
    except (KeyError, ValueError, TypeError) as e:
        raise ExtractionFailed(
            f"extractor returned unparseable output: {e.__class__.__name__}: {e}"
        ) from e


def _chat(
    messages: list[dict[str, str]],
    *,
    json_format: bool,
    model: str | None = None,
    keep_alive: int | str | None = None,
    seed: int | None = None,
    think: bool | None = None,
) -> str:
    """POST /api/chat; return the message content.

    ``keep_alive`` defaults to ``settings.kb_llm_keep_alive`` (None sentinel →
    resolved at call time; a FINITE residency, Invariant 8). Callers may override
    it per call.

    ``seed`` (optional) pins Ollama sampling so a re-run reproduces the same draw —
    the nightly judge paths pass a per-content seed so their verdicts stop drifting
    night to night (review finding). None leaves sampling unseeded.

    ``think`` (review #8 H3) is INERT unless set: Ollama's ``think`` key is emitted
    ONLY when it is a real bool, so a caller that does not pass it sends the
    byte-identical request it always did. Never a per-model policy.
    """
    payload: dict[str, Any] = {
        "model": model or settings.kb_extract_model,
        "keep_alive": _keep_alive() if keep_alive is None else keep_alive,
        "stream": False,
        "messages": messages,
    }
    if json_format:
        payload["format"] = "json"
    if think is not None:
        payload["think"] = think
    # Same explicit-num_ctx rule as extract_items: the runtime default truncates
    # long judge/summary inputs and cuts output mid-JSON (bench 2026-07-18).
    payload["options"] = {"num_ctx": settings.kb_llm_num_ctx}
    if seed is not None:
        payload["options"]["seed"] = seed
    try:
        resp = httpx.post(f"{settings.ollama_url}/api/chat", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()["message"]["content"]
    except httpx.HTTPError as e:
        raise ExtractorUnavailable(
            f"extraction call to {settings.ollama_url} failed: {e.__class__.__name__}: {e}"
        ) from e
    except (KeyError, ValueError, TypeError) as e:
        raise ExtractorUnavailable(
            f"extractor returned unusable output: {e.__class__.__name__}: {e}"
        ) from e


def chat(
    messages: list[dict[str, str]],
    *,
    json_format: bool = False,
    model: str | None = None,
    keep_alive: int | str | None = None,
    seed: int | None = None,
    think: bool | None = None,
) -> str:
    """Public single-shot chat (deep search, same-fact judge). Same contract as ``_chat``."""
    return _chat(
        messages,
        json_format=json_format,
        model=model,
        keep_alive=keep_alive,
        seed=seed,
        think=think,
    )


_SUMMARIZE_SYSTEM = (
    "You write a concise, factual summary of what a project knows about one topic, "
    "grounded ONLY in the items provided. Preserve specific identifiers, values, part "
    "numbers, and decisions verbatim; do not speculate or add outside facts. One or two "
    "short paragraphs, no preamble."
)


def summarize_items(topic: str, items: list[dict[str, Any]]) -> str:
    """Grounded synthesis of a topic's source items into a summary body (§7.3 step 2)."""
    if not items:
        return ""
    corpus = "\n\n".join(f"### {it.get('title', '')}\n{it.get('body', '')}".strip() for it in items)
    content = _chat(
        [
            {"role": "system", "content": _SUMMARIZE_SYSTEM},
            {
                "role": "user",
                "content": f"Topic: {topic}\n\nItems:\n{corpus}\n\nWrite the summary.",
            },
        ],
        json_format=False,
    )
    return content.strip()


_EVIDENCE_SYSTEM = (
    "Two knowledge items (A and B) contradict each other. You are given a third "
    "item as evidence. Decide which side the evidence supports. Answer STRICT "
    'JSON only: {"side": "a"} or {"side": "b"} or {"side": "neither"}. '
    'If the evidence is off-topic, ambiguous, or supports both, answer "neither".'
)


def evidence_side(
    a_title: str, a_body: str, b_title: str, b_body: str, c_title: str, c_body: str
) -> str:
    """Which side of a contradiction the candidate item supports: 'a'|'b'|'neither'.
    Mirrors :func:`items_contradict`: an unparseable/unexpected answer degrades to
    'neither' (never raises); network failures surface as :class:`ExtractorUnavailable`."""
    content = _chat(
        [
            {"role": "system", "content": _EVIDENCE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Item A: {a_title}\n{a_body}\n\n"
                    f"Item B: {b_title}\n{b_body}\n\n"
                    f"Evidence: {c_title}\n{c_body}"
                ),
            },
        ],
        json_format=True,
        seed=_draw_seed(a_title, a_body, b_title, b_body, c_title, c_body),
    )
    try:
        data = parse_model_json(content)
    except (ValueError, TypeError):
        return "neither"
    if not isinstance(data, dict):
        return "neither"
    side = str(data.get("side", "")).strip().lower()
    return side if side in ("a", "b") else "neither"


def _pair_judge_kwargs() -> dict[str, Any]:
    """Model + ``think`` for the TWO pair judges ONLY — :func:`items_contradict` and
    ``twin_judge.judge_same_fact`` (review #8 H3). Empty ``KB_PAIR_JUDGE_MODEL`` ⇒ the
    extract model with ``think`` unset: the byte-identical incumbent request."""
    return {"model": settings.pair_judge_model, "think": settings.pair_judge_think}


_CONTRADICT_SYSTEM = (
    "You compare two knowledge items and decide whether they factually CONTRADICT — "
    "state incompatible facts or decisions about the same thing (not merely different "
    'topics). Answer STRICT JSON only: {"contradict": true} or {"contradict": false}. '
    "If you are unsure, answer false."
)


def items_contradict(a_title: str, a_body: str, b_title: str, b_body: str) -> bool:
    """True iff the extractor judges the two items to conflict (§7.3 step 3). Never
    raises on an ambiguous/unparseable answer (False); network failures still surface
    as :class:`ExtractorUnavailable` via ``_chat`` so consolidation logs and continues."""
    content = _chat(
        [
            {"role": "system", "content": _CONTRADICT_SYSTEM},
            {
                "role": "user",
                "content": f"Item A: {a_title}\n{a_body}\n\nItem B: {b_title}\n{b_body}",
            },
        ],
        json_format=True,
        seed=_draw_seed(a_title, a_body, b_title, b_body),
        **_pair_judge_kwargs(),
    )
    try:
        data = parse_model_json(content)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    val = data.get("contradict")
    # Some models emit a stringified bool under format=json; bool("false") is
    # True, so coerce strings explicitly instead of trusting bool() on them.
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    return bool(val)


# --- Factored verifier (pipeline v2 WP-B1, owner decision 2026-07-18) ---------
# The judge sees ONLY the transcript and the item's title/body — never the
# grounding span, never which model produced the item. Factored setups measured
# -77% hallucinated entities; our probe test: a qwen3:14b judge caught 6/6
# fabricated items while approving the real ones (bench 2026-07-18).

_VERIFY_SYSTEM = (
    "You judge whether a knowledge item's claim is supported by a work transcript.\n"
    "You are given the transcript and ONE item (its title and body).\n"
    "Answer STRICT JSON only, shaped exactly like:\n"
    '{"supported": true, "reason": "<one sentence>"}\n'
    "Rules:\n"
    "- supported is true only if the transcript contains evidence for the item's "
    "claim (its identifiers, values, decisions, and causal statements).\n"
    "- unsupported means the transcript does not contain evidence for the claim.\n"
    "- reason is one short sentence."
)


def verify_item(
    title: str,
    body: str,
    transcript: str,
    *,
    judge_model: str | None = None,
    keep_alive: int | None = None,
    think: bool | None = None,
) -> dict[str, Any]:
    """Factored support verdict for one extracted item: does the transcript back it?

    Returns ``{"supported": bool, "reason": str}``. Strict parsing: malformed or
    unparseable output — including a non-bool ``supported`` (the string "true", a
    number) — raises :class:`ExtractionFailed`, NEVER coerced to a truthy verdict;
    the caller isolates the item. Connectivity failures surface as
    :class:`ExtractorUnavailable` via ``_chat``. Deterministic per-content seed
    (:func:`_draw_seed`) so re-judging the same item reproduces the same draw;
    ``judge_model``/``keep_alive``/``think`` default to the settings-driven behavior.
    """
    content = _chat(
        [
            {"role": "system", "content": _VERIFY_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Transcript:\n{transcript}\n\n"
                    f"Item title: {title}\n"
                    f"Item body: {body}\n\n"
                    "Answer with the JSON verdict."
                ),
            },
        ],
        json_format=True,
        model=judge_model,
        keep_alive=keep_alive,
        seed=_draw_seed(title, body),
        think=think,
    )
    try:
        parsed = parse_model_json(content)
    except (ValueError, TypeError) as e:
        raise ExtractionFailed(
            f"verifier returned unparseable output: {e.__class__.__name__}: {e}"
        ) from e
    if not isinstance(parsed, dict) or not isinstance(parsed.get("supported"), bool):
        raise ExtractionFailed("verifier verdict lacks a boolean 'supported' field")
    reason = " ".join(str(parsed.get("reason") or "").split())
    return {"supported": parsed["supported"], "reason": reason}


# --- Review-queue triage (WP-2, owner decision 2026-07-18) --------------------
# The nightly 14B SUGGESTS a disposition per pending unverified extraction; a
# human applies suggestions in bulk. The model never archives anything itself.

TRIAGE_ACTIONS = ("keep", "reject_noise", "reject_duplicate")

_TRIAGE_BODY_CAP = 2000  # chars of item body shown to the judge (short prompt)

_TRIAGE_SYSTEM = (
    "You triage ONE machine-extracted knowledge item that awaits human review. "
    "Answer STRICT JSON only, shaped exactly like:\n"
    '{"action": "keep", "dup_of": null, "rationale": "<one sentence>"}\n'
    "Rules:\n"
    '- action is one of: "keep", "reject_noise", "reject_duplicate".\n'
    "- keep: durable, self-contained engineering knowledge (a decision, gotcha, "
    "bug/fix pair, config, reusable snippet).\n"
    "- reject_noise: chit-chat, transient status, tool output, or content that is "
    "meaningless outside its original session.\n"
    "- reject_duplicate: the SAME knowledge already exists in one of the listed "
    "similar items; set dup_of to that item's integer id. dup_of MUST be one of "
    "the listed ids — if none matches, use keep.\n"
    "- If unsure, answer keep. rationale is one short sentence."
)


def _validate_triage(parsed: Any, similar_ids: set[int]) -> dict[str, Any] | None:
    """Hard-validate a parsed triage verdict; ``None`` = no usable suggestion.

    A stored suggestion later drives a bulk archive (once a HUMAN applies it),
    so nothing loose survives: ``action`` must be in :data:`TRIAGE_ACTIONS`,
    and a reject_duplicate's ``dup_of`` must be one of the ACTUALLY-PRESENTED
    similar ids — a hallucinated id must never mark anything duplicate (returns
    None rather than degrading). ``dup_of`` is meaningful only for
    reject_duplicate and is nulled otherwise.
    """
    if not isinstance(parsed, dict):
        return None
    action = str(parsed.get("action") or "").strip().lower()
    if action not in TRIAGE_ACTIONS:
        return None
    dup_of: int | None = None
    if action == "reject_duplicate":
        raw = parsed.get("dup_of")
        if isinstance(raw, bool) or not isinstance(raw, int):
            try:  # tolerate a stringified int; anything else is unusable
                raw = int(str(raw).strip())
            except (TypeError, ValueError):
                return None
        if raw not in similar_ids:
            return None
        dup_of = raw
    rationale = " ".join(str(parsed.get("rationale") or "").split())[:300]
    return {"action": action, "dup_of": dup_of, "rationale": rationale}


def triage_extraction(
    item: dict[str, Any], similar: list[dict[str, Any]], usage_count: int
) -> dict[str, Any] | None:
    """One triage SUGGESTION for a pending unverified extraction, or ``None``.

    Owner decision 2026-07-18: the LLM only suggests; applying is always an
    explicit human action, so a malformed/hallucinated verdict returns None
    (validated by :func:`_validate_triage`) instead of ever driving an archive.
    ``similar`` entries are ``{id, title, similarity, status}`` dicts — the dup
    candidates offered to the judge. Deterministic per-content seed via
    :func:`_draw_seed`, so re-triaging the same row reproduces the same draw.
    Unparseable output raises :class:`ExtractionFailed` (content-deterministic,
    caller skips the row); connectivity failures surface as
    :class:`ExtractorUnavailable` via ``_chat``.
    """
    title = str(item.get("title") or "")
    body = str(item.get("body") or "")[:_TRIAGE_BODY_CAP]
    tags = ", ".join(t for t in (item.get("tags") or []) if isinstance(t, str)) or "-"
    sim_lines = (
        "\n".join(
            f"- id {s['id']}: {s['title']} (similarity {s['similarity']:.3f}, status {s['status']})"
            for s in similar
        )
        or "(none)"
    )
    # Audit 2026-07-26 FIX 2: identifiers the item cites that resolve to nothing
    # (kbcore.ref_check) — the strongest machine-checkable confabulation signal.
    unresolved = ", ".join(str(r) for r in (item.get("unresolved_refs") or []))
    unresolved_line = (
        f"Identifiers cited by this item that do NOT exist in the repo, the installed "
        f"hooks, or the config: {unresolved}\n\n"
        if unresolved
        else ""
    )
    user = (
        f"Item under review:\n"
        f"type: {item.get('type')}\n"
        f"project: {item.get('project')}\n"
        f"title: {title}\n"
        f"tags: {tags}\n"
        f"body:\n{body}\n\n"
        f"{unresolved_line}"
        f"Times this item was recalled by a user: {usage_count}\n\n"
        f"Similar existing items:\n{sim_lines}\n\n"
        f"Answer with the JSON verdict."
    )
    content = _chat(
        [{"role": "system", "content": _TRIAGE_SYSTEM}, {"role": "user", "content": user}],
        json_format=True,
        seed=_draw_seed(title, body, ",".join(str(s["id"]) for s in similar)),
    )
    try:
        parsed = parse_model_json(content)
    except (ValueError, TypeError) as e:
        raise ExtractionFailed(
            f"triage returned unparseable output: {e.__class__.__name__}: {e}"
        ) from e
    return _validate_triage(parsed, {s["id"] for s in similar})
