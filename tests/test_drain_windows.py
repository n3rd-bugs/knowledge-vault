"""The nightly drain re-windows a coalesced group to fit the extractor's context.

``ingest_queue.pending_groups`` promised the caller "concatenates, assembles
ONCE, and re-windows the assembled text". The drain did the first two and not the
third: a coalesced session became ONE prompt, Ollama truncated it to num_ctx/2
tokens keeping only the first 4, and the extraction system prompt was what went
over the side (measured 2026-07-31: a 1,034,082-token prompt truncated to 8,194,
"truncating input prompt" x13 in journald). The model was handed an
instruction-less tail slice and returned nothing, with a 200 and no error.

These tests pin the split, its budget, the tripwire that keeps a truncation LOUD,
and the group-wide flip/failure semantics that must survive being spread over N
extraction calls.
"""

from __future__ import annotations

import logging

import pytest

from kbcore import ingest_queue
from kbcore.config import settings
from kbcore.errors import ExtractionFailed, ExtractorUnavailable
from kbworker import consolidate, sessions


def _group(uri: str, text: str, n_rows: int = 1) -> dict:
    """A pending_groups-shaped group whose rows concatenate to ``text``."""
    step = max(1, len(text) // n_rows)
    parts = [text[i : i + step] for i in range(0, len(text), step)] or [""]
    return {
        "uri": uri,
        "rows": [
            {
                "id": 100 + i,
                "project": "p",
                "kind": "session",
                "content": part,
                "meta": {"source_uri": uri},
            }
            for i, part in enumerate(parts)
        ],
    }


def _prose(kb_size: int) -> str:
    line = "User: tell me about the drain\nAssistant: it windows the transcript now\n"
    return line * ((kb_size * 1000) // len(line) + 1)


# --------------------------------------------------------------- the budget


def test_the_budget_is_derived_from_num_ctx_and_is_conservative():
    """Ollama's prompt ceiling is num_ctx/2 TOKENS; the budget is bytes, so it
    must sit well under a naive tokens x 4 conversion or the split is theatre."""
    assert settings.kb_llm_num_ctx == 16384, "default changed — recheck the numbers below"
    budget = consolidate.window_budget_bytes()
    assert 20_000 <= budget <= 30_000, f"{budget} is not the ~25 kB the 16K context affords"
    assert budget < (settings.kb_llm_num_ctx // 2) * 4, "no margin for the system prompt"


def test_a_bigger_context_buys_bigger_windows(monkeypatch):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 32768)
    assert consolidate.window_budget_bytes() > 50_000


def test_a_tiny_context_cannot_produce_absurd_windows(monkeypatch):
    """num_ctx below the prompt overhead would compute a negative budget and
    shred every transcript into unusable slivers."""
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 512)
    assert consolidate.window_budget_bytes() == consolidate.MIN_WINDOW_BYTES


# ------------------------------------------------------------- the windowing


def test_a_coalesced_group_is_split_into_windows_that_fit():
    budget = consolidate.window_budget_bytes()
    jobs = consolidate._assemble_jobs([_group("sess:big", _prose(200), n_rows=8)])

    assert len(jobs) > 1, "the group was submitted whole — this is the truncation bug"
    for job in jobs:
        assert len(job["text"].encode("utf-8")) <= budget, "a window still overflows the context"


def test_the_whole_transcript_survives_the_split():
    """A window boundary must not eat content: truncation-by-windowing would be
    the same data loss with a nicer name."""
    text = _prose(120)
    jobs = consolidate._assemble_jobs([_group("sess:whole", text)])
    # assemble_text strips the blob's outer whitespace; nothing else may go.
    assert "".join(j["text"] for j in jobs) == text.strip()


def test_a_small_group_is_still_exactly_one_job():
    jobs = consolidate._assemble_jobs([_group("sess:small", "User: hi\nAssistant: hello\n")])
    assert len(jobs) == 1
    assert jobs[0]["key"] == "sess:small", "a single-window group must keep its plain cohort key"
    assert "window" not in jobs[0]["detail"], "no window bookkeeping when there is one window"


def test_every_window_carries_the_same_group_identity():
    jobs = consolidate._assemble_jobs([_group("sess:ident", _prose(150), n_rows=4)])
    ids = jobs[0]["ids"]

    assert len({j["uri"] for j in jobs}) == 1, "windows must share the group's source uri"
    assert all(j["ids"] == ids for j in jobs)
    assert all(j["group"] is jobs[0]["group"] for j in jobs), "windows must share ONE group state"
    assert all(j["detail"]["ingest_queue_id"] == jobs[0]["detail"]["ingest_queue_id"] for j in jobs)
    assert [j["detail"]["window"] for j in jobs] == list(range(len(jobs)))
    assert all(j["detail"]["windows"] == len(jobs) for j in jobs)


def test_each_window_gets_its_OWN_cohort_key():
    """The cohort maps prefetched extractions and plans by key. Two windows
    sharing a key would hand one window its neighbour's extraction."""
    jobs = consolidate._assemble_jobs([_group("sess:keys", _prose(150), n_rows=2)])
    keys = [j["key"] for j in jobs]
    assert len(set(keys)) == len(keys), f"duplicate cohort keys: {keys}"


# ---------------------------------------------------------------- tripwire


def test_the_tripwire_fires_on_an_oversized_submission(caplog):
    """Measured in TOKENS now: the byte version was the bug (dense content runs
    at 1.2-2.1 bytes/token and sailed under a byte check while overflowing)."""
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        fired = consolidate._warn_if_oversized("j1", "word " * 2_000, 500)
    assert fired is True
    assert "TRUNCATION RISK" in caplog.text
    assert "j1" in caplog.text


def test_dense_content_cannot_slip_under_the_token_ceiling():
    """THE VERIFIER'S REPRODUCTION, pinned. base64 runs ~1.23 bytes/token, so a
    byte-budget-full window decodes to ~2x the context ceiling; every emitted
    window must fit by REAL count, not by estimate."""
    import base64
    import random

    from kbcore.chunk import count_tokens

    rng = random.Random(42)
    dense = base64.b64encode(bytes(rng.randrange(256) for _ in range(90_000))).decode()
    windows = consolidate._fit_windows(dense)
    ceiling = consolidate._token_ceiling()
    assert len(windows) > 1
    for w in windows:
        assert count_tokens(w) <= ceiling, "a dense window still overflows the context"


def test_the_token_ceiling_keeps_a_qwen_mismatch_margin():
    """cl100k is not qwen3's tokenizer; the ceiling must sit visibly below the
    raw budget so a counting mismatch cannot overflow."""
    assert consolidate._token_ceiling() < consolidate.window_budget_tokens()


def test_the_tripwire_is_silent_for_windows_the_splitter_produced(caplog):
    with caplog.at_level(logging.WARNING, logger="kbworker.consolidate"):
        jobs = consolidate._assemble_jobs([_group("sess:quiet", _prose(300), n_rows=6)])
    assert len(jobs) > 1
    assert "TRUNCATION RISK" not in caplog.text


# -------------------------------------------------- group flip / failure (DB)


@pytest.fixture()
def big_row(kb):
    """One queued submission far larger than a single extraction window."""
    budget = consolidate.window_budget_bytes()
    res = ingest_queue.enqueue("qwin", _prose((budget * 3) // 1000), kind="session")
    assert res.id is not None
    return res.id


def _status(qid: int) -> tuple[str, int, int]:
    from kbcore.db import connect

    with connect() as conn:
        return conn.execute(
            "SELECT status, saved_count, rejected_count FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()


def test_the_group_flips_only_after_every_window_is_processed(kb, big_row, monkeypatch):
    """A partial flip would strand the unextracted tail of a session forever."""
    seen: list[str] = []

    def fake(text, project, uri, **kw):
        seen.append(text)
        assert _status(big_row)[0] == "pending", "the group flipped before its last window"
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    drained, saved = consolidate._drain_ingest_queue()

    assert len(seen) > 1, "the oversized row was not windowed"
    assert (drained, saved) == (1, len(seen)), "rows are counted once, saved counts every window"
    assert _status(big_row) == ("extracted", len(seen), 0), "the group's totals are cumulative"


def test_a_drained_group_is_not_re_drained(kb, big_row, monkeypatch):
    """Invariant 3: the queue is the checkpoint, and a finished group leaves it."""
    monkeypatch.setattr(sessions, "extract_and_ingest", lambda *a, **k: (1, 0))
    consolidate._drain_ingest_queue()
    assert consolidate._drain_ingest_queue() == (0, 0)


def test_a_failing_window_fails_the_WHOLE_group_and_skips_the_rest(kb, big_row, monkeypatch):
    """Group-wide failure semantics survive the split: the next run re-extracts
    the whole session rather than stitching it together from two nights."""
    calls: list[str] = []

    def fake(text, project, uri, **kw):
        calls.append(text)
        if len(calls) == 2:
            raise ExtractionFailed("unparseable model output")
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    consolidate._drain_ingest_queue()

    assert len(calls) == 2, "windows kept extracting after the group had already failed"
    assert _status(big_row)[0] == "failed", "the whole group was not failed by its bad window"
    assert any(r["id"] == big_row for g in ingest_queue.pending_groups() for r in g["rows"]), (
        "one content failure retired the group instead of spending one attempt"
    )


def test_a_connectivity_failure_mid_group_defers_without_burning_an_attempt(
    kb, big_row, monkeypatch
):
    calls: list[str] = []

    def fake(text, project, uri, **kw):
        calls.append(text)
        if len(calls) == 2:
            raise ExtractorUnavailable("ollama restarting")
        return 1, 0

    monkeypatch.setattr(sessions, "extract_and_ingest", fake)
    consolidate._drain_ingest_queue()

    from kbcore.db import connect

    with connect() as conn:
        status, attempts = conn.execute(
            "SELECT status, attempts FROM ingest_queue WHERE id = %s", (big_row,)
        ).fetchone()
    assert (status, attempts) == ("pending", 0), "an outage must not burn the row's retry budget"
    assert len(calls) == 2, "the remaining windows ran anyway after the group was deferred"
