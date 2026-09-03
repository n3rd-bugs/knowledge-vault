"""kbcore.bench_pairs — the labelled PAIR SOURCES for the pair-judge bench (review #8 H4).

Split from :mod:`kbcore.bench_judge` at landing (300-line new-file ratchet); that
module re-imports every public name here, so ``bench_judge.<name>`` stays the
address for the CLI and the tests. Everything in this module READS:

* every query runs on a connection Postgres itself holds READ ONLY — an INSERT/
  UPDATE/DELETE raises ``ReadOnlySqlTransaction`` whatever the SQL says
  (:func:`_read_only`);
* no write chokepoint is imported (``tests/test_bench_judge.py`` walks the AST of
  both bench modules and fails the import if one appears).

Labels come from what the system already believes: an item-item ``alternative_to``
edge is the linker's (or the owner's) standing same-fact verdict; a
``contradiction_judgements`` row at the CURRENT embed model is the incumbent
judge's verdict (migration 017 keys the memory by embed_model, so a re-embed
legitimately empties this source until the nightly re-judges).
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation
from kbcore.twin_probe import probe_candidates

KIND_SAME_FACT = "same_fact"  # twin_judge prompt; label = an alternative_to edge exists
KIND_CONTRADICT = "contradict"  # items_contradict prompt; label = the incumbent's verdict
KINDS = (KIND_SAME_FACT, KIND_CONTRADICT)

# The near-but-not-dup band both judges live in (tail_ops.CONTRA_LOW/HIGH).
BAND_LOW, BAND_HIGH = 0.80, 0.95
SAMPLE_SEED = 0.42  # setseed()/random.Random argument: the drawn set is reproducible
# Edges older than the linker predate it: manual ADR-sense edges (297->294,
# 302->294) are flagged so a "rejected by both judges" reading exempts them (KB 31855).
LINKER_EPOCH = datetime(2026, 8, 9, tzinfo=UTC)


@dataclass(frozen=True)
class BenchPair:
    """One labelled pair. ``label`` is the reference verdict the candidates are scored
    against; ``manual`` marks a pre-linker edge (exempt from any reject reading)."""

    a_id: int
    b_id: int
    kind: str
    label: bool
    source: str  # 'edge' | 'judged' | 'band' | 'file'
    manual: bool = False


@contextmanager
def _read_only() -> Iterator[psycopg.Connection]:
    """A connection Postgres itself holds to READ ONLY (``default_transaction_read_only``):
    any INSERT/UPDATE/DELETE raises ``ReadOnlySqlTransaction`` regardless of the SQL."""
    conn = connect()
    conn.read_only = True
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def edge_pairs() -> list[BenchPair]:
    """Every live item-item ``alternative_to`` edge (search_rank's collapse shape),
    labelled same-fact TRUE — the linker's (or the owner's) standing verdict."""
    with _read_only() as conn:
        rows = conn.execute(
            "SELECT src_id, dst_id, created_at FROM relations "
            "WHERE rel = 'alternative_to' AND src_kind = 'item' AND dst_kind = 'item' "
            "ORDER BY id"
        ).fetchall()
    return [
        BenchPair(int(a), int(b), KIND_SAME_FACT, True, "edge", manual=ts < LINKER_EPOCH)
        for a, b, ts in rows
    ]


def judged_pairs(sample: int) -> list[BenchPair]:
    """``contradiction_judgements`` rows at the CURRENT embed model (migration 017 keys
    by embed_model), the incumbent's verdict as label. Positives (rare) come first,
    then a reproducible random draw of negatives, ``sample`` rows in all."""
    with _read_only() as conn:
        conn.execute("SELECT setseed(%s)", (SAMPLE_SEED,))
        rows = conn.execute(
            "SELECT a_id, b_id, verdict FROM contradiction_judgements "
            "WHERE embed_model = %s ORDER BY verdict DESC, random() LIMIT %s",
            (settings.kb_embed_model, max(0, int(sample))),
        ).fetchall()
    return [BenchPair(int(a), int(b), KIND_CONTRADICT, bool(v), "judged") for a, b, v in rows]


def band_negatives(n: int, exclude: set[tuple[int, int]]) -> list[BenchPair]:
    """``n`` cross-project pairs in ``[BAND_LOW, BAND_HIGH)`` with NO alternative_to edge
    — the same-fact judge's hard negatives. KNN-assisted via ``twin_probe`` (bounded,
    never the O(N^2) self-join); the draw is seeded so a re-run sees the same set."""
    if n <= 0:
        return []
    linked = {(p.a_id, p.b_id) for p in edge_pairs()} | exclude
    linked |= {(b, a) for a, b in linked}
    pairs, _coverage = probe_candidates(threshold=BAND_LOW, cap=max(1000, 20 * n))
    band = [
        p
        for p in pairs
        if BAND_LOW <= p.similarity < BAND_HIGH and (p.a.item_id, p.b.item_id) not in linked
    ]
    drawn = random.Random(SAMPLE_SEED).sample(band, min(n, len(band)))
    return [BenchPair(p.a.item_id, p.b.item_id, KIND_SAME_FACT, False, "band") for p in drawn]


def file_pairs(path: Path) -> list[BenchPair]:
    """Re-run a set: a JSON list of ``{a_id, b_id, kind, label}`` or a previous results
    file (its ``pairs`` key). Same set, same seeds ⇒ a like-for-like re-measure."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("pairs", []) if isinstance(data, dict) else data
    out: list[BenchPair] = []
    for r in rows:
        if r.get("kind") not in KINDS:
            raise InvalidOperation(f"pair kind must be one of {KINDS}; got {r.get('kind')!r}")
        out.append(
            BenchPair(
                int(r["a_id"]),
                int(r["b_id"]),
                r["kind"],
                bool(r["label"]),
                "file",
                bool(r.get("manual", False)),
            )
        )
    return out


def load_pairs(spec: str, *, sample: int, negatives: int) -> list[BenchPair]:
    """``edges`` (+ ``negatives`` band pairs) | ``judged`` (``sample`` rows) | a JSON file."""
    if spec == "edges":
        pairs = edge_pairs()
        pairs += band_negatives(negatives, {(p.a_id, p.b_id) for p in pairs})
    elif spec == "judged":
        pairs = judged_pairs(sample)
    else:
        path = Path(spec)
        if not path.is_file():
            raise InvalidOperation(
                f"--pairs must be 'edges', 'judged' or a JSON file; {spec!r} is none of those"
            )
        pairs = file_pairs(path)
    if not pairs:
        raise InvalidOperation(f"no pairs to judge for --pairs {spec!r}")
    return pairs
