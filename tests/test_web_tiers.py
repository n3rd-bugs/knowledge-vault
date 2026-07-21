"""Phase 2 tiered access — capability/role model + web zone walls (plan §2, §5 P2).

ZERO feature change is the contract under test: a 'reader' grantee (migration
012's default — the exact shape every pre-migration row is left in) behaves
byte-identically to a grantee created without a role, and existing rw-grantees
are backfilled to 'contributor' so no working credential is silently revoked.
The new walls: the web answers an under-capped principal with the router's
byte-identical unknown-path 404 (feature existence joins data existence under
the no-oracle discipline — reusing the Phase-1 ghost harness), nav renders
strictly from the LIVE principal's caps, and the admin form can create only
guest/reader grantees (A11-2a) — role change stays CLI-only (`set_role`
trip-wire). Real DB in kb_test; Ollama mocked by conftest.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import re

import psycopg
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from kbcore import db, principals, scope
from kbcore.config import settings
from kbcore.errors import InvalidOperation, NotFound
from kbcore.graph import link
from kbcore.ingest import ingest
from kbcore.migrate import MIGRATIONS_DIR, migrate
from kbcore.supersede import supersede
from tests.test_web_caps import GHOST_PATH, assert_ghost_identical

TEST_TOKEN = "kbtest-" + "0" * 32


# --- 1. the capability model (pure in-memory) ---------------------------------


def test_role_caps_bundles_pinned():
    """The tier ladder is code, not data — pin every bundle (plan §2)."""
    assert set(scope.ROLE_CAPS) == {"guest", "reader", "contributor", "reviewer"}
    assert scope.ROLE_CAPS["guest"] == frozenset({scope.CAP_SEARCH})
    assert scope.ROLE_CAPS["reader"] == frozenset({scope.CAP_SEARCH, scope.CAP_BROWSE})
    assert scope.ROLE_CAPS["contributor"] == frozenset(
        {scope.CAP_SEARCH, scope.CAP_BROWSE, scope.CAP_SAVE}
    )
    assert "owner" not in scope.ROLE_CAPS, "owner is credential-derived, never a role"


def test_reviewer_bundle_now_carries_review_caps():
    """A11-3 (2026-07-21): the review_queue.detail JSONB scrub landed in
    kbcore.review.list_open_review, so the reviewer bundle now carries BOTH review
    caps — a reviewer may list + reject/dismiss. Trust escalation (promote) stays
    owner-only in kbcore.review, so CAP_REVIEW_RESOLVE never reaches 'verified'."""
    assert scope.CAP_REVIEW_READ in scope.ROLE_CAPS["reviewer"]
    assert scope.CAP_REVIEW_RESOLVE in scope.ROLE_CAPS["reviewer"]
    assert scope.ROLE_CAPS["reviewer"] == frozenset(
        {
            scope.CAP_SEARCH,
            scope.CAP_BROWSE,
            scope.CAP_SAVE,
            scope.CAP_REVIEW_READ,
            scope.CAP_REVIEW_RESOLVE,
        }
    )


def test_only_reviewer_bundle_holds_the_review_caps():
    """Drift-pin (A11-3 finding 2): no OTHER role bundle may carry the review caps —
    a future bundle edit cannot silently widen the trust-adjacent resolve surface."""
    for role, caps in scope.ROLE_CAPS.items():
        if role == "reviewer":
            continue
        assert scope.CAP_REVIEW_READ not in caps, f"{role} leaked CAP_REVIEW_READ"
        assert scope.CAP_REVIEW_RESOLVE not in caps, f"{role} leaked CAP_REVIEW_RESOLVE"


def test_no_bundle_grants_deep_search():
    # Owner GPU (Invariant 8): grantable later by a one-line code change, no bundle today.
    assert all(scope.CAP_DEEP_SEARCH not in caps for caps in scope.ROLE_CAPS.values())


def test_default_caps_empty_fail_closed():
    p = scope.Principal(name="bare")
    assert p.caps == frozenset(), "a construction site that forgets expansion gets NOTHING"
    assert not p.has_cap(scope.CAP_SEARCH)


def test_owner_short_circuits_has_cap_and_sentinel_is_unreachable():
    for cap in sorted(scope.ALL_CAPS | {"owner", "future-cap"}):
        assert scope.OWNER.has_cap(cap), "is_owner short-circuit: stale ALL_CAPS never costs owner"
    impostor = scope.Principal(name="impostor", caps=scope.ALL_CAPS)  # is_owner=False
    assert not impostor.has_cap("owner"), "the OWNER_ONLY zone sentinel must not ride caps"


def test_require_cap_uniform_wording_never_names_the_cap():
    g = scope.Principal(name="g", role="guest", caps=frozenset({scope.CAP_SEARCH}))
    messages = set()
    for cap in sorted(scope.ALL_CAPS - {scope.CAP_SEARCH}):
        with scope.use_principal(g), pytest.raises(InvalidOperation) as ei:
            scope.require_cap(cap)
        messages.add(str(ei.value))
        assert cap not in str(ei.value), "the missing capability must never be named"
    assert len(messages) == 1, "wording must not vary by capability (no tier-ladder oracle)"
    with scope.use_principal(g):
        scope.require_cap(scope.CAP_SEARCH)  # a held cap passes silently
    with scope.use_principal(scope.OWNER):
        for cap in scope.ALL_CAPS:
            scope.require_cap(cap)


# --- 2. resolution + lifecycle plumbing (real DB) -----------------------------


def test_resolvers_carry_role_and_caps(kb):
    _pid, token = principals.create_principal("g1", kind="token", role="guest")
    for p in (principals.resolve_token(token), principals.resolve_name("g1")):
        assert p is not None and p.role == "guest"
        assert p.caps == frozenset({scope.CAP_SEARCH})
    principals.create_principal("g2", kind="google", email="g2@example.com", role="contributor")
    p = principals.resolve_email("g2@example.com")
    assert p is not None and p.role == "contributor" and scope.CAP_SAVE in p.caps


def test_default_role_is_reader_the_pre_migration_shape(kb):
    """A row created without a role — exactly what migration 012 leaves every
    pre-migration grantee as — resolves to reader (today's grantee surface)."""
    _pid, token = principals.create_principal("legacy", kind="token")
    p = principals.resolve_token(token)
    assert p is not None and p.role == "reader"
    assert p.caps == scope.ROLE_CAPS["reader"]


def test_create_principal_rejects_unknown_role(kb):
    for bad in ("owner", "admin", "", "Reader"):
        with pytest.raises(InvalidOperation):
            principals.create_principal("bad", kind="token", role=bad)
    assert principals.list_principals() == [], "no row on a rejected role"


def test_set_role_updates_and_flushes_cache(kb, monkeypatch):
    """Cache-flush parity with grant/ungrant/revoke: promotion AND demotion take
    effect on the next resolution, never lagging up to the token cache TTL."""
    monkeypatch.setattr(settings, "kb_principal_cache_ttl", 60)
    _pid, token = principals.create_principal("mover", kind="token")
    p1 = principals.resolve_token(token)  # positive resolution → cached as reader
    assert p1 is not None and scope.CAP_SAVE not in p1.caps
    principals.set_role("mover", "contributor")
    p2 = principals.resolve_token(token)
    assert p2 is not None and p2.role == "contributor" and scope.CAP_SAVE in p2.caps, (
        "set_role must clear the token cache (parity with grant/revoke)"
    )
    principals.set_role("mover", "guest")  # demotion is just as instant
    p3 = principals.resolve_token(token)
    assert p3 is not None and p3.caps == frozenset({scope.CAP_SEARCH})


def test_set_role_validates_role_and_name(kb):
    principals.create_principal("v", kind="token")
    with pytest.raises(InvalidOperation):
        principals.set_role("v", "owner")  # the escalation firewall, code side
    with pytest.raises(NotFound):
        principals.set_role("nobody", "guest")


def test_unknown_role_serves_empty_caps_and_warns_not_500(kb, caplog):
    """Schema/code skew (a role this build does not know) degrades to ZERO
    capabilities with a loud warning — never a 500, never more access."""
    pid, _tok = principals.create_principal("skew", kind="token")
    with db.connect() as conn, caplog.at_level(logging.WARNING, logger="kbcore.principals"):
        p = principals._build_grantee(conn, pid, "skew", "token", "time-traveler")
    assert p.caps == frozenset() and not p.is_owner
    assert not p.has_cap(scope.CAP_SEARCH)
    assert any("unknown role" in r.getMessage() for r in caplog.records), "skew must warn"


def test_list_principals_and_cli_report_role(kb):
    runner = CliRunner()
    from kbcli.main import app as cli_app

    r = runner.invoke(cli_app, ["principal", "create", "cli-g", "--role", "guest"])
    assert r.exit_code == 0, r.output
    roles = {p.name: p.role for p in principals.list_principals()}
    assert roles["cli-g"] == "guest"
    r = runner.invoke(cli_app, ["principal", "role", "cli-g", "contributor"])
    assert r.exit_code == 0 and "contributor" in r.output
    assert {p.name: p.role for p in principals.list_principals()}["cli-g"] == "contributor"
    r = runner.invoke(cli_app, ["principal", "role", "cli-g", "owner"])
    assert r.exit_code == 1, "role escalation to owner must be refused"
    listed = runner.invoke(cli_app, ["principal", "list"]).output
    assert "role=contributor" in listed


# --- 3. migration 012 ---------------------------------------------------------


def test_migration_012_rerun_is_noop(kb):
    """Registry-level: migrate() again applies nothing. Raw-SQL level: executing
    012 a second time changes no schema and rewrites no role (additive, Inv. 5)."""
    assert migrate() == []  # already applied into kb_test by the session fixture
    principals.create_principal("keep-guest", kind="token", role="guest")
    principals.create_principal("keep-writer", kind="token", role="contributor")
    principals.grant("keep-writer", "p", rw=True)

    def snapshot() -> list[tuple[str, str]]:
        with db.connect() as conn:
            return conn.execute("SELECT name, role FROM principals ORDER BY name").fetchall()

    before = snapshot()
    sql = (MIGRATIONS_DIR / "012_principal_roles.sql").read_text(encoding="utf-8")
    with db.transaction() as conn:  # kb_test schema (conftest pins the pointer)
        conn.execute(sql)
    assert snapshot() == before, "re-running 012 must not rewrite any role"


def test_role_check_constraint_blocks_owner_row(kb):
    """The escalation firewall: no DB row — forged, injected, or fat-fingered —
    can ever carry role='owner' (the CHECK is frozen; ownership is credential-
    derived only)."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO principals (name, kind, secret_hash, role) "
                "VALUES ('evil', 'token', 'deadbeef', 'owner')"
            )


def test_backfill_promotes_only_rw_grantees(kb):
    """The 012 backfill semantics, replayed: a reader WITH an rw grant becomes
    contributor (a working credential is never silently revoked); read-only and
    non-reader rows are untouched."""
    principals.create_principal("was-rw", kind="token")  # pre-migration default shape
    principals.grant("was-rw", "p", rw=True)
    principals.create_principal("was-ro", kind="token")
    principals.grant("was-ro", "p", rw=False)
    principals.create_principal("was-guest", kind="token", role="guest")
    sql = (MIGRATIONS_DIR / "012_principal_roles.sql").read_text(encoding="utf-8")
    with db.transaction() as conn:
        conn.execute(sql)
    principals._clear_cache()
    roles = {p.name: p.role for p in principals.list_principals()}
    assert roles == {"was-rw": "contributor", "was-ro": "reader", "was-guest": "guest"}


# --- 4. kbcore double-gating (capability ANDs with rw grants) -----------------


def test_reader_with_rw_grant_is_double_gated_then_contributor_saves(kb):
    """Capabilities gate the FEATURE, rw grants gate the ROWS — they AND. A
    'reader' explicitly rw-granted (a post-migration anomaly the backfill never
    creates) cannot ingest/link/supersede; promoting the ROLE opens the feature
    while the same rw grant keeps gating the rows."""
    seed = ingest("p", "note", "seed for tiers", "seed body")
    seed2 = ingest("p", "note", "second seed", "second body")
    principals.create_principal("odd", kind="token", role="reader")
    principals.grant("odd", "p", rw=True)
    p = principals.resolve_name("odd")
    assert p is not None
    with scope.use_principal(p):
        with pytest.raises(InvalidOperation):
            ingest("p", "note", "blocked", "reader lacks the save capability")
        with pytest.raises(InvalidOperation):
            link(seed, "mentions", seed2)
        with pytest.raises(InvalidOperation):
            supersede(seed, new_body="blocked body", reason="blocked")
    principals.set_role("odd", "contributor")
    p = principals.resolve_name("odd")
    with scope.use_principal(p):
        assert ingest("p", "note", "allowed now", "contributor saves") is not None
        with pytest.raises(InvalidOperation):
            ingest("q", "note", "still walled", "no grant on q")  # rows still gated


def test_deep_search_capability_and_review_owner_pins():
    """Call-site map pins (plan §2): deep-search tools are CAP_DEEP_SEARCH-gated
    (owner-only in practice — no bundle grants it); review tools stay
    require_owner until the Phase-4 scrub; consolidate stays owner forever."""
    from kbmcp import tools_deepsearch, tools_phase5, tools_setup

    ds = inspect.getsource(tools_deepsearch)
    assert "require_cap(scope.CAP_DEEP_SEARCH" in ds
    assert "require_owner" not in ds
    p5 = inspect.getsource(tools_phase5)
    assert "require_owner" in p5 and "require_cap" not in p5
    assert 'require_owner("kb_consolidate")' in inspect.getsource(tools_setup)
    contributor = scope.Principal(name="c", caps=scope.ROLE_CAPS["contributor"])
    with scope.use_principal(contributor), pytest.raises(InvalidOperation):
        scope.require_cap(scope.CAP_DEEP_SEARCH, "kb_deep_search")
    with scope.use_principal(scope.OWNER):
        scope.require_cap(scope.CAP_DEEP_SEARCH, "kb_deep_search")


# --- 5. the web zone wall -----------------------------------------------------


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


@pytest.fixture()
def world(app):
    a = ingest(
        "proj-a", "note", "seeded spi note", "the widget uses SPI mode 1", tags=["protocol:spi"]
    )
    nda = ingest("nda-hidden", "note", "hidden secret", "withheld body")
    return {"a": a, "nda": nda}


def _client(app, role: str | None, name: str, projects: tuple[str, ...] = ()) -> TestClient:
    """A signed-in grantee session; role=None models a pre-migration row."""
    kwargs = {} if role is None else {"role": role}
    _pid, token = principals.create_principal(name, kind="token", **kwargs)
    for p in projects:
        principals.grant(name, p, rw=False)
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": token}).status_code == 200
    return c


def _owner(app) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": TEST_TOKEN}).status_code == 200
    return c


READER_WALLED = (
    "/review",
    "/review/1",
    "/setup",
    "/setup/x",
    "/admin",
    "/admin/grant",
    # Phase 3 zones: /jobs is owner-only (worker jobs run under implicit OWNER
    # scope); /export.{csv,json} needs CAP_EXPORT, which NO role bundle carries.
    "/jobs",
    "/jobs/1",
    "/export.csv",
    "/export.json",
    "/export.csv?q=spi",
)


def _guest_walled(world) -> tuple[str, ...]:
    return READER_WALLED + (
        "/timeline",
        "/projects",
        "/project/proj-a",  # even a GRANTED project's browse page is feature-walled
        "/project/nda-hidden",  # withheld and granted must stay indistinguishable
        "/stats",
        f"/trace/{world['a']}",
        f"/similar/{world['a']}",
        "/trace/999999",  # existent vs missing target: same ghost bytes
    )


def test_ghost_matrix_guest_and_reader_all_methods(app, world):
    """AC (plan §5 P2): ghost loop over (role × walled route × every method) —
    each walled zone byte-identical to /no-such-path for that principal."""
    for role, walled in (("reader", READER_WALLED), ("guest", _guest_walled(world))):
        c = _client(app, role, f"probe-{role}", projects=("proj-a",))
        assert c.get(GHOST_PATH).status_code == 404  # the ghost really is absent
        for path in walled:
            assert_ghost_identical(c, path)


def test_walled_zones_pre_auth_parity(app):
    """Anonymous probes: walled zones and the ghost bounce identically to /login
    (first-hop Location included) — no pre-auth feature oracle either."""
    c = TestClient(app, base_url="https://testserver")
    for path in ("/review", "/setup", "/timeline", "/stats"):
        assert_ghost_identical(c, path)


def test_reader_byte_identical_to_pre_migration_grantee_on_every_route(app, world):
    """THE zero-behavior-change proof: a grantee row created WITHOUT a role (the
    exact shape 012 leaves every pre-migration grantee in) and an explicit
    'reader' answer byte-identically on every route — data pages, walls, and
    the ghost alike."""
    legacy = _client(app, None, "legacy", projects=("proj-a",))
    reader = _client(app, "reader", "modern", projects=("proj-a",))
    a = world["a"]
    routes = [
        "/",
        "/?q=SPI+mode",
        "/tags/protocol:spi",
        f"/item/{a}",
        f"/item/{world['nda']}",  # withheld item: same 404 for both
        "/timeline",
        "/timeline?project=proj-a",
        "/projects",
        "/project/proj-a",
        "/stats",
        f"/trace/{a}",
        f"/similar/{a}",
        "/review",
        "/setup",
        "/admin",
        GHOST_PATH,
    ]
    for path in routes:
        r1 = legacy.get(path, follow_redirects=False)
        r2 = reader.get(path, follow_redirects=False)
        assert r1.status_code == r2.status_code, path
        assert r1.content == r2.content, f"{path}: default-role grantee differs from reader"
    # /welcome differs only by the principal's own display name
    w1 = legacy.get("/welcome").text.replace("legacy", "<NAME>")
    w2 = reader.get("/welcome").text.replace("modern", "<NAME>")
    assert w1 == w2


def test_reader_keeps_todays_full_grantee_surface(app, world):
    """Route-level: everything a grantee can reach today stays reachable for a
    reader, with its data rendered (the 'reader == today' behavioral half)."""
    c = _client(app, "reader", "surface", projects=("proj-a",))
    a = world["a"]
    for path in (
        "/",
        "/tags/protocol:spi",
        f"/item/{a}",
        "/timeline",
        "/projects",
        "/project/proj-a",
        "/stats",
        f"/trace/{a}",
        f"/similar/{a}",
        "/welcome",
    ):
        assert c.get(path).status_code == 200, path
    assert "seeded spi note" in c.get("/", params={"q": "SPI mode"}).text


def test_route_caps_totality(app):
    """AC: every registered route carries a tier decision (default-deny); an
    uncharted route refuses to serve at startup."""
    from kbweb import server

    server.assert_route_caps_total(server.ROUTES)  # must not raise

    class Uncharted:
        path = "/uncharted"

    with pytest.raises(SystemExit):
        server.assert_route_caps_total([*server.ROUTES, Uncharted()])


# --- 6. nav honesty + per-tier /welcome ---------------------------------------


def test_nav_honesty_per_role(app, world):
    guest = _client(app, "guest", "nav-guest", projects=("proj-a",))
    home = guest.get("/").text
    for href in (
        'href="/timeline"',
        'href="/projects"',
        'href="/review"',
        'href="/stats"',
        'href="/setup"',
        'href="/admin"',
        'href="/jobs"',
        'href="/export.csv',
    ):
        assert href not in home, f"guest chrome leaks {href}"
    assert 'href="/welcome"' in home
    item_page = guest.get(f"/item/{world['a']}").text
    assert 'href="/similar/' not in item_page and 'href="/trace/' not in item_page
    assert 'href="/project/' not in item_page, "sidebar project link must degrade to text"

    reader = _client(app, "reader", "nav-reader", projects=("proj-a",))
    home = reader.get("/").text
    for href in ('href="/timeline"', 'href="/projects"', 'href="/stats"'):
        assert href in home
    for href in (
        'href="/review"',
        'href="/setup"',
        'href="/admin"',
        'href="/jobs"',
        'href="/export.csv',
    ):
        assert href not in home, f"reader chrome leaks {href} (the old 400-link bug)"
    assert 'href="/review"' not in reader.get("/stats").text, "stats KPI must not link /review"
    # Phase 3: a capped-out reader's RESULTS page must not link the export zone.
    results = reader.get("/", params={"q": "SPI mode"}).text
    assert "seeded spi note" in results and 'href="/export.csv' not in results

    owner_home = _owner(app).get("/").text
    for href in (
        'href="/timeline"',
        'href="/review"',
        'href="/setup"',
        'href="/admin"',
        'href="/jobs"',
    ):
        assert href in owner_home, "owner chrome unchanged"


def test_welcome_renders_own_caps_only(app, world):
    guest = _client(app, "guest", "w-guest", projects=("proj-a",))
    text = guest.get("/welcome").text
    assert '<span class="tag">search</span>' in text
    for other in ("browse", "save", "review_read", "review_resolve", "deep_search"):
        assert f'<span class="tag">{other}</span>' not in text, "cross-tier catalog leaked"
    reader = _client(app, "reader", "w-reader", projects=("proj-a",))
    t2 = reader.get("/welcome").text
    assert '<span class="tag">browse</span>' in t2
    assert '<span class="tag">save</span>' not in t2


# --- 7. admin create-grantee role picker (A11-2a) -----------------------------


def _csrf_from(page_html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', page_html)
    assert m, "admin page must mint a CSRF token"
    return m.group(1)


def test_admin_form_offers_only_low_privilege_roles(app):
    page = _owner(app).get("/admin").text
    assert 'name="role"' in page and 'value="guest"' in page and 'value="reader"' in page
    for forbidden in ('value="contributor"', 'value="reviewer"', 'value="owner"'):
        assert forbidden not in page, "the form must not even offer an elevated role"


def test_hostile_role_post_rejected_no_row(app, world):
    """AC: a forged role=owner/contributor/anything-else POST to
    /admin/principal re-renders a 400 and creates NO row."""
    owner = _owner(app)
    for evil in ("owner", "contributor", "reviewer", "root'; --", "Reader"):
        csrf = _csrf_from(owner.get("/admin").text)
        r = owner.post(
            "/admin/principal",
            data={"name": "sneaky", "email": "s@client.example", "csrf": csrf, "role": evil},
            follow_redirects=False,
        )
        assert r.status_code == 400, f"role={evil!r} must be rejected"
        assert "sneaky" not in {p.name for p in principals.list_principals()}, (
            f"row created for forged role={evil!r}"
        )
    # absent role field → 'reader' (byte-parity with a pre-role form submit)
    csrf = _csrf_from(owner.get("/admin").text)
    r = owner.post(
        "/admin/principal",
        data={"name": "plain", "email": "p@client.example", "csrf": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    roles = {p.name: p.role for p in principals.list_principals()}
    assert roles["plain"] == "reader"
    # guest is the one permitted downgrade
    csrf = _csrf_from(owner.get("/admin").text)
    r = owner.post(
        "/admin/principal",
        data={"name": "guesty", "email": "g@client.example", "csrf": csrf, "role": "guest"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert {p.name: p.role for p in principals.list_principals()}["guesty"] == "guest"


def test_set_role_never_reachable_from_kbweb():
    """Trip-wire (A11-2a): role CHANGE is CLI-only forever — the string
    'set_role' may not appear in any kbweb module."""
    import kbweb

    for mod_info in pkgutil.iter_modules(kbweb.__path__):
        src = inspect.getsource(importlib.import_module(f"kbweb.{mod_info.name}"))
        assert "set_role" not in src, f"set_role leaked into kbweb.{mod_info.name}"
