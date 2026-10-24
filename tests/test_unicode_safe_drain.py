"""Drain-level proof of the lone-surrogate fix (incident 2026-10-18) — real kb_test.

26 rows of repeater session 0854af52 failed all three attempts: an extractor
reply carried a ``"\\ud834"`` escape with no low half, json.loads made it a lone
surrogate, and httpx's strict ``json=`` encoder raised inside the ensemble's
clustering embed (plan_ensemble -> _cluster -> embed_texts). The cohort plan and
the per-job fallback failed identically (pinned seed), so the group burned its
cap. In pipeline v1 the same item dies one step later, in ingest's content hash.

Nothing is mocked above the wire: the real llm.extract_items / recover_items /
parse_model_json, the real embed_texts, the real quote gate and ingest. Only
Ollama's two endpoints are faked, and every fake request is built as a REAL
``httpx.Request(json=...)`` so httpx's own strict encoder runs on it exactly as
in production — that is the line that raised.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kbcore import db, embed, ingest_queue, llm
from kbcore.config import settings
from kbcore.embed import embed_texts as _REAL_EMBED_TEXTS  # before conftest's autouse fake
from kbcore.items import get_item
from kbcore.unicode_safe import REPLACEMENT
from kbworker import consolidate, ensemble
from tests.conftest import fake_vector

pytestmark = pytest.mark.usefixtures("kb")

HIGH_ESC = "\\ud834"  # the incident's escape, as the 6 ASCII chars the model emitted
LOW_ESC = "\\udd1e"
PRIMARY = "prime"

USER = "Why does the DRV8706 SPI bus glitch at boot?"
ANSWER = (
    "The DRV8706 needs SPI mode 1 with CPOL=0 and CPHA=1; mode 0 latches garbage "
    "on the first frame after power-up."
)
GROUNDED = "The DRV8706 needs SPI mode 1 with CPOL=0 and CPHA=1"
REPAIRABLE = "mode 0 latches garbage on the first frame after power-up"


def _transcript(answer: str = ANSWER) -> str:
    rows = [
        {"type": "user", "message": {"content": USER}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _incident_reply() -> str:
    """The primary's reply as TEXT, with raw lone-surrogate escapes spliced in:

    A — title AND body carry one (the incident's shape), quote verbatim;
    B — the escape trails an otherwise verbatim quote;
    C — the quote is nothing but the escape.
    """
    items = [
        {
            "type": "gotcha",
            "title": "DRV8706 SPI mode @H@",
            "body": "Use SPI mode 1 (CPOL=0, CPHA=1) on the DRV8706 @L@ bus; mode 0 "
            "corrupts the first frame after power-up.",
            "quote": GROUNDED,
            "tags": ["hw:drv8706"],
        },
        {
            "type": "gotcha",
            "title": "DRV8706 first SPI frame after power-up",
            "body": "In SPI mode 0 the DRV8706 latches garbage on the first frame after power-up.",
            "quote": REPAIRABLE + "@H@",
            "tags": ["hw:drv8706"],
        },
        {
            "type": "note",
            "title": "orphan quote",
            "body": "An item whose only grounding is an orphaned surrogate escape.",
            "quote": "@H@",
            "tags": [],
        },
    ]
    return json.dumps({"items": items}).replace("@H@", HIGH_ESC).replace("@L@", LOW_ESC)


def _clean_reply() -> str:
    item = {
        "type": "gotcha",
        "title": "DRV8706 SPI mode 1",
        "body": "Use SPI mode 1 (CPOL=0, CPHA=1) on the DRV8706; mode 0 corrupts frame one.",
        "quote": GROUNDED,
        "tags": ["hw:drv8706"],
    }
    return json.dumps({"items": [item]})


def _install_ollama(monkeypatch, primary_reply: str) -> list[dict]:
    """Fake /api/chat + /api/embed behind the REAL client code paths."""
    monkeypatch.setattr(embed, "embed_texts", _REAL_EMBED_TEXTS)  # undo the autouse fake
    monkeypatch.setattr(settings, "kb_embed_url", "")
    embed.reset_host_health()
    calls: list[dict] = []

    def post(url, *, json=None, timeout=None):  # noqa: A002 — httpx's own kwarg name
        req = httpx.Request("POST", url, json=json)  # strict UTF-8: the line that raised
        if url.endswith("/api/embed"):
            calls.append({"ep": "embed", "input": list(json["input"])})
            body = {"embeddings": [fake_vector(t) for t in json["input"]]}
        elif url.endswith("/api/chat"):
            model, system = json["model"], json["messages"][0]["content"]
            calls.append({"ep": "chat", "model": model, "prompt": json["messages"][1]["content"]})
            if system.startswith(llm.SYSTEM_PROMPT):
                content = primary_reply if model == PRIMARY else '{"items": []}'
            else:  # any judge: approve (the policy matrix is pinned in test_ensemble)
                content = '{"supported": true, "reason": "test"}'
            body = {"message": {"content": content}}
        else:
            raise AssertionError(f"unexpected Ollama call: {url}")
        return httpx.Response(200, json=body, request=req)

    monkeypatch.setattr(httpx, "post", post)
    return calls


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")
    monkeypatch.setattr(settings, "kb_extract_model", PRIMARY)
    monkeypatch.setattr(settings, "kb_quote_repair_min_ratio", 0.95)
    monkeypatch.setattr(settings, "kb_ensemble_cluster_threshold", 0.80)
    monkeypatch.setattr(settings, "kb_extract_json_retry", 1)
    ensemble.reset_shadow_counter()


# v1 (ensemble off) and LIVE ensemble — the incident ran live: plan_ensemble -> _cluster.
_MODES = {"v1": ("", True, "off"), "live": ("ex-a", False, "live")}


@pytest.fixture(params=sorted(_MODES))
def pipeline(request, monkeypatch) -> str:
    extras, shadow, expect = _MODES[request.param]
    monkeypatch.setattr(settings, "kb_extract_ensemble", extras)
    monkeypatch.setattr(settings, "kb_ensemble_shadow", shadow)
    assert ensemble.mode() == expect
    return request.param


def _row(qid: int) -> tuple:
    with db.connect() as conn:
        return conn.execute(
            "SELECT status, saved_count, rejected_count, attempts, error "
            "FROM ingest_queue WHERE id = %s",
            (qid,),
        ).fetchone()


def _session_items() -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session' ORDER BY i.id"
        ).fetchall()
    return {it.title: it for it in (get_item(r[0]) for r in rows)}


def test_a_lone_surrogate_in_the_extractor_reply_no_longer_fails_the_group(pipeline, monkeypatch):
    calls = _install_ollama(monkeypatch, _incident_reply())
    qid = ingest_queue.enqueue("motor-ctl", _transcript(), kind="session").id
    assert qid is not None

    drained, saved = consolidate._drain_ingest_queue()

    # The row reaches 'extracted' on its FIRST attempt: no UnicodeEncodeError.
    status, saved_n, rejected_n, _attempts, error = _row(qid)
    assert (status, error) == ("extracted", None), f"group failed: {error}"
    assert (drained, saved, saved_n, rejected_n) == (1, 2, 2, 1)

    items = _session_items()
    # A — SANITISED, not dropped: U+FFFD where the orphans were; quote untouched.
    a = items["DRV8706 SPI mode " + REPLACEMENT]
    assert "DRV8706 " + REPLACEMENT + " bus" in a.body
    assert a.meta["quote"] == GROUNDED
    # B — the cleaned quote ("...power-up" + U+FFFD) is not verbatim, so the quote
    # gate's one fuzzy relocation REPAIRS it to the real span; the model's
    # (sanitised) near-miss survives only as audit meta.
    b = items["DRV8706 first SPI frame after power-up"]
    assert b.meta["quote"].strip() == REPAIRABLE and b.meta["quote"] in _assembled()
    assert b.meta["quote_repair"]["model_quote"] == REPAIRABLE + REPLACEMENT
    # C — a quote that is ONLY the orphan grounds nowhere: REJECTED by the gate.
    assert "orphan quote" not in items
    for it in items.values():
        assert it.trust == "unverified"
        (it.title + it.body + json.dumps(it.meta, ensure_ascii=False)).encode("utf-8")

    embeds = [c for c in calls if c["ep"] == "embed"]
    assert embeds, "nothing was embedded — the test did not exercise the encoder"
    if pipeline == "live":
        # The incident's exact call: ensemble._cluster embeds `title: body` VERBATIM
        # (an ingest chunk never takes that shape), now sanitised.
        cluster_text = f"{a.title}: {a.body}"
        assert REPLACEMENT in cluster_text
        assert any(cluster_text in c["input"] for c in embeds), "the clustering embed never ran"


def _assembled() -> str:
    from kbworker.sessions import assemble_text

    return assemble_text(_transcript())


def test_an_orphan_escape_in_the_TRANSCRIPT_is_cleaned_at_assembly(pipeline, monkeypatch):
    """The other entry point: a Node.js slice that split a pair leaves '\\ud83e'
    alone in the JSONL. The prompt, the quote gate and the stored raw must all see
    the same U+FFFD — and the queued JSONL itself stays byte-identical."""
    answer = ANSWER + " Tool output was cut mid-emoji: \ud83e"
    transcript = _transcript(answer)
    assert "\\ud83e" in transcript, "stored as a plain ASCII escape"
    calls = _install_ollama(monkeypatch, _clean_reply())
    qid = ingest_queue.enqueue("motor-ctl", transcript, kind="session").id

    consolidate._drain_ingest_queue()

    status, saved_n, _rej, _attempts, error = _row(qid)
    assert (status, error, saved_n) == ("extracted", None, 1), f"group failed: {error}"
    prompts = [c["prompt"] for c in calls if c["ep"] == "chat"]
    assert prompts and all("cut mid-emoji: " + REPLACEMENT in p for p in prompts)
    with db.connect() as conn:
        (content,) = conn.execute(
            "SELECT content FROM ingest_queue WHERE id = %s", (qid,)
        ).fetchone()
    assert "\\ud83e" in content and REPLACEMENT not in content, "the queued JSONL is untouched"
