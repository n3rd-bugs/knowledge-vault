# SHARING.md — per-project sharing (operator guide)

How to give another person (or a script) access to *specific projects* in the
Knowledge Vault without handing over the master token. Introduced by migration
`011_principals.sql`; managed with the `kb principal` CLI on the host.

Hostnames below are placeholders — substitute your own deployment:

| Surface | Placeholder URL |
|---|---|
| Remote MCP (claude.ai connector) | `https://kb.example.com/mcp` |
| REST API | `https://api.example.com/api` |
| Read-only web UI | `https://web.example.com` |

## 1. The model in one minute

- **Owner** — not a database row. The master `KB_TOKEN` and any email on
  `KB_WEB_ALLOWED_EMAILS` resolve to the implicit owner principal with
  unrestricted read/write over everything. Nothing about the owner changes when
  you add grantees; zero grantees behaves exactly like the pre-sharing system.
- **Grantee** — a row in `principals` plus explicit rows in `principal_grants`.
  A grantee sees *only* the projects granted to it, everywhere (MCP tools, REST,
  web UI). Requests touching a non-granted project get the same `NotFound` a
  truly nonexistent target would — a grantee cannot even detect that other
  projects exist (the "no-oracle" wall in `kbcore.scope`).
- **Read-only by default.** A grant confers read access. A per-project `--rw`
  flag exists for the rare case a grantee should write to that one project
  (write implies read); leave it off unless you have a concrete reason. The web
  UI is read-only for everyone, owner included, regardless of grants.

## 2. Two credential kinds — which one, for what

Every grantee is one of two kinds (`principals.kind`):

| | `kind='google'` (email identity) | `kind='token'` (`kbg_` bearer) |
|---|---|---|
| What it is | A verified Google email bound to the grantee | A `kbg_`-prefixed secret (256-bit; only its SHA-256 is stored) |
| claude.ai remote connector | **Yes** — signs in via Google OAuth during the connector handshake | Not the normal path (the connector speaks OAuth or a static bearer; per-grantee tokens are meant for direct clients) |
| Web UI (`web.example.com`) | **Yes** — "Sign in with Google" | Yes — paste the `kbg_` token into the login form |
| REST API / local MCP / scripts | No (there is no bearer to send) | **Yes** — `Authorization: Bearer kbg_...` |

Rule of thumb: a **human** who will use claude.ai and/or the browser gets a
`google` grantee for their Gmail address. A **script, CI job, or another
machine's MCP client** gets a `token` grantee. One person can hold both (two
principals, same grants).

Honest note on the connector: claude.ai remote connectors authenticate via
OAuth (here: Google) or a static bearer — there is no password form in the
handshake. So the practical way to give a person connector access is a
`google` grantee for their email.

## 3. Sharing a project with a new person (owner steps)

Run these on the host, as the owner (the CLI uses the owner's `.env`).

### 3a. Human collaborator (Google identity)

```bash
# 1. Create the grantee bound to their Google email
kb principal create alice --email alice@gmail.com --note "Alice, acme collab"

# 2. Grant the project(s) — read-only unless you add --rw
kb principal grant alice acme-webapp
kb principal grant alice acme-infra

# 3. Verify (never prints secrets)
kb principal list
```

If your Google OAuth app is still in **Testing** status, also add
`alice@gmail.com` as a test user in the Google Cloud console — see §6.

### 3b. Script / machine (bearer token)

```bash
# --token is the default when no --email is given; shown here for clarity.
# Optional: --expires for a time-boxed credential.
kb principal create ci-reporter --token --expires 2026-12-31 --note "read-only CI"
kb principal grant ci-reporter acme-webapp
```

The command prints the `kbg_` token **once**. Only its hash is stored — copy it
now; it is not recoverable (use `kb principal rotate` if lost).

### 3c. Web admin

An **owner-only** web admin at `https://web.example.com/admin` performs the
same grant/revoke operations point-and-click: it lists every principal and
their project grants, and offers forms to grant a project (read-only) or revoke
one. It is visible and reachable **only** to the owner — a signed-in grantee
gets a plain 404 (byte-identical to any unknown path, so the page's existence is
never revealed), and the mutating actions are CSRF-protected. It is the sole
mutating surface on the otherwise read-only web UI (Invariant 11 carve-out,
scoped strictly to owner-only share authz). The `kb principal` CLI remains the
full-power surface (create/rotate/expire live only there).

## 4. What the new person does

### Google grantee

- **claude.ai connector:** Settings → Connectors → *Add custom connector*,
  URL `https://kb.example.com/mcp`, and complete the Google sign-in with the
  granted email. The KB tools then appear scoped to their projects.
- **Web UI:** open `https://web.example.com`, click *Sign in with Google*,
  choose the granted email.

Signing in with a Google account that has no grants authenticates but resolves
to an empty-grant identity: no data is visible and nothing leaks.

### Token grantee

- **REST:** send the token as a bearer header:

  ```bash
  curl -H "Authorization: Bearer kbg_XXXX..." \
       "https://api.example.com/api/search?q=deploy"
  ```

- **Local MCP / Claude Code:** configure the KB MCP server with the `kbg_`
  token in place of the master token (same `Authorization: Bearer` header).
- **Web UI (fallback):** the login form also accepts a pasted `kbg_` token.

## 5. Revocation, rotation, un-granting

```bash
kb principal revoke alice          # kill the credential entirely
kb principal rotate ci-reporter    # new kbg_ secret; old one dies immediately
kb principal ungrant alice acme-infra   # remove one project, keep the rest
```

- **`revoke`** flips `revoked_at` (never a row delete). The credential stops
  authenticating on the next request — **immediately** for web sessions and
  Google OAuth (both are re-resolved against the DB per request; a still-valid
  cookie dies at once), and **within the token cache TTL** for `kbg_` bearers
  (`KB_PRINCIPAL_CACHE_TTL`, default 60 s — only successful resolutions are
  cached). Revocation also flushes the cache in the process that ran it.
- **`rotate`** mints a new secret for a `token` grantee and prints it once; the
  old hash is overwritten so the old token dies immediately. Grants, name, and
  audit history are preserved (same row). Rejected for `google` grantees
  (nothing to rotate) and for revoked ones (create a new principal instead).
- **`ungrant`** removes a single project grant; the de-scoped project stops
  being readable on the next request (grant changes flush the token cache
  too). Re-running `grant` is an idempotent upsert — it updates the `--rw`
  flag rather than duplicating.
- **`--expires`** on `create` sets a `TIMESTAMPTZ` after which the credential
  simply stops authenticating — good for time-boxed access with no follow-up.

## 6. Security notes

- **Read-only web UI.** The web app imports zero write functions; no grant can
  make the browser a write surface.
- **No-oracle guarantee.** A grantee probing a withheld project gets responses
  byte-identical to a nonexistent one — no revoked-vs-unknown, no
  granted-vs-hidden distinction, on any surface. Unknown and revoked bearer
  tokens get the same 401.
- **Master key stays with the owner.** Sharing never involves handing out
  `KB_TOKEN`. Secrets are never stored (token hash only) and never printed by
  `kb principal list`; the plaintext appears exactly once, at create/rotate.
- **Audit.** Grantee reads are attributed by name in `access_log.principal`
  (NULL = owner/legacy).
- **One manual step that is NOT code:** the Google OAuth app must be
  **published out of Testing status** in the Google Cloud console. While the
  app is in Testing, (a) every grantee's Google account must be manually added
  to the test-user list before sign-in works, and (b) Google expires
  refresh tokens for Testing-status apps after about 7 days — so claude.ai
  connector sessions for added users silently die weekly until the app is
  published to Production.
- **Kill switch.** Stopping the tunnel container removes all public exposure
  at once, grantees included, with zero data change.

## 7. Quick reference

```
kb principal create <name> [--email <addr> | --token] [--expires <iso>] [--note <text>]
kb principal grant   <name> <project> [--rw]
kb principal ungrant <name> <project>
kb principal revoke  <name>
kb principal rotate  <name>
kb principal list
```
