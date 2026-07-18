"""kbcore.embed — Ollama /api/embed batch embeddings (PLAN.md §5.1, OPS-1).

GPU is a guest: every call sets a short keep_alive so bge-m3 unloads quickly
after use (Invariant 8). ``KB_EMBED_DEVICE=cpu`` forces CPU as an escape hatch.
"""

from __future__ import annotations

import httpx

from kbcore.config import settings
from kbcore.errors import EmbedderUnavailable, ExtractionFailed

# Short keep-alive per OPS-1 — embedder loads on demand and unloads fast.
KEEP_ALIVE = "2m"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def _classify_embed_error(e: httpx.HTTPError) -> Exception:
    """Connectivity (defer) vs content (bounded) for an embed HTTP failure.

    Mirror of llm._classify_extractor_error: a per-row ReadTimeout or a 4xx shaped
    by a pathological chunk is deterministic → :class:`ExtractionFailed` (the queue
    drain bounds it via the attempts cap); connect errors, model-not-loaded and 5xx
    are transient → :class:`EmbedderUnavailable` (deferred and retried).
    """
    msg = f"embedding call to {settings.ollama_url} failed: {e.__class__.__name__}: {e}"
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        body = (e.response.text or "").lower()
        model_missing = status == 404 or ("model" in body and "not found" in body)
        if not model_missing and 400 <= status < 500:
            return ExtractionFailed(msg)
        return EmbedderUnavailable(msg)
    if isinstance(e, httpx.TimeoutException) and not isinstance(e, httpx.ConnectTimeout):
        return ExtractionFailed(msg)
    return EmbedderUnavailable(msg)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one ``KB_EMBED_DIM`` vector per text."""
    if not texts:
        return []
    payload: dict = {
        "model": settings.kb_embed_model,
        "input": texts,
        "keep_alive": KEEP_ALIVE,
    }
    if settings.kb_embed_device == "cpu":
        payload["options"] = {"num_gpu": 0}
    try:
        resp = httpx.post(f"{settings.ollama_url}/api/embed", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        # Clean domain error for CLI/MCP consumers; full trace stays in logs
        # (CLAUDE.md §Code style). Connectivity → EmbedderUnavailable (defer);
        # content/timeout → ExtractionFailed (attempts-capped in the drain).
        raise _classify_embed_error(e) from e
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


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


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
    return embed_one(settings.kb_embed_query_prefix + text)
