"""kbcore.eval_epoch — the eval EPOCH id: ``eval/golden.yaml`` + the embedder, hashed.

The eval-epoch rule (review #8 "E1 contents", KB 31857; review #9a clean-
baseline rules, KB 32942): golden.yaml and ranking changes land only at a
review boundary, batched, and that commit "re-anchors the floor and resets
the week-over-week baseline". Before E1 the sentinel had no epoch notion —
the first night after the golden set grew from 52 to 59 positive-criterion
cases (61 cases in all) would have been compared against a 52-case prior, and
the weekly's ``eval mrr`` trend would have drawn one line through two
different denominators.

An epoch is identified by the CONTENT of the golden file, not by a version
field someone must remember to bump: ANY byte change to the file opens a new
epoch, which is exactly what the rule says. Every eval report records the
hash it was measured under (``meta.eval_golden_hash``); the baseline read
(``kbcore.eval_baseline``), the detector (``kbworker.eval_regression``) and
the weekly series (``kbworker.eval_epoch_lines``) compare like with like.

E2 (review #9 KB 40402, landed at review #10): the golden bytes alone missed
the two other inputs every rank depends on. The key now also covers the
EMBED MODEL (``kb_embed_model``) and the CONFIGURED embed host preference list
(``kbcore.embed.configured_hosts``, credential-free): a model swap or a
re-pointed ``KB_EMBED_URL`` opens a new epoch exactly like a golden edit. A
serving-host FALLBACK does not — the configuration is unchanged; which host
actually served a replay is ``eval_embed_host``, recorded beside the key.
The function name and the meta key ``eval_golden_hash`` are unchanged; the
key's two non-file inputs ride beside it as scalars (:func:`key_inputs`:
``eval_embed_model``, ``eval_embed_hosts_key``) so a flip names its cause.

RESTART RULE: ANY change to the configured embed model or hosts
(``KB_EMBED_MODEL`` / ``KB_EMBED_URL`` / ``OLLAMA_URL``) opens a new epoch AND
needs a kb-worker restart. The settings singleton is loaded once per process,
and kb-worker runs on-demand consolidations (kb consolidate-now / MCP / REST)
IN-PROCESS: a worker that missed the .env edit keys the OLD epoch while the
nightly one-shot keys the new one, and each run then closes the other's open
regression row as 'epoch changed'.

Deliberately light: no yaml, no DB — a file read, two settings and a digest,
importable from anywhere without dragging the harness in.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from kbcore import embed
from kbcore.config import settings

HASH_HEX_CHARS = 12  # 48 bits: distinct across every golden edit this KB will ever see


def embed_hosts_key() -> str:
    """The configured embed host preference list as ONE canonical string.

    Comma-joined in preference order, each host credential-free
    (``embed.public_host``) — a rotated password in a URL is not a ranking
    input, so it must not re-key the epoch.
    """
    return ",".join(embed.public_host(h) for h in embed.configured_hosts())


def key_inputs() -> dict[str, str]:
    """The epoch key's configured inputs as report scalars, beside ``eval_golden_hash``
    (meta only: kbcore.report_meta keeps ``eval_embed_*`` out of the indexed body).

    ``eval_embed_model`` (``kb_embed_model``) and ``eval_embed_hosts_key`` — the
    exact credential-free string :func:`golden_hash` hashes. No query text.
    """
    return {"eval_embed_model": settings.kb_embed_model, "eval_embed_hosts_key": embed_hosts_key()}


def golden_hash(path: Path | None = None) -> str:
    """The epoch id: the first 12 hex chars of sha256(golden bytes ``\\x00``
    ``kb_embed_model`` ``\\x00`` :func:`embed_hosts_key`).

    ``path`` defaults to :data:`kbcore.eval_harness.GOLDEN_PATH` (imported
    lazily — the harness pulls in search and rerank, which this module must
    not). A missing file hashes as empty bytes, so the value is always a
    stable string; the sentinel only records it after ``load_golden`` found
    cases, so an absent file never opens an epoch of its own.
    """
    if path is None:
        from kbcore.eval_harness import GOLDEN_PATH

        path = GOLDEN_PATH
    data = path.read_bytes() if path.exists() else b""
    parts = (data, settings.kb_embed_model.encode("utf-8"), embed_hosts_key().encode("utf-8"))
    return hashlib.sha256(b"\x00".join(parts)).hexdigest()[:HASH_HEX_CHARS]
