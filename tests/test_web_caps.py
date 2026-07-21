"""Phase 1 web-hardening harness (WEBUI_TIERED_ACCESS_PLAN §5 Phase 1).

Reusable trip-wires the later phases build on, all serving the no-oracle
guarantee and Invariant 11 (the web mutates no knowledge):

1. ``assert_ghost_identical`` — a walled path must be BYTE-IDENTICAL (status +
   body + content-type) to a genuinely-nonexistent path for the SAME
   client/principal, across every HTTP method — so no probe (verb, subpath)
   can distinguish "exists but withheld" from "does not exist". Lifted from
   test_web_admin.py's byte-identical-404 loop; Phases 2-4 reuse it for every
   new walled route.
2. The INVERTED kbcore-import allowlist — every kbcore symbol any kbweb module
   imports OR touches must appear in READ_ALLOWLIST; a new import fails closed
   until a human adds it with intent. This makes a future Branch-B write
   import a conscious, reviewed act instead of a drive-by (Invariant 11).
3. Consistency of the knowledge-mutation guard (test_phase6_web.WRITE_NAMES)
   with the newer kbcore write surfaces (jobs / deep search / triage /
   review resolution / reprocess / item+project mutation).

The KB_WEB_SIGNING_SECRET decoupling tests live here too: the web signers
(session, CSRF, OAuth state) must accept exactly ONE secret source — the
dedicated secret when set, KB_TOKEN as the rollout fallback — with the
itsdangerous salt separation intact either way.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.testclient import TestClient

from kbcore import principals
from kbcore.config import settings
from kbcore.ingest import ingest
from kbweb import auth
from tests.test_phase6_web import WRITE_NAMES

TEST_TOKEN = "kbtest-" + "0" * 32
WEB_SECRET = "kbweb-signing-" + "1" * 32

_KBWEB_SRC = Path(__file__).resolve().parent.parent / "src" / "kbweb"


# --- 1. the ghost harness -----------------------------------------------------

GHOST_PATH = "/no-such-path-xyz"
ALL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def assert_ghost_identical(
    client: TestClient, path: str, methods: tuple[str, ...] = ALL_METHODS
) -> None:
    """Assert ``path`` is indistinguishable from a nonexistent path for ``client``.

    For each method, the walled path's response must match the SAME-method
    response for :data:`GHOST_PATH` on status, body bytes, content-type and any
    redirect Location — the no-oracle guarantee: existence itself is the secret,
    so a walled surface may never answer differently from an absent one (not
    even a 405 to a method probe). Redirects are NOT followed: raw first-hop
    bytes are what a probing client observes.
    """
    for method in methods:
        ghost = client.request(method, GHOST_PATH, follow_redirects=False)
        got = client.request(method, path, follow_redirects=False)
        assert got.status_code == ghost.status_code, (
            f"{method} {path} → {got.status_code}, ghost → {ghost.status_code}"
        )
        assert got.content == ghost.content, f"{method} {path} body differs from ghost"
        assert got.headers.get("content-type") == ghost.headers.get("content-type"), (
            f"{method} {path} content-type differs from ghost"
        )
        assert got.headers.get("location") == ghost.headers.get("location"), (
            f"{method} {path} redirect target differs from ghost"
        )


@pytest.fixture()
def app(kb, monkeypatch):
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_bind", "127.0.0.1")
    from kbweb.server import build_app

    return build_app()


def _grantee_client(app, name: str = "probe", projects: tuple[str, ...] = ()) -> TestClient:
    """A signed-in kbg_ grantee session (via the real /login route)."""
    _pid, token = principals.create_principal(name, kind="token")
    for p in projects:
        principals.grant(name, p, rw=False)
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/login", data={"token": token}).status_code == 200
    return c


def test_ghost_harness_walls_admin_subtree_for_grantee(app):
    ingest("proj-a", "note", "seed", "seed body")
    g = _grantee_client(app, projects=("proj-a",))
    assert g.get(GHOST_PATH).status_code == 404  # the ghost really is absent
    for path in ("/admin", "/admin/grant", "/admin/revoke", "/admin/principal", "/admin/x/y"):
        assert_ghost_identical(g, path)


def test_ghost_harness_is_not_vacuous(app):
    """Self-test: the helper must FAIL on a path that visibly exists — otherwise
    a broken harness would green-light every future wall."""
    ingest("proj-a", "note", "seed", "seed body")
    g = _grantee_client(app, projects=("proj-a",))
    with pytest.raises(AssertionError):
        assert_ghost_identical(g, "/", methods=("GET",))


def test_ghost_harness_holds_for_unauthenticated_client(app):
    """Anonymous probes get the same login redirect for walled and absent paths
    (first-hop Location included) — no pre-auth existence oracle either."""
    c = TestClient(app, base_url="https://testserver")
    for path in ("/admin", "/admin/grant"):
        assert_ghost_identical(c, path)


# --- 2. the inverted kbcore-import allowlist ----------------------------------

# EXACTLY today's kbweb→kbcore surface (discovered 2026-07-20, seeded with
# intent). Growing this set is a REVIEWED act: read functions extend the
# read-only web (fine); any WRITE name here means Branch B was consciously
# opened — never add one as a side effect (Invariant 11).
READ_ALLOWLIST = {
    "browse.item_refs",
    "browse.items_with_tag",
    "browse.list_tags",
    "config.settings",
    # A11-3: DuplicateContent is caught by the confined write surface to render a
    # constant (no-oracle) response — a domain-error class, not a write function.
    "errors.DuplicateContent",
    "errors.InvalidOperation",
    "errors.KBError",
    "errors.NotFound",
    "graph.similar",
    "graph.trace",
    "items.get_item",
    "items.list_projects",
    "items.timeline",
    # Phase 3 (read-feature surfacing, plan §5): STATUS-ONLY job reads for the
    # owner-only /jobs viewer — list_deep_search_jobs is a parameterized SELECT
    # that never fetches the report text; latest_consolidation_status is the
    # existing poll read. Both pure reads (jobs' write surfaces — enqueue/claim/
    # finish — stay in WRITE_NAMES and out of kbweb).
    "jobs.latest_consolidation_status",
    "jobs.list_deep_search_jobs",
    "models.ITEM_TYPES",
    # A11-3 confined web-write surface (kbweb.views_write): the closed request
    # shape + vocabulary the /save and /link forms render/parse against — pure
    # read constants/models, NOT write functions (those are in WRITE_ALLOWLIST).
    "models.RELATIONS",
    "models.WEB_ITEM_TYPES",
    "models.WebSaveRequest",
    # Phase 3: the export view type-annotates/serializes the SearchHit result
    # shape of search.search — a pydantic model, not a write surface.
    "models.SearchHit",
    # views_admin authz-config carve-out (A11-1/A11-2) + login resolution
    "principals.PrincipalInfo",
    "principals.create_principal",
    "principals.grant",
    "principals.list_principals",
    "principals.resolve_email",
    "principals.resolve_name",
    "principals.resolve_token",
    "principals.ungrant",
    "review.list_open_review",
    "scope.OWNER",
    "scope.Principal",
    # Phase 2 (tiered access): the ROUTE_CAPS zone table in kbweb.server names
    # the capability constants — pure read-only vocabulary, no write surface.
    "scope.CAP_BROWSE",
    # Phase 3: CAP_EXPORT gates the GET-only /export.{csv,json} zone (plan §3d;
    # in NO role bundle at launch) — a constant, not a write surface.
    "scope.CAP_EXPORT",
    # A11-3: capability constants naming the confined write / review zones in the
    # ROUTE_CAPS wall + the in-view layer-2 backstops — constants, not surfaces.
    "scope.CAP_REVIEW_READ",
    "scope.CAP_REVIEW_RESOLVE",
    "scope.CAP_SAVE",
    "scope.CAP_SEARCH",
    "scope.current",
    "scope.enter_server_mode",
    "scope.require_owner",
    "scope.use_principal",
    "search.search",
    "stats.stats",
    # Phase 3: access_log-derived analytics for the owner-only /stats section —
    # a require_owner-walled pure read over access_log (never grantee-visible).
    "stats.usage_stats",
}

# A11-3 CONFINED-WRITE ALLOWLIST (2026-07-21): the amendment relaxed Invariant 11
# from "kbweb imports ZERO write functions" to "kbweb imports ONLY these composed
# kbcore chokepoints, and ONLY from the confined module kbweb.views_write". Every
# guarantee (forced-unverified, quarantine, project-scope, owner-only promote) lives
# in kbcore; the web layer merely composes these five. Trust escalation (promote /
# raw ingest / raw supersede) is DELIBERATELY absent — re-adding one fails this test.
WRITE_ALLOWLIST = {
    "web_write.save_for_review",
    "web_write.supersede_for_review",
    "graph.link",
    "review.reject",
    "review.dismiss",
}

# The single module allowed to hold WRITE_ALLOWLIST symbols (all others: read-only).
_CONFINED_WRITE_FILE = "views_write.py"

_TRIPWIRE_MSG = (
    "new kbcore import into kbweb — add to READ_ALLOWLIST (or, for a composed "
    "write chokepoint into views_write only, WRITE_ALLOWLIST) WITH the commit "
    "that justifies it (Invariant 11 / A11-3)."
)


def _kbweb_kbcore_symbols() -> dict[str, set[str]]:
    """Every kbcore symbol each kbweb module imports or touches, as "mod.name".

    Two forms compose the full surface: ``from kbcore.mod import name`` yields
    "mod.name" directly; ``from kbcore import mod`` (whole-module) yields every
    ``mod.attr`` ATTRIBUTE ACCESS in that file — so a new call such as
    ``principals.revoke(...)`` trips the wire even though no import line
    changed. A plain ``import kbcore[.x]`` is flagged outright ("!plain-import")
    because attribute chains through it would evade this walk (fail closed).
    """
    per_file: dict[str, set[str]] = {}
    for py in sorted(_KBWEB_SRC.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        syms: set[str] = set()
        whole_modules: dict[str, str] = {}  # local binding -> kbcore submodule
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module == "kbcore":
                    for alias in node.names:
                        whole_modules[alias.asname or alias.name] = alias.name
                elif node.module.startswith("kbcore."):
                    sub = node.module.split(".", 1)[1]
                    syms.update(f"{sub}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                syms.update(
                    f"!plain-import:{alias.name}"
                    for alias in node.names
                    if alias.name == "kbcore" or alias.name.startswith("kbcore.")
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in whole_modules
            ):
                syms.add(f"{whole_modules[node.value.id]}.{node.attr}")
        if syms:
            per_file[py.name] = syms
    return per_file


def test_every_kbweb_kbcore_import_is_allowlisted():
    per_file = _kbweb_kbcore_symbols()
    assert per_file, "discovery walked zero kbweb modules — harness broken, not clean"
    for fname, syms in sorted(per_file.items()):
        unknown = sorted(syms - READ_ALLOWLIST - WRITE_ALLOWLIST)
        assert not unknown, f"{fname}: {unknown} — {_TRIPWIRE_MSG}"


def test_write_functions_are_confined_to_views_write():
    """A11-3: the WRITE_ALLOWLIST chokepoints may appear ONLY in kbweb.views_write.
    Any other kbweb module importing a knowledge-write function fails closed — the
    confinement is mechanical, not a convention."""
    for fname, syms in sorted(_kbweb_kbcore_symbols().items()):
        writes = sorted(syms & WRITE_ALLOWLIST)
        if fname == _CONFINED_WRITE_FILE:
            continue
        assert not writes, f"{fname} imports confined write functions {writes} outside views_write"


def test_promote_and_raw_ingest_never_allowlisted():
    """The trust-escalation / raw-write surfaces are DELIBERATELY absent from BOTH
    allowlists — a web promote/ingest/supersede must be impossible to import."""
    forbidden = {
        "review.promote",
        "review.resolve_via_supersede",
        "ingest.ingest",
        "ingest.ingest_in",
        "supersede.supersede",
    }
    both = READ_ALLOWLIST | WRITE_ALLOWLIST
    assert not (forbidden & both), "a trust-escalation/raw-write surface was allowlisted"


def test_allowlist_is_exactly_todays_surface():
    """No stale entries either: a symbol kbweb stopped using must LEAVE the
    allowlist, or it silently pre-authorizes a future re-import."""
    used = set().union(*_kbweb_kbcore_symbols().values())
    stale = sorted((READ_ALLOWLIST | WRITE_ALLOWLIST) - used)
    assert not stale, f"allowlist entries no longer used by kbweb — prune them: {stale}"


def test_read_allowlist_contains_no_write_names():
    """The two guards must stay consistent: nothing in the READ allowlist may be a
    known knowledge-write surface (test_phase6_web.WRITE_NAMES) — those live ONLY
    in WRITE_ALLOWLIST, whose basenames are exactly a subset of WRITE_NAMES."""
    read_basenames = {s.rsplit(".", 1)[-1] for s in READ_ALLOWLIST}
    assert not (read_basenames & WRITE_NAMES), "READ_ALLOWLIST contains a write surface"
    write_basenames = {s.rsplit(".", 1)[-1] for s in WRITE_ALLOWLIST}
    assert write_basenames <= WRITE_NAMES, "a WRITE_ALLOWLIST entry is not a known write name"


# --- 3. WRITE_NAMES expansion consistency -------------------------------------

# The post-guard kbcore write surfaces, mapped to their defining module so a
# typo here (or a future rename there) fails loudly instead of guarding nothing.
PHASE1_WRITE_EXPANSION = {
    "enqueue_deep_search": "kbcore.jobs",
    "request_consolidation": "kbcore.jobs",
    "deep_search": "kbcore.deepsearch",
    "run_triage": "kbcore.review_triage",
    "store_triage": "kbcore.review_triage",
    "apply_triage": "kbcore.review_triage",
    "dismiss": "kbcore.review",
    "bulk_resolve": "kbcore.review_auto",
    "reprocess": "kbcore.reprocess",
    "archive_item": "kbcore.items",
    "set_project": "kbcore.items",
}


def test_write_guard_covers_newer_write_functions():
    """test_phase6_web's dir()-scan guard is only as strong as WRITE_NAMES: the
    newer write/GPU surfaces must be in it (extend there, assert here — one
    guard, no duplicate to drift), and each name must really exist in kbcore."""
    import importlib

    missing = sorted(set(PHASE1_WRITE_EXPANSION) - WRITE_NAMES)
    assert not missing, f"newer write surfaces absent from WRITE_NAMES: {missing}"
    for name, module in PHASE1_WRITE_EXPANSION.items():
        assert hasattr(importlib.import_module(module), name), (
            f"{module}.{name} does not exist — WRITE_NAMES guards a phantom"
        )


# --- 4. stored-XSS template lint (A11-3 finding 26 / deliverable 7) -----------

_TEMPLATES_DIR = _KBWEB_SRC / "templates"


def _template_files() -> list[Path]:
    return sorted(_TEMPLATES_DIR.rglob("*.html"))


# FIX 5 (A11-3 template-lint regex): match `|safe` with ANY (or no) surrounding
# whitespace and `Markup(` with any whitespace before the paren — a fixed `"| safe"`
# substring missed `|safe` (no space), `|  safe` (multi-space), and `Markup (`. The
# `\bsafe\b` word boundary keeps `known-safe` prose (no leading `|`) from tripping.
_AUTOESCAPE_BYPASS_RE = re.compile(r"\|\s*safe\b|Markup\s*\(")


def test_no_template_bypasses_autoescape():
    """Mechanical XSS pin: no template may bypass autoescape via `|safe` / `| safe` /
    `Markup(` (any whitespace) — the only HTML that reaches the page must flow through
    the autoescaping `| markdown` filter (html=False escapes raw HTML). A future
    resolve/item body rendered raw would chain stored XSS into the owner's session —
    this fails CI first. FIX 5 replaces the fixed substring that missed the no-space /
    multi-space / `Markup (` variants."""
    # the regex MUST catch a no-space `{{ x|safe }}` and its whitespace variants...
    assert _AUTOESCAPE_BYPASS_RE.search("{{ x|safe }}")
    assert _AUTOESCAPE_BYPASS_RE.search("{{ x |  safe }}")
    assert _AUTOESCAPE_BYPASS_RE.search("Markup ( x )")
    # ...and must NOT trip on the legitimate markdown filter or `known-safe` prose.
    assert not _AUTOESCAPE_BYPASS_RE.search("{{ item.body | markdown }}")
    assert not _AUTOESCAPE_BYPASS_RE.search("ENUMERATED known-safe fields ONLY")
    files = _template_files()
    assert files, "template lint walked zero files — harness broken"
    for path in files:
        text = path.read_text(encoding="utf-8")
        hit = _AUTOESCAPE_BYPASS_RE.search(text)
        assert hit is None, (
            f"{path.name}: `{hit.group() if hit else ''}` bypasses autoescape (finding 26)"
        )


def test_item_body_renders_via_markdown_filter():
    """The item body MUST render through the autoescaping markdown filter."""
    item_html = (_TEMPLATES_DIR / "item.html").read_text(encoding="utf-8")
    assert "item.body | markdown" in item_html, "item body must use the | markdown filter"


# --- KB_WEB_SIGNING_SECRET decoupling (Phase 1 deliverable 2) -----------------


def _foreign(secret: str, salt: str) -> URLSafeTimedSerializer:
    """An attacker's serializer: same itsdangerous construction, their secret."""
    return URLSafeTimedSerializer(secret, salt=salt)


def test_dedicated_secret_verifies_and_master_token_does_not(monkeypatch):
    """With KB_WEB_SIGNING_SECRET set, holding the master API token must no
    longer let anyone forge a session cookie, CSRF token, or OAuth state —
    cookie-forgery and API-master become distinct secrets."""
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_web_signing_secret", WEB_SECRET)
    assert auth.cookie_identity(auth.issue_cookie(auth.OWNER_IDENTITY)) == auth.OWNER_IDENTITY
    assert auth.csrf_ok(auth.issue_csrf(auth.OWNER_IDENTITY), auth.OWNER_IDENTITY)
    assert auth.state_ok(auth.issue_state("st8"), "st8")
    forged_cookie = _foreign(TEST_TOKEN, auth._SALT).dumps(auth.OWNER_IDENTITY)
    assert auth.cookie_identity(forged_cookie) is None, "kb_token-signed cookie accepted"
    forged_csrf = _foreign(TEST_TOKEN, auth._CSRF_SALT).dumps({"n": "x", "id": auth.OWNER_IDENTITY})
    assert not auth.csrf_ok(forged_csrf, auth.OWNER_IDENTITY), "kb_token-signed CSRF accepted"
    forged_state = _foreign(TEST_TOKEN, auth._STATE_SALT).dumps("st8")
    assert not auth.state_ok(forged_state, "st8"), "kb_token-signed OAuth state accepted"


def test_unset_secret_falls_back_to_master_token(monkeypatch):
    """Empty KB_WEB_SIGNING_SECRET keeps today's deployments working: kb_token
    signs/verifies, and the dedicated secret is NOT yet accepted (vice-versa)."""
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_web_signing_secret", "")
    assert auth.cookie_identity(auth.issue_cookie(auth.OWNER_IDENTITY)) == auth.OWNER_IDENTITY
    under_token = _foreign(TEST_TOKEN, auth._SALT).dumps(auth.OWNER_IDENTITY)
    assert auth.cookie_identity(under_token) == auth.OWNER_IDENTITY  # fallback = kb_token
    under_web = _foreign(WEB_SECRET, auth._SALT).dumps(auth.OWNER_IDENTITY)
    assert auth.cookie_identity(under_web) is None, "unset secret must not accept it"


def test_setting_the_secret_forces_one_relogin(monkeypatch):
    """The documented rollout cost: cookies minted under the fallback die the
    moment the owner sets the dedicated secret — one clean re-login, never a
    silent dual-secret window where both verify."""
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_web_signing_secret", "")
    old_cookie = auth.issue_cookie(auth.OWNER_IDENTITY)
    monkeypatch.setattr(settings, "kb_web_signing_secret", WEB_SECRET)
    assert auth.cookie_identity(old_cookie) is None


def test_salt_separation_survives_the_secret_switch(monkeypatch):
    """Distinct salts still partition the token classes under the new secret: a
    session cookie is not a CSRF token is not an OAuth state."""
    monkeypatch.setattr(settings, "kb_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "kb_web_signing_secret", WEB_SECRET)
    cookie = auth.issue_cookie(auth.OWNER_IDENTITY)
    assert not auth.csrf_ok(cookie, auth.OWNER_IDENTITY), "session cookie passed as CSRF"
    assert not auth.state_ok(cookie, "any-state"), "session cookie passed as OAuth state"
    csrf = auth.issue_csrf(auth.OWNER_IDENTITY)
    assert auth.cookie_identity(csrf) is None, "CSRF token passed as session cookie"
