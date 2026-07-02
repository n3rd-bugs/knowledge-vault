"""kbcore.embed — Ollama /api/embed batch embeddings (PLAN.md §5.1, OPS-1).

GPU is a guest: every call sets a short keep_alive so bge-m3 unloads quickly
after use (Invariant 8). ``KB_EMBED_DEVICE=cpu`` forces CPU as an escape hatch.
"""

from __future__ import annotations

import httpx

from kbcore.config import settings
from kbcore.errors import EmbedderUnavailable

# Short keep-alive per OPS-1 — embedder loads on demand and unloads fast.
KEEP_ALIVE = "2m"
_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


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
        # (CLAUDE.md §Code style).
        raise EmbedderUnavailable(
            f"embedding call to {settings.ollama_url} failed: {e.__class__.__name__}: {e}"
        ) from e
    vectors = resp.json()["embeddings"]
    for v in vectors:
        if len(v) != settings.kb_embed_dim:
            raise ValueError(
                f"embedding dim {len(v)} != configured KB_EMBED_DIM {settings.kb_embed_dim}"
            )
    return vectors


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]
