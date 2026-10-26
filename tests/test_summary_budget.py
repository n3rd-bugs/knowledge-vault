"""Summary input budgets and provenance for exactly the sources the model sees."""

from __future__ import annotations

import logging

import pytest

from kbcore import consolidation, llm
from kbcore.chunk import count_tokens
from kbcore.config import settings
from kbworker import prep, summary_budget, summary_stamps, tail_ops


def _sources(n: int, words: int = 70) -> list[dict]:
    return [{"id": i, "title": f"Source {i}", "body": "fact " * words} for i in range(1, n + 1)]


def _prompt_tokens(topic: str, items: list[dict]) -> int:
    corpus = "\n\n".join(f"### {it.get('title', '')}\n{it.get('body', '')}".strip() for it in items)
    return count_tokens(llm._SUMMARIZE_SYSTEM) + count_tokens(
        f"Topic: {topic}\n\nItems:\n{corpus}\n\nWrite the summary."
    )


def test_pure_fitting_corpus_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    sources = _sources(3, 5)
    kept, left_out = summary_budget.fit_summary_sources("lang:python", sources)
    assert left_out == 0
    assert len(kept) == len(sources)
    assert all(a is b for a, b in zip(kept, sources, strict=True))


def test_pure_newest_first_and_full_prompt_fits(monkeypatch):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    sources = _sources(20)
    kept, left_out = summary_budget.fit_summary_sources("lang:python", sources)
    assert 3 <= len(kept) < len(sources)
    assert kept == sources[-len(kept) :]
    assert left_out == len(sources) - len(kept)
    assert _prompt_tokens("lang:python", kept) <= prep._token_ceiling()
    assert _prompt_tokens("lang:python", sources[-len(kept) - 1 :]) > prep._token_ceiling()
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 16384)
    assert summary_budget.fit_summary_sources("lang:python", sources)[1] == 0


def test_pure_oversized_source_skipped(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    sources = _sources(3, 5) + [{"id": 4, "body": "huge " * 2000}]
    with caplog.at_level(logging.INFO):
        kept, left_out = summary_budget.fit_summary_sources("topic", sources)
    assert kept == sources[:3]
    assert left_out == 1
    assert "summary source 4" in caplog.text
    assert _prompt_tokens("topic", kept) <= prep._token_ceiling()


def test_pure_empty_and_wrapper_over_budget(monkeypatch):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    assert summary_budget.fit_summary_sources("topic", []) == ([], 0)
    assert summary_budget.fit_summary_sources("topic " * 1000, _sources(3, 1)) == ([], 3)


def test_pure_too_few_fitted_stamped_and_stats_reset(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    monkeypatch.setattr(settings, "kb_state_dir", tmp_path)
    sources = _sources(2, 5) + [{"id": 3, "body": "huge " * 2000}]
    monkeypatch.setattr(consolidation, "tags_needing_summary", lambda _: [("p", "tag", 3)])
    monkeypatch.setattr(consolidation, "active_items_with_tag", lambda *a: sources)

    def unexpected(*args):
        pytest.fail("fewer than three fitted sources must not call the model or write a summary")

    monkeypatch.setattr(llm, "summarize_items", unexpected)
    monkeypatch.setattr(consolidation, "create_summary", unexpected)
    with caplog.at_level(logging.WARNING):
        assert tail_ops._summaries(None) == 0
    assert summary_stamps.load()["p\ttag"]["why"] == "too_few_sources"
    assert tail_ops._SUMMARY_STATS == {"summaries_capped": 1, "summary_sources_left_out": 1}
    assert len(caplog.records) == 1
    assert "project=p tag=tag kept 2/3" in caplog.text
    assert tail_ops._summaries(None) == 0
    assert tail_ops._SUMMARY_STATS == {"summaries_capped": 0, "summary_sources_left_out": 0}


@pytest.mark.parametrize("large", [True, False])
def test_db_summary_links_only_fitted_sources(kb, monkeypatch, caplog, large):
    from kbcore import db
    from kbcore.ingest import ingest
    from kbcore.items import get_item
    from kbworker import consolidate

    monkeypatch.setattr(settings, "kb_llm_num_ctx", 2048)
    tag = "lang:budget"
    ids = [
        ingest(
            "p",
            "decision",
            s["title"],
            s["body"],
            tags=[tag],
            trust="unverified" if large and s["id"] == 1 else "verified",
        )
        for s in _sources(20 if large else 3, 70 if large else 5)
    ]
    sources = consolidation.active_items_with_tag("p", tag)
    kept, left_out = summary_budget.fit_summary_sources(tag, sources)
    assert [s["id"] for s in kept] == ids[-len(kept) :]
    assert bool(left_out) == large
    seen = []

    def summarize(topic, items):
        assert topic == tag
        seen.extend(s["id"] for s in items)
        return "A grounded summary."

    monkeypatch.setattr(llm, "summarize_items", summarize)
    # Exercise the real run/report merge without unrelated nightly work.
    monkeypatch.setattr(consolidate, "_dedupe", lambda p: 0)
    monkeypatch.setattr(llm, "items_contradict", lambda *a, **kw: False)
    with caplog.at_level(logging.WARNING):
        counts = consolidate.main()
    assert seen == [s["id"] for s in kept]
    assert counts["summaries_capped"] == int(large)
    assert counts["summary_sources_left_out"] == left_out
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM items WHERE project=%s AND type='summary' AND meta->>'topic'=%s",
            ("p", tag),
        ).fetchone()
        assert row is not None
        linked = conn.execute(
            "SELECT dst_id FROM relations WHERE src_id=%s AND rel='derived_from' ORDER BY dst_id",
            (row[0],),
        ).fetchall()
    assert [r[0] for r in linked] == seen
    assert get_item(row[0]).meta["source_ids"] == seen
    assert get_item(row[0]).meta["source_trust"] == "verified"
    warnings = [r.message for r in caplog.records if "token ceiling=" in r.message]
    assert len(warnings) == int(large)
    if large:
        assert f"project=p tag={tag} kept {len(kept)}/{len(ids)}" in warnings[0]
        assert str(prep._token_ceiling()) in warnings[0]
