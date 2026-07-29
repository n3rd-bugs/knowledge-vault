"""kbcore.embed — Ollama /api/embed batch embeddings (PLAN.md §5.1, OPS-1).

GPU is a guest: every call sets a short keep_alive so bge-m3 unloads quickly
after use (Invariant 8). ``KB_EMBED_DEVICE=cpu`` forces CPU as an escape hatch.
"""

from __future__ import annotations

import logging

import httpx

from kbcore.config import settings
from kbcore.errors import EmbedderUnavailable, ExtractionFailed

# Short keep-alive per OPS-1 — embedder loads on demand and unloads fast.
logger = logging.getLogger("kbcore.embed")

KEEP_ALIVE = "2m"
# The DRAIN budget. 120s used to be the ceiling here while every LLM call got
# 600s, and Ollama's own model-load budget is 300s — so the client gave up while
# the server was still legitimately loading, and the drain recorded a failure for
# a model that was about to be ready. Matching llm.py removes that mismatch.
_TIMEOUT = httpx.Timeout(600.0, connect=10.0)
# The INTERACTIVE budget. A user's kb_search must never inherit the drain's
# patience: a wedged embedder blocking a prompt for ten minutes is a worse
# failure than returning nothing. embed_query is the seam between the two, so
# the split lives there rather than in a global.
_QUERY_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _classify_embed_error(e: httpx.HTTPError, host: str | None = None) -> Exception:
    """Connectivity (defer) vs content (bounded) for an embed HTTP failure.

    A 4xx shaped by a pathological chunk is deterministic → :class:`ExtractionFailed`
    (the queue drain bounds it via the attempts cap). Everything else — connect
    errors, timeouts, model-not-loaded, 5xx — is transient →
    :class:`EmbedderUnavailable`, deferred and retried, with the deferral cap as
    the escape hatch for a "transient" that never heals.
    """
    # Name the host that ACTUALLY failed. This used to hardcode settings.embed_url,
    # so a fallback failure reported the remote address and made a local problem
    # look like a network one — an hour of misdiagnosis waiting to happen.
    msg = (
        f"embedding call to {host or settings.embed_url} failed: "
        f"{e.__class__.__name__}: {e}"
    )
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body = (e.response.text or "").lower()
        model_missing = status == 404 or ("model" in body and "not found" in body)
        if not model_missing and 400 <= status < 500:
            return ExtractionFailed(msg)
        return EmbedderUnavailable(msg)
    # A ReadTimeout used to be classified as ExtractionFailed on the theory that
    # it is "shaped by a pathological chunk" and therefore deterministic. That
    # holds when ONE chunk is pathological. It does not hold when the embedder
    # itself is starved: a drain hit 28 rows across three projects with this
    # identical error while the GPU sat at 0% and VRAM was full, and every one
    # was marked permanently failed after burning its attempts budget — for a
    # condition that would have cleared on its own.
    #
    # Treat it as transient. That is right for BOTH causes, because the deferral
    # counter already distinguishes them: an environmental stall heals and the
    # row drains later, while a genuinely pathological chunk keeps timing out and
    # mark_deferred escalates it to 'failed' at DEFERRAL_CAP. Deferring costs a
    # retry; misclassifying costs the row.
    return EmbedderUnavailable(msg)


def embed_texts(texts: list[str], *, timeout: httpx.Timeout | None = None) -> list[list[float]]:
    """Embed a batch of texts. Returns one ``KB_EMBED_DIM`` vector per text.

    ``timeout`` defaults to the drain budget; the read path passes the shorter
    interactive one so a stalled embedder cannot hold a user's prompt open.
    """
    if not texts:
        return []
    payload: dict = {
        "model": settings.kb_embed_model,
        "input": texts,
        "keep_alive": KEEP_ALIVE,
    }
    if settings.kb_embed_device == "cpu":
        payload["options"] = {"num_gpu": 0}
    # The embed host may be a SECOND machine (KB_EMBED_URL) that is not always
    # ours — the RTX box is a gaming desktop by day. So a remote embedder is a
    # PREFERENCE, never a dependency: if it cannot be reached we fall back to the
    # main host, which is slower under VRAM contention but always there. Only
    # CONNECTIVITY falls back; a 4xx is about the content and would fail
    # identically anywhere, so retrying it elsewhere just doubles the cost.
    hosts = [settings.embed_url]
    if settings.embed_url != settings.ollama_url.rstrip("/"):
        hosts.append(settings.ollama_url.rstrip("/"))
    resp = None
    for i, host in enumerate(hosts):
        try:
            resp = httpx.post(f"{host}/api/embed", json=payload, timeout=timeout or _TIMEOUT)
            resp.raise_for_status()
            if i:
                # Observable on purpose: a silent fallback would hide the remote
                # box being gone, which is the whole reason we made it observable.
                logger.warning("embed host %s unreachable; served by %s", hosts[0], host)
            break
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # Cannot REACH this host — try the next one. The last one re-raises.
            #
            # TimeoutException, not just ConnectError: a machine that is powered
            # off or busy gaming DROPS packets rather than refusing them, so it
            # presents as a ReadTimeout, not a refused connection. Failing over
            # only on ConnectError meant the fallback never fired in the exact
            # scenario it was written for — verified against a dead LAN address,
            # which timed out at 48s and raised instead of falling back.
            if i == len(hosts) - 1:
                raise _classify_embed_error(e, host) from e
        except httpx.HTTPError as e:
            # Reached it and it answered badly (or timed out mid-request): that
            # is not a reachability problem, so failing over would just repeat it.
            # Clean domain error for CLI/MCP consumers; full trace stays in logs
            # (CLAUDE.md §Code style).
            raise _classify_embed_error(e, host) from e
    # A 200 whose BODY is not the expected shape (Ollama emitting {"error":...}
    # with 200 during a restart, a proxy error page, or wrong-length vectors) would
    # otherwise escape as a raw KeyError/JSONDecodeError/TypeError and the drain
    # would misfile a transient server hiccup as a content failure. Wrap it as
    # EmbedderUnavailable — a garbage 200 is a server-side anomaly → defer/retry,
    # matching the classification _classify_embed_error already claims (finding).
    try:
        vectors = resp.json()["embeddings"]
        for v in vectors:
            if len(v) != settings.kb_embed_dim:
                raise ValueError(
                    f"embedding dim {len(v)} != configured KB_EMBED_DIM {settings.kb_embed_dim}"
                )
    except (KeyError, ValueError, TypeError) as e:
        raise EmbedderUnavailable(
            f"embedder returned unusable output: {e.__class__.__name__}: {e}"
        ) from e
    return vectors


def embed_one(text: str, *, timeout: httpx.Timeout | None = None) -> list[float]:
    return embed_texts([text], timeout=timeout)[0]


def embed_query(text: str) -> list[float]:
    """Embed a USER QUERY, applying ``KB_EMBED_QUERY_PREFIX`` when configured.

    Instruction-aware embedders (qwen3-embedding) are ASYMMETRIC: queries must
    carry an instruction prefix while documents are embedded plain. Misapplying
    it either way is a measured regression (MODEL_RESEARCH_2026-07 addendum:
    without the query prefix qwen3-embedding retrieves worse than bge-m3;
    prefixed documents break the contract the model was trained with). So:
    every place a user query is embedded calls THIS; document/chunk/reembed
    paths call embed_texts/embed_one directly and must NEVER get the prefix.

    Routes through embed_one -> embed_texts by design — the test seams that
    fake embed_texts (tests/conftest.py fake_embeddings, autouse) and inject
    failures via embed_one intercept query embeds too. Asserted in
    tests/test_embed_query_prefix.py; do not inline an HTTP call here.
    """
    # The interactive budget, not the drain's: this is the read path.
    return embed_one(settings.kb_embed_query_prefix + text, timeout=_QUERY_TIMEOUT)
