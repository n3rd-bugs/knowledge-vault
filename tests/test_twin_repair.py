"""Package Y — `kbcore.twin_repair` / `kb repair twins`.

The 2026-07-27 double-capture residue: project ``gateway`` holds verbatim copies
of ``data-server`` items. The nightly dedupe cannot see them (its join is
SAME-project by settled design and is NOT widened), so this owner-fired
instrument enumerates the cross-project twins and retires the gateway copy of
each confirmed pair through the EXISTING duplicate transition.

Real DB in kb_test (conftest ``kb`` fixture); embeddings come from conftest's
deterministic stub and lead-chunk vectors are then forced to exact cosines so
the distance band is not luck. No Ollama, no live schema.
"""

from __future__ import annotations

import math
from contextlib import contextmanager

import pytest
from typer.testing import CliRunner

from kbcli.main import app
from kbcore import db, scope, twin_repair
from kbcore.config import settings
from kbcore.errors import InvalidOperation
from kbcore.ingest import ingest

runner = CliRunner()

GW = twin_repair.GATEWAY_PROJECT
DS = twin_repair.ORIGINAL_PROJECT


# --- helpers -----------------------------------------------------------------


_AXES: dict[str, int] = {}


def _sparse(components: dict[int, float]) -> str:
    """A pgvector literal with only ``components`` non-zero."""
    v = [0.0] * settings.kb_embed_dim
    for i, x in components.items():
        v[i] = x
    return "[" + ",".join(repr(x) for x in v) + "]"


def _axis(group: str) -> int:
    """Three private dimensions per group, so DIFFERENT groups are orthogonal.

    Every original used to sit on one shared axis, which silently made each
    gateway copy a twin of every other group's original — the seeded corpus has
    to isolate groups or the distance band proves nothing.
    """
    return _AXES.setdefault(group, 3 * len(_AXES))


def _base(group: str) -> str:
    """The group's canonical direction — an original sits exactly here."""
    return _sparse({_axis(group): 1.0})


def _offset(group: str, distance: float, plane: int = 1) -> str:
    """A unit vector at exactly ``distance`` cosine distance from ``_base(group)``.

    ``plane`` picks which private dimension carries the perpendicular part, so two
    offsets in different planes are also a known distance from EACH OTHER.
    """
    cos = 1.0 - distance
    return _sparse({_axis(group): cos, _axis(group) + plane: math.sqrt(max(0.0, 1.0 - cos * cos))})


def _force_lead(item_id: int, vec: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embedding = %s::vector WHERE item_id = %s AND ord = 0",
            (vec, item_id),
        )


def _status(item_id: int) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT status FROM items WHERE id = %s", (item_id,)).fetchone()[0]


def _relation(src: int, rel: str, dst: int) -> bool:
    with db.connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM relations WHERE src_kind='item' AND src_id=%s "
                "AND rel=%s AND dst_kind='item' AND dst_id=%s",
                (src, rel, dst),
            ).fetchone()
            is not None
        )


def _counts() -> tuple[int, int, int]:
    """(non-active items, relations, access_log rows) — the "nothing written" probe."""
    with db.connect() as conn:
        return (
            conn.execute("SELECT count(*) FROM items WHERE status <> 'active'").fetchone()[0],
            conn.execute("SELECT count(*) FROM relations").fetchone()[0],
            conn.execute("SELECT count(*) FROM access_log").fetchone()[0],
        )


def _twin(
    title: str, body: str, *, dist: float = 0.0, trust: str = "unverified"
) -> tuple[int, int]:
    """Seed one gateway copy + its data-server original, separated by ``dist``.

    Bodies differ by a project marker so ingest's content-hash idempotency does
    not collapse them into one item — the twins are near in EMBEDDING space,
    which is exactly what the real residue looks like after two boxes captured
    the same session through slightly different funnels.
    """
    gw = ingest(
        GW,
        "gotcha",
        title,
        f"{body}\n\ncaptured by the gateway box",
        uri=f"gw:{title}",
        trust=trust,
        kind="session",
    )
    ds = ingest(
        DS,
        "gotcha",
        title,
        f"{body}\n\ncaptured by this box",
        uri=f"ds:{title}",
        trust=trust,
        kind="session",
    )
    assert gw is not None and ds is not None
    _force_lead(ds, _base(title))
    _force_lead(gw, _offset(title, dist))
    return gw, ds


# --- 1. enumerate ------------------------------------------------------------


def test_enumerate_finds_cross_project_twins_ranked_by_distance(kb):
    far_gw, _far_ds = _twin("far", "unrelated capture", dist=0.40)
    near_gw, near_ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    mid_gw, mid_ds = _twin("mid", "the rerank sidecar gotcha", dist=0.015)

    pairs = twin_repair.enumerate_twins(GW, DS, 0.02)

    assert [(p.retire.item_id, p.keep.item_id) for p in pairs] == [
        (near_gw, near_ds),
        (mid_gw, mid_ds),
    ], "closest first, and the 0.40 pair is outside the band"
    assert far_gw not in {p.retire.item_id for p in pairs}
    assert pairs[0].distance == pytest.approx(0.001, abs=1e-6)
    assert pairs[0].retire.project == GW and pairs[0].keep.project == DS
    assert pairs[0].retire.title == "near"
    assert pairs[0].retire.created_at is not None
    assert pairs[0].retire.trust == "unverified"


def test_enumerate_carries_redaction_fingerprints(kb):
    token = "ghp_" + "0" * 36  # fake, shaped like the real thing (CLAUDE.md hygiene)
    gw, _ds = _twin("secretish", f"the token was {token} in the log", dist=0.001)

    pair = twin_repair.enumerate_twins(GW, DS, 0.02)[0]

    assert pair.retire.redactions == {"github_token": 1}
    assert pair.retire.fingerprint() == "github_token:1"
    assert pair.retire.item_id == gw
    with db.connect() as conn:
        body = conn.execute("SELECT body FROM items WHERE id = %s", (gw,)).fetchone()[0]
    assert token not in body, "the scrubber must have redacted the seeded credential"


def test_enumerate_writes_nothing(kb):
    _twin("near", "the drain deadline gotcha", dist=0.001)
    before = _counts()

    twin_repair.enumerate_twins(GW, DS, 0.02)

    assert _counts() == before


def test_enumerate_never_pairs_within_one_project(kb):
    """The same-project join is settled design — this instrument cannot back-door it."""
    a = ingest(GW, "gotcha", "one", "same project body one", uri="gw:1", trust="unverified")
    b = ingest(GW, "gotcha", "two", "same project body two", uri="gw:2", trust="unverified")
    _force_lead(a, _base("solo"))
    _force_lead(b, _base("solo"))

    assert twin_repair.enumerate_twins(GW, DS, 0.02) == []
    with pytest.raises(InvalidOperation, match="CROSS-project"):
        twin_repair.enumerate_twins(GW, GW, 0.02)


def test_enumerate_skips_consolidation_output_and_foreign_embed_models(kb):
    s_gw = ingest(GW, "summary", "s", "summary body gw", uri="gw:s", trust="unverified")
    s_ds = ingest(DS, "summary", "s", "summary body ds", uri="ds:s", trust="unverified")
    _force_lead(s_gw, _base("consolidation-output"))
    _force_lead(s_ds, _base("consolidation-output"))
    stale_gw, _stale_ds = _twin("stale-model", "embedded by the previous model", dist=0.001)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE chunks SET embed_model = 'retired-model' WHERE item_id = %s", (stale_gw,)
        )

    assert twin_repair.enumerate_twins(GW, DS, 0.02) == []


# --- 2. repair: dry run ------------------------------------------------------


def test_dry_run_writes_nothing(kb):
    gw, ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    before = _counts()

    outcomes = twin_repair.repair(twin_repair.enumerate_twins(GW, DS, 0.02))

    assert [(o.eligible, o.retired, o.reason) for o in outcomes] == [(True, False, "ok")]
    assert _counts() == before
    assert _status(gw) == "active" and _status(ds) == "active"
    assert not _relation(gw, "derived_from", ds)


# --- 3. repair: apply --------------------------------------------------------


def test_apply_retires_the_gateway_copy_only_with_a_relation(kb):
    gw, ds = _twin("near", "the drain deadline gotcha", dist=0.001)

    outcomes = twin_repair.repair(twin_repair.enumerate_twins(GW, DS, 0.02), dry_run=False)

    assert [(o.eligible, o.retired, o.reason) for o in outcomes] == [(True, True, "ok")]
    assert _status(gw) == "duplicate", "the GATEWAY copy is the one retired"
    assert _status(ds) == "active", "the data-server original stays canonical"
    assert _relation(gw, "derived_from", ds), "the audit edge dup -> canonical"
    with db.connect() as conn:
        body = conn.execute("SELECT body FROM items WHERE id = %s", (gw,)).fetchone()[0]
    assert "captured by the gateway box" in body, "Invariant 1: the body is untouched"


def test_apply_is_idempotent(kb):
    gw, ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    pairs = twin_repair.enumerate_twins(GW, DS, 0.02)
    twin_repair.repair(pairs, dry_run=False)
    after_first = _counts()

    again = twin_repair.repair(pairs, dry_run=False)

    assert [o.reason for o in again] == [twin_repair.REFUSE_INACTIVE]
    assert [o.retired for o in again] == [False]
    assert _counts() == after_first
    assert _status(gw) == "duplicate" and _status(ds) == "active"
    # ...and the item has left the enumerable band entirely.
    assert twin_repair.enumerate_twins(GW, DS, 0.02) == []


def test_a_copy_is_retired_once_against_its_closest_original(kb):
    gw, near_ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    other_ds = ingest(
        DS,
        "gotcha",
        "near-2",
        "a second original of the same capture",
        uri="ds:2",
        trust="unverified",
    )
    assert other_ds is not None
    # In band relative to the gateway copy (~0.006) but farther than near_ds (0.001):
    # a different perpendicular plane, so the two originals are a known distance apart.
    _force_lead(other_ds, _offset("near", 0.005, plane=2))

    pairs = twin_repair.enumerate_twins(GW, DS, 0.02)
    assert len(pairs) == 2, "one gateway copy matches both originals"
    outcomes = twin_repair.repair(pairs, dry_run=False)

    assert [o.retired for o in outcomes] == [True, False]
    assert outcomes[1].reason == twin_repair.REFUSE_ALREADY_HANDLED
    assert _relation(gw, "derived_from", near_ds)
    assert not _relation(gw, "derived_from", other_ds)


# --- 4. the three refusal guards ---------------------------------------------


def test_refuses_a_verified_gateway_copy(kb):
    gw, ds = _twin("near", "a human already judged this", dist=0.001, trust="verified")

    outcomes = twin_repair.repair(twin_repair.enumerate_twins(GW, DS, 0.02), dry_run=False)

    assert [(o.eligible, o.retired, o.reason) for o in outcomes] == [
        (False, False, twin_repair.REFUSE_VERIFIED)
    ]
    assert _status(gw) == "active" and _status(ds) == "active"


def test_refuses_a_copy_read_in_the_last_30_days(kb):
    gw, _ds = _twin("near", "someone is using this id", dist=0.001)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, ts) "
            "VALUES (%s, 'kb_get', now() - make_interval(days => 3))",
            (gw,),
        )

    outcomes = twin_repair.repair(twin_repair.enumerate_twins(GW, DS, 0.02), dry_run=False)

    assert [o.reason for o in outcomes] == [twin_repair.REFUSE_RECENT_ACCESS]
    assert _status(gw) == "active"


def test_an_access_older_than_the_window_does_not_refuse(kb):
    gw, _ds = _twin("near", "nobody has touched this in ages", dist=0.001)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO access_log (item_id, tool, ts) "
            "VALUES (%s, 'kb_get', now() - make_interval(days => 400))",
            (gw,),
        )

    outcomes = twin_repair.repair(twin_repair.enumerate_twins(GW, DS, 0.02), dry_run=False)

    assert [o.retired for o in outcomes] == [True]
    assert _status(gw) == "duplicate"


def test_refuses_a_copy_that_is_itself_canonical_for_other_duplicates(kb):
    """Chain guard: A is dup-of B, B is a twin — retiring B would strand A."""
    from kbcore.consolidation import mark_duplicate

    b_gw, b_ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    a = ingest(
        GW, "gotcha", "a", "an earlier gateway duplicate of b", uri="gw:a", trust="unverified"
    )
    assert a is not None
    _force_lead(a, _base("chain-orphan"))  # out of the twin band itself
    mark_duplicate(a, b_gw)  # A -derived_from-> B, A is 'duplicate'
    assert _status(a) == "duplicate"

    outcomes = twin_repair.repair(twin_repair.enumerate_twins(GW, DS, 0.02), dry_run=False)

    assert [(o.eligible, o.retired, o.reason) for o in outcomes] == [
        (False, False, twin_repair.REFUSE_CHAIN)
    ]
    assert _status(b_gw) == "active", "the chain's canonical must stay reachable"
    assert _status(b_ds) == "active"


def test_refuses_when_the_canonical_is_not_active(kb):
    gw, ds = _twin("near", "the original was archived", dist=0.001)
    pairs = twin_repair.enumerate_twins(GW, DS, 0.02)
    with db.transaction() as conn:
        conn.execute("UPDATE items SET status = 'archived' WHERE id = %s", (ds,))

    outcomes = twin_repair.repair(pairs, dry_run=False)

    assert [o.reason for o in outcomes] == [twin_repair.REFUSE_CANONICAL_INACTIVE]
    assert _status(gw) == "active"


def test_repair_refuses_a_same_project_pair_outright(kb):
    gw, _ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    pair = twin_repair.enumerate_twins(GW, DS, 0.02)[0]
    forged = twin_repair.TwinPair(
        distance=0.0,
        retire=pair.retire,
        keep=twin_repair.TwinSide(
            item_id=pair.keep.item_id,
            project=GW,
            type="gotcha",
            title="x",
            created_at=pair.keep.created_at,
            status="active",
            trust="unverified",
        ),
    )

    with pytest.raises(InvalidOperation, match="SAME-project"):
        twin_repair.repair([forged], dry_run=False)
    assert _status(gw) == "active"


# --- 5. owner scope ----------------------------------------------------------


def test_both_surfaces_are_owner_only(kb):
    _twin("near", "the drain deadline gotcha", dist=0.001)
    pairs = twin_repair.enumerate_twins(GW, DS, 0.02)
    grantee = scope.Principal(
        name="grantee",
        read_projects=frozenset({GW, DS}),
        write_projects=frozenset({GW, DS}),
        caps=scope.ALL_CAPS,
    )

    with scope.use_principal(grantee):
        with pytest.raises(InvalidOperation, match="owner only"):
            twin_repair.enumerate_twins(GW, DS, 0.02)
        with pytest.raises(InvalidOperation, match="owner only"):
            twin_repair.repair(pairs, dry_run=False)


# --- 6. CLI ------------------------------------------------------------------


def _fake_lock(acquired: bool):
    @contextmanager
    def _lock(_key: int):
        yield acquired

    return _lock


@pytest.fixture()
def lock_free(monkeypatch):
    """Pin the consolidation advisory lock to ACQUIRED for the apply path.

    Every pytest process on this box shares one ``kb_test`` schema, and
    ``advisory_lock`` keys are schema-scoped — so a REAL
    ``pg_try_advisory_lock(42)`` here is a coin flip against whatever else is
    running. The CLI genuinely (and correctly) refuses to write while the key is
    held; that refusal gets its own deterministic test below instead of being a
    race in every apply test. The patch lands on ``kbcore.db`` because
    ``repair_cmds`` imports the helper at CALL time.
    """
    monkeypatch.setattr(db, "advisory_lock", _fake_lock(True))


def test_cli_default_is_a_dry_run_table(kb):
    gw, ds = _twin("near", "the drain deadline gotcha", dist=0.001)

    result = runner.invoke(app, ["repair", "twins"])

    assert result.exit_code == 0, result.output
    assert f"#{gw}" in result.output and f"#{ds}" in result.output
    assert "would retire" in result.output
    assert "1 candidate pair(s), 1 eligible, 0 retired" in result.output
    assert "dry run — nothing written" in result.output
    assert _status(gw) == "active"


def test_cli_apply_confirms_then_retires_and_prints_undo(kb, lock_free):
    gw, ds = _twin("near", "the drain deadline gotcha", dist=0.001)

    result = runner.invoke(app, ["repair", "twins", "--apply"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "[y/N]" in result.output, "--apply must ask before it writes"
    assert "RETIRED" in result.output
    assert "1 candidate pair(s), 1 eligible, 1 retired" in result.output
    assert "undo / audit trail" in result.output
    assert "kb get <retire-id>" in result.output
    assert _status(gw) == "duplicate" and _status(ds) == "active"
    assert _relation(gw, "derived_from", ds)


def test_cli_apply_refuses_while_a_consolidation_holds_the_lock(kb, monkeypatch):
    """The nightly dedupe is the one writer that could interleave — never race it."""
    gw, _ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    before = _counts()
    monkeypatch.setattr(db, "advisory_lock", _fake_lock(False))

    result = runner.invoke(app, ["repair", "twins", "--apply"], input="y\n")

    assert result.exit_code == 1
    assert "consolidation lock 42 is held" in result.output
    assert _status(gw) == "active"
    assert _counts() == before


def test_cli_apply_aborts_on_no(kb):
    gw, _ds = _twin("near", "the drain deadline gotcha", dist=0.001)
    before = _counts()

    result = runner.invoke(app, ["repair", "twins", "--apply"], input="n\n")

    assert result.exit_code == 1
    assert _status(gw) == "active"
    assert _counts() == before


def test_cli_max_dist_widens_the_band_and_limit_caps_it(kb):
    near_gw, _ = _twin("near", "the drain deadline gotcha", dist=0.001)
    mid_gw, _ = _twin("mid", "the rerank sidecar gotcha", dist=0.05)

    tight = runner.invoke(app, ["repair", "twins"])
    wide = runner.invoke(app, ["repair", "twins", "--max-dist", "0.1"])
    capped = runner.invoke(app, ["repair", "twins", "--max-dist", "0.1", "--limit", "1"])

    assert f"#{mid_gw}" not in tight.output and f"#{near_gw}" in tight.output
    assert "2 candidate pair(s), 2 eligible" in wide.output
    assert "1 candidate pair(s), 1 eligible" in capped.output
    assert f"#{mid_gw}" not in capped.output, "the closest pair is kept when capped"


def test_cli_reports_an_empty_band_without_a_table(kb):
    result = runner.invoke(app, ["repair", "twins"])

    assert result.exit_code == 0, result.output
    assert "no gateway/data-server twins within distance" in result.output


def test_cli_apply_stops_when_every_candidate_is_refused(kb):
    gw, _ds = _twin("near", "a human already judged this", dist=0.001, trust="verified")
    before = _counts()

    result = runner.invoke(app, ["repair", "twins", "--apply"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "nothing eligible to retire" in result.output
    assert f"skip: {twin_repair.REFUSE_VERIFIED}" in result.output
    assert _status(gw) == "active"
    assert _counts() == before
