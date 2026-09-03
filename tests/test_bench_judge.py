"""`kb bench judge` / kbcore.bench_judge — the pair-judge agreement probe (review #8 H4).

Guarantees under test: (1) the AST import walls — no write chokepoint reachable from
kbcore.bench_judge at any depth, none at module level in kbcli.bench_cmds, whose
--archive imports lazily; (2) the consolidation advisory-lock probe is TRY-AND-RELEASE
(refuses while held, holds nothing afterwards); (3) hermetic payload tests through the
real llm._chat path (model/think/seed/num_ctx; UNMODIFIED prompts — byte parity with
items_contradict and judge_same_fact pinned); (4) scoring math on fixtures; (5) the
Postgres read-only connection refuses a write; (6) the CLI writes judge-<stamp>.json,
touches no table, and --archive goes through put_document(extract=False) plus one
derived_from link. Ollama is the only mock; DB tests run in kb_test.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import bench_judge, bench_pairs, consolidation, db, graph, llm, twin_judge
from kbcore.bench_judge import KIND_CONTRADICT, KIND_SAME_FACT, BenchPair, Verdict
from kbcore.config import settings
from kbcore.errors import ExtractorUnavailable, InvalidOperation
from kbcore.ingest import ingest

runner = CliRunner()
SRC = Path(__file__).resolve().parent.parent / "src"
BENCH_CORE = SRC / "kbcore" / "bench_judge.py"
BENCH_PAIRS = SRC / "kbcore" / "bench_pairs.py"  # the pair sources, split for the ratchet
BENCH_CLI = SRC / "kbcli" / "bench_cmds.py"


def _out(res) -> str:
    try:
        return res.output + res.stderr
    except (ValueError, AttributeError):
        return res.output


# --- (1) AST import walls ----------------------------------------------------

# Whole modules whose import puts a writer one attribute away.
WRITE_MODULES = {
    "kbcore.db",  # transaction()
    "kbcore.ingest",
    "kbcore.supersede",
    "kbcore.graph",  # link()
    "kbcore.documents",  # put_document()
    "kbcore.items",
    "kbcore.consolidation",  # record_*, mark_duplicate, create_summary
    "kbcore.reprocess",
    "kbcore.twin_repair",
    "kbcore.twin_crossproject",  # apply_links
    "kbcore.web_write",
}
# Names that ARE write chokepoints wherever they come from.
WRITE_NAMES = {
    "transaction",
    "link",
    "ingest",
    "ingest_in",
    "supersede",
    "put_document",
    "mark_duplicate",
    "create_summary",
    "promote",
    "promote_batch",
    "reject",
    "dismiss",
    "apply_links",
}


def _is_write_module(name: str) -> bool:
    return name in WRITE_MODULES or name.startswith("kbcore.review") or name.startswith("kbworker")


def _is_write_name(name: str) -> bool:
    return name in WRITE_NAMES or name.startswith("record_")


def _violations(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names if _is_write_module(a.name)]
    mod = node.module or ""
    names = [a.name for a in node.names]
    out: list[str] = []
    # The ONE prescribed seam: the schema-scoped lock key, nothing else from kbworker.
    prescribed = mod == "kbworker.consolidate" and names == ["ADVISORY_KEY"]
    if _is_write_module(mod) and mod != "kbcore.db" and not prescribed:
        out.append(mod)
    if mod in ("kbcore", "kbworker"):
        out += [f"{mod}.{n}" for n in names if _is_write_module(f"{mod}.{n}")]
    out += [f"{mod}.{n}" for n in names if _is_write_name(n)]
    return out


def _imports_by_scope(path: Path) -> list[tuple[ast.Import | ast.ImportFrom, str | None]]:
    """Every import node with the name of its enclosing function (None = module level)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nested: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Import | ast.ImportFrom):
                    nested.setdefault(id(n), fn.name)
    return [
        (n, nested.get(id(n))) for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom)
    ]


@pytest.mark.parametrize("path", [BENCH_CORE, BENCH_PAIRS], ids=["bench_judge", "bench_pairs"])
def test_bench_modules_import_no_write_chokepoint_at_any_depth(path):
    bad = [(_violations(n), scope) for n, scope in _imports_by_scope(path) if _violations(n)]
    assert not bad, f"{path.name} reaches a write chokepoint: {bad}"
    # ...and never reaches one by attribute either (db.transaction, graph.link, x.record_*).
    tree = ast.parse(path.read_text(encoding="utf-8"))
    attrs = sorted({n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})
    assert not [a for a in attrs if _is_write_name(a)], attrs


def _froms(path: Path) -> set[tuple[str | None, tuple[str, ...]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        (n.module, tuple(sorted(a.name for a in n.names)))
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
    }


def test_bench_modules_declare_exactly_the_read_only_seams_they_rely_on():
    core, pairs = _froms(BENCH_CORE), _froms(BENCH_PAIRS)
    assert ("kbcore.db", ("advisory_lock",)) in core, "the probe, never transaction()"
    assert ("kbworker.consolidate", ("ADVISORY_KEY",)) in core, "the schema-scoped key seam"
    assert ("kbcore.db", ("connect",)) in pairs, "read-only connections only"
    db_names = {n for m, names in core | pairs if m == "kbcore.db" for n in names}
    assert db_names == {"advisory_lock", "connect"}


def test_bench_cmds_is_read_only_at_module_level_and_archive_imports_lazily():
    scoped = _imports_by_scope(BENCH_CLI)
    top = [(_violations(n), n.lineno) for n, s in scoped if s is None and _violations(n)]
    assert not top, f"kbcli.bench_cmds imports a write module at module level: {top}"
    nested = [(n, s) for n, s in scoped if s is not None]
    assert nested, "the --archive path must exist and import lazily"
    assert {s for _n, s in nested} == {"_archive"}, "only _archive may import a writer"
    lazily = {(n.module, tuple(a.name for a in n.names)) for n, _s in nested}
    assert lazily == {("kbcore", ("documents", "graph"))}


def test_kb_bench_is_wired_once_with_the_judge_command():
    res = runner.invoke(app, ["bench", "--help"])
    assert res.exit_code == 0, _out(res)
    assert "judge" in res.output
    res = runner.invoke(app, ["bench", "judge", "--help"])
    assert res.exit_code == 0, _out(res)
    for flag in ("--models", "--think", "--pairs", "--sample", "--negatives", "--out", "--archive"):
        assert flag in res.output


# --- (2) advisory-lock probe: try-and-release --------------------------------


def test_gpu_slot_probe_refuses_while_held_and_holds_nothing_afterwards(kb):
    from kbworker.consolidate import ADVISORY_KEY

    with db.advisory_lock(ADVISORY_KEY) as held:
        assert held
        with pytest.raises(InvalidOperation, match="advisory lock"):
            bench_judge.assert_gpu_slot_free()
    bench_judge.assert_gpu_slot_free()  # free now: passes ...
    with db.advisory_lock(ADVISORY_KEY) as held_again:
        assert held_again, "... and RELEASED its probe: try-and-release, never held for the run"


# --- (3) hermetic payloads through llm._chat ---------------------------------


def _capture_post(store: dict, content: str):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"message": {"content": content}}

    def post(url, *, json=None, timeout=None):
        store["payload"] = json
        return _Resp()

    return post


@pytest.fixture
def rows(monkeypatch):
    monkeypatch.setattr(
        bench_judge,
        "_pair_rows",
        lambda a, b: (("unroll", "A title", "A body"), ("muhammadusama", "B title", "B body")),
    )
    monkeypatch.setattr(
        twin_judge, "item_pair_texts", lambda a, b: (("A title", "A body"), ("B title", "B body"))
    )


SAME = BenchPair(1, 2, KIND_SAME_FACT, True, "edge")
CONTRA = BenchPair(1, 2, KIND_CONTRADICT, False, "judged")


def test_same_fact_payload_carries_model_think_seed_num_ctx_and_the_unmodified_prompt(
    rows, monkeypatch
):
    store: dict = {}
    monkeypatch.setattr(
        llm.httpx, "post", _capture_post(store, '{"same_fact": true, "reason": "tw"}')
    )
    v = bench_judge.judge_pair(SAME, model="granite4.2:8b", think=False)
    p = store["payload"]
    assert p["model"] == "granite4.2:8b" and p["think"] is False and p["format"] == "json"
    assert p["options"] == {
        "num_ctx": settings.kb_llm_num_ctx,
        "seed": llm._draw_seed("A title", "A body", "B title", "B body"),
    }
    assert p["messages"][0]["content"] == twin_judge.SAME_FACT_SYSTEM
    user = p["messages"][1]["content"]
    assert "BEGIN ITEM_A" in user and "(project unroll)" in user and "A body" in user
    assert user.endswith("Do ITEM_A and ITEM_B record the same underlying fact?")
    assert (v.verdict, v.rationale, v.label, v.kind) == (True, "tw", True, KIND_SAME_FACT)
    assert v.secs >= 0.0


def test_think_default_omits_the_key_and_think_on_sends_true(rows, monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, '{"contradict": false}'))
    bench_judge.judge_pair(CONTRA, model="qwen3:14b", think=None)
    assert "think" not in store["payload"] and store["payload"]["model"] == "qwen3:14b"
    bench_judge.judge_pair(CONTRA, model="qwen3:14b", think=True)
    assert store["payload"]["think"] is True


def test_contradict_request_is_byte_identical_to_items_contradict(rows, monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, '{"contradict": false}'))
    monkeypatch.setattr(settings, "kb_pair_judge_model", "granite4.2:8b")
    monkeypatch.setattr(settings, "kb_pair_judge_think", False)
    llm.items_contradict("A title", "A body", "B title", "B body")
    production = json.dumps(store["payload"])
    bench_judge.judge_pair(CONTRA, model="granite4.2:8b", think=False)
    assert json.dumps(store["payload"]) == production


def test_same_fact_request_matches_judge_same_fact_up_to_the_per_call_nonce(rows, monkeypatch):
    store: dict = {}
    monkeypatch.setattr(llm.httpx, "post", _capture_post(store, '{"same_fact": false}'))
    monkeypatch.setattr(settings, "kb_pair_judge_model", "granite4.2:8b")
    monkeypatch.setattr(settings, "kb_pair_judge_think", False)
    twin_judge.judge_same_fact(1, "unroll", 2, "muhammadusama")
    production = json.dumps(store["payload"])
    bench_judge.judge_pair(SAME, model="granite4.2:8b", think=False)
    bench = json.dumps(store["payload"])
    strip = re.compile(r"ITEM_([AB]) [0-9a-f]{16}")
    assert strip.sub(r"ITEM_\1 <nonce>", bench) == strip.sub(r"ITEM_\1 <nonce>", production)
    assert bench != production, "a fresh nonce per call, never reused"


def test_an_unavailable_judge_is_unjudged_not_a_negative(rows, monkeypatch):
    def boom(*_a, **_k):
        raise ExtractorUnavailable("ollama unreachable")

    monkeypatch.setattr(llm, "_chat", boom)
    v = bench_judge.judge_pair(SAME, model="m", think=False)
    assert v.verdict is None and v.rationale.startswith("judge unavailable")


@pytest.mark.parametrize("body", ['{"same_fact": "true"}', '{"same_fact": 1}', "nope", "[1]"])
def test_a_non_boolean_same_fact_is_no_verdict(rows, monkeypatch, body):
    monkeypatch.setattr(llm, "_chat", lambda *_a, **_k: body)
    assert bench_judge.judge_pair(SAME, model="m", think=False).verdict is None


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"contradict": false}', False),
        ('{"contradict": true}', True),
        ('{"contradict": "false"}', False),
        ('{"contradict": "true"}', True),
        ("not json", False),
        ('{"other": 1}', False),
        ("[1,2,3]", False),
    ],
)
def test_contradict_verdict_parity_with_items_contradict(monkeypatch, payload, expected):
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: payload)
    assert bench_judge.contradict_verdict(payload) is expected
    assert bench_judge.contradict_verdict(payload) is llm.items_contradict("a", "x", "b", "y")


# --- (4) scoring math ----------------------------------------------------------


def _v(label: bool, verdict: bool | None, secs: float) -> Verdict:
    return Verdict(1, 2, KIND_CONTRADICT, label, verdict, "", secs)


def test_score_math_on_fixtures():
    s = bench_judge.score(
        [
            _v(True, True, 5.0),
            _v(True, False, 1.0),
            _v(False, False, 1.0),
            _v(False, True, 1.0),
            _v(True, None, 0.5),
        ]
    )
    assert (s["pairs"], s["judged"], s["judged_rate"]) == (5, 4, 0.8)
    assert s["agreement"] == 0.5, "over JUDGED pairs only"
    assert (s["tp"], s["tn"], s["fp"], s["fn"]) == (1, 1, 1, 1)
    assert s["s_per_pair_median"] == 1.0 and s["total_s"] == 8.5 and s["s_per_pair_mean"] == 1.7
    assert s["first_call_s"] == 5.0 and s["cold_load_s"] == 4.0


def test_score_of_nothing_is_zeros_not_a_division_error():
    s = bench_judge.score([])
    assert s["pairs"] == 0 and s["agreement"] == 0.0 and s["judged_rate"] == 0.0
    assert s["s_per_pair_median"] == 0.0 and s["cold_load_s"] == 0.0


def test_summary_lines_name_model_think_agreement_and_speed():
    result = {
        "configs": [
            {
                "model": "granite4.2:8b",
                "think": False,
                "score": bench_judge.score([_v(True, True, 0.7)]),
            },
            {
                "model": "qwen3:14b",
                "think": None,
                "score": bench_judge.score([_v(True, None, 12.0)]),
            },
        ]
    }
    a, b = bench_judge.summary_lines(result)
    assert (
        a.startswith("granite4.2:8b") and "think=off" in a and "agreement 1.000 (1/1 judged)" in a
    )
    assert "0.70 s/pair" in a and "FP 0 FN 0" in a
    assert "think=default" in b and "agreement 0.000 (0/1 judged)" in b
    assert bench_judge.THINK_MODES == {
        "on": [True],
        "off": [False],
        "both": [True, False],
        "default": [None],
    }


# --- (5) read-only connections + pair sources (kb_test) ------------------------


def test_read_only_connection_refuses_a_write(kb):
    assert bench_judge._read_only is bench_pairs._read_only, "one seam, re-exported"
    with bench_judge._read_only() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(
                "INSERT INTO contradiction_judgements (a_id, b_id, embed_model, verdict) "
                "VALUES (1, 2, 'm', false)"
            )


def _item(project: str, n: int) -> int:
    item_id = ingest(
        project,
        "gotcha",
        f"t{n}",
        f"body {n} [{project}]",
        uri=f"{project}:{n}",
        trust="unverified",
    )
    assert item_id is not None
    return item_id


def test_edge_pairs_label_alternative_to_edges_true_and_flag_pre_linker_edges(kb):
    a, b = _item("unroll", 1), _item("muhammadusama", 2)
    assert bench_judge.edge_pairs() == []
    graph.link(a, "alternative_to", b)
    (pair,) = bench_judge.edge_pairs()
    assert pair == BenchPair(a, b, KIND_SAME_FACT, True, "edge", manual=False)
    with db.transaction() as conn:  # a 07-03 ADR-sense edge predates the linker
        conn.execute(
            "UPDATE relations SET created_at = '2026-07-03T00:00:00Z' WHERE rel = 'alternative_to'"
        )
    assert bench_judge.edge_pairs()[0].manual is True


def test_judged_pairs_carry_the_incumbent_verdict_positives_first_at_this_embed_model(kb):
    ids = [_item("unroll", n) for n in range(4)]
    assert consolidation.record_pair_judgement(ids[0], ids[1], False)
    assert consolidation.record_pair_judgement(ids[1], ids[2], True)
    assert consolidation.record_pair_judgement(ids[2], ids[3], False)
    pairs = bench_judge.judged_pairs(sample=10)
    assert len(pairs) == 3 and pairs[0].label is True
    assert {(p.a_id, p.b_id, p.label) for p in pairs} == {
        (ids[0], ids[1], False),
        (ids[1], ids[2], True),
        (ids[2], ids[3], False),
    }
    assert all(p.kind == KIND_CONTRADICT and p.source == "judged" for p in pairs)
    capped = bench_judge.judged_pairs(sample=2)
    assert len(capped) == 2 and capped[0].label is True, "positives first, then the draw"
    # A judgement written under ANOTHER embed model is not this model's memory (017 keying).
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO contradiction_judgements (a_id, b_id, embed_model, verdict) "
            "VALUES (%s, %s, 'previous-embedder', true)",
            (ids[0], ids[3]),
        )
    assert len(bench_judge.judged_pairs(sample=10)) == 3


def test_load_pairs_reads_a_plain_list_or_a_previous_results_file(tmp_path):
    plain = tmp_path / "pairs.json"
    plain.write_text(json.dumps([{"a_id": 1, "b_id": 2, "kind": "same_fact", "label": True}]))
    assert bench_judge.load_pairs(str(plain), sample=0, negatives=0) == [
        BenchPair(1, 2, KIND_SAME_FACT, True, "file")
    ]
    results = tmp_path / "judge-20260903-010203.json"
    results.write_text(json.dumps({"pairs": [asdict(CONTRA), asdict(SAME)], "configs": []}))
    got = bench_judge.load_pairs(str(results), sample=0, negatives=0)
    assert [(p.a_id, p.b_id, p.kind, p.label) for p in got] == [
        (1, 2, KIND_CONTRADICT, False),
        (1, 2, KIND_SAME_FACT, True),
    ]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"a_id": 1, "b_id": 2, "kind": "vibes", "label": True}]))
    with pytest.raises(InvalidOperation, match="kind"):
        bench_judge.load_pairs(str(bad), sample=0, negatives=0)
    with pytest.raises(InvalidOperation, match="none of those"):
        bench_judge.load_pairs(str(tmp_path / "missing.json"), sample=0, negatives=0)
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    with pytest.raises(InvalidOperation, match="no pairs"):
        bench_judge.load_pairs(str(empty), sample=0, negatives=0)


# --- (6) the CLI ---------------------------------------------------------------


def _stub_chat(monkeypatch) -> list[dict]:
    """The only mock: answers same-fact TRUE and contradict FALSE, recording each call."""
    calls: list[dict] = []

    def fake(messages, *, json_format, model=None, keep_alive=None, seed=None, think=None):
        calls.append({"model": model, "think": think, "seed": seed, "json": json_format})
        if messages[0]["content"] == twin_judge.SAME_FACT_SYSTEM:
            return '{"same_fact": true, "reason": "same drain gotcha"}'
        assert messages[0]["content"] == llm._CONTRADICT_SYSTEM
        return '{"contradict": false}'

    monkeypatch.setattr(llm, "_chat", fake)
    return calls


def _table_counts() -> tuple[int, int, int]:
    with db.connect() as conn:
        return tuple(  # type: ignore[return-value]
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("relations", "contradiction_judgements", "items")
        )


def test_cli_judges_every_config_writes_the_json_prints_agreement_and_writes_no_row(
    kb, tmp_path, monkeypatch
):
    a, b = _item("unroll", 1), _item("muhammadusama", 2)
    graph.link(a, "alternative_to", b)
    calls = _stub_chat(monkeypatch)
    before = _table_counts()
    res = runner.invoke(
        app,
        [
            "bench",
            "judge",
            "--models",
            "granite4.2:8b,qwen3:14b",
            "--think",
            "both",
            "--pairs",
            "edges",
            "--out",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, _out(res)
    assert _table_counts() == before, "the bench writes NOTHING to the database"
    (path,) = tmp_path.glob("judge-*.json")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert (
        doc["incumbent"] == settings.kb_extract_model
        and doc["embed_model"] == settings.kb_embed_model
    )
    assert doc["pairs"] == [asdict(BenchPair(a, b, KIND_SAME_FACT, True, "edge"))]
    configs = {(c["model"], c["think"]) for c in doc["configs"]}
    assert configs == {
        ("granite4.2:8b", True),
        ("granite4.2:8b", False),
        ("qwen3:14b", True),
        ("qwen3:14b", False),
    }
    assert all(
        c["score"] == bench_judge.score([Verdict(**v) for v in c["verdicts"]])
        for c in doc["configs"]
    )
    assert all(c["score"]["agreement"] == 1.0 and c["score"]["judged"] == 1 for c in doc["configs"])
    assert {(c["model"], c["think"]) for c in calls} == configs and all(c["json"] for c in calls)
    (a_title, a_body), (b_title, b_body) = consolidation.item_pair_texts(a, b)
    assert {c["seed"] for c in calls} == {llm._draw_seed(a_title, a_body, b_title, b_body)}
    out = _out(res)
    assert "1 pair(s): 1 same-fact, 0 contradict; 1 labelled true; 4 config(s)" in out
    assert out.count("agreement 1.000 (1/1 judged)") == 4 and f"results: {path}" in out
    if str(path.resolve()).startswith("/tmp/"):
        assert "does not survive a reboot" in out


def test_cli_judged_pairs_score_against_the_incumbent_verdicts(kb, tmp_path, monkeypatch):
    ids = [_item("unroll", n) for n in range(3)]
    consolidation.record_pair_judgement(ids[0], ids[1], True)  # the stub answers false ⇒ FN
    consolidation.record_pair_judgement(ids[1], ids[2], False)  # ⇒ TN
    _stub_chat(monkeypatch)
    res = runner.invoke(
        app,
        [
            "bench",
            "judge",
            "--models",
            "granite4.2:8b",
            "--think",
            "off",
            "--pairs",
            "judged",
            "--sample",
            "30",
            "--out",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, _out(res)
    (path,) = tmp_path.glob("judge-*.json")
    (cfg,) = json.loads(path.read_text(encoding="utf-8"))["configs"]
    assert (cfg["model"], cfg["think"]) == ("granite4.2:8b", False)
    assert cfg["score"]["pairs"] == 2 and cfg["score"]["agreement"] == 0.5
    assert (cfg["score"]["fn"], cfg["score"]["tn"], cfg["score"]["fp"]) == (1, 1, 0)
    assert "agreement 0.500 (2/2 judged)  FP 0 FN 1" in res.output


def test_cli_archive_uses_put_document_extract_false_and_links_derived_from(
    kb, tmp_path, monkeypatch
):
    from kbcore import documents

    a, b = _item("unroll", 1), _item("muhammadusama", 2)
    graph.link(a, "alternative_to", b)
    _stub_chat(monkeypatch)
    seen: dict = {}

    class _Doc:
        item_id, source_id, uri = 777, 9, "doc://kb-system/judge.json"

    def fake_put(project, name, *, content, note=None, tags=None, extract=True, **_kw):
        seen.update(project=project, name=name, content=content, tags=tags, extract=extract)
        return _Doc()

    links: list[tuple] = []
    monkeypatch.setattr(documents, "put_document", fake_put)
    monkeypatch.setattr(
        graph, "link", lambda s, rel, d, evidence_item=None: links.append((s, rel, d)) or 1
    )
    res = runner.invoke(
        app,
        [
            "bench",
            "judge",
            "--models",
            "granite4.2:8b",
            "--think",
            "off",
            "--pairs",
            "edges",
            "--out",
            str(tmp_path),
            "--archive",
            "--decision",
            "31857",
        ],
    )
    assert res.exit_code == 0, _out(res)
    assert seen["project"] == "kb-system" and seen["extract"] is False
    assert seen["name"].startswith("judge-") and seen["name"].endswith(".json")
    assert json.loads(seen["content"])["configs"][0]["model"] == "granite4.2:8b"
    assert links == [(777, "derived_from", 31857)]
    assert "archived: doc://kb-system/judge.json (source 9, derived_from #31857)" in res.output


def test_cli_refuses_while_a_consolidation_holds_the_lock_and_calls_no_model(
    kb, tmp_path, monkeypatch
):
    from kbworker.consolidate import ADVISORY_KEY

    monkeypatch.setattr(llm, "_chat", lambda *a, **k: pytest.fail("no LLM call while refused"))
    with db.advisory_lock(ADVISORY_KEY) as held:
        assert held
        res = runner.invoke(
            app, ["bench", "judge", "--models", "m", "--pairs", "edges", "--out", str(tmp_path)]
        )
    assert res.exit_code == 1 and "advisory lock" in _out(res)
    assert not list(tmp_path.glob("judge-*.json"))


def test_cli_rejects_a_bad_think_mode_and_an_empty_model_list_before_touching_anything(
    monkeypatch,
):
    monkeypatch.setattr(bench_judge, "assert_gpu_slot_free", lambda: pytest.fail("touched the DB"))
    res = runner.invoke(app, ["bench", "judge", "--models", "m", "--think", "maybe"])
    assert res.exit_code == 1 and "--think must be one of" in _out(res)
    res = runner.invoke(app, ["bench", "judge", "--models", " , "])
    assert res.exit_code == 1 and "--models needs at least one model" in _out(res)
