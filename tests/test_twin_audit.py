"""Wave H (review #8 H15 tail): `kbcore.twin_audit` / `kb repair twins-audit`.

The REPORT-ONLY re-judge of every item-item ``alternative_to`` edge with the
current pair judge (KB 31855 / 31857): the mislabeled-twin class becomes visible,
nothing is unlinked, no status flips, no review row is queued — the 297<->294
disposition is KEEP regardless of any verdict. Real DB in kb_test (conftest ``kb``);
the judge (``llm.chat``) is the only mock. Every test that runs the sweep also
asserts it wrote NOTHING, and a mechanical test forbids the module from importing
any write path at all.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import db, graph, llm, scope, twin_audit
from kbcore.errors import ExtractorUnavailable, InvalidOperation
from kbcore.ingest import ingest

pytestmark = pytest.mark.usefixtures("kb")
runner = CliRunner()

REPO = Path(__file__).resolve().parents[1]
ALT = "alternative_to"
PA = "unroll"
PB = "muhammadusama"
MANUAL_DAY = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)  # the 297->294 ADR edge's day


# --- seeding -----------------------------------------------------------------


def _item(project: str, title: str) -> int:
    item_id = ingest(
        project,
        "gotcha",
        title,
        f"{title} — body in {project}",
        uri=f"{project}:{title}",
        trust="unverified",
    )
    assert item_id is not None
    return item_id


def _edge(a: int, b: int) -> int:
    return graph.link(a, ALT, b)


def _backdate(relation_id: int, when: datetime) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE relations SET created_at = %s WHERE id = %s", (when, relation_id))


def _snapshot() -> tuple[int, int, int, int]:
    """(relations, non-active items, review rows, items) — the 'wrote nothing' probe."""
    with db.connect() as conn:
        return (
            conn.execute("SELECT count(*) FROM relations").fetchone()[0],
            conn.execute("SELECT count(*) FROM items WHERE status <> 'active'").fetchone()[0],
            conn.execute("SELECT count(*) FROM review_queue").fetchone()[0],
            conn.execute("SELECT count(*) FROM items").fetchone()[0],
        )


# --- the judge stub (the ONLY mock) --------------------------------------------
# Verdict chosen by a marker in the item titles the judge is shown, so one stub
# serves every class; counts calls so exemption can be proven as "not asked".


class _Judge:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, messages, **_kw) -> str:
        self.calls += 1
        text = messages[-1]["content"]
        if "BOOM" in text:
            raise ExtractorUnavailable("ollama /api/chat unreachable")
        if "GARBLED" in text:
            return "not json at all"
        if "DIFFERENT" in text:
            return '{"same_fact": false, "reason": "different subsystems entirely"}'
        return '{"same_fact": true, "reason": "both record the same gotcha"}'


@pytest.fixture()
def judge(monkeypatch) -> _Judge:
    j = _Judge()
    monkeypatch.setattr(llm, "chat", j)
    return j


def _seed_all() -> dict[str, int]:
    """One edge per class. Returns {class: relation_id}."""
    same = _edge(_item(PA, "same drain gotcha"), _item(PB, "same drain gotcha copy"))
    rejected = _edge(_item(PA, "DIFFERENT fact a"), _item(PB, "DIFFERENT fact b"))
    boom = _edge(_item(PA, "BOOM pair a"), _item(PB, "BOOM pair b"))
    garbled = _edge(_item(PA, "GARBLED pair a"), _item(PB, "GARBLED pair b"))
    manual = _edge(_item(PA, "DIFFERENT adr alternative"), _item(PB, "DIFFERENT adr chosen"))
    _backdate(manual, MANUAL_DAY)
    same_project = _edge(_item(PA, "DIFFERENT sibling adr"), _item(PA, "DIFFERENT sibling chosen"))
    return {
        "same": same,
        "rejected": rejected,
        "boom": boom,
        "garbled": garbled,
        "manual": manual,
        "same_project": same_project,
    }


# --- list_edges ---------------------------------------------------------------


def test_list_edges_returns_every_item_item_alternative_to_edge_oldest_first():
    ids = _seed_all()
    # A typed edge of another kind is never part of the population.
    graph.link(_item(PA, "a fix"), "fixes", _item(PA, "a bug"))

    edges = twin_audit.list_edges()

    assert [e.relation_id for e in edges][0] == ids["manual"], "backdated edge sorts first"
    assert {e.relation_id for e in edges} == set(ids.values())
    manual = next(e for e in edges if e.relation_id == ids["manual"])
    assert (manual.a_project, manual.b_project) == (PA, PB)
    assert manual.a_title == "DIFFERENT adr alternative" and manual.a_status == "active"
    assert manual.created_at == MANUAL_DAY
    assert [e.relation_id for e in twin_audit.list_edges(limit=1)] == [ids["manual"]]


def test_exemption_is_derived_from_the_edge_itself():
    ids = _seed_all()
    by_id = {e.relation_id: e for e in twin_audit.list_edges()}
    assert by_id[ids["manual"]].exempt_reason == twin_audit.EXEMPT_MANUAL
    assert by_id[ids["same_project"]].exempt_reason == twin_audit.EXEMPT_SAME_PROJECT
    assert by_id[ids["same"]].exempt_reason is None
    assert twin_audit.LINKER_EPOCH == datetime(2026, 8, 9, tzinfo=UTC)


# --- audit: classification ----------------------------------------------------


def test_audit_classifies_same_rejected_unjudged_and_exempt(judge):
    ids = _seed_all()

    outcomes = {o.edge.relation_id: o for o in twin_audit.audit(twin_audit.list_edges())}

    assert outcomes[ids["same"]].verdict == twin_audit.SAME
    assert "same gotcha" in outcomes[ids["same"]].rationale
    assert outcomes[ids["rejected"]].verdict == twin_audit.REJECTED
    assert "different subsystems" in outcomes[ids["rejected"]].rationale
    # FAIL-CLOSED is inherited from twin_judge: no verdict is NOT a rejection.
    assert outcomes[ids["boom"]].verdict == twin_audit.UNJUDGED
    assert "unavailable" in outcomes[ids["boom"]].rationale
    assert outcomes[ids["garbled"]].verdict == twin_audit.UNJUDGED
    # Exempt edges are flagged and NOT asked (a GPU call on a settled disposition).
    assert outcomes[ids["manual"]].verdict == twin_audit.EXEMPT
    assert outcomes[ids["manual"]].exempt == twin_audit.EXEMPT_MANUAL
    assert "KB 31855" in outcomes[ids["manual"]].rationale
    assert outcomes[ids["same_project"]].verdict == twin_audit.EXEMPT
    assert outcomes[ids["same_project"]].exempt == twin_audit.EXEMPT_SAME_PROJECT
    assert judge.calls == 4, "exactly the four non-exempt edges reached the judge"

    counts = twin_audit.summarize(list(outcomes.values()))
    assert counts == {
        "edges": 6,
        "same": 1,
        "rejected": 1,
        "unjudged": 2,
        "exempt": 2,
        "exempt_judged": 0,
    }


def test_judge_exempt_asks_about_exempt_edges_but_keeps_the_flag(judge):
    ids = _seed_all()

    outcomes = {
        o.edge.relation_id: o for o in twin_audit.audit(twin_audit.list_edges(), judge_exempt=True)
    }

    assert judge.calls == 6
    manual = outcomes[ids["manual"]]
    assert manual.verdict == twin_audit.REJECTED, "the judge's honest reading is shown"
    assert manual.exempt == twin_audit.EXEMPT_MANUAL, "…and the edge stays exempt"
    assert twin_audit.summarize(list(outcomes.values()))["exempt_judged"] == 2


def test_audit_writes_nothing_and_every_edge_survives(judge):
    ids = _seed_all()
    before = _snapshot()

    twin_audit.audit(twin_audit.list_edges(), judge_exempt=True)

    assert _snapshot() == before, "the audit must not write, flip or queue anything"
    with db.connect() as conn:
        live = {r[0] for r in conn.execute("SELECT id FROM relations WHERE rel = %s", (ALT,))}
    assert live == set(ids.values()), "no edge was unlinked — not even a REJECTED one"


def test_audit_is_owner_only():
    _seed_all()
    grantee = scope.Principal(
        name="reader",
        read_projects=frozenset({PA, PB}),
        write_projects=frozenset(),
        caps=scope.ALL_CAPS,
    )
    with scope.use_principal(grantee):
        with pytest.raises(InvalidOperation, match="owner only"):
            twin_audit.list_edges()
        with pytest.raises(InvalidOperation, match="owner only"):
            twin_audit.audit([])


def test_an_injected_judge_is_asked_the_linker_shape_of_question():
    ids = _seed_all()
    asked: list[tuple] = []

    def fake(a_id: int, a_project: str, b_id: int, b_project: str):
        asked.append((a_id, a_project, b_id, b_project))
        return False, True, "no"

    outcomes = twin_audit.audit(twin_audit.list_edges(), judge=fake)

    same = next(o for o in outcomes if o.edge.relation_id == ids["same"])
    assert (same.edge.a_id, same.edge.a_project, same.edge.b_id, same.edge.b_project) in asked
    assert same.verdict == twin_audit.REJECTED


# --- the CLI -----------------------------------------------------------------


def _text(result) -> str:
    """stdout + stderr + any swallowed exception (CliRunner turns those into exit 1)."""
    try:
        err = result.stderr
    except (ValueError, AttributeError):
        err = ""
    exc = result.exception
    boom = f"\n[exception] {exc!r}" if exc and not isinstance(exc, SystemExit) else ""
    return result.output + err + boom


def test_cli_reports_every_class_and_changes_nothing(judge):
    _seed_all()
    before = _snapshot()

    result = runner.invoke(app, ["repair", "twins-audit"])

    out = _text(result)
    assert result.exit_code == 0, out
    assert "REJECTED" in out and "exempt:manual" in out and "exempt:same_project" in out
    assert "unjudged" in out and "same" in out
    assert "6 alternative_to edge(s): 1 same, 1 REJECTED, 2 unjudged, 2 exempt" in out
    assert "rejected by the pair judge (owner reading list; nothing was changed)" in out
    assert "NOTHING was changed" in out and "KB 31855" in out
    assert "kb graph unlink" not in out, (
        "no unlink tool exists yet; never print a command that does not"
    )
    assert _snapshot() == before


def test_cli_limit_and_judge_exempt_flags(judge):
    ids = _seed_all()

    limited = runner.invoke(app, ["repair", "twins-audit", "--limit", "1"])
    assert limited.exit_code == 0, _text(limited)
    assert "1 alternative_to edge(s)" in _text(limited)
    assert f"#{ids['manual']}" in _text(limited)
    assert judge.calls == 0, "the single oldest edge is exempt, so nothing was asked"

    read = runner.invoke(app, ["repair", "twins-audit", "--judge-exempt"])
    assert read.exit_code == 0, _text(read)
    assert "(2 exempt judged for reading)" in _text(read)
    assert "REJECTED (exempt:manual)" in _text(read)


def test_cli_with_no_edges_says_so(judge):
    result = runner.invoke(app, ["repair", "twins-audit"])
    assert result.exit_code == 0
    assert "no item-item alternative_to edges to audit" in _text(result)
    assert judge.calls == 0


def test_cli_refuses_while_a_consolidation_run_holds_the_lock(judge):
    from kbcore.db import advisory_lock
    from kbworker.consolidate import ADVISORY_KEY

    _seed_all()
    with advisory_lock(ADVISORY_KEY) as acquired:
        assert acquired, "the test could not take the (schema-scoped) nightly lock"
        result = runner.invoke(app, ["repair", "twins-audit"])
    assert result.exit_code == 1
    assert "in flight" in _text(result)
    assert judge.calls == 0, "refused before any judge call"

    # And the probe is TRY-AND-RELEASE: after the run the lock is free again.
    with advisory_lock(ADVISORY_KEY) as acquired_after:
        assert acquired_after


# --- mechanical: the module cannot write ------------------------------------------


def test_twin_audit_imports_no_write_path():
    """AST guard (the H4 bench's guarantee, applied here): the audit module may
    reach the DB only through ``connect`` and the judge only through
    ``twin_judge``; no transaction, no graph.link, no ingest/supersede/review."""
    tree = ast.parse((REPO / "src" / "kbcore" / "twin_audit.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module or ''}.{a.name}" for a in node.names)
    forbidden = (
        "kbcore.db.transaction",
        "kbcore.graph",
        "kbcore.ingest",
        "kbcore.ingest_queue",
        "kbcore.supersede",
        "kbcore.review",
        "kbcore.web_write",
        "kbcore.consolidation",
        "kbcore.twin_crossproject",
        "kbcore.twin_repair",
    )
    hits = {i for i in imported if any(i == f or i.startswith(f + ".") for f in forbidden)}
    assert not hits, f"twin_audit imports a write path: {sorted(hits)}"
    assert {"kbcore.db.connect", "kbcore.scope", "kbcore.twin_judge"} <= imported

    # Every execute() runs the one module constant, and that constant is a SELECT.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "execute":
            assert isinstance(node.args[0], ast.Name) and node.args[0].id == "_EDGES_SQL"
    sql = twin_audit._EDGES_SQL.strip().upper()
    assert sql.startswith("SELECT")
    assert not any(k in sql for k in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"))
