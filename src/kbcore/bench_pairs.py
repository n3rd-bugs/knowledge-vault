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
legitimately empties this source until the nightly re-judges) — except a pre-fit
verdict on an oversized pair, which may be a blind reply (KB 55589, :func:`judged_pairs`).
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from kbcore import judge_fit
from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation
from kbcore.twin_probe import probe_candidates

logger = logging.getLogger("kbcore.bench_pairs")

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
    then a reproducible random draw of negatives, ``sample`` rows in all.

    A row checked before :data:`kbcore.judge_fit.FIT_EPOCH` whose UNFITTED prompt was over
    :data:`~kbcore.judge_fit.LEGACY_CEILING` is not a label: its verdict may be the blind
    reply to a prompt Ollama had truncated (KB 55589). It is skipped BEFORE the sample is
    counted, so ``sample`` still means usable labels; rows from the fitted judge stay.
    The byte prefilter skips the count for the rest (a cl100k token is never shorter
    than one byte)."""
    want, ceiling = max(0, int(sample)), judge_fit.LEGACY_CEILING
    out: list[BenchPair] = []
    skipped = 0
    with _read_only() as conn:
        conn.execute("SELECT setseed(%s)", (SAMPLE_SEED,))
        # random() is drawn in the subquery over the memory table ALONE, in its scan order —
        # as before the size check joined items — so the seeded draw does not depend on
        # the join plan, and with nothing skipped it is the draw the bench always made.
        rows = conn.execute(
            "SELECT j.a_id, j.b_id, j.verdict, j.checked_at < %s AND"
            " octet_length(ia.title) + octet_length(ia.body) + octet_length(ib.title)"
            " + octet_length(ib.body) > %s"
            " FROM (SELECT a_id, b_id, verdict, checked_at, random() AS draw"
            " FROM contradiction_judgements WHERE embed_model = %s) j"
            " JOIN items ia ON ia.id = j.a_id JOIN items ib ON ib.id = j.b_id"
            " ORDER BY j.verdict DESC, j.draw",
            (judge_fit.FIT_EPOCH, ceiling // 2, settings.kb_embed_model),
        ).fetchall()
        for a, b, verdict, suspect in rows:
            if len(out) >= want:
                break
            if suspect and _oversized(conn, int(a), int(b), ceiling):
                skipped += 1
                continue
            out.append(BenchPair(int(a), int(b), KIND_CONTRADICT, bool(verdict), "judged"))
    if skipped:
        logger.info("judged pairs: %d pre-fit verdict(s) over the ceiling skipped", skipped)
    return out


def _oversized(conn: psycopg.Connection, a_id: int, b_id: int, ceiling: int) -> bool:
    """True when the pair's unfitted contradiction prompt is over ``ceiling``."""
    rows = conn.execute(
        "SELECT id, title, body FROM items WHERE id = ANY(%s)", ([a_id, b_id],)
    ).fetchall()
    texts = {r[0]: (r[1], r[2]) for r in rows}
    return judge_fit.contradict_tokens(*texts[a_id], *texts[b_id]) > ceiling


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
