"""kbcore.embed_identity — WHICH embedder served the eval's fingerprint probe (E2, review #10).

``eval_embed_fingerprint`` (kbcore.embed.query_fingerprint) is an exact-equality
flag over a 4-decimal probe vector, and the preferred embed host is not
load-deterministic (EPOCHS.md: cosine 0.999837 between two loads), so the hash
moves after every model load. A change is only diagnosable beside the identity
of what served the probe: the credential-free host
(``eval_embed_fingerprint_host``, ``embed.last_query_host()`` right after the
probe) and :func:`runtime` — ``"ollama <version> <model>@<digest12>"`` for that
host (``eval_embed_runtime``) from its ``GET /api/version`` and ``GET
/api/tags`` (the entry named exactly the configured model, or model + ":latest";
the first 12 hex of its digest). A new value beside a new digest or server build
means the embedder changed. Beside an unchanged runtime it is a RELOAD — or one
of the probe's inputs this runtime and the epoch key never see (fix-round review
H4): a query-prefix edit, a device change, or Ollama silently offloading part of
the model to CPU under VRAM pressure. The first two ride beside the fingerprint
(``eval_embed_query_prefix_sha``, ``eval_embed_device``, kbworker.eval_cases); a
partial offload is recorded nowhere, so an unexplained move is a reload OR an
offload until the host says otherwise.

The requests go to the CONFIGURED (credentialed) URL whose public form is the
host that served — the same URL the embed call used — with short timeouts
(:data:`IDENTITY_TIMEOUT`, <= 3 s each), and FAIL OPEN: any error leaves the key
absent and logs ONE warning; identity telemetry may never cost the eval.
:func:`_get` is the one network seam (tests replace it; conftest arms a guard
so no test can reach a real host). Read-only GETs — no model is loaded.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from kbcore import embed
from kbcore.config import settings

logger = logging.getLogger("kbcore.embed_identity")
IDENTITY_TIMEOUT = httpx.Timeout(3.0)  # each identity GET: connect/read/write/pool <= 3 s
DIGEST_HEX_CHARS = 12
_MAX_FIELD = 40  # a server-reported field longer than this is cut (meta / log hygiene)


def _get(url: str) -> Any:
    """``GET url`` -> its JSON body (raises on any HTTP or decode error)."""
    resp = httpx.get(url, timeout=IDENTITY_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _configured_url(public: str) -> str | None:
    """The configured (credentialed) embed URL whose credential-free form is ``public``."""
    return next((h for h in embed.configured_hosts() if embed.public_host(h) == public), None)


def _field(value: Any) -> str:
    """A server-reported string, printable ASCII only and capped — never trusted raw."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected a non-empty string, got {value!r}")
    text = "".join(c for c in value.strip() if c.isascii() and c.isprintable() and c != " ")
    return text[:_MAX_FIELD]


def runtime(public: str) -> str | None:
    """``"ollama <version> <model>@<digest12>"`` for the host that served the probe.

    ``public`` is the credential-free host (``embed.last_query_host()``). None —
    after ONE warning — when it is not a configured host or either read fails.
    """
    url = _configured_url(public)
    if url is None:
        logger.warning("embed runtime unavailable: %s is not a configured embed host", public)
        return None
    try:
        version = _field(_get(f"{url}/api/version")["version"])
        names = (settings.kb_embed_model, f"{settings.kb_embed_model}:latest")
        models = _get(f"{url}/api/tags")["models"]
        entry = next(m for m in models if isinstance(m, dict) and m.get("name") in names)
        digest = _field(entry["digest"]).rpartition(":")[2][:DIGEST_HEX_CHARS]
        int(digest, 16)  # a digest is hex, or it is not a digest
        return f"ollama {version} {_field(entry['name'])}@{digest}"
    except Exception:  # noqa: BLE001 — identity telemetry may never cost the eval
        logger.warning("embed runtime unavailable for %s; the eval stands", public, exc_info=True)
        return None
