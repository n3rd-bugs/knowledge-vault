"""Wave-E hygiene pins (review #4 Sonnet catches, KB 21332).

1. The triage JSON-path SQL now has ONE owner (kbcore.triage_sql; the 300-line
   ratchet kept it out of review_triage itself): the helpers must render
   BYTE-IDENTICAL to the literals the five modules used to copy-paste, and no
   src module outside the owner may spell the literal again.
2. .env.example must never ship the forbidden KB_LLM_KEEP_ALIVE=-1 (CLAUDE.md
   rule 8) and documents the weekly knobs.

Pure filesystem/string checks — no DB, no network.
"""

from __future__ import annotations

from pathlib import Path

from kbcore import review_auto, review_expire, review_triage, triage_sql

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
OWNER = SRC_ROOT / "kbcore" / "triage_sql.py"

# The exact spellings every call site carried before the single-ownership
# refactor. Byte-identity is the contract: identical SQL text in, identical
# query plans and semantics out.
HISTORIC = {
    "has": "COALESCE(rq.detail, '{}'::jsonb) ? 'triage'",
    "has_bare": "COALESCE(detail, '{}'::jsonb) ? 'triage'",
    "not": "NOT (COALESCE(rq.detail, '{}'::jsonb) ? 'triage')",
    "obj_bare": "detail->'triage'",
    "action": "rq.detail->'triage'->>'action'",
    "dup_of": "rq.detail->'triage'->>'dup_of'",
    "at": "rq.detail->'triage'->>'at'",
    "prior_dup_of": "rq.detail->'triage_prior'->>'dup_of'",
}


def test_the_owned_predicates_render_byte_identical_to_the_old_literals():
    assert triage_sql.sql_has_triage() == HISTORIC["has"]
    assert triage_sql.sql_has_triage("detail") == HISTORIC["has_bare"]
    assert triage_sql.sql_not_triaged() == HISTORIC["not"]
    assert triage_sql.sql_triage_obj("detail") == HISTORIC["obj_bare"]
    assert triage_sql.sql_triage_field("action") == HISTORIC["action"]
    assert triage_sql.sql_triage_field("dup_of") == HISTORIC["dup_of"]
    assert triage_sql.sql_triage_field("at") == HISTORIC["at"]
    assert triage_sql.sql_triage_prior_field("dup_of") == HISTORIC["prior_dup_of"]
    assert triage_sql.TRIAGE_KEY_SQL == "'triage'"
    assert triage_sql.TRIAGE_PRIOR_KEY_SQL == "'triage_prior'"
    # review_triage stays a consumer of the owned spellings, never a second owner.
    assert review_triage.TRIAGE_KEY == triage_sql.TRIAGE_KEY
    assert review_triage.sql_has_triage is triage_sql.sql_has_triage
    assert review_triage.sql_not_triaged is triage_sql.sql_not_triaged


def test_the_shared_expiry_predicates_kept_their_bytes():
    """QUIET_ELIGIBLE / EXPIRY_GATE are consumed by the sweep AND the report;
    the refactor must not have moved a byte of their rendered SQL."""
    assert (
        "AND NOT (COALESCE(rq.detail->'triage'->>'action', '') = ANY(%(reject)s))"
        in review_expire.QUIET_ELIGIBLE
    )
    assert review_expire.EXPIRY_GATE == (
        "(\n"
        "                  COALESCE(rq.detail, '{}'::jsonb) ? 'triage'\n"
        "                  AND rq.detail->'auto_resolution'->>'undone_at' IS NULL\n"
        "              )"
    )
    assert review_auto.EXAMINED_DETAIL_KEYS == ("triage", "triage_prior")


def test_no_src_module_outside_the_owner_spells_the_literal():
    """The copy-paste can never quietly come back (mechanical, like the size
    ratchet). Docstrings mentioning the key are fine; the SQL spellings are not."""
    forbidden = ("? 'triage'", "->'triage'", "-> 'triage'", "'triage_prior'")
    offenders: dict[str, list[str]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == OWNER or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        found = [f for f in forbidden if f in text]
        if found:
            offenders[path.relative_to(REPO_ROOT).as_posix()] = found
    assert not offenders, (
        "triage JSON-path SQL literals outside kbcore.review_triage "
        f"(import the owned helpers instead): {offenders}"
    )


def test_env_example_keep_alive_is_finite_and_explained():
    """CLAUDE.md rule 8: KB_LLM_KEEP_ALIVE is a FINITE seconds value, never -1.
    The template shipped -1 for weeks after the rule flipped (review #4 Sonnet
    catch) — an unedited `cp .env.example .env` reproduced the one-slot pin."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "KB_LLM_KEEP_ALIVE=-1" not in text
    assert "KB_LLM_KEEP_ALIVE=600" in text
    assert "NEVER -1" in text and "rule 8" in text, "the WHY must ship with the value"


def test_env_example_documents_the_weekly_knobs():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "KB_WEEKLY_REPORT=true" in text
    assert "KB_WEEKLY_DAY=sun" in text
