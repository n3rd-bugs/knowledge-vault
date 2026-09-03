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
  (write implies read); leave it off unless you have a concrete reason.
- **Capability roles gate FEATURES; grants gate ROWS (they AND).** Each grantee
  has a `role` (`guest` < `reader` < `contributor` < `reviewer`) whose capability
  bundle decides which features it may use at all — search, browse, **save**,
  **review-resolve** — while grants keep deciding which projects it may touch.
  Default `reader` is today's read-only surface. `contributor` adds web/MCP
  **save** (ANDed with an `--rw` grant); `reviewer` adds listing and
  reject/dismiss of the review queue. Set with `kb principal create --role ...`
  or `kb principal role <name> <role>` (role change is CLI-only). The web UI is
  read-only for `guest`/`reader`; contributors/reviewers get the confined write
  surface below — but **trust escalation (promote) is always owner-only**.

### Web contributions (A11-3) and the pending-review lifecycle

A `contributor`/`reviewer` (or the owner) can write from the browser, but a
non-owner web write is **capability-gated, project-scoped, forced
`trust='unverified'`, and quarantined from retrieval until you promote it**:

- **Contribute** (`/save`, and the supersede/link forms on `/item/{id}`) — a save
  lands the item `status='pending'` + `unverified` with a `web_contribution`
  review row, in one transaction. It is **absent from every `kb_search`** (owner
  and grantee) until you promote — so a web contribution can never inject
  content into an agent session before you have seen it. A web *supersede* is a
  PROPOSAL: the original stays active until you promote the replacement.
- **Review** (`/review`, reviewer+) — reject archives an unverified item; dismiss
  closes the row untouched. Both need an `--rw` grant on the item's project.
  There is **no web promote** — promoting a contribution to `verified`/`active`
  stays CLI/MCP-only (`kb review promote <qid>`, owner). A reviewer's queue shows
  only its granted rows, with cross-project titles/ids scrubbed from the detail.

The web can never mint verified knowledge nor de-quarantine one; a stolen grantee
cookie can at most *propose* content that waits for your review.

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

Prefer point-and-click? The web admin (§3c) can create the same `google`
grantee — with its initial read-only shares and a ready-made invite email —
without touching the CLI.

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

An **owner-only** web admin at `https://web.example.com/admin` covers the
human-collaborator flow point-and-click. It lists every principal and their
project grants, and offers forms to grant a project (read-only) or revoke one.
Since the 2026-07-19 extension (Invariant 11 carve-out, Amendment A11-2) it can
also **create a `google` grantee**: one form takes the name (slug), their
Google email, an optional note, and a multi-select of projects — the principal
and its read-only initial shares land in a single submit (inputs are validated
server-side; unknown project values are silently skipped).

Each live google grantee then gets an **invite block** on its card: a
`mailto:` link plus copyable plain text with a personalized getting-started
guide (their project list, the connector URL, the web URL, what they can and
can't do, and that access is revocable). The server sends **no** email — the
link opens the owner's own mail client — and the text contains no secret by
construction (a google grantee has none). Grantees themselves get a read-only
`/welcome` page with the same steps, scoped to exactly their own grants.

Every grant made here is `rw=false`. **Token principals and principal
revoke / rotate / expiry remain CLI-only** — no secret ever transits a
browser. The admin surface is visible and reachable **only** to the owner — a
signed-in grantee gets a plain 404 (byte-identical to any unknown path, so the
page's existence is never revealed), and the mutating actions are
CSRF-protected. It is the sole mutating surface on the otherwise read-only web
UI (Invariant 11 carve-out, scoped strictly to owner-only share admin). The
`kb principal` CLI remains the full-power surface.

## 4. What the new person does

### Google grantee

- **claude.ai connector:** Settings → Connectors → *Add custom connector*,
  URL `https://kb.example.com/mcp`, and complete the Google sign-in with the
  granted email. The KB tools then appear scoped to their projects.
- **Web UI:** open `https://web.example.com`, click *Sign in with Google*,
  choose the granted email. `https://web.example.com/welcome` shows a
  personalized getting-started page (their identity + granted projects).

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

- **Confined web writes (A11-3).** The web's only knowledge-write module is
  `kbweb.views_write`, importing exactly five composed kbcore chokepoints
  (save-for-review, supersede-for-review, link, reject, dismiss) — never raw
  ingest/supersede, never promote (a mechanical allowlist test enforces this).
  A non-owner web write is forced `unverified` + quarantined until you promote,
  so the browser can never mint verified knowledge nor reach an agent's search
  before review. `guest`/`reader` grantees remain fully read-only.
## 2.4 Per-project capture tokens

A machine that works across several projects should carry one **narrow,
revocable** credential per project rather than one broad credential for all of
them. Ask the KB for one:

```
kb_project_token(project="myproj", machine="laptop")
  -> KB_TOKEN_MYPROJ=kbg_…      # add to ~/.claude/kb-tokens.env (chmod 600)
```

| Property | Behaviour |
| --- | --- |
| Scope | `contributor` role + exactly ONE read-write grant. It can search, browse and save in that project and **nothing anywhere else** |
| Naming | `<project>@<machine>`, so `access_log` names the machine and revocation is per machine |
| Revoke | `kb principal revoke myproj@laptop` — a timestamp flip; the next request stops |
| Re-issue | Issuing again for the same (project, machine) **revokes the previous token**, so a leaked one dies when you replace it |
| New projects | An owner may take a token for a project that does not exist yet — that is how you start one |
| Never an escalation | You can only get a token for a project you can already write; it carries access, it never grants it |
| Expiry | Opt-in (`expires_days`). Default none: a capture credential that expires unannounced is a silent outage — revocation is the answer, and it is immediate |

`kb_project_tokens()` lists live tokens (never secrets), including which have
expired — so a machine that stopped capturing can be diagnosed rather than
guessed at.

**The hook picks the right one automatically.** It already knows the project
(the slugified cwd), so it selects `KB_TOKEN_<PROJECT>` from
`~/.claude/kb-tokens.env`. If there is no token for that project it does not
fail obscurely — it writes a `NEEDS TOKEN` line naming the variable to add and
the exact call to make, and the next session's pre-brief puts it in front of
you. A machine with only the shared `KB_TOKEN` keeps working unchanged: that
stays the fallback.

## 2.5 Enrolment codes

A machine sets itself up by redeeming a **one-time code** (`kbe_…`) rather than
by having a bearer pasted into it:

```bash
export KB_ENROL_CODE=kbe_…            # from kb_setup; single use, ~15 min
export KB_REST_URL=https://api.<host>
mkdir -p ~/.claude/kb-setup && cd ~/.claude/kb-setup
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh \
         kb-mcp-call.sh notify-complete.sh turn-start.sh \
         kb-hook-dispatch.sh kb-flush-backfill.sh update.sh; do
  curl -4 -fsS -H "Authorization: Bearer $KB_ENROL_CODE" "$KB_REST_URL/api/setup/$f" -o "$f"
done
bash install.sh
```

Fetch **every served asset, not just the four hook scripts**: `install.sh`
installs `kb-hook-dispatch.sh` unconditionally (it is the only script registered
with Claude Code), so a four-file fetch dies at the installer. The authoritative
list is `GET /api/setup/manifest.json` (a content-derived version plus a sha256
per file); a mechanical test keeps this block equal to the served set. Once a
box is installed, `~/.claude/hooks/update.sh` refreshes everything from that
manifest with one command and re-runs the installer — the pre-brief's
`[KB setup] ... OUT OF DATE` banner points at it.

Why a code rather than the credential: `kb_setup` answers into a chat
transcript, and the session hooks flush transcripts **into the KB**, which is
then backed up off-site. A live bearer sent that way is durable in three places
at once. A code is single-use and short-lived, so after redemption the
transcript holds a dead string.

| Property | Behaviour |
| --- | --- |
| Authority | Never more than the identity that asked. Owner → a per-project token for the project the code names; grantee → a fresh per-machine `kbg_` with exactly their grants; ungranted identity → no code at all |
| Lifetime | ~15 min (clamped to 60), max 5 unredeemed per identity |
| Single use | One atomic `UPDATE … WHERE redeemed_at IS NULL`; two racing redemptions cannot both win |
| At rest | SHA-256 only (`enrolment_codes.code_hash`); plaintext shown once |
| Refusals | Unknown / malformed / expired / already-used are **byte-identical** — no oracle |
| Authentication | A `kbe_` code authenticates as an identity with **no projects and no capabilities**, so `POST /api/enrol` needs no auth exemption and every other route sees a caller that can read and write nothing |
| Revocation | Expire the row, or let the TTL lapse; the nightly pass sweeps dead rows |

A code is a credential and is redacted on ingest like any other (`kbe_`/`kbg_`
both match the scrubber).

- **No-oracle guarantee.** A grantee probing a withheld project gets responses
  byte-identical to a nonexistent one — no revoked-vs-unknown, no
  granted-vs-hidden distinction, on any surface. Unknown and revoked bearer
  tokens get the same 401.
- **Master key stays with the owner — for SHARING.** No act of sharing hands out
  `KB_TOKEN`: a grantee always gets their own `kbg_` token. Secrets are never
  stored (token hash only) and never printed by `kb principal list`; the
  plaintext appears exactly once, at create/rotate.

  **This now holds for enrolment too.** An owner's enrolment code briefly did
  hand over `KB_TOKEN`, because a fixed-grant credential cannot write a project
  that does not exist yet. [Per-project tokens](#24-per-project-capture-tokens)
  removed that reason: an owner code names the project the machine will capture
  and redeems to a narrow, revocable token for exactly it, and the machine
  requests more with `kb_project_token` as it starts working elsewhere. No
  enrolled machine holds owner authority, and any one of them can be revoked on
  its own.
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
