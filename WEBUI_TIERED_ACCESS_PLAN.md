<!-- Design plan produced 2026-07-20 by a 6-agent Fable design-review workflow (5 perspectives: capability model, guest tier, read-feature surfacing, the Inv-11 write fork, UI/phasing -> synthesis). Grounded against commit a41f019. STATUS: awaiting owner decision on the Invariant-11 write fork (§6 Q1). -->

# Web UI Tiered-Access Design — Decision Plan (synthesized 2026-07-20)

All citations verified against the working tree at commit `a41f019`.

---

## 1. INTERPRETATION

The owner is asking for two things at once:

1. **Surface more features in the web UI.** Today's web is search/browse plus an owner-only read of /review and /setup (`views_meta.py:66-94`). The MCP/CLI surface is much bigger: review triage, deep-search jobs and reports, richer stats, export — most of which is *readable* today but not rendered.
2. **A tiered access model.** Today there are exactly two tiers (`scope.Principal` at `scope.py:44-58` has no capability field; the only feature gate is `require_owner`, `scope.py:271-279`). The owner wants "guest users" and "restricted zones" — different user classes seeing different feature subsets, composed with the existing per-project grants (`principal_grants`, `migrations/011_principals.sql:38-48`).

**The one unavoidable decision:** part of "ALL the features" is *writes* — `kb_save`/`kb_supersede`/`kb_link`, `kb_review_resolve`/`bulk`, deep-search *submission* (which writes even on enqueue: `jobs.enqueue_deep_search`, and `deep_search()` saves its report via `ingest()` at `deepsearch.py:26,222`). Every one of these collides head-on with Invariant 11: "data routes are GET/HEAD only; kbweb imports zero knowledge-write functions," enforced mechanically by `test_phase6_web.py:109-157`. There is no compromise position — either the web stays a read surface (writes flow through MCP/CLI, which already work end-to-end for rw grantees), or Invariant 11 is formally amended (A11-3) with a full adversarial re-review. Everything else in this plan is fork-independent and can ship regardless. **Recommendation up front: Branch A — keep Invariant 11 intact** (argued in §4).

A second, smaller interpretation choice: "guest" here means **a least-capable *authenticated* principal**, not anonymous internet access. Anonymous admission on a Cloudflare-public hostname fronting NDA data inverts the fail-closed posture (`server.py:62-63` — anonymous = 302, zero data surface) and is specified but recommended **not built** (§6, Q2).

---

## 2. PROPOSED MODEL (foundation for both branches)

**Role enum on `principals` + capability expansion in code.** A `role` column (not a caps table, not a per-grant role): a caps table adds a join and a second cache-invalidation path to `_build_grantee` (`principals.py:234-249`) with free-form rows; a grant-level role has no home for non-project-scoped features (review queue is global, deep-search is a GPU budget). Roles-as-bundles keep `kb principal list` auditable.

### Migration `migrations/012_principal_roles.sql` (additive — Invariant 5)

```sql
ALTER TABLE principals
  ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'reader'
    CONSTRAINT principals_role_check
    CHECK (role IN ('guest', 'reader', 'contributor', 'reviewer'));

-- Backfill: an rw-grantee can kb_save TODAY (assert_writable is the only wall,
-- scope.py:253-268). Seed those as 'contributor' so this migration never
-- silently revokes a working credential.
UPDATE principals SET role = 'contributor'
 WHERE role = 'reader'
   AND id IN (SELECT principal_id FROM principal_grants WHERE rw);
```

`'owner'` is deliberately **not** a value: the owner is not a row (`011_principals.sql:6-8`), `_build_grantee` hardcodes `is_owner=False` (`principals.py:245`), and ownership stays credential-derived (master token constant-time match `auth.py:74-76`; allowlisted email). No forged DB row can ever resolve to owner — this frozen CHECK is the escalation firewall. `'reader'` as default is behavior-preserving: today's grantee surface is exactly every non-`require_owner` view/tool.

### Capability vocabulary + `scope.Principal` diff (`scope.py`, above line 44)

```python
CAP_SEARCH = "search"            # kb_search + kb_get; web /, /tags/{tag}, /item/{id}
CAP_BROWSE = "browse"            # timeline/stats/projects/trace/similar (tools + views)
CAP_SAVE = "save"                # kb_save / kb_supersede / kb_link — ANDed with rw grants
CAP_REVIEW_READ = "review_read"      # kb_review_list, /review — gated on the detail-scrub precondition
CAP_REVIEW_RESOLVE = "review_resolve"
CAP_DEEP_SEARCH = "deep_search"  # owner GPU (Invariant 8)
ALL_CAPS = frozenset({...all six...})

ROLE_CAPS: dict[str, frozenset[str]] = {
    "guest":       frozenset({CAP_SEARCH}),
    "reader":      frozenset({CAP_SEARCH, CAP_BROWSE}),   # == today's grantee surface
    "contributor": frozenset({CAP_SEARCH, CAP_BROWSE, CAP_SAVE}),
    "reviewer":    frozenset({CAP_SEARCH, CAP_BROWSE, CAP_SAVE}),  # review caps ONLY after
                   # the detail-JSONB scrub lands (views_meta.py:67-70 rationale) — §3
}
```

```python
@dataclass(frozen=True)
class Principal:
    ...existing five fields (scope.py:54-58)...
    role: str = "reader"
    caps: frozenset[str] = field(default_factory=frozenset)  # EMPTY unless expanded — fail-closed

    def has_cap(self, cap: str) -> bool:
        # Pure in-memory membership — no DB round trip, no timing oracle
        # (same property assert_readable documents, scope.py:234-236).
        return self.is_owner or cap in self.caps

OWNER = Principal(name="owner", is_owner=True, kind="owner", role="owner", caps=ALL_CAPS)
```

Default-empty `caps` mirrors `enter_server_mode`'s philosophy (`scope.py:75-101`): a construction site that forgets expansion degrades to *nothing*, never to more. `has_cap` short-circuits on `is_owner`, so a stale `ALL_CAPS` can never cost the owner a feature.

### Enforcement: `scope.require_cap()` inside kbcore

```python
def require_cap(cap: str, what: str = "this operation") -> None:
    if not current().has_cap(cap):
        raise InvalidOperation(f"{what} is not available with this credential")
```

Placed **in kbcore service functions** beside the existing walls (Invariant 2: every surface inherits through the single funnel; CLI/worker unaffected — non-server mode ⇒ OWNER, `scope.py:93-101`). Uniform wording, never naming the missing cap — the tier ladder is not enumerable through error text. Call-site map:
- `require_cap(CAP_SAVE)` before `assert_writable` in ingest/supersede/graph.link (per-principal-constant check, zero target-existence signal).
- `tools_deepsearch.py:56,86` → `require_cap(CAP_DEEP_SEARCH)` — no bundle grants it, so behavior is identical today; flippable later without a migration.
- `tools_phase5.py:103,123,157` (review) STAY `require_owner` until the scrub precondition (§3); `tools_setup.py:184` (consolidate) stays `require_owner` permanently (instance-wide GPU/maintenance, not a shareable feature).

**Composition contract (enshrined in the scope.py docstring): capabilities gate FEATURES; project grants gate ROWS. `require_cap` never replaces `project_predicate`/`assert_readable`/`assert_writable` (`scope.py:134-268`) — they AND.** A contributor without an rw grant still gets the uniform not-writable rejection (`scope.py:249-250`, unchanged); a reviewer with zero grants sees zero rows everywhere.

### Resolution + lifecycle plumbing

- The three resolvers add `role` to their SELECT (`principals.py:271,295,314`); `_build_grantee` grows a `role` parameter and expands `caps = ROLE_CAPS.get(role)`; **unknown role ⇒ `frozenset()` + `log.warning`** (schema/code skew serves the wall loudly, never a 500, never more access).
- Cache: role rides inside the cached Principal (`principals.py:227,283`) — refreshes within `KB_PRINCIPAL_CACHE_TTL` (60s, `config.py:55`) for kbg_ tokens; the web/google paths are uncached (`principals.py:287-320`) so browser downgrades are instant.
- `create_principal` gains `role: str = "reader"` (validated against `ROLE_CAPS`); new `principals.set_role(name, role)` calling `_clear_cache()` — exact parity with `grant()/ungrant()/revoke()`. `PrincipalInfo` + `list_principals` + CLI: `kb principal create --role ...`, `kb principal role <name> <role>`.

### Web zone wall (the read-side frame)

A declarative `ROUTE_CAPS` prefix→cap table in `kbweb/server.py`, consulted in `SessionAuthMiddleware.dispatch` at the exact point the /admin wall sits (`server.py:64-69`), answering with the **same** `PlainTextResponse("Not Found", 404)` **before routing** — so to an under-capped principal, a walled zone is byte-indistinguishable from a path that never existed, and no method probe elicits a 405. This *extends* the no-oracle discipline from data existence to feature existence. `/review` and `/setup` move from today's explanatory 400 ("available to the KB owner only" via `views_meta.py:70,81` → the KBError handler) — an existing feature-existence disclosure — behind the 404 wall; the in-view `require_owner`/`require_cap` stays as defense-in-depth (the `_owner_or_404` layering pattern, `views_admin.py:53-82`). Default-deny: a startup assertion + test require every registered route to have a `ROUTE_CAPS` entry. Documented asymmetry: MCP keeps `require_cap`'s clean message (tool existence is not secret on that surface, `scope.py:273-276`); the web — the phishable, probeable surface — gets the 404.

Chrome: nav rendered strictly from `request.state.principal.caps` (live principal, not cookie), fixing the existing bug where grantees see review/setup links that 400. `/welcome` becomes per-tier, rendering only the principal's own caps — never a cross-tier catalog.

---

## 3. TIERS

| Tier | Storage | Caps | CAN | CANNOT |
|---|---|---|---|---|
| **guest** | `role='guest'` row, read-only grants | `search` | `/`, `/tags/{t}`, `/item/{id}`; MCP kb_search/kb_get — within granted projects only | Every other zone (`/timeline /stats /projects /project/* /trace/* /similar/* /review /setup /admin`) = byte-identical 404; all writes |
| **restricted reader** (`reader`, default) | `role='reader'` | `search, browse` | Everything a grantee can do today: all current data views + kb_timeline/stats/projects/trace/similar | Review, setup, admin, all writes, deep-search. **Byte-identical to a pre-migration grantee — the zero-behavior-change proof.** |
| **contributor** | `role='contributor'` (backfilled for existing rw-grantees) | `+ save` | kb_save/kb_supersede/kb_link **via MCP/CLI only**, ANDed with rw grants; audit via `log_write` (`scope.py:282-296`) | Any web write (Branch A); review resolve; deep-search |
| **reviewer** | `role='reviewer'` | contributor caps; `+ review_read/review_resolve` **only after** the detail scrub | (future) scoped review list + resolve via MCP | Cross-project review detail — hard precondition below |
| **owner** | not a row; master token / allowlisted email | `is_owner` short-circuit | Everything, incl. /setup (renders master bearer, `views_meta.py:74-94` — permanently owner-only), consolidate, deep-search, admin | — |

**Reviewer precondition (hard):** `views_meta.py:67-70` and `review.py:93-96` document why review is owner-only: `review_queue.detail` JSONB carries cross-project titles/ids past the row-level `_visibility_gate` (`review.py:87-99`). The bundle ships with reviewer == contributor; flipping in the review caps is one line, gated on (a) an **allowlist-scrubbing** projection in `kbcore.review` (emit only known-safe keys whose ids pass `readable_item_ids`, `scope.py:211-227`) — in kbcore, never the web layer, so MCP `kb_review_list` gets the identical wall — and (b) the `_queued_item_id` fix (§4, P5-1). A mechanical test pins `ROLE_CAPS["reviewer"]` to exclude review caps until then.

**Restricted zones** = exactly this composition: project grants restrict WHAT data (rows), roles restrict WHICH features (zones). Both walls already AND-compose the way `public_only` × grants do (`scope.py:140-149`).

---

## 4. THE FORK

### Branch A — stay read-only; Invariant 11 intact (RECOMMENDED)

**Ships:** everything in §2/§3, plus the full read-feature surfacing: `/jobs` (read-only deep-search/consolidation status via `jobs.deep_search_status`/`latest_consolidation_status` + one new `list_deep_search_jobs` SELECT; owner-cap at launch — worker jobs run under implicit OWNER scope and `jobs.result` carries whole-KB synthesis), rich `type='report'` item rendering (reports are ordinary scope-gated items, `deepsearch.py:222`), the review triage cockpit (rendering `detail.triage`/`suggested_resolution` that `review_triage.store_triage` already persists — template-only, with **copyable** `kb review resolve` commands), stats expansion (every new aggregate composes `project_predicate` exactly like `stats.py`), GET-only `/export.{csv,json}` behind a separate `export` cap, and a contributor-onboarding page (MCP URL + instructions; **never** the token — no secret transits a browser, A11-2).

**Security cost:** zero. The mechanical test suite (`test_phase6_web.py:109-157`) passes with its POST allowlist (`{"/login"} | {"/admin/grant","/admin/revoke","/admin/principal"}`, line 106,122-123) untouched.

**Adversarial-review demand:** one narrow **A11-2a clarification** (not amendment): the existing `POST /admin/principal` may additionally set the authz-config `role` field at creation, restricted to a hardwired non-privileged allowlist `{guest, reader}` — the exact structural parallel to `rw=False` (`views_admin.py:125,171`) and `kind="google"` (`views_admin.py:162`) hardwiring. Role *change* and elevated roles stay CLI-only, enforced by a trip-wire test that `set_role` never appears in any kbweb module.

**Product cost:** writes stay on MCP/CLI. The write-capable population is the owner (full MCP/CLI already) plus at most a few grantees for whom the rw-grant MCP path is complete and no-oracle-tested (`test_no_oracle.py`). The marginal user Branch B serves — a contributor who won't configure an MCP client — is currently **zero people**.

### Branch B — amend Invariant 11 (A11-3) for scoped web writes (specified, declined)

**Would ship:** one confined module `kbweb/views_write.py` importing exactly {ingest, supersede, link, promote, reject, dismiss}; POST `/save`, `/item/{id}/supersede`, `/item/{id}/link`, `/review/{qid}/resolve`; four-layer gating (zone-404 wall → `require_cap` → `assert_writable` → same-origin + identity-bound CSRF, `auth.issue_csrf` is already identity-parameterized); every non-owner web save forced `trust='unverified'` + review-queued in the same transaction. The zero-write-import test becomes a confined-write-import test.

**Security cost, plainly:** today an XSS bug, a stolen grantee cookie, or a middleware-ordering regression on this internet-facing NDA host can only *read* what that grantee could read. Under B, the same bug **forges knowledge** — and the KB is consumed by Claude sessions, so a poisoned "decision" is an indirect prompt-injection vector into every future agent session. Unverified+review bounds trust but not retrieval.

**Adversarial-review demand:** a full A11-3 round (it is exactly the "any future mutating web route re-opens this review" trigger), PLUS three hard prerequisites:
1. **`_queued_item_id` project-name leak** (`review.py:158-173`): it calls `assert_writable(row[1])` on a caller-supplied guessable queue id, and the rejection embeds the project name (`scope.py:249-250`) — a non-owner resolver could enumerate withheld NDA project *names*. Fix: `assert_readable(project, f"review queue row {queue_id} does not exist")` **before** `assert_writable` (the readability-first ordering supersede already uses). **Land regardless of branch** — zero owner-visible change.
2. **Cookie-secret decoupling**: the session/CSRF/OAuth-state signing secret IS `settings.kb_token` (`auth.py:37-39`). Add `KB_WEB_SIGNING_SECRET` so cookie forgery and API mastery stop being the same secret. **Land regardless** (one forced re-login at rollout).
3. **Byte-identical error-mapping pins** at the web layer for write rejections (nonexistent vs withheld vs read-only-granted project).

**RECOMMENDATION: Branch A.** For a single-owner NDA vault with a handful of read-mostly grantees, B buys almost nothing the MCP write path doesn't deliver and spends the property that makes an internet-exposed UI over NDA data tolerable ("the most phishable surface can never alter knowledge"). Record the fork in the KB (`kb_save(project='kb-system', type='decision', title='Inv-11 write fork: Branch A adopted, Branch B declined')` with the B spec + prerequisites attached), so reversal is deliberate — via `kb_supersede` with the A11-3 review as reason — never accidental. Re-open trigger, stated now: a real second contributor who cannot use MCP, or the deployment ceasing to carry NDA data.

---

## 5. PHASING (one phase per session, per CLAUDE.md)

### Phase 1 — Hardening + test harness (Inv-11-safe; ship anytime; no schema change)
**Scope:** P5 fixes 1-2 (`review.py:158-173` readability-first ordering; `KB_WEB_SIGNING_SECRET` in config with kb_token fallback). New `tests/test_web_caps.py` harness: `ghost_identical(client, path)` lifted from `test_web_admin.py`'s byte-identical-404 loop; WRITE_NAMES expansion (`enqueue_deep_search`, `request_consolidation`, `deep_search`, `run_triage`, `store_triage`, `apply_triage`, `dismiss`, `bulk_resolve`, ...); the inverted **READ_API import allowlist** test (every kbweb kbcore import must appear in an explicit read allowlist — fails closed on unknown imports, with a failure message demanding the commit cite the function).
**AC:** `pytest -q` green; a seeded contradiction row spanning granted+withheld projects proves queue-row probes are byte-identical to missing; masked-secret output only.
**Adversarial focus:** revoked-vs-unknown-vs-withheld uniformity on the review path; cookie invalidation rollout.
**No-oracle discipline:** the ghost harness itself is the deliverable.

### Phase 2 — Capability model + zone walls, zero feature change (Inv-11-safe; A11-2a clarification only)
**Scope:** migration 012 + backfill; scope.py caps/`has_cap`/`require_cap` + `ROLE_CAPS`; resolver/`_build_grantee`/cache plumbing; `set_role` + CLI; kbcore call-site migration (§2 map); `ROUTE_CAPS` middleware wall incl. /review + /setup 400→404; caps-driven nav + per-tier /welcome; admin form role picker with `_WEB_ROLE_ALLOWLIST = {"guest","reader"}`.
**AC (the zero-behavior-change proof):** a `reader` grantee's responses byte-identical to a pre-migration grantee on **every** route; `test_phase6_web.py:109-157` unchanged and green; migration re-run is a no-op; `set_role` cache-flush parity test; hostile `role='owner'` POST ⇒ 400 + no row; ghost loop over (role × walled route × six methods) all byte-identical to `/no-such-path`; nav-honesty per role; `ROUTE_CAPS` totality assertion.
**Adversarial focus:** role-name leakage in any error body; method probes; cache TTL vs demotion; the 404 body never drifting from the router's real unknown-path bytes (always compare against a fetched ghost, never hardcode).

### Phase 3 — Guest tier + read-feature surfacing (Inv-11-safe)
**Scope:** guest-as-role live (create via admin form or CLI + read-only grants — data walls already proven for arbitrary grant sets); `/jobs` viewer (owner-cap; status fields only, report text only via scope-gated `/item/{id}`); review triage cockpit (enumerated fields only — never a raw `detail` dump; dup_of links re-gate through `/item/{id}`); stats expansion (items_per_week, tag landscape, trust trend — every query composes `project_predicate`; access_log-derived analytics owner-only); `/export.{csv,json}` behind the `export` cap with hard row cap + CSV formula-injection quoting; role-aware invite text.
**AC:** guest matrix green (guest sees only `/`, `/tags`, `/item`; everything else == ghost bytes); one-project grantee's stats series sums equal their by_project count (no unscoped residual); export rows stamped to access_log per principal; parametrized `test_no_oracle.py` suite run with a guest principal.
**Adversarial focus:** aggregate-inference leaks in new stats; the `/jobs` result field as a whole-KB oracle if its cap is ever widened; bulk-exfiltration economics of export.

### Phase 4 — The fork phase (GATED on the owner's §6 Q1 answer; default = Branch A)
**Branch A scope (Inv-11-safe):** kbcore review detail allowlist-scrub + `readable_item_ids` projection; flip reviewer's `ROLE_CAPS` to include review caps (the one-line change, with its trip-wire test consciously rewritten); contributor onboarding page; copyable resolve commands on /review for reviewers; KB decision item recording the fork.
**Branch A AC:** seeded cross-project contradiction row renders scrubbed for a reviewer, byte-identical to a world where the withheld item never existed — at both the web AND MCP surfaces (same kbcore function, or the two diverge into an oracle).
**Branch B (only if the owner re-opens):** full A11-3 adversarial review **before any code**; then the P4 spec with all Phase-1 prerequisites already landed; every new POST route consciously added to `ADMIN_MUTATING_ROUTES`' successor allowlist.
**Adversarial focus:** the detail-JSONB scrub allowlist (blocklist scrubbing is forbidden); scope creep — "just one resolve button" breaks the zero-import prong exactly as thoroughly as five routes do; the mechanical POST-allowlist test makes any premature mutating route a hard CI failure.

---

## 6. OPEN QUESTIONS FOR THE OWNER

1. **THE FORK (decides Phase 4):** Accept Branch A — web stays read-only, writes flow through MCP/CLI where your rw-grant machinery already works — or insist on in-web writes, which requires the A11-3 amendment, a full adversarial re-review, and the Phase-1 prerequisites first? *Recommendation: Branch A; file B's spec as a sealed, superseded-not-contradicted design.*
2. **Anonymous guest:** "Guest" here = an *authenticated* least-capable principal. Do you additionally want **anonymous** (no-login) access to `projects.public`-flagged projects (the `003_project_public.sql` lever)? Specified (flag-gated, per-request materialization, Cloudflare rate limits, `Cache-Control: private`), but *recommended NO on this NDA deployment* — if you ever want a public showcase, a separate `KB_PUBLIC_ONLY` instance is the safe shape.
3. **Reviewer tier activation:** OK to ship reviewer == contributor until the detail-JSONB scrub lands (Phase 4A), or is scoped review reading urgent enough to pull the scrub into Phase 3?
4. **Export cap defaults:** grant `export` to readers by default, or owner + named grantees only? *Recommendation: not for guests; per-grantee decision for readers.*
5. **`CAP_DEEP_SEARCH` grantability:** the vocabulary supports granting the 14B to a trusted grantee later (Invariant 8 is your GPU-courtesy rule to waive). Keep no-bundle (owner-only) indefinitely? *Recommendation: yes for now; flipping is a one-line code change, no migration.*
6. **Web role management ceiling:** confirm the A11-2a line — creation-time role selection from `{guest, reader}` only in the admin form; role *change* and contributor/reviewer assignment CLI-only forever (or until a dedicated A11-x amendment)?

**Key files for the implementing sessions:** `/home/usama/data-server/src/kbcore/scope.py`, `/home/usama/data-server/src/kbcore/principals.py`, `/home/usama/data-server/src/kbcore/review.py`, `/home/usama/data-server/src/kbweb/server.py`, `/home/usama/data-server/src/kbweb/views_admin.py`, `/home/usama/data-server/src/kbweb/views_meta.py`, `/home/usama/data-server/src/kbweb/auth.py`, `/home/usama/data-server/tests/test_phase6_web.py`, `/home/usama/data-server/tests/test_no_oracle.py`, `/home/usama/data-server/tests/test_web_admin.py`, `/home/usama/data-server/migrations/011_principals.sql`, `/home/usama/data-server/migrations/003_project_public.sql` (new: `migrations/012_principal_roles.sql`, `tests/test_web_caps.py`).
