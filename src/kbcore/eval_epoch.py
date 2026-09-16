"""kbcore.eval_epoch — the eval EPOCH id: a content hash of ``eval/golden.yaml``.

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

Deliberately light: no yaml, no DB — a file read and a digest, importable
from anywhere without dragging the harness in.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

HASH_HEX_CHARS = 12  # 48 bits: distinct across every golden edit this KB will ever see


def golden_hash(path: Path | None = None) -> str:
    """The epoch id: sha256 of the golden file's bytes, first 12 hex chars.

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
    return hashlib.sha256(data).hexdigest()[:HASH_HEX_CHARS]
