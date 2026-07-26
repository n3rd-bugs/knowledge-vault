"""kbcore.reprocess — re-run the CURRENT extraction pipeline over retained raw.

Owner decision 2026-07-19: every session item in the corpus was extracted by
the OLD single-model pipeline, and the original transcripts are retained on
disk (``sources.raw_path`` — zstd under KB_RAW, OPS-4). Pipeline v2 (ensemble +
quote repair + secondary judge) yields ~4x quote-anchored knowledge, so this
module replays retained raw through TODAY's pipeline to apply that improvement
retroactively. Design rules (the WHY):

- SESSION-ONLY. Only ``kind='session'`` sources ever went through the 14B
  extractor (remote chat drains into kind='session' via the ingest queue too);
  git/file/manual content is stored AS-IS — there is no extraction step to
  re-run, so replaying them could only duplicate bodies.
- NEW-NOT-SUPERSEDE. Improved/extra extractions land as NEW unverified items
  with review rows — the existing trust flow. Reprocessing never supersedes,
  archives, or deletes prior items (Invariant 5); a human (or review triage)
  decides what, if anything, a better extraction replaces.
- IDEMPOTENT BY CONSTRUCTION. Safety leans entirely on the ingest dedupe
  (Invariant 3): an identical re-extraction re-produces the same (title, body)
  → the same content_hash → a ``UNIQUE(kind, uri, content_hash)`` no-op, while
  the ensemble's extra yield and repaired quotes are genuinely different and DO
  land. Re-running reprocess is therefore always safe.
- SHADOW FIRST. ``shadow=True`` is the DEFAULT (writes are opt-in): the same
  extraction compute runs, but candidate content hashes are only CHECKED
  against the uri's existing sources and the would-save/dedup split reported —
  no item/source/review row is ever written on the shadow path.
- PROVENANCE. Applied items carry ``meta.reprocessed_from`` (source id),
  ``meta.reprocessed_at`` and ``meta.prev_pipeline`` — stamped on the ITEM meta
  (not only the review-queue detail) so provenance survives review resolution.

Invariant 8: reprocessing invokes the extractor stack; it runs only from the
explicit owner-invoked ``kb reprocess`` (sanctioned like ``kb ingest-session``).
All SQL here is parameterized and lives in kbcore (Invariant 2); the pipeline
itself is reused from kbworker.sessions (lazy imports — kbworker imports kbcore
at module load, so the reverse edge must resolve at call time only).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import zstandard

from kbcore import llm, session_noise
from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation, NotFound
from kbcore.ingest import content_hash, normalize, normalize_title
from kbcore.items import parse_instant
from kbcore.scrub import scrub

logger = logging.getLogger("kbcore.reprocess")


def reprocessable_sources(
    project: str | None = None,
    before: str | None = None,
    extractor: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Session sources whose raw archive is retained on disk. Read-only.

    One raw transcript window backs SEVERAL source rows (ingest creates one per
    extracted item, all sharing kind/uri/raw_path), so rows are grouped per
    (project, uri, raw_path) with MIN(id) as the representative ``source_id`` —
    reprocessing runs once per retained raw, never once per old item.

    Filters: ``project``; ``before`` (ISO date/timestamp — sources ingested
    strictly before it); ``extractor`` — only items whose ``meta->>'extractor'``
    matches (targets a specific old pipeline). ``limit`` caps the SQL result;
    rows whose archive has vanished from disk are dropped AFTER it, so the
    returned count can be below the limit.
    """
    clauses = ["s.kind = 'session'", "s.raw_path IS NOT NULL"]
    params: list[Any] = []
    if project is not None:
        clauses.append("s.project = %s")
        params.append(project)
    if before is not None:
        clauses.append("s.ingested_at < %s")
        params.append(parse_instant(before, "before"))
    if extractor is not None:
        clauses.append("i.meta->>'extractor' = %s")
        params.append(extractor)
    lim = "LIMIT %s" if limit is not None else ""
    if limit is not None:
        params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT MIN(s.id), s.project, s.uri, s.raw_path,
                   array_agg(i.id ORDER BY i.id),
                   (array_agg(i.meta->>'extractor' ORDER BY i.id)
                      FILTER (WHERE i.meta->>'extractor' IS NOT NULL))[1]
            FROM sources s
            JOIN items i ON i.source_id = s.id
            WHERE {" AND ".join(clauses)}
            GROUP BY s.project, s.uri, s.raw_path
            ORDER BY MIN(s.id)
            {lim}
            """,  # noqa: S608 — clauses/lim are constant fragments; values ride params
            params,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for sid, proj, uri, raw_path, item_ids, prev in rows:
        if not Path(raw_path).exists():
            continue  # archive gone — nothing to replay
        out.append(
            {
                "source_id": int(sid),
                "project": proj,
                "uri": uri,
                "raw_path": raw_path,
                "item_ids": [int(i) for i in item_ids],
                "prev_extractor": prev,
            }
        )
    return out


def _source_context(source_id: int) -> dict[str, Any]:
    """Load one source's replay context: uri/raw_path, the uri's existing
    content hashes (the dedupe baseline), and code_ref/branch/extractor
    reconstructed from the sibling items' meta (first non-null wins)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT kind, project, uri, raw_path FROM sources WHERE id = %s", (source_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"source {source_id} does not exist")
        kind, project, uri, raw_path = row
        if kind != "session":
            raise InvalidOperation(
                f"source {source_id} is kind={kind!r}; only session sources are reprocessable"
            )
        if not raw_path:
            raise InvalidOperation(f"source {source_id} has no retained raw archive")
        metas = conn.execute(
            "SELECT i.meta FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session' AND s.uri = %s ORDER BY i.id",
            (uri,),
        ).fetchall()
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT content_hash FROM sources WHERE kind = 'session' AND uri = %s", (uri,)
            ).fetchall()
        }
    code_ref = branch = prev = None
    for (meta,) in metas:
        meta = meta if isinstance(meta, dict) else {}
        code_ref = code_ref or meta.get("code_ref")
        branch = branch or meta.get("branch")
        prev = prev or meta.get("extractor")
    return {
        "project": project,
        "uri": uri,
        "raw_path": raw_path,
        "existing_hashes": existing,
        "code_ref": code_ref,
        "branch": branch,
        "prev_extractor": prev,
    }


def _read_raw(raw_path: str) -> bytes:
    """Decompress one retained archive (zstd frames written by archive_raw)."""
    return zstandard.ZstdDecompressor().decompress(Path(raw_path).read_bytes())


def _shadow_counts(text: str, existing: set[str], project: str, uri: str) -> tuple[int, int, int]:
    """(new, dedup, rejected) the CURRENT pipeline WOULD produce — compute only.

    Mirrors kbworker.sessions.extract_and_ingest's write set without writing:
    ensemble live mode reuses run_ensemble (compute-and-report by design — it
    imports no DB machinery); otherwise the v1 extractor plus the SHARED
    gate_quotes. Candidate hashes use the exact ingest transforms (normalize →
    scrub → content_hash) and are checked against the uri's existing sources,
    so new-vs-dedup is reported without touching the database.
    """
    from kbworker.sessions import gate_quotes  # lazy: kbworker imports kbcore at load

    if settings.extract_ensemble and not settings.kb_ensemble_shadow:
        from kbworker import ensemble

        report = ensemble.run_ensemble(text, project, uri)
        candidates = [s["item"] for s in report["survivors"]]
        rejected = report["unanchored"] + report["rejected_reps"]
    else:
        survivors, rejected, _reasons = gate_quotes(
            llm.extract_items(text), text, settings.kb_quote_repair_min_ratio
        )
        candidates = [it for it, _repair in survivors]
    hashes: set[str] = set()
    for it in candidates:
        title = (it.get("title") or "").strip() or "session note"
        body = (it.get("body") or "").strip()
        if not body:
            continue
        if session_noise.noise_reason(title, body) is not None:
            continue  # mirrors the mint-time noise skip (audit 2026-07-26 FIX 7)
        title_s, _ = scrub(normalize_title(title))
        body_s, _ = scrub(normalize(body))
        hashes.add(content_hash(title_s, body_s))
    return len(hashes - existing), len(hashes & existing), rejected


def reprocess_source(source_id: int, *, shadow: bool) -> dict[str, Any]:
    """Replay ONE retained raw through the current pipeline.

    shadow=True: report {would_save, new, dedup, rejected} — writes NOTHING.
    shadow=False: run :func:`kbworker.sessions.extract_and_ingest` on the SAME
    uri, so identical re-extractions dedupe to no-ops and only new/different
    items land (unverified + review row), each stamped with provenance in both
    the item meta and the review detail. Returns {saved, rejected, source_id}.
    """
    from kbworker.sessions import assemble_text, extract_and_ingest  # lazy (see module doc)

    ctx = _source_context(source_id)
    text = assemble_text(_read_raw(ctx["raw_path"]), kind="session")
    if shadow:
        new, dedup, rejected = _shadow_counts(
            text, ctx["existing_hashes"], ctx["project"], ctx["uri"]
        )
        return {
            "source_id": source_id,
            "shadow": True,
            "would_save": new,
            "new": new,
            "dedup": dedup,
            "rejected": rejected,
        }
    provenance: dict[str, Any] = {
        "reprocessed_from": source_id,
        "reprocessed_at": datetime.now(UTC).isoformat(),
    }
    if ctx["prev_extractor"]:
        provenance["prev_pipeline"] = ctx["prev_extractor"]
    saved, rejected = extract_and_ingest(
        text,
        ctx["project"],
        ctx["uri"],
        code_ref=ctx["code_ref"],
        branch=ctx["branch"],
        extra_detail=provenance,
        extra_meta=provenance,
    )
    return {"source_id": source_id, "shadow": False, "saved": saved, "rejected": rejected}


def reprocess(
    project: str | None = None,
    before: str | None = None,
    extractor: str | None = None,
    limit: int | None = None,
    shadow: bool = True,
) -> dict[str, Any]:
    """Replay every matching retained raw. Default shadow=True — writes are opt-in.

    Per-source isolation: one failing source (vanished/corrupt archive, a
    pipeline error) logs, counts in ``errors``, and the run CONTINUES — a batch
    replay must never wedge on one bad archive.
    """
    rows = reprocessable_sources(project=project, before=before, extractor=extractor, limit=limit)
    totals: dict[str, Any] = {
        "sources_seen": len(rows),
        "shadow": shadow,
        "would_save": 0,
        "saved": 0,
        "new": 0,
        "dedup": 0,
        "rejected": 0,
        "errors": 0,
    }
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            res = reprocess_source(row["source_id"], shadow=shadow)
        except Exception as e:  # noqa: BLE001 — per-source isolation (see docstring)
            logger.exception("reprocess of source %s (%s) failed", row["source_id"], row["uri"])
            totals["errors"] += 1
            results.append({"source_id": row["source_id"], "error": str(e)})
            continue
        for key in ("would_save", "saved", "new", "dedup", "rejected"):
            totals[key] += res.get(key, 0)
        results.append(res)
    totals["per_source"] = results
    return totals
