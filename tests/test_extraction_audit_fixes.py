"""Extraction-pipeline defects found by the code-grounded audit of 2026-07-26.

The audit read 30 machine-extracted items against the code they described and
found the nightly extractor minting items that are verbatim-GROUNDED yet FALSE,
which consolidation then propagated as authoritative. One test per root cause:

FIX 1  modality gate      — a PLAN quote grounds perfectly; reject it (item 1073
                            asserted a {"status":"ok"} contract in no code path)
FIX 2  unresolved refs    — flag invented identifiers, never strip them
FIX 3  project router     — KB-tooling knowledge belongs in kb-system, not in
                            whatever repo the session ran in (one fact / 9 projects)
FIX 4  tag-bleed filter   — the session's domain vocabulary must not stick to
                            unrelated items (hw:winet on a bash hook runbook)
FIX 5  summary provenance — stamp the weakest source trust; never summarize a
                            flagged item
FIX 6  no phantom code_ref— {"repo": X, "path": "."} verifies nothing
FIX 7  noise filter       — session receipts/telemetry are not knowledge

Real DB in kb_test; the extractor is a stub and embeddings come from conftest
(CLAUDE.md: mock only Ollama).
"""

from __future__ import annotations

import pytest

from kbcore import consolidation, db
from kbcore.config import settings
from kbcore.ingest import ingest
from kbcore.items import get_item
from kbcore.project_router import route_project
from kbcore.quote_modality import is_aspirational
from kbcore.ref_check import unresolved_refs
from kbcore.session_noise import noise_reason
from kbcore.tag_hygiene import filter_bled_tags
from kbworker.sessions import extract_and_ingest

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _tmp_raw(tmp_path, monkeypatch):
    """Keep raw archives off the real ~/kb during tests."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


def _stub(items: list[dict]):
    """An extractor stub returning fixed items (never touches Ollama)."""
    return lambda text: [dict(it) for it in items]


def _items() -> dict[str, object]:
    """Every stored session item, keyed by title (across ALL projects — FIX 3
    re-files individual items, so a project-scoped lookup would hide them)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT i.id, i.title FROM items i JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind = 'session'"
        ).fetchall()
    return {title: get_item(item_id) for item_id, title in rows}


def _details() -> list[dict]:
    with db.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT detail FROM review_queue WHERE reason = 'unverified_extraction'"
            ).fetchall()
        ]


# --- FIX 1: modality gate on the grounding quote ------------------------------

PLAN_TEXT = (
    'Expected Results: the hook returns {"status":"ok"} to the caller.\n\n'
    "$ ./hook.sh\n"
    "local data-server ok bytes=123\n"
)
PLAN_QUOTE = 'Expected Results: the hook returns {"status":"ok"}'
OBSERVED_QUOTE = "$ ./hook.sh\nlocal data-server ok bytes=123"


def test_plan_quote_rejected_observed_quote_kept():
    """The SAME subject: the PLAN is rejected, the OBSERVATION is kept.

    Both quotes are verbatim substrings, so SEC-3 alone cannot tell them apart —
    that is exactly the hole this gate closes.
    """
    assert PLAN_QUOTE in PLAN_TEXT and OBSERVED_QUOTE in PLAN_TEXT, "sanity: both grounded"
    saved, rejected = extract_and_ingest(
        PLAN_TEXT,
        "proj",
        "test://audit/modality",
        extractor=_stub(
            [
                {
                    "type": "spec",
                    "title": "planned response contract",
                    "body": 'The hook returns {"status":"ok","queued":true}.',
                    "quote": PLAN_QUOTE,
                    "tags": [],
                },
                {
                    "type": "note",
                    "title": "observed hook output",
                    "body": "The hook prints a one-line local receipt with a byte count.",
                    "quote": OBSERVED_QUOTE,
                    "tags": [],
                },
            ]
        ),
    )

    assert (saved, rejected) == (1, 1)
    assert set(_items()) == {"observed hook output"}, "the plan never became an item"

    (detail,) = _details()
    assert detail["rejected_count"] == 1
    (rejection,) = detail["quote_rejections"]
    assert rejection["title"] == "planned response contract"
    assert "aspirational quote" in rejection["reason"], "the rejection is auditable"


@pytest.mark.parametrize(
    "quote",
    [
        "Ensure the hook returns a JSON response",
        "verify that the queue drains",
        "execute the following steps",
        "we should pin the pool to 8",
        "the endpoint should return 202",
        "TODO: rotate the token",
        "## Plan\n1. wire the gate",
        "**Expected Results**: a green suite",
    ],
)
def test_aspirational_shapes_are_flagged(quote):
    assert is_aspirational(quote) is not None


@pytest.mark.parametrize(
    "quote",
    [
        "$ ./hook.sh\nlocal data-server ok bytes=123",
        'curl -s /hook returned {"status":"ok"} in 12ms',
        "the run exited with code 1 after we should have seen a 200",
        "HTTP/1.1 401 Unauthorized",
        "Decision: pin the psycopg pool to 8 connections.",
    ],
)
def test_observational_quotes_survive_the_modality_gate(quote):
    """Observed-output evidence always wins: the gate errs toward KEEPING."""
    assert is_aspirational(quote) is None


# --- FIX 2: unresolved-identifier check (flag, never destroy) -----------------

REF_TEXT = (
    "The ingester lives in ingester.py and reads OAUTH_TOKEN from the env.\n\n"
    "Ingest is idempotent because src/kbcore/ingest.py dedupes on the content hash.\n"
)


def test_invented_identifiers_are_flagged_not_stripped():
    saved, rejected = extract_and_ingest(
        REF_TEXT,
        "proj",
        "test://audit/refs",
        extractor=_stub(
            [
                {
                    "type": "note",
                    "title": "ingest entry point",
                    "body": "The ingest entry point is ingester.py and it reads OAUTH_TOKEN.",
                    "quote": "lives in ingester.py and reads OAUTH_TOKEN",
                    "tags": [],
                },
                {
                    "type": "note",
                    "title": "ingest dedupe",
                    "body": "src/kbcore/ingest.py dedupes on the content hash.",
                    "quote": "src/kbcore/ingest.py dedupes on the content hash",
                    "tags": [],
                },
            ]
        ),
    )
    assert (saved, rejected) == (2, 0), "flagging never rejects — the claim may still be right"

    items = _items()
    flagged = items["ingest entry point"]
    # 'proj' has no configured tree, so PATH tokens are deliberately NOT flagged:
    # a path is only checkable against the repo the knowledge is about, and
    # accusing every path in every foreign project produced a 97.4% false-positive
    # rate (703 of 722 open flags, measured 2026-07-30) that buried the real ones.
    # ENV tokens are still checked — they resolve against this process's
    # environment and config, which is meaningful whatever the project — so the
    # OAUTH_TOKEN detection the audit added survives. See
    # test_a_project_with_a_known_tree_is_still_checked for the path half.
    assert set(flagged.meta["unresolved_refs"]) == {"OAUTH_TOKEN"}
    assert flagged.body.count("ingester.py") == 1, "the body is untouched (never stripped)"

    clean = items["ingest dedupe"]
    assert "unresolved_refs" not in clean.meta, "a real repo path resolves"

    # The reviewer AND the triage LLM see the list on the review row — and only
    # on the flagged item's row.
    flagged_details = [d for d in _details() if "unresolved_refs" in d]
    assert len(flagged_details) == 1
    # Same project-aware semantics as the meta assertion above: env token only.
    assert set(flagged_details[0]["unresolved_refs"]) == {"OAUTH_TOKEN"}


# --- FIX 3: project router for KB-tooling knowledge ---------------------------

ROUTER_TEXT = (
    "flush-session-to-kb.sh derives the project from basename $CWD, so one fact "
    "scatters across repos.\n\n"
    "The inverter speaks Modbus TCP on port 502 and reports the DC string voltage.\n"
)


def test_tooling_item_routes_to_kb_system_domain_item_stays():
    saved, rejected = extract_and_ingest(
        ROUTER_TEXT,
        "sungrow",
        "test://audit/router",
        extractor=_stub(
            [
                {
                    "type": "gotcha",
                    "title": "flush hook project naming",
                    "body": (
                        "flush-session-to-kb.sh derives the project from basename $CWD, "
                        "so one fact scatters across repos."
                    ),
                    "quote": "derives the project from basename $CWD",
                    "tags": ["tool:kb"],
                },
                {
                    "type": "note",
                    "title": "inverter modbus port",
                    "body": "The inverter speaks Modbus TCP on port 502.",
                    "quote": "speaks Modbus TCP on port 502",
                    "tags": ["protocol:modbus"],
                },
            ]
        ),
    )
    assert (saved, rejected) == (2, 0)

    items = _items()
    hook = items["flush hook project naming"]
    assert hook.project == "kb-system", "tooling knowledge is re-filed, not left in the repo"
    assert hook.meta["rerouted_from"] == "sungrow", "the reroute is auditable"

    domain = items["inverter modbus port"]
    assert domain.project == "sungrow", "only the tooling ITEM moves, never the whole session"
    assert "rerouted_from" not in domain.meta


def test_local_ingest_still_accepts_the_reserved_kb_system_project():
    """The reserved-name gate lives on the REMOTE queue path only
    (kbcore.ingest_queue.enqueue), so the router's local write is legal."""
    from kbcore.ingest_queue import RESERVED_PROJECTS

    assert "kb-system" in RESERVED_PROJECTS
    item_id = ingest("kb-system", "note", "local write", "a locally-ingested kb-system note")
    assert item_id is not None
    assert get_item(item_id).project == "kb-system"


def test_router_is_per_item_and_never_touches_kb_system_items():
    assert route_project("kb-system", title="t", body="src/kbcore/ingest.py") == ("kb-system", None)
    assert route_project("p", title="t", body="nothing tooling here") == ("p", None)


# --- FIX 4: tag-bleed filter --------------------------------------------------

TAG_TEXT = (
    "Runbook: run the bash hook by hand and tail its log to debug a silent flush.\n\n"
    "The poller uses pymodbus to read holding registers from the inverter.\n"
)


def test_bled_domain_tags_are_dropped_mentioned_ones_survive():
    saved, _rejected = extract_and_ingest(
        TAG_TEXT,
        "sungrow",
        "test://audit/tags",
        extractor=_stub(
            [
                {
                    "type": "runbook",
                    "title": "bash hook runbook",
                    "body": "Run the bash hook by hand and tail its log to debug a silent flush.",
                    "quote": "run the bash hook by hand and tail its log",
                    "tags": ["framework:pymodbus", "hw:winet", "lang:bash", "tool:kb"],
                },
                {
                    "type": "note",
                    "title": "poller library",
                    "body": "The poller uses pymodbus to read holding registers.",
                    "quote": "uses pymodbus to read holding registers",
                    "tags": ["framework:pymodbus"],
                },
            ]
        ),
    )
    assert saved == 2

    items = _items()
    runbook = items["bash hook runbook"]
    assert runbook.tags == ["lang:bash", "tool:kb"], "hw:/framework: bleed dropped"
    assert runbook.meta["dropped_tags"] == ["framework:pymodbus", "hw:winet"], "drop is audited"

    poller = items["poller library"]
    assert poller.tags == ["framework:pymodbus"], "a tag the item actually mentions survives"
    assert "dropped_tags" not in poller.meta


def test_tag_filter_ignores_hyphens_and_leaves_other_namespaces_alone():
    kept, dropped = filter_bled_tags(
        ["hw:sun-grow", "hw:winet", "lang:bash", "protocol:modbus", "domain:solar", "os:linux"],
        "SunGrow string inverter",
        "Notes about the sungrow inverter.",
    )
    assert kept == ["hw:sun-grow", "lang:bash", "protocol:modbus", "domain:solar", "os:linux"]
    assert dropped == ["hw:winet"]


# --- FIX 5: consolidation trust stamp ----------------------------------------


def test_summary_stamps_min_source_trust_and_skips_flagged_items():
    tag = "topic:trust-stamp"
    unver = ingest(
        "p",
        "note",
        "a",
        "a note about the summary trust stamp",
        tags=[tag],
        uri="a",
        trust="unverified",
    )
    ver = ingest(
        "p",
        "note",
        "b",
        "another note about the summary trust stamp",
        tags=[tag],
        uri="b",
        trust="verified",
    )
    flagged = ingest(
        "p",
        "note",
        "c",
        "a note citing ghost.py",
        tags=[tag],
        uri="c",
        trust="verified",
        meta={"unresolved_refs": ["ghost.py"]},
    )

    sources = consolidation.active_items_with_tag("p", tag)
    assert [s["id"] for s in sources] == [unver, ver], "a flagged item never feeds a summary"
    assert get_item(flagged).status == "active", "exclusion is not a status change (Invariant 5)"

    summary_id = consolidation.create_summary("p", tag, "summary body", [s["id"] for s in sources])
    summary = get_item(summary_id)
    assert summary.meta["source_trust"] == "unverified", "the WEAKEST source governs"
    assert summary.meta["source_ids"] == [unver, ver]

    all_verified = consolidation.create_summary("p", "topic:verified-only", "body", [ver])
    assert get_item(all_verified).meta["source_trust"] == "verified"


def test_quarantined_and_archived_items_still_never_feed_a_summary():
    """Pre-existing guarantee, pinned so FIX 5's new WHERE clause cannot regress it."""
    tag = "topic:status-filter"
    active = ingest("p", "note", "live", "an active note on the status filter", tags=[tag], uri="l")
    pending = ingest("p", "note", "held", "a pending note on the filter", tags=[tag], uri="h")
    archived = ingest("p", "note", "old", "an archived note on the filter", tags=[tag], uri="o")
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'pending' WHERE id = %s", (pending,))
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (archived,))

    assert [s["id"] for s in consolidation.active_items_with_tag("p", tag)] == [active]


# --- FIX 6: no phantom code_ref ----------------------------------------------

ANCHOR_TEXT = "Ingest dedupes on the content hash, which is why a re-run is a no-op.\n"
ANCHOR_ITEM = {
    "type": "note",
    "title": "dedupe anchor",
    "body": "A re-run is a no-op because ingest dedupes on the content hash.",
    "quote": "dedupes on the content hash",
    "tags": [],
}


def test_repo_root_code_ref_is_dropped_but_a_file_anchor_round_trips():
    extract_and_ingest(
        ANCHOR_TEXT,
        "proj",
        "test://audit/anchor-phantom",
        code_ref={"repo": "data-server", "path": ".", "commit": "abc1234"},
        extractor=_stub([ANCHOR_ITEM]),
    )
    assert _items()["dedupe anchor"].code_ref is None, "a repo-root anchor verifies nothing"

    extract_and_ingest(
        ANCHOR_TEXT,
        "proj",
        "test://audit/anchor-real",
        code_ref={"repo": "data-server", "path": "src/kbcore/ingest.py", "lines": "131-278"},
        extractor=_stub([{**ANCHOR_ITEM, "title": "dedupe anchor real"}]),
    )
    ref = _items()["dedupe anchor real"].code_ref
    assert ref is not None
    assert (ref.repo, ref.path, ref.lines) == ("data-server", "src/kbcore/ingest.py", "131-278")


def test_session_git_context_survives_as_meta_provenance():
    from kbworker.sessions import _session_provenance

    assert _session_provenance("/home/u/motor-ctl", "a1b2c3d") == {
        "session_repo": "motor-ctl",
        "session_commit": "a1b2c3d",
    }
    assert _session_provenance(None, None) == {}


# --- FIX 7: noise filter at mint time ----------------------------------------

NOISE_TEXT = (
    "The user opened the file app.txt in the IDE. This may or may not be related to "
    "the current task.\n\n"
    "items: 3 chunks: 12 sources: 3\n\n"
    "the smoke test passed\n\n"
    "Run make test before the nightly drain; the smoke test passed once the age "
    "binary was in the image.\n"
)


def test_session_receipts_are_skipped_but_a_real_runbook_survives():
    bodies = {
        "ide telemetry": (
            "The user opened the file app.txt in the IDE. This may or may not be "
            "related to the current task."
        ),
        "counter snapshot": "items: 3 chunks: 12 sources: 3",
        "bare receipt": "the smoke test passed",
        "release runbook": (
            "Run make test before the nightly drain; the smoke test passed once the "
            "age binary was in the image."
        ),
    }
    saved, rejected = extract_and_ingest(
        NOISE_TEXT,
        "proj",
        "test://audit/noise",
        extractor=_stub(
            [
                {"type": "note", "title": t, "body": b, "quote": b, "tags": []}
                for t, b in bodies.items()
            ]
        ),
    )
    assert (saved, rejected) == (1, 0), "noise is SKIPPED, not rejected — it was grounded"
    assert set(_items()) == {"release runbook"}

    (detail,) = _details()
    assert detail["noise_skipped"] == 3, "skips are counted, never silent"
    assert {d["reason"] for d in detail["noise_detail"]} == {
        "IDE telemetry line",
        "counter snapshot",
        "contentless pass/fail receipt",
    }


@pytest.mark.parametrize(
    "body",
    [
        "Postgres 16: max_connections = 100, shared_buffers = 2GB",
        "Set num_ctx=16384 explicitly or Ollama truncates the transcript.",
        "The smoke test passed only after we raised the timeout to 30s.",
        "The deploy worked; see scripts/backup.sh for the exact command.",
    ],
)
def test_noise_filter_favours_false_negatives(body):
    """A wrongly-kept item is cheap; a wrongly-dropped insight is not."""
    assert noise_reason("t", body) is None


def test_unresolved_refs_is_capped_and_ordered():
    text = " ".join(f"ghost{i}.py" for i in range(30))
    refs = unresolved_refs(text)
    assert len(refs) == 12 and refs[0] == "ghost0.py"


# ------------------------------------------------ ref_check is project-aware


def test_refs_are_not_flagged_for_a_project_whose_tree_we_cannot_see(monkeypatch):
    """A reference is only checkable against the repo the knowledge is about.

    ref_check resolved everything against the data-server tree, so a session
    about ANY other project had every path-shaped token flagged: measured
    2026-07-30, 703 of 722 open unresolved_refs flags (97.4%) came from
    aquamonix / i-redis / repeater — files that could not exist here. The noise
    buried the 19 real flags. "I cannot see the tree" is not evidence that a
    reference is broken.
    """
    from kbcore import ref_check

    text = "the fix is in src/driver/main.py and scripts/deploy.sh"
    assert ref_check.unresolved_refs(text, project="aquamonix") == [], (
        "flagged references for a project with no known tree"
    )


def test_a_project_with_a_known_tree_is_still_checked(tmp_path, monkeypatch):
    """Suppressing the false positives must not disable the check where it works."""
    from kbcore import ref_check
    from kbcore.config import settings

    repo = tmp_path / "myproj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "real.py").write_text("x = 1\n")
    monkeypatch.setattr(settings, "kb_git_repos", str(repo))

    refs = ref_check.unresolved_refs("see src/real.py and src/ghost.py", project="myproj")
    assert "src/ghost.py" in refs, "a genuinely missing file was not flagged"
    assert "src/real.py" not in refs, "an existing file was flagged"


def test_an_explicit_repo_root_still_wins(tmp_path):
    """The explicit argument is what the existing tests drive; it must not change."""
    from kbcore import ref_check

    (tmp_path / "here.py").write_text("x = 1\n")
    refs = ref_check.unresolved_refs(
        "here.py and gone.py", project="unknown-project", repo_root=tmp_path
    )
    assert "gone.py" in refs and "here.py" not in refs
