I have complete grounding. Producing the plan document now.

# Knowledge Vault — Next Enhancements Plan

**Repo:** `/home/usama/data-server` · **Target file:** `docs/NEXT_ENHANCEMENTS_PLAN.md`
**Authored:** 2026-07-17 · **Baseline commit:** `0999e29` (master) · **Next migration number: `012`**

> **How to use this document.** It is written to be executed by a *fresh* Claude Code session with no memory of the analysis that produced it. Every claim is grounded in a real file:line in this repo. Read `PLAN.md` (invariants §1) and `CLAUDE.md` (hard rules) before starting any work package. Work one package per session; end each with its acceptance criteria (AC) run and raw output pasted; commit per the `phase-N:`/`wp-X:` convention. Do **not** re-litigate settled verdicts (V1–V8) or the nine PLAN.md invariants (plus the Phase-6 additions Inv. 10 network / Inv. 11 read-only-web / Inv. 12 encrypted-offsite).

---

## 1. Context — what shipped, what this plan covers

The KB is a single-owner, NDA-data, cross-project engineering-memory system: Python 3.12 / `uv` / Postgres+pgvector / local Ollama, exposed publicly via a Cloudflare Tunnel on 443 (`kb`/`api`/`web.n3rd-bugs.dev`) serving MCP (FastMCP 3.4.4), a REST facade (FastAPI), and a read-only web UI (Starlette). Owner preference, thrice stated: **"as standalone / low-maintenance as possible."**

Recent campaign (commits `d4328b9` connector batch, `161b014` review round-2, `69bb18b` per-project sharing, `0999e29` write-path oracle fixes) delivered: per-project sharing (migration `011_principals.sql` — grantee `kbg_` tokens + `kind='google'` grantees + scoped grants + the `kbcore.scope` no-oracle wall), async deep-search jobs (migration `010`), a nightly search-quality regression sentinel, and OAuth/CIMD connector plumbing.

This plan covers, in order: (2) immediate cleanup of cross-batch seams found in retrospective; (3) the centrepiece feature — **email/username + password authentication for the web UI**; (4) a ranked roadmap of the deferred backlog (encrypted offsite backups, the 2026-07-28 MCP spec revision, CIMD flip, grantee write-audit, decay/recency scoring); (5) ready-to-paste sub-agent prompts for every work package; (6) decisions reserved for the owner.

---

## 2. Immediate cleanup (do before new features)

Two **medium** findings and three **low** findings. None is a live security hole, but the two mediums are correctness seams that silently degrade behavior, and the lows are cheap. Fix the mediums first; the lows can ride along in the same cleanup wave.

### 2.1 [MEDIUM] Deep-search reports poison the nightly consolidation "since" marker

- **Where:** `src/kbcore/consolidation.py:23-30` (`last_consolidation_at()`).
- **What:** `last_consolidation_at()` returns `max(created_at) FROM items WHERE project='kb-system' AND type='report'`. Its result is the `since` marker fed to `tags_needing_summary()` (`consolidation.py:135-158`, `HAVING count(*) FILTER (WHERE created_at > since) >= 3`). The design assumes only the nightly run writes `kb-system` reports. Deep-search broke that: `deep_search()` writes `ingest(project or 'kb-system', 'report', …, trust='unverified')` at `src/kbcore/deepsearch.py:218-226` — a `kb-system` `type='report'` item at an arbitrary daytime. After any deep-search, `since` jumps forward, `tags_needing_summary` undercounts "new" items, and the `>=3` gate skips topic summaries that should have refreshed; those items are "old" next run and never counted again. The sibling reader `previous_eval_metrics()` (`consolidation.py:35-61`) was already hardened against this exact pollution (bounded walk-back + `meta.eval_status=='ok'` filter) — the asymmetry is the tell.
- **Fix:** the nightly report **does** carry `meta={"consolidation": True, …}` (verified `src/kbworker/consolidate.py:260`), and deep-search reports do **not**. Change the query to:
  ```sql
  SELECT max(created_at) FROM items
  WHERE project = 'kb-system' AND type = 'report' AND meta->>'consolidation' = 'true'
  ```
- **Test:** insert a nightly-shaped report (meta.consolidation=true) at T0, then a deep-search-shaped report (no meta.consolidation) at T1>T0; assert `last_consolidation_at() == T0`.

### 2.2 [MEDIUM] Sentinel enqueues NULL-item `search_regression` rows that no human surface can resolve

- **Where:** `src/kbworker/eval_sentinel.py:104-117` enqueues `queue_review(None, 'search_regression', {…})` (item_id NULL).
- **What:** every human resolve path funnels through `review._queued_item_id()` (`src/kbcore/review.py:157-172`), which raises `InvalidOperation("review queue row N has no associated item")` when `item_id IS NULL`. That blocks `kb_review_resolve` promote/reject/supersede (`src/kbmcp/tools_phase5.py:90-106`), CLI `kb review promote|reject|supersede|apply` (`src/kbcli/review_cmds.py:32-90`), and REST `POST /api/review/{id}/resolve` (`src/kbrest/server.py:239-249`). The only caller of the generic item-less `resolve_review()` (`review.py:76-83`) is the sentinel's own auto-recovery (`eval_sentinel.py:122-130`), which fires only when hit@5/MRR climb back to the max-anchored baseline. So a regression the owner accepts as the new normal — or a spurious trip after enabling `KB_GRAPH_EXPAND_HOPS>0` (which shifts eval scores because `eval_harness.evaluate → search()` inherits `settings.kb_graph_expand_hops`) — is un-dismissable and sticks open forever.
- **Fix:** add a `dismiss` action wired to `review.resolve_review(queue_id)` for **item-less** rows only, on all three owner surfaces:
  - `kb_review_resolve(queue_id, action='dismiss')` in `tools_phase5.py` (keep `scope.require_owner`).
  - `kb review dismiss <queue_id>` in `review_cmds.py`.
  - `POST /api/review/{id}/resolve` accept `action='dismiss'` in `kbrest/server.py`.
  Guard: `dismiss` must refuse a row that *has* an item (steer to promote/reject/supersede), so it stays a narrow escape hatch. Add a `review.dismiss_review(queue_id)` helper in `kbcore/review.py` that looks up the row, asserts `item_id IS NULL`, and calls the status flip.
- **Test:** enqueue a null-item `search_regression` row; assert each surface's `dismiss` resolves it and that `dismiss` on an item-bearing row is rejected.

### 2.3 [LOW] Eval sentinel fetches relations it discards, every night

- **Where:** `src/kbcore/eval_harness.py:56` — `get_item(hit.item_id).body`. `get_item` defaults `with_relations=True` (`src/kbcore/items.py:14`), so each hit opens a fresh connection and runs the relations query + the scope hidden-predicate join (`items.py:49-77`) only to read `.body`. The sentinel replays this every nightly consolidation (`consolidate.py:328`): 25 golden queries × up to K=5 hits ≈ 125 discarded relation queries/night.
- **Fix:** `get_item(hit.item_id, with_relations=False).body`.
- **Test:** none strictly required; assert eval results unchanged with the flag.

### 2.4 [LOW] Test truncate fixture drifted out of sync with the schema

- **Where:** `tests/conftest.py:116-123` — the function-scoped `kb` fixture TRUNCATEs 12 tables but omits `deep_search_jobs` (migration `010`) and `consolidation_requests` (migration `004`), both BIGSERIAL job queues consumed by `claim_pending_*` (`SELECT … ORDER BY id … FOR UPDATE SKIP LOCKED`). Affected tests each add a manual DELETE workaround; any new test that enqueues without replicating it picks up a leaked stale pending row.
- **Fix:** add `deep_search_jobs, consolidation_requests` to the TRUNCATE list; remove the now-redundant per-file DELETE workarounds in `tests/test_deep_search_async.py` and `tests/test_consolidate_trigger_surfaces.py`.
- **Test:** the suite itself; confirm no order-dependent flake by running `pytest -q -p no:randomly` and a shuffled run.

### 2.5 [LOW] Four core modules crossed the ~300-line guideline

- **Where:** `src/kbcore/search.py` (373), `items.py` (340), `graph.py` (340), `ingest.py` (327) — all over CLAUDE.md's "keep files under ~300 lines" (verified via `wc -l`). `src/kbmcp/server.py` is 576 (nearly 2×) and `consolidation.py` is 338.
- **Fix (optional, low priority):** carve cohesive helpers out (the campaign already did this cleanly with `search_expand.py` and `principal_ctx.py`). Do **not** bundle this with a behavioral change — a pure move-only diff is reviewable; mixing it with 2.1/2.2 is not. Defer `server.py` unless it is touched anyway by the connector work (§4 Wave 1).
- **AC:** `ruff check` + full `pytest -q` green; no public symbol import path changes (re-export from the original module if needed).

---

## 3. Feature: Email/username + password authentication (centrepiece)

### 3.0 Design provenance (settled — do not re-open)

Three designs were scored by three independent judges. **Consensus winner: Design 1 — "Minimal zero-dependency password auth"** (web-only, stdlib scrypt, `kind='password'` on `principals`), with **one mandatory security graft from Design 3** (a DB-backed login throttle) and several smaller grafts. Design 2 was disqualified from winning because its recommended self-service change put a mutating `POST /account/password` route on the read-only web UI, which breaks `tests/test_phase6_web.py:99-110` (Invariant 11). The synthesized artifact below **is** the merge; the "must-fix" items are folded in as requirements.

**One-line summary:** add a fourth principal kind, `password`, resolved to the *same* `scope.Principal` (`is_owner=False`) as a `kbg_` token or Google grantee — so all of `kbcore.scope`'s per-project no-oracle walling is inherited for free (`scope.py:134-149`, `230-244`, `_build_grantee` at `principals.py:227-242`). Only the `/login` endpoint gains new code. Session reuses the existing itsdangerous cookie verbatim; `principals.resolve_name` (`principals.py:280-293`) is already kind-agnostic, so a password cookie re-resolves live with zero session-layer change. scrypt runs **once per login**, never per request.

### 3.1 Surfaces served — and the honest limits

| Surface | Password login? | Rationale (grounded) |
|---|---|---|
| **kbweb web UI** (`web.n3rd-bugs.dev`) | **YES** — primary and only surface | The existing `POST /login` (`kbweb/server.py:65-77`) gains a username/password branch. |
| **kbrest REST** (`api.n3rd-bugs.dev`) | **NO** (default) | Stays bearer-only via the shared `BearerAuthMiddleware` (`kbmcp/server.py:447-489`). scrypt is deliberately ~15-50 ms — verifying it per request is a DoS/latency problem. Optional opt-in extension only: a token-exchange `POST /auth/session` (Basic once → mint a `kbg_`), **OFF by default**. Do **not** add per-request HTTP Basic to `principal_ctx.resolve_bearer` (`principal_ctx.py:42-56`) — it is shared by local MCP, so Basic there would silently make local MCP accept Basic too. |
| **claude.ai remote connector** (`kb.n3rd-bugs.dev/mcp`) | **NO — categorically** | Remote connectors authenticate via OAuth or a static bearer only (verified `principal_ctx.py:87-104`, `resolve_current_principal`). There is no interactive password form in the connector handshake. **State this on every surface:** a human needing both web (password) and the connector needs a *second* identity (`kind='google'` or a `kbg_` token). |
| **local MCP** (Claude Code, `127.0.0.1:8321`) | **NO** (bearer only) | Unchanged. |

### 3.2 Migration `012_password_principals.sql` (additive)

```sql
-- 012_password_principals.sql — add a 'password' principal kind for web login.
-- ADDITIVE (Invariant 5): two nullable columns + one new table + a WIDENED kind
-- domain CHECK. The TWO biconditional CHECKs on principals (011:34-35) are left
-- UNTOUCHED — a password row keeps secret_hash NULL and email NULL, satisfying
-- both unchanged. Widening the kind CHECK is the ONE existing-object change:
-- data-preserving, strictly permissive, and unavoidable for any new kind.
-- Flag it in the commit for Invariant-5 sign-off (see Open Questions Q9).

ALTER TABLE principals ADD COLUMN IF NOT EXISTS username      TEXT;
ALTER TABLE principals ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- Case-insensitive uniqueness of the login identifier, enforced in the DB (not
-- just app-layer). principals.name is case-SENSITIVE UNIQUE (011:24) and email
-- login must be case-insensitive, so a dedicated username column + a partial
-- unique index on lower(username) is the structural fix (avoids the case-mismatch
-- lockout footgun of reusing name).
CREATE UNIQUE INDEX IF NOT EXISTS principals_username_lower_idx
  ON principals (lower(username)) WHERE username IS NOT NULL;

-- Widen ONLY the kind domain CHECK. The 011 constraint is auto-named
-- (principals_kind_check for a column-level check), but drop it via a
-- pg_get_constraintdef lookup — robust even if the auto-name ever differs, and it
-- deliberately EXCLUDES the two biconditionals (which mention secret_hash/email).
DO $$
DECLARE cname text;
BEGIN
  SELECT conname INTO cname
  FROM pg_constraint
  WHERE conrelid = 'principals'::regclass AND contype = 'c'
    AND pg_get_constraintdef(oid) LIKE '%kind%'
    AND pg_get_constraintdef(oid) NOT LIKE '%secret_hash%'
    AND pg_get_constraintdef(oid) NOT LIKE '%email%';
  IF cname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE principals DROP CONSTRAINT %I', cname);
  END IF;
END $$;
ALTER TABLE principals
  ADD CONSTRAINT principals_kind_check CHECK (kind IN ('token','google','password'));

-- DB-backed brute-force throttle: shared across kb-web and any kb-rest process,
-- durable across restart. Keyed on a HASH of the identifier (raw username never
-- stored) AND a separate per-IP row to bound password spraying.
CREATE TABLE IF NOT EXISTS login_throttle (
  scope        TEXT NOT NULL CHECK (scope IN ('identifier','ip')),
  key_hash     TEXT NOT NULL,               -- sha256(lower(strip(identifier)))  or sha256(client_ip)
  window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
  fail_count   INT NOT NULL DEFAULT 0,
  locked_until TIMESTAMPTZ,
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (scope, key_hash)
);
CREATE INDEX IF NOT EXISTS login_throttle_updated_idx ON login_throttle(updated_at);
```

**Invariant note:** widening the `kind` CHECK is the single existing-object modification (CLAUDE.md hard-rule 5 is "additive only"). It is a monotonic domain *widening* — no rows are rewritten, and a password row satisfies both untouched biconditionals with `secret_hash NULL`/`email NULL`. Flag it in the commit message; it needs owner sign-off (§6 Q9), not a rewrite.

### 3.3 Hashing choice + justification

- **Choice:** Python-stdlib `hashlib.scrypt` (OpenSSL-backed, memory-hard), **n=2¹⁴, r=8, p=1**, 16-byte per-row salt (`secrets.token_bytes(16)`), `dklen=32`. **Zero new dependency** — the decisive fit to the standalone mandate.
- **Why n=2¹⁴ and not 2¹⁵:** n=2¹⁵,r=8,p=1 needs exactly 32 MiB and `hashlib.scrypt` **raises** against OpenSSL's default `maxmem` cap → every login 500s. n=2¹⁴ (~16 MiB) stays under the default and needs no `maxmem` argument. (If the owner insists on 2¹⁵, pass `maxmem=64*1024*1024` explicitly — but the default is 2¹⁴.)
- **Rejected:** argon2-cffi / bcrypt (compiled wheels that must build on the host *and* on every `restore-test` — conflicts with "as standalone as possible" for a handful of human passwords); pbkdf2_hmac (not memory-hard; documented as the same-wrapper CPU fallback only).
- **Storage:** a self-describing PHC-style string in `password_hash`: `scrypt$<n>$<r>$<p>$<b64salt>$<b64dk>`. Verify with `hmac.compare_digest`. **Lazy rehash-on-successful-login:** `verify_password(encoded, plain) -> (ok: bool, needs_rehash: bool)`; when a row's params are below the current target, rehash and `UPDATE` inside the same successful-login path. Params live in the string, so a future bump is config-only, no migration.
- **CI footgun guard:** ship a test that actually calls `hash_password` then `verify_password` round-trip, so the `maxmem`/params combination is exercised in CI (not just imported).

### 3.4 New / changed files

| File | New/changed | Responsibility |
|---|---|---|
| `migrations/012_password_principals.sql` | new | §3.2 DDL. |
| `src/kbcore/passwords.py` | new | `hash_password(plain) -> str`, `verify_password(encoded, plain) -> (ok, needs_rehash)`. Enforce `settings.kb_password_min_len` at hash time. `DUMMY_HASH` constant for constant-work on miss. |
| `src/kbcore/login_throttle.py` | new | `record_failure(identifier, client_ip)`, `check_locked(identifier, client_ip) -> bool`, `clear(identifier)`, `prune()`. Sliding window + lockout; sha256-keyed; per-identifier AND per-IP; prune-on-write. **Not** in `tests/test_phase6_web.py` WRITE_NAMES, so calling it from `/login` keeps the read-only invariant green. |
| `src/kbcore/principals.py` | changed | Widen `create_principal` to `kind='password'` (accept `username`, `password`; store `hash_password`); add `set_password(name, plain)` (rejects non-password kinds, like `rotate` rejects google at `principals.py:171-172`; clears throttle); add `resolve_password(identifier, plain, client_ip) -> Principal | None`. Keep `resolve_name` kind-agnostic (add a regression test pinning that). |
| `src/kbweb/server.py` | changed | Add branch **(c)** to `POST /login` (§3.5). Import `login_throttle` + `principals.resolve_password`. No new route, no write import → Invariant 11 preserved. |
| `src/kbweb/templates/login.html` | changed | Add `username` + `password` fields beside the existing token field and Google link. |
| `src/kbcli/principal_cmds.py` | changed | `kb principal create <name> --password [--username <email>]` (hidden confirmed prompt), `kb principal set-password <name>`, `kb principal unlock <name>`. |
| `src/kbcore/config.py` | changed | `kb_password_min_len: int = 12`, `kb_login_max_attempts: int = 5`, `kb_login_window_seconds: int = 900`, `kb_login_lockout_seconds: int = 900`. (No composition rules — length only.) |
| `.env.example` | changed | Document the four new knobs. |
| `tests/test_password_auth.py` | new | §3.8 checklist. |

### 3.5 Login flow (extends `kbweb/server.py:65-77`)

`GET /login` renders the form. `POST /login` handler order:
- **(a)** token field present and `auth.token_ok(supplied)` → owner session (`_issue_session("/", auth.OWNER_IDENTITY)`) — **unchanged**.
- **(b)** `elif` `principals.resolve_token(supplied)` returns a grantee → grantee session — **unchanged**.
- **(c)** `elif` username **and** password present → `principals.resolve_password(username, password, client_ip)`; on success `_issue_session("/", auth.grantee_identity(grantee.name))`.
- **(d)** any miss → one generic `401 "Invalid credentials."` (same wording for wrong token and wrong password — no oracle).

`resolve_password(identifier, plain, client_ip)` steps:
1. normalize `identifier = identifier.strip().lower()`.
2. `login_throttle.check_locked(identifier, client_ip)` → if locked, run a throwaway scrypt against `DUMMY_HASH` anyway (no early-out timing tell) and return `None`.
3. `SELECT id, name, kind, password_hash FROM principals WHERE lower(username)=%s AND <_live_where()>` (reuse `principals._live_where()`, `principals.py:245-246`).
4. no row → verify presented password against `DUMMY_HASH` to equalize timing, `record_failure`, return `None`.
5. row → scrypt-verify; on success: `clear` the throttle, lazy-rehash if params stale, `_build_grantee(...)` → return the Principal. On failure: `record_failure`, return `None`.

Client IP: read from the request; behind Cloudflare use the left-most `X-Forwarded-For` / `CF-Connecting-IP` (document that the throttle trusts the edge — belt-and-suspenders with the Cloudflare rate-limit).

### 3.6 Session model (zero change)

Reuse the itsdangerous `URLSafeTimedSerializer` cookie verbatim (`kbweb/auth.py:21-53`): identity payload `g:<name>` (`auth.grantee_identity`), `MAX_AGE=12h`, `httponly`, `samesite=lax`, signed with `KB_TOKEN`. Each request re-resolves via `_principal_for_identity → principals.resolve_name` (`kbweb/server.py:32-42`) — already kind-agnostic. Consequences for free: scrypt runs once at login; revoke/expiry is immediate (`_live_where` re-checked per request); rotating `KB_TOKEN` invalidates all sessions.

### 3.7 Lockout mechanics (the mandatory Design-3 graft)

Replace any in-memory idea with the DB-backed `login_throttle`: sliding window of `kb_login_window_seconds`; after `kb_login_max_attempts` failures set `locked_until = now() + kb_login_lockout_seconds`; prune rows older than the window on each write. Keyed on **both** `sha256(lower(identifier))` and `sha256(client_ip)` — a lock on either blocks. Durable across restart and shared across processes (correct even if REST later shares it). `kb principal unlock <name>` clears the identifier row. Cloudflare edge rate-limit on `/login` is an explicit second layer (the deployment already fronts 443 via Cloudflare).

### 3.8 Ops CLI

- `kb principal create <name> --password [--username <email-or-handle>]` → `typer.prompt(hide_input=True, confirmation_prompt=True)`; username defaults to `name` if omitted. No SMTP anywhere.
- `kb principal set-password <name>` → hidden+confirmed prompt; both first-set and reset (the no-SMTP "forgot password" answer — owner resets out-of-band); clears the throttle for that identifier.
- `kb principal unlock <name>` → clears the throttle row.
- `kb principal list` already exists — extend its display to show `[password:<username>]` and a `LOCKED until …` status when throttled.
- Optional nicety (owner call, §6 Q6): a `password_must_change` column + `--must-change` first-login flag — implemented CLI/REST-only, **never** as a kbweb POST route.

### 3.9 Security & acceptance criteria (test checklist)

All tests use `kb_test`, mock Ollama, and a fake token shaped like the real one. `tests/test_password_auth.py`:

1. **Round-trip + CI footgun:** `verify_password(hash_password("pw"), "pw") == (True, _)`; wrong password → `(False, _)`. (Proves the scrypt params run under default `maxmem`.)
2. **Username-enumeration parity:** a login for a **nonexistent** username and a login for an existing username with a **wrong** password both return the byte-identical `401 "Invalid credentials."`, and both perform a scrypt (timing parity via `DUMMY_HASH`). No DB write on the read path beyond the throttle.
3. **Brute-force lockout:** `kb_login_max_attempts` failures → the next attempt is locked even with the *correct* password until `locked_until`; `kb principal unlock` clears it; the lock survives a simulated process restart (row persists).
4. **Per-IP spray bound:** failures across *different* usernames from one IP trip the IP-scoped lock.
5. **Owner unaffected:** the master-token branch (a) and the `kbg_` branch (b) still log in; owner throttling never engages for the master token.
6. **Grantee walling inherited (no-oracle parity):** a password grantee granted only project X sees byte-identical `NotFound` for a withheld project Y — mirror `tests/test_no_oracle.py`. Confirms `resolve_password → _build_grantee → scope.Principal(is_owner=False)` inherits every wall.
7. **Hash upgrade path:** a row stored with weaker params (e.g. n=2¹³) is transparently rehashed to the current target on a successful login (`needs_rehash` → UPDATE); the next login verifies against the upgraded hash.
8. **resolve_name stays kind-agnostic:** a `password` grantee's `g:<name>` cookie re-resolves live; add a regression test so a future `kind` filter in `resolve_name` (`principals.py:280`) can't silently break password sessions.
9. **Read-only invariant intact:** `tests/test_phase6_web.py::test_web_is_read_only` still passes (no new mutating route, no WRITE_NAMES import). Add an assertion that `login_throttle`'s function names are absent from `kbweb.*` `dir()`.
10. **Case-insensitivity:** creating `Alice@x.com` then logging in as `alice@x.com` succeeds; a second create of `ALICE@x.com` is rejected by the partial unique index on `lower(username)`.
11. **Never owner:** a password principal always resolves with `is_owner=False` (assert directly).

**This feature is security-sensitive → it MUST get an adversarial/security review (`/security-review`) before the migration is applied or anything is deployed.**

---

## 4. Ranked enhancement roadmap

Value/effort/risk are from the campaign retrospective, re-checked against code. Sequenced into waves; the auth feature is slotted after the top-risk and time-boxed items.

| # | Item | Value | Effort | Risk if skipped | Wave |
|---|---|---|---|---|---|
| B1 | **Encrypted offsite backups** (PLAN §6d) | High | Low | **HIGHEST** — total, unrecoverable loss of all NDA memory on one disk failure/ransomware/theft. Every backup lives on the same box today. | **1** |
| B2 | **2026-07-28 stateless MCP spec revision prep** | High | Med | Hard external deadline (11 days out). Broker handshake change + lagging fastmcp = public connector breaks with no in-repo warning. | **1** |
| B3 | **Flip OAuth onboarding to CIMD** (`KB_OAUTH_CIMD`) | Med | Low | Onboarding rides deprecated DCR; if claude.ai drops DCR, re-adding the connector fails until CIMD is enabled. Pairs with B2. | **1** |
| A1 | **Immediate cleanup** (§2) | — | Low | Silent under-generation of summaries; un-dismissable review rows; nightly waste; test flakiness. | **0** |
| F1 | **Email/username + password auth** (§3) | Med-High | Med | No browser login without sharing the master token or a `kbg_`. Convenience + reduces master-token spread. | **2** |
| G1 | **Grantee write-audit surface** | Med | Med | Once `rw` grants reach external principals, no way to review what a grantee created/superseded. Writes made before the trail exists can never be attributed. | **2** |
| R1 | **Decay/recency scoring in hybrid search** | Med | Med | Stale items rank equal to fresh at equal fused score (recency is only a tiebreak today). Retrieval-quality, not correctness. | **3** |

### Wave sequencing rationale
- **Wave 0 — cleanup (§2):** land before anything else; the two mediums are active behavioral bugs and the fixes are small.
- **Wave 1 — resilience + external deadline (B1, B2, B3):** B1 is the single highest risk in the whole backlog — do it first; every deferred day is another day with exactly one copy of irreplaceable data. B2 has a dated deadline (2026-07-28); B3 is a near-zero-code flip that shares B2's connector test path.
- **Wave 2 — auth + governance (F1, G1):** the password feature is the owner's requested centrepiece; build the grantee write-audit trail alongside/just before, so attribution exists *before* write access spreads.
- **Wave 3 — retrieval quality (R1):** pure enhancement, gated by the regression sentinel so a bad decay weight trips a flag.

### Backlog item detail (grounding for Waves 1 & 3)

**B1 — Encrypted offsite backups (§6d).** Grounded: `scripts/backup.sh` does only local `pg_dump → zstd → chmod600 → prune` (verified full file read); no `age`/`rclone`/`KB_AGE`/`KB_RCLONE` anywhere in `scripts/`, `src/`, `config.py`, or `Makefile`. Extension point is `backup.sh`; PLAN §6d specifies the flow (cross-disk copy to `/backup/disk2/kb-backups/`, then `age -r $KB_AGE_RECIPIENT | rclone rcat`, so no plaintext leaves the box). `restore_test.sh` already restores into a throwaway `:5599` container and counts within 1% — add an `--offsite` fetch+decrypt branch. New config: `kb_age_recipient`, `kb_rclone_remote`. Owner-only steps: `rclone config`, off-box copy of the age private key. Inv. 12 (age key never beside ciphertext / never in git).

**B2 — Stateless MCP spec prep.** Partial head start exists: `kbmcp/server.py:536` mounts `mcp.http_app(path="/mcp", json_response=True)` (complete-body JSON, the stateless-friendly shape); fastmcp pinned `>=2.0`, repo on 3.4.4. Work: track the fastmcp release implementing the revision, upgrade, re-verify OAuth+bearer end-to-end. Tests to extend: `tests/test_mcp_smoke.py`, `tests/test_connector_batch.py`.

**B3 — CIMD flip.** Fully wired: `config.py:75` `kb_oauth_cimd` (default False) → `kbmcp/server.py:120` `enable_cimd=settings.kb_oauth_cimd`. The in-code comment (`server.py:111-131`) documents DCR is deprecated and claude.ai prefers CIMD; kept off pending a staging re-test (the original failure attributed to metadata missing `"none"` in `token_endpoint_auth_methods_supported` — verify that before flipping). Work is the live A/B: flip true, reconnect, confirm sign-in, keep the kill-switch (`.env` flag).

**G1 — Grantee write-audit surface.** Reads are attributed (`access_log.principal` stamped on reads/gets, `scope.log_write` at `scope.py:282-296` already attributes grantee *writes* into `access_log`). What's missing is a **read-only review surface**: a kbweb `/audit` view (or REST read endpoint) over `access_log` filtered to grantee (`principal IS NOT NULL`) activity, plus a `kbcore.browse`-style read function. Build-on: `access_log` already carries the `principal` column (migration 011:52); kbweb read views are the pattern. Keep it read-only (Invariant 11). Security-sensitive → security review before deploy.

**R1 — Decay/recency scoring.** Today recency is only a stability tiebreak: `search.py:359-360` sorts by `(score, trust=='verified', created_at)` *after* RRF fusion; there is no age/half-life term in the fusion itself. Single extension point: the RRF fusion in `search.py`. Pattern: `kb_graph_expand_hops` (a config-gated search knob, `config.py:95-96`). Gate behind a new `kb_recency_half_life_days` (0 = off, preserving current behavior) and validate against the 25-query golden set via the sentinel — a bad weight trips `search_regression`.

---

## 5. Sub-agent execution guide

Each work package below is a **standalone** Opus-subagent prompt. They reference no prior conversation. **Standing guardrails for every sub-agent (baked into each prompt):**

> Read `CLAUDE.md` and `PLAN.md` (invariants §1) first. Write **code + tests only**. All DB work runs against the `kb_test` schema via the existing `conftest.py` fixtures — **never** touch the live `public` schema. Do **NOT** commit, do **NOT** apply migrations to a live DB, do **NOT** run `docker`/`systemctl`, do **NOT** deploy, do **NOT** read or print `.env` or any secret. The main agent reviews, commits, migrates, and deploys. DB access only through `kbcore` service functions in transactions; parameterized SQL only; config only via `kbcore.config`. Keep files under ~300 lines. Every new behavior lands with at least one real-DB test (mock only Ollama). End by running `ruff check --fix && ruff format && pytest -q` and pasting raw output. Report changed files as absolute paths.

### WP-A — Immediate cleanup (findings §2.1, §2.3, §2.4)

```
Read CLAUDE.md and PLAN.md (§1 invariants) in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; kb_test only; no commit/migrate/docker/systemctl/deploy; never read or print .env; parameterized SQL only; config only via kbcore.config; end with `ruff check --fix && ruff format && pytest -q` and paste raw output.

Fix three cross-batch cleanup items:

1. src/kbcore/consolidation.py:23-30 — last_consolidation_at() must only count NIGHTLY consolidation reports, not deep-search reports. The nightly report carries meta.consolidation=true (src/kbworker/consolidate.py:260); deep-search reports (src/kbcore/deepsearch.py:218-226) do not. Change the query to add `AND meta->>'consolidation' = 'true'`. Add a test: insert a nightly-shaped report (meta.consolidation true) at T0, a deep-search-shaped report (no meta.consolidation) at T1>T0, assert last_consolidation_at() == T0.

2. src/kbcore/eval_harness.py:56 — change get_item(hit.item_id).body to get_item(hit.item_id, with_relations=False).body (it discards relations today; get_item defaults with_relations=True per src/kbcore/items.py:14). Assert eval results are unchanged with the flag.

3. tests/conftest.py:116-123 — the `kb` fixture TRUNCATE list omits deep_search_jobs (migration 010) and consolidation_requests (migration 004). Add both to the TRUNCATE. Then remove the now-redundant manual DELETE workarounds in tests/test_deep_search_async.py and tests/test_consolidate_trigger_surfaces.py. Confirm the full suite passes both in normal and shuffled order.

Do not bundle any unrelated change. Report the absolute paths you changed.
```

### WP-B — Null-item review resolution (finding §2.2)

```
Read CLAUDE.md and PLAN.md (§1 invariants) in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; kb_test only; no commit/migrate/docker/systemctl/deploy; never read or print .env; parameterized SQL only; config only via kbcore.config; end with `ruff check --fix && ruff format && pytest -q` and paste raw output.

Problem: the search-quality sentinel enqueues review rows with item_id NULL via review.queue_review(None, 'search_regression', …) (src/kbworker/eval_sentinel.py:104-117). Every human resolve path funnels through review._queued_item_id() (src/kbcore/review.py:157-172), which raises when item_id IS NULL — so these rows are un-dismissable in kb_review_resolve (src/kbmcp/tools_phase5.py:90-106), the CLI (src/kbcli/review_cmds.py), and REST (src/kbrest/server.py:239-249). The only item-less resolver is review.resolve_review() (review.py:76-83), reachable today only by the sentinel's own auto-recovery.

Add a narrow owner-only 'dismiss' action for ITEM-LESS rows on all three surfaces:
- Add review.dismiss_review(queue_id) in src/kbcore/review.py: look up the row; if it does not exist, NotFound; if it HAS an item_id, raise InvalidOperation steering the caller to promote/reject/supersede; else call the resolved-status flip (reuse resolve_review's UPDATE). One transaction.
- kb_review_resolve(queue_id, action='dismiss') in tools_phase5.py — keep scope.require_owner.
- `kb review dismiss <queue_id>` in src/kbcli/review_cmds.py.
- POST /api/review/{id}/resolve accept action='dismiss' in src/kbrest/server.py (keep scope.require_owner).

Tests (kb_test): enqueue a null-item search_regression row and assert each surface's dismiss resolves it; assert dismiss on an item-bearing row is rejected with a clear message; assert an owner-only guard still holds for a grantee principal.

Report the absolute paths you changed.
```

### WP-C — God-module split (finding §2.5) [optional, low priority]

```
Read CLAUDE.md and PLAN.md in /home/usama/data-server first. Follow the standing sub-agent guardrails. This is a MOVE-ONLY refactor: NO behavioral change, NO test changes beyond import paths.

Four kbcore modules exceed CLAUDE.md's ~300-line guideline: src/kbcore/search.py (373), items.py (340), graph.py (340), ingest.py (327). Carve cohesive helpers into new sibling modules (follow the existing precedent of search_expand.py and principal_ctx.py, which were split "to keep that module lean"). Preserve every public symbol's import path by re-exporting from the original module where needed. Do NOT touch src/kbmcp/server.py in this package.

AC: `ruff check` clean, full `pytest -q` green with zero test-logic changes, and no public import path broken (grep the codebase for imports of the moved symbols and confirm they still resolve). Report the absolute paths you changed and a one-line map of what moved where.
```

### WP-D — Encrypted offsite backups (B1) [security-sensitive → security review before deploy]

```
Read CLAUDE.md, PLAN.md §6d, and PLAN.md §9 in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; NO commit/migrate/docker/systemctl/deploy; never read or print .env or any key; config only via kbcore.config.

Goal: add ENCRYPTED OFFSITE backups on top of the existing local-only backup. Today scripts/backup.sh does only local pg_dump→zstd→chmod600→prune (read it fully first). There is no age/rclone anywhere.

Tasks:
1. Add config in src/kbcore/config.py: kb_age_recipient: str = "" and kb_rclone_remote: str = "" (empty = offsite disabled, both must be set to enable). Document them in .env.example.
2. Extend scripts/backup.sh: after the local dump succeeds, (a) cross-disk copy the dump to /backup/disk2/kb-backups/ (configurable), (b) pipe `age -r "$KB_AGE_RECIPIENT" | rclone rcat "$KB_RCLONE_REMOTE/kb-<date>.sql.zst.age"` so NO plaintext byte leaves the box, (c) prune the remote to 14 daily + 8 weekly mirroring the local logic. Offsite failure must NEVER break or fail the local backup (log and exit 0 on the offsite leg). When kb_age_recipient/kb_rclone_remote are empty, skip the offsite leg entirely (behavior identical to today).
3. Extend scripts/restore_test.sh with an --offsite branch: pull the newest remote object via rclone, decrypt with the local age key (~/.config/kb/age.key), restore into the throwaway :5599 container, assert items/chunks counts within 1%, destroy the container, print PASS/FAIL. Decrypt with a wrong key must FAIL loudly.
4. Guard Invariant 12: the age PRIVATE key must never be written into the repo or beside the ciphertext; assert scripts never echo it. Add a shellcheck-clean guard.

Do NOT run rclone/age/docker or apply anything — only write the scripts and config. Provide a short "owner steps" note (rclone config, age keygen, off-box key copy) as text in your final message, NOT as a committed file. This is security-sensitive; flag that the main agent must run `/security-review` before deploying.

Report the absolute paths you changed.
```

### WP-E — Stateless MCP spec prep + CIMD flip (B2 + B3) [security-sensitive → security review + staging before deploy]

```
Read CLAUDE.md and PLAN.md §10 in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; NO deploy; never read or print .env.

Context: the public claude.ai connector at kb.n3rd-bugs.dev/mcp is live and load-bearing. A dated MCP spec revision lands 2026-07-28. The server already mounts mcp.http_app(path="/mcp", json_response=True) (src/kbmcp/server.py:536, the stateless-friendly complete-body JSON shape) and fastmcp is pinned in pyproject. CIMD is fully wired: config.py:75 kb_oauth_cimd → server.py:120 enable_cimd (default False, DCR-only for historical claude.ai compat; the in-code comment at server.py:111-131 says DCR is deprecated and claude.ai prefers CIMD, and attributes the original CIMD failure to metadata missing "none" in token_endpoint_auth_methods_supported).

Tasks (code/tests only — the LIVE flip and reconnect are the main agent's job on staging):
1. Survey the installed fastmcp version and the pyproject pin; write a short compatibility note (in your final message) on what the 2026-07-28 revision requires and whether the pinned fastmcp implements it. Do NOT upgrade blindly — if an upgrade is needed, propose the exact version bump and the risk, but leave the actual upgrade decision to the owner.
2. Extend tests/test_mcp_smoke.py and tests/test_connector_batch.py to assert the OAuth-metadata document advertises token_endpoint_auth_methods_supported including "none" when kb_oauth_cimd is True (so the documented original CIMD failure cannot recur), and that the bearer 401 path is unchanged.
3. Add a test that toggling settings.kb_oauth_cimd changes only the advertised registration mechanism and does not affect the static-bearer path.

Do NOT flip kb_oauth_cimd in .env, do NOT reconnect any live connector. This is security-sensitive: flag that the main agent must A/B the CIMD flip on a staging host and keep the .env kill-switch. Report absolute paths changed and your compatibility note.
```

### WP-F — Email/username + password authentication (F1) [security-sensitive → mandatory security review before migrate/deploy]

```
Read CLAUDE.md, PLAN.md (§1 invariants + §10 access model), and migrations/011_principals.sql in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; ALL DB work against kb_test via conftest.py fixtures; NEVER touch the live public schema; NO commit/migrate/docker/systemctl/deploy; never read or print .env; parameterized SQL only; config only via kbcore.config; keep files under ~300 lines.

Build web-only email/username + password login as a fourth principal kind. It must resolve to the SAME kbcore.scope.Principal (is_owner=False) as a kbg_ token, so all of kbcore/scope.py's no-oracle walling is inherited (see _build_grantee at src/kbcore/principals.py:227-242 and resolve_name at 280-293, which is already kind-agnostic). Only the /login endpoint gains new code. Password login does NOT authenticate the claude.ai connector or REST — web UI only.

1. migrations/012_password_principals.sql (ADDITIVE): ADD nullable columns username TEXT and password_hash TEXT to principals; CREATE UNIQUE INDEX principals_username_lower_idx ON principals(lower(username)) WHERE username IS NOT NULL; WIDEN ONLY the kind CHECK to include 'password' via a pg_get_constraintdef DO-block lookup that drops the auto-named kind check (excluding the two biconditionals that mention secret_hash/email — leave those UNTOUCHED so a password row with secret_hash NULL + email NULL still satisfies them); CREATE TABLE login_throttle(scope CHECK IN ('identifier','ip'), key_hash, window_start, fail_count, locked_until, updated_at, PRIMARY KEY(scope,key_hash)). Comment that the kind-CHECK widen is the one existing-object change (data-preserving domain widening) needing Invariant-5 sign-off.

2. src/kbcore/passwords.py: hash_password(plain)->str using hashlib.scrypt n=2**14,r=8,p=1, 16-byte secrets salt, dklen=32, stored as PHC-style "scrypt$<n>$<r>$<p>$<b64salt>$<b64dk>"; verify_password(encoded, plain)->(ok, needs_rehash) with hmac.compare_digest and lazy-rehash when params are below target; a module-level DUMMY_HASH for constant-work on miss; enforce settings.kb_password_min_len at hash time. Ship a test that actually runs hash+verify (CI catches the scrypt maxmem footgun — n=2**14 stays under OpenSSL's default cap; do NOT use n=2**15 without an explicit maxmem>=64MiB).

3. src/kbcore/login_throttle.py: record_failure(identifier, client_ip), check_locked(identifier, client_ip)->bool, clear(identifier), prune(). sha256(lower(strip(identifier))) and sha256(client_ip) keys; sliding window kb_login_window_seconds; lock after kb_login_max_attempts for kb_login_lockout_seconds; prune-on-write. One transaction per write.

4. src/kbcore/principals.py: widen create_principal to accept kind='password' with username+password (store hash_password); add set_password(name, plain) (reject non-password kinds like rotate rejects google at 171-172; clear the throttle); add resolve_password(identifier, plain, client_ip)->Principal|None following the exact miss-equalizing/throttle-checking steps: normalize→check_locked (run scrypt vs DUMMY_HASH even when locked)→SELECT ... WHERE lower(username)=%s AND <_live_where()>→no row: verify vs DUMMY_HASH + record_failure + None→row: scrypt-verify, on success clear+lazy-rehash+_build_grantee, on failure record_failure+None. KEEP resolve_name kind-agnostic.

5. src/kbcore/config.py: kb_password_min_len=12, kb_login_max_attempts=5, kb_login_window_seconds=900, kb_login_lockout_seconds=900. Document in .env.example.

6. src/kbweb/server.py POST /login: add branch (c) — after the token and kbg_ branches, elif username AND password present → principals.resolve_password(...); success → _issue_session("/", auth.grantee_identity(name)); any miss → the SAME generic 401 "Invalid credentials." Read client IP from the request (CF-Connecting-IP / left-most X-Forwarded-For behind Cloudflare). Add username+password fields to src/kbweb/templates/login.html. Do NOT add any new route and do NOT import any kbcore write function — Invariant 11 must stay green.

7. src/kbcli/principal_cmds.py: `create --password [--username <email>]` (hidden confirmed prompt), `set-password <name>`, `unlock <name>`. Extend `list` to show [password:<username>] and LOCKED status.

Tests (tests/test_password_auth.py, kb_test): (1) hash/verify round-trip; (2) enumeration parity — nonexistent user and wrong-password both return byte-identical 401 and both run scrypt; (3) brute-force lockout blocks even a correct password until unlock, and survives a simulated restart; (4) per-IP spray lock; (5) master token + kbg_ still log in; (6) no-oracle parity — a password grantee sees byte-identical NotFound for a withheld project (mirror tests/test_no_oracle.py); (7) lazy rehash upgrades a weak-param row on login; (8) resolve_name kind-agnostic regression; (9) test_web_is_read_only still green + login_throttle names absent from kbweb.* dir(); (10) case-insensitive uniqueness; (11) password principal always is_owner=False.

This is SECURITY-SENSITIVE. In your final message, explicitly instruct the main agent to run `/security-review` and apply migration 012 to the live DB ONLY after that review passes. Report absolute paths changed.
```

### WP-G — Grantee write-audit surface (G1) [security-sensitive → security review before deploy]

```
Read CLAUDE.md, PLAN.md §10, and migrations/011_principals.sql in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; kb_test only; NO deploy; never read or print .env; keep kbweb READ-ONLY (Invariant 11 — no mutating routes, no write-function imports; tests/test_phase6_web.py enforces this).

Goal: a READ-ONLY audit surface to review grantee activity. Writes ARE already attributed: kbcore.scope.log_write (src/kbcore/scope.py:282-296) stamps access_log.principal for non-owner writes, and reads stamp it too (search._log_access, items get). access_log.principal is a plain TEXT column (migration 011:52). What's missing is a way to REVIEW it.

Tasks:
1. src/kbcore/browse.py (or extend the existing read module): a scoped read function returning recent access_log rows joined to items (id, title, project, tool, principal, ts), filtered to principal IS NOT NULL (grantee activity), owner-only (scope.require_owner), paginated/limited. Parameterized SQL; no write.
2. A read-only kbweb view (GET /audit) rendering that list with the SEC-3 provenance discipline, plus an equivalent REST GET endpoint (GET /api/audit) behind the bearer. NO POST, NO mutation.
3. Tests (kb_test): a grantee write and a grantee read both appear attributed in the audit read; an owner write does not (owner writes are intentionally unattributed per scope.log_write); the /audit route is GET-only and imports zero write functions (extend the test_phase6_web assertions); require_owner blocks a grantee.

This is security-sensitive (it exposes cross-grantee, cross-project audit data): instruct the main agent to run `/security-review` before deploy. Report absolute paths changed.
```

### WP-H — Decay/recency scoring in hybrid search (R1)

```
Read CLAUDE.md, PLAN.md §5.4 and §8, and src/kbcore/search.py in /home/usama/data-server first. Follow the standing sub-agent guardrails: code + tests only; kb_test only; NO deploy; config only via kbcore.config.

Goal: make recency a RANKING signal, not just a tiebreak. Today search.py:359-360 sorts by (score, trust=='verified', created_at) AFTER RRF fusion — recency only breaks ties. Add an optional age/half-life term INTO the fusion.

Tasks:
1. src/kbcore/config.py: kb_recency_half_life_days: float = 0.0 (0 = OFF, behavior byte-identical to today). Follow the kb_graph_expand_hops precedent (config.py:95-96) for a config-gated search knob.
2. In search.py RRF fusion, when the half-life > 0, multiply or add a recency factor derived from item age and the half-life, clearly documented. Preserve the verified>=unverified ordering and the boost_project bonus. When the knob is 0, produce identical results to today (assert this with a test).
3. Add >=2 golden-style tests: with decay ON, a fresh item outranks a stale item at otherwise-equal fused score; with decay OFF, ordering is unchanged.
4. Run `make eval` logic (eval_harness) with decay OFF and confirm hit@5 is unchanged; note in your final message that the main agent should re-run the 25-query golden eval with decay ON before enabling it live (the nightly sentinel will flag a search_regression if a bad weight degrades retrieval).

Report absolute paths changed.
```

---

## 6. Open questions for the owner

Decisions reserved for the human before/while executing the above:

1. **argon2 vs stdlib scrypt (§3.3).** Recommendation: **stdlib scrypt** (zero new dep, fits the standalone mandate). Confirm — or accept argon2-cffi as a compiled dependency on the host and on every `restore-test`.
2. **May a password user EVER be owner-equivalent?** Recommendation: **never** — password principals are always `is_owner=False`; the owner remains the master `KB_TOKEN` + `KB_WEB_ALLOWED_EMAILS`. Confirm this hard rule.
3. **Lockout thresholds.** Defaults proposed: `kb_login_max_attempts=5`, `kb_login_window_seconds=900`, `kb_login_lockout_seconds=900`. Adjust to taste (too aggressive risks self-lockout with no SMTP reset — `kb principal unlock` is the recovery).
4. **scrypt cost.** n=2¹⁴ (default, no `maxmem` needed) vs n=2¹⁵ with explicit `maxmem≥64 MiB`. Recommendation: n=2¹⁴.
5. **Username identity.** Dedicated `username` column with `lower(username)` unique index (recommended) vs overloading the case-sensitive `name`. Recommendation: dedicated column.
6. **First-login `--must-change` flag (§3.8)?** Optional nicety; if wanted, CLI/REST-only, never a kbweb POST. Default: skip.
7. **REST reach for password users?** Default: **OFF**. If ever needed, a token-exchange `POST /auth/session` (Basic once → mint a `kbg_`), never per-request HTTP Basic on the shared `resolve_bearer` (that would leak Basic into local MCP).
8. **CIMD flip timing (B3)** and acceptance of DCR-deprecation risk — flip on staging first, keep the `.env` kill-switch. Confirm the go/no-go and the staging window relative to 2026-07-28.
9. **Migration 012 kind-CHECK widen sign-off.** It is the one existing-object change (data-preserving, strictly permissive domain widening) and brushes CLAUDE.md hard-rule 5 (additive-only). Approve it explicitly in the commit.
10. **B1 offsite custody:** which `rclone` remote (Drive per PLAN §6d, or other), and confirm the age **private** key is copied off-box and never enters the repo or sits beside the ciphertext (Invariant 12).
11. **Decay half-life (R1):** enable at all, and if so what half-life (days)? Default 0 (off) until the golden eval confirms no regression.
