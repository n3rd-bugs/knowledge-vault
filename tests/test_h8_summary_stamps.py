"""Review #8 H8 — summaries: todo == writable work; no-yield tags stop re-trying.

(a) ``tags_needing_summary`` applies the SAME ``SUMMARY_EXCLUDING_META`` predicate
as ``active_items_with_tag``, so a tag whose fresh items are all pipeline-flagged
(the ~113-tag 'deferred' residue) is never selected. (b) a tag the summarizer
could not summarize (empty body) or whose feeding set came back <3 is stamped in
KB_STATE_DIR and skipped until a NEW item arrives for it — tried once, not
nightly. The stamp is a non-knowledge marker; nothing here writes, flips or
deletes a knowledge row.

Real DB (kb_test); the summarizer is patched (Ollama never called); the state dir
is the autouse tmp one.
"""

from __future__ import annotations

import pytest

from kbcore import consolidation
from kbcore.ingest import ingest
from kbworker import summary_stamps, tail_ops


def _tagged(project: str, tag: str, n: int, *, meta: dict | None = None, prefix: str = "d"):
    return [
        ingest(
            project,
            "decision",
            f"{prefix}{i} {tag}",
            f"body {prefix}{i} {tag}",
            tags=[tag],
            uri=f"{project}:{tag}:{prefix}{i}",
            meta=meta,
        )
        for i in range(n)
    ]


# --- (a) the residue never enters todo -----------------------------------------


def test_a_tag_whose_fresh_items_are_all_flagged_is_never_selected(kb):
    _tagged("p", "hw:residue", 3, meta={"unresolved_refs": ["U1"]})
    _tagged("p", "hw:aspire", 3, meta={"aspirational": True}, prefix="a")
    assert consolidation.tags_needing_summary(None) == [], "all-flagged tags are residue, not work"

    # The exclusion is per ITEM: 2 clean + 2 flagged is still under the floor...
    _tagged("p", "hw:mixed", 2)
    _tagged("p", "hw:mixed", 2, meta={"unresolved_refs": ["U2"]}, prefix="f")
    assert consolidation.tags_needing_summary(None) == []
    # ...and the third CLEAN item selects it, with exactly the feeding set's size.
    ingest("p", "decision", "d2 hw:mixed", "body d2 hw:mixed", tags=["hw:mixed"], uri="p:mixed:d2")
    assert consolidation.tags_needing_summary(None) == [("p", "hw:mixed", 3)]
    assert len(consolidation.active_items_with_tag("p", "hw:mixed")) == 3


def test_the_step_no_longer_pays_a_feeding_query_per_residue_tag(kb, monkeypatch):
    _tagged("p", "hw:residue", 3, meta={"unresolved_refs": ["U1"]})
    monkeypatch.setattr(
        tail_ops.consolidation,
        "active_items_with_tag",
        lambda p, t: pytest.fail("a residue tag reached the feeding query"),
    )
    monkeypatch.setattr(
        tail_ops.llm, "summarize_items", lambda t, items: pytest.fail("summarizer called")
    )
    assert tail_ops._summaries(None) == 0


# --- (b) a no-yield tag is tried once ------------------------------------------


def test_an_empty_body_tag_is_tried_once_then_skipped_until_a_new_item_arrives(
    kb, monkeypatch, tmp_path
):
    tag = "lang:rust"
    _tagged("p", tag, 3)
    calls: list[str] = []
    monkeypatch.setattr(tail_ops.llm, "summarize_items", lambda t, items: calls.append(t) or "")

    assert tail_ops._summaries(None) == 0
    assert calls == [tag]
    path = summary_stamps.stamp_path()
    assert str(path).startswith(str(tmp_path)), "the stamp lives under the (tmp) KB_STATE_DIR"
    assert summary_stamps.load()[f"p\t{tag}"]["why"] == "empty_body"
    assert consolidation.tags_needing_summary(None) == [("p", tag, 3)], (
        "the DATA still says the tag needs a summary..."
    )

    assert tail_ops._summaries(None) == 0
    assert calls == [tag], "...but a stamped tag is not re-tried while nothing changed"

    ingest("p", "decision", f"d3 {tag}", f"body d3 {tag}", tags=[tag], uri=f"p:{tag}:d3")
    monkeypatch.setattr(
        tail_ops.llm, "summarize_items", lambda t, items: calls.append(t) or "A real summary."
    )
    assert tail_ops._summaries(None) == 1
    assert calls == [tag, tag], "a new item re-opens the tag"
    assert f"p\t{tag}" not in summary_stamps.load(), "a written summary clears the stamp"
    assert consolidation.tags_needing_summary(None) == []


def test_a_too_small_feeding_set_is_stamped_too(kb, monkeypatch):
    tag = "tool:make"
    _tagged("p", tag, 3)
    two = [{"id": 1, "title": "a", "body": "b", "trust": "verified"}] * 2
    monkeypatch.setattr(tail_ops.consolidation, "active_items_with_tag", lambda p, t: two)
    monkeypatch.setattr(
        tail_ops.llm,
        "summarize_items",
        lambda t, items: pytest.fail("summarizer called on <3 sources"),
    )
    assert tail_ops._summaries(None) == 0
    assert summary_stamps.load()[f"p\t{tag}"]["why"] == "too_few_sources"

    monkeypatch.setattr(
        tail_ops.consolidation,
        "active_items_with_tag",
        lambda p, t: pytest.fail("a stamped tag was re-tried with nothing new"),
    )
    assert tail_ops._summaries(None) == 0


def test_a_stand_down_reports_only_writable_work(kb, monkeypatch):
    """The 'deferred' number: neither the residue (never selected) nor a stamped
    tag (skipped) counts — only tags this run would actually have written."""
    _tagged("p", "a:stamped", 3)
    _tagged("p", "b:live", 3, prefix="l")
    _tagged("p", "c:residue", 3, meta={"aspirational": True}, prefix="r")
    summary_stamps.record(summary_stamps.load(), "p", "a:stamped", 3, "empty_body")
    monkeypatch.setattr(
        tail_ops.llm, "summarize_items", lambda t, items: pytest.fail("wrote past the guard")
    )
    tail_ops._STOP_LOG.clear()
    try:
        assert tail_ops._summaries(None, lambda: "deadline") == 0
        assert tail_ops._STOP_LOG["summaries_written"] == {"reason": "deadline", "deferred": 1}
    finally:
        tail_ops._STOP_LOG.clear()


def test_the_stamps_fail_open_and_tolerate_garbage(kb, monkeypatch):
    path = summary_stamps.stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert summary_stamps.load() == {}

    _tagged("p", "x:y", 3)
    monkeypatch.setattr(tail_ops.llm, "summarize_items", lambda t, items: "Written.")
    assert tail_ops._summaries(None) == 1, "a corrupt marker never fails the step"

    assert summary_stamps.skip({"p\tq": {"new_count": "junk"}}, "p", "q", 3) is False
    assert summary_stamps.skip({"p\tq": "garbage"}, "p", "q", 3) is False
    assert summary_stamps.skip({"p\tq": {"new_count": 3}}, "p", "q", 3) is True
    assert summary_stamps.skip({"p\tq": {"new_count": 3}}, "p", "q", 4) is False
    assert summary_stamps.skip({}, "p", "q", 3) is False
