---
name: kb-session-flush
description: Install Claude Code hooks that connect sessions to the Knowledge Vault (KB) - flush each session's transcript to the KB before every compaction, and pre-brief each NEW session with relevant KB knowledge at start. Use when the user wants to auto-save Claude Code sessions to the KB, get KB context injected on session start, or add these hooks to another machine.
---

# Install the KB session hooks (flush + pre-brief)

This skill installs three hook behaviors, plus optional completion notifications
on `Stop` (inert until `~/.claude/notify.env` names a channel):

1. **Flush** (`PreCompact` + `SessionEnd`): the session's new transcript delta is
   flushed into the Knowledge Vault before every compaction. Never blocks
   compaction (detaches immediately), never uses the GPU at capture time — the
   delta is enqueued and the nightly `consolidate` pass runs the 14B grounded
   extraction, landing unverified items in the review queue (§7.2).
2. **Pre-brief** (`SessionStart`): when a NEW session starts (startup or /clear —
   resume/compact are skipped), a quick KB search seeded by the repo's recent
   commit subjects is INJECTED into Claude's starting context, so it begins the
   activity aware of prior decisions and gotchas. `KB_BRIEF_ON_RESUME=1` briefs
   resumes too.
3. **Per-prompt recall** (`UserPromptSubmit`): on every substantive prompt, a KB
   search seeded by THE PROMPT ITSELF is injected before Claude answers — so each
   question is grounded in prior knowledge, not just the session opener. Noise
   controls: slash commands and <12-char prompts skip; a CONTENT-WORD GATE skips
   prompts made only of acknowledgements/stopwords/generic imperatives ("Yes
   please review and update") — hybrid search would return k-best noise for
   those; and a repeat of the same query within 10 min IN THE SAME SESSION is
   suppressed (its hits are already in context). The suppression gate is keyed
   per session (`session_id`) + project + query, in per-session state files, so a
   different session, a different project, or `/clear` (a fresh session id) never
   falsely suppresses recall. Disable with `KB_PROMPT_RECALL=0`.
   CONNECTOR mode is skipped here by default (a headless `claude -p` per prompt
   costs tokens + latency) — opt in with `KB_PROMPT_RECALL_CONNECTOR=1`. Both
   pre-brief and recall are the same script (`kb-session-brief.sh`), branching on
   the hook event.

All hooks fail silently — recall/brief never break a prompt or session.

**One hook per event.** `install.sh` registers exactly ONE command with Claude
Code for each of the five events — `kb-hook-dispatch.sh <event>` — and that
script reads the event JSON once and pipes the same bytes to the scripts the
event needs (`UserPromptSubmit` → `turn-start.sh`, then `kb-session-brief.sh`).
Claude Code on Windows has an open upstream bug (GH #77078 and siblings): with
more than one hook on a single event, only one of the spawned processes receives
stdin and the others read empty — so recall ran with no prompt and captures died
in silence. Registering one dispatcher is the documented workaround. The
dispatcher always exits 0, runs its children sequentially, passes their stdout
through untouched (the brief's stdout IS the injected context), and logs one
`hook event arrived EMPTY: event=…` line to `flush-session.log` if it is ever
handed an empty event anyway, so residual hits stay countable.

Four modes, auto-detected by `install.sh` (in priority order):
- **LOCAL** (the KB host): the hook runs `kb ingest-session <transcript> --queue`
  (checkpointed, GPU-free enqueue). Config: `KB_DIR` in `~/.claude/kb-flush.env`.
- **MCP** (any machine already connected via `claude mcp`): **zero config** — the
  installer auto-discovers the KB server's URL + bearer from `~/.claude.json` and
  the hooks call `kb_ingest_session` / `kb_search` over that same MCP endpoint (a
  tiny `kb-mcp-call.sh` helper speaks the streamable-HTTP protocol). Config keys
  `KB_MCP_URL` + `KB_TOKEN` are written automatically.
- **CONNECTOR** (no bearer anywhere, but Claude Code is logged into a claude.ai
  account whose KB connector is bridged in): **zero config** — the hooks relay
  through headless `claude -p` calls using the cloud-held connector auth
  (`KB_CONNECTOR=1` + `CLAUDE_BIN`, written automatically). The model is the
  data path, so chunks are small (~24 KB) and capped per firing
  (`KB_CONNECTOR_MAX`, default 240 KB); each flush/brief spends Claude usage.
  A recursion guard (`KB_HOOK_GUARD`) keeps the child `claude` run's own hooks
  inert.
- **REMOTE** (none of the above): the hook POSTs the delta to
  `POST /api/ingest-session` over HTTPS with a bearer token, tracking a per-session
  byte offset so each compaction submits only the new window. Config:
  `KB_REST_URL` + `KB_TOKEN` in `~/.claude/kb-flush.env`.

## Requirements (NO sudo, ever)

`bash`, `curl`, and **one JSON tool**. The scripts resolve it through a fallback
chain, in this order (pinned by `tests/hookrig/cases/20`, `22`, `26`):

1. `jq` on `PATH`
2. a `jq` **co-located with the hooks** — `~/.claude/hooks/jq` (`jq.exe` on
   Windows); the installer copies whichever jq it used to there, so the box keeps
   working if `PATH` later loses it
3. `KB_JQ=/path/to/jq` in the environment
4. **`python3`** (stdlib `json` only) — a full fallback, not a degraded one:
   every jq program in the skill, including the `settings.json` merge, has a
   python twin that produces byte-identical output

Nothing needs installing as root: jq is a single static binary, so dropping it in
`~/.local/bin` (or beside the hooks, or anywhere `KB_JQ` points) is enough. If
NEITHER jq nor python3 exists, `install.sh` stops with those instructions instead
of a package-manager command, and a hook fired in that state writes a
rate-limited `NOT CONFIGURED` line to `~/.claude/hooks/flush-session.log` — which
the next session's pre-brief surfaces. A hook never dies silently.

## Steps

1. Run the installer (it writes `~/.claude/hooks/flush-session-to-kb.sh`, creates
   `~/.claude/kb-flush.env`, and registers the hooks in `~/.claude/settings.json`):

   ```bash
   bash "<this skill dir>/install.sh"
   ```

2. Read the printed `mode:` line.
   - If it says **REMOTE**, the KB isn't local. Ask the user for the KB REST base
     URL (default `https://api.example.com`) and a bearer token, then write
     them into `~/.claude/kb-flush.env` (`chmod 600`), e.g.:
     ```
     KB_REST_URL=https://api.example.com
     KB_TOKEN=<the KB bearer token>
     ```
   - If **LOCAL**, nothing more to configure.

3. Run the smoke test the installer prints (feeds a fake `PreCompact` event to the
   hook) and confirm `~/.claude/hooks/flush-session.log` shows the marker for the
   installed mode — `local <proj> ok bytes=…` (LOCAL), `flush <proj> ok bytes=…`
   (MCP **and** REMOTE — they share one branch), or `connector <proj> ok bytes=…`
   (CONNECTOR). `ok` means fully drained; `partial` means the rest follows on the
   next firing. (No hook writes `http=`.) Tell the user it's live: every
   compaction and session-end now flushes to the KB.

## Porting to another machine

Copy this whole skill directory to `~/.claude/skills/kb-session-flush/` on the
other machine and invoke it there (or run `install.sh` directly). On a non-KB
host it lands in REMOTE mode — just fill in `KB_REST_URL` + `KB_TOKEN`.

## Notes / limits (field-verified — KB items 495-498)

- The hook returns instantly; extraction is deferred to the nightly drain.
- **Chunking**: deltas are cut on an ABSOLUTE lattice — cell edges every
  `KB_FLUSH_CHUNK` (700 KB) bytes from byte 0, each snapped back to a line end —
  so a from-0 pass and a delta pass produce the same chunks and the server can
  dedupe a resend by content; no chunk exceeds `KB_FLUSH_SEND_MAX` (950 KB;
  server row cap is 1 MB); up to 1 GB per session. Chunks travel gzip+base64 in `transcript_gz`
  (~10x less wire). Do NOT use HTTP `Content-Encoding: gzip` — the origin does not
  decode it (KB item 497).
- **Edge limits** (when the KB is behind a CDN/proxy): keep requests small — a
  single huge request can hit the edge's ~100 s origin window (524). The chunker
  already guarantees this. A transient client status `000` whose body still
  carries a `queue_id` WAS accepted — the hooks treat it as success (item 497).
- **WAF caveat** (item 498): a managed WAF/OWASP ruleset in front of the KB API
  can block PLAINTEXT bodies containing shell/code snippets with an opaque HTML
  block page. The gzip path is immune (base64 body). For kb_save/plaintext
  writes, add a WAF skip rule for the bearer-authed API routes.
- **Windows / Git Bash** (item 496) — handled automatically by the installer and
  hooks, and simulated end to end by `tests/hookrig/cases/23` + `25`:
  `nohup` detach fallback (no `setsid`), hooks registered as `bash.exe` + args
  (Claude Code on Windows spawns exe+args, not a shell), jq co-located next to
  the hooks + PATH-prepended, and native `C:\...` event paths converted before
  use. Every one of those decisions lives in ONE shared block (`kb_is_windows`,
  `kb_winpath`, `kb_nativepath`, `kb_detach_cmd`, `kb_timeout`, the JSON
  backend) that is byte-identical in all four scripts. **cygpath is not
  required**: `kb_winpath` falls back to the pure-shell mapping
  (`C:\Users\First Last\p` -> `/c/Users/First Last/p`) and `kb_nativepath` falls
  back to `$EXEPATH`, then to a bare `bash.exe` resolved through the Windows
  PATH. Paths with SPACES are the norm there, so every path expansion in the
  four scripts is quoted (`tests/hookrig/cases/24` drives install + flush +
  recall through a `HOME` and a project dir that both contain spaces).
  **Wave C.1 (KB 40411, cases 40-44):** Claude Code spawns the registered
  `bash.exe` as a NON-login shell on the bare Windows PATH, so the dispatcher
  and the shared block put `/usr/bin:/mingw64/bin` back first, gated on bash's
  own `$OSTYPE` (`msys*`/`cygwin*`) — `uname` is itself one of the missing
  binaries, so it cannot be the guard; `kb_is_windows` reads `$OSTYPE` first.
  The `kb_winpath` fallback is pure bash (a native `tr` had its lone `/`
  argument rewritten by MSYS, garbling every transcript path). The installer
  registers Git's LAUNCHER `<git root>\bin\bash.exe` when it exists (it sets
  MSYSTEM and PATH before bash starts), else today's value. A named-but-missing
  transcript logs one hourly `BUG: transcript not found` line instead of exiting
  in silence — except the SessionEnd of a prompt-less session (KB 51863): Claude
  Code writes `~/.claude/projects/<slug>/<sid>.jsonl` at the first recorded
  message, so a SessionEnd naming exactly that file, for a session no firing ever
  got past the file test (no `state/<sid>.*`, no recall stamp), logs one neutral
  `flush skip:` line (the VS Code extension's config-cache probe and
  opened-and-closed panels end this way, on every OS). The pre-brief drops skip
  lines before its 40-line failure window. A Stop or PreCompact, another root
  (`CLAUDE_CONFIG_DIR` — the backfill sweep never scans it), a non-canonical path, or a
  session seen before is still a BUG (`tests/hookrig/cases/48`); a CRLF-written
  pin is read CR-stripped; a
  rate-limited NEEDS TOKEN refusal is labelled `refused`; and the `[KB sweep]`
  line names each stalled session with its remedy (verdict `refused` + a
  `.notoken` stamp only).
- **A session resumed from another directory** (KB 49064) leaves TWO transcripts
  with ONE session id — the frozen copy in the old project dir and the live one
  in the new — sharing one checkpoint. A transcript behind that checkpoint while
  another copy of its session reaches it is the stale copy, never a shrink: the
  hook skips it (nothing sent, offset untouched, one `stale copy:` line a day),
  the sweep lists it as `stale-copy` and never fires it, and `--seed` seeds from
  the largest copy. That silent skip covers only a copy that is a pure
  byte-PREFIX of the session (its tail matches the covering copy). A copy
  behind the checkpoint whose tail DIFFERS (`diverged`), or one frozen behind it
  with no newer copy on the box (`behind`: last modified before the checkpoint's
  last write, or already named by a marker), is REFUSED — nothing sent, no
  restart — and surfaced: marker `state/<okey>.diverged` / `state/<okey>.behind`,
  the notification's `kb ?/<size> diverged|behind`, the sweep's `diverged=` /
  `behind=` fields and table states, and the session brief. A path a marker
  names stays refused at ANY size, even once it grows past the checkpoint
  (that checkpoint is another copy's: a drain from it would start mid-record).
  The owner decides: `kb-flush-backfill.sh --seed <sid> …` settles a `behind`
  copy (once it was resumed, at the marker's `frozen=` size, which its line
  and the table name — never `current`), or a `diverged` one that no copy
  reaches (`current` there ACCEPTS the loss of its unsent records). A copy that
  another copy still covers is settled only by moving it out of
  `~/.claude/projects`: a `--seed` is undone by the next sweep or firing.
  Resuming the session from that directory later is safe: the file Claude Code
  writes there carries the live copy's prefix, and the `diverged` marker naming
  that path releases itself (a content check of the 4 KB below the checkpoint,
  one `marker cleared: … which now carries the checkpoint holder's prefix`
  line), so the new records drain from the checkpoint as usual. The markers
  clear when the copy is gone, when that release applies, on `--seed`, or with
  the offset. Only a transcript modified AFTER the checkpoint's last write, with no
  copy reaching the checkpoint and no marker naming it, is a genuine rewrite and
  still restarts from 0 (KB 49064).
- **macOS / BSD / Windows** — the brief+recall hook wraps every bounded search in a
  portable `kb_timeout`: it uses GNU `timeout` when present (Linux — unchanged),
  else `gtimeout` (Homebrew coreutils), else a built-in perl watchdog (perl ships
  on macOS and Git Bash) that forwards the child's stdout + exit code. Stock macOS
  has no `timeout` binary, so without this the pre-brief and per-prompt recall were
  a silent no-op there. A resolved `timeout`/`gtimeout` is TRUSTED only after a
  behaviour check (a coreutils `--version`, or a `<bin> 1 true` self-test with stdin
  redirected): on Git Bash `command -v timeout` finds
  `C:\Windows\System32\timeout.exe` — a wait utility that cannot run commands and
  rejects redirected stdin — so the bare name is not enough; it is rejected and the
  perl watchdog is used instead. If the perl `fork()` itself fails (heavy
  fork/memory pressure) `kb_timeout` now bails with an error rather than running the
  command unbounded, and the caller degrades to no recall for that firing.
- **Reachability probe / no prompt stalls** — because `UserPromptSubmit` recall runs
  synchronously (40 s hook budget; the brief child gets 34 s), a cheap reachability check runs first: a
  `curl --max-time 3` connect to the KB (MCP/REMOTE) or a trivial local check
  (LOCAL). A dead/blackholed KB skips recall in ~3 s instead of stalling the prompt
  for the whole budget. The REMOTE probe carries the bearer (the same stdin
  config as the search), so kb-rest answers it past its bearer gate instead of
  logging a `401 unauthorized ... GET /` WARNING and counting it toward the
  401-burst alert; a `SessionStart` whose authenticated manifest fetch already
  answered skips it (rig case 49). Carrying the bearer, the probe's answer now
  waits on the gate's credential lookup (a `kbg_` token is one indexed SELECT,
  cached 60 s), so a stalled KB database reads as `unreachable rc=28` — the rc
  tells that timeout apart from a refused connect (`rc=7`). The MCP probe stays
  bare on purpose: a session-less request past the gate opens a stateful MCP
  session that never expires (mcp 1.28 / fastmcp 3.4 set no session idle timeout).
  All inner search timeouts are kept strictly under the brief child's 34 s
  budget (recall: the 3 s probe + a 30 s search, connector recall ≤ 30 s; the
  SessionStart brief search ≤ 20 s, connector brief ≤ 25 s). **No hook curl carries
  `--connect-timeout`** (KB 1195: on Git Bash/Schannel it aborts healthy slow
  requests) — every bound is a `--max-time`; the ingest POST additionally carries
  `--retry`/`--retry-max-time` and never `--fail` (a 401/403 must not be retried).
- **IPv4 pinned** — every KB curl runs over IPv4: the shared config emitters
  (`kb_curl_auth_config` in the hooks, `setup_auth_config` and the enrolment
  configs in `install.sh`) put `ipv4` in the config they feed `curl -K -`, the
  reachability probes carry `-4`, and the ntfy push has pinned `-4` all along.
  On networks where the AAAA record resolves first but IPv6 egress fails,
  unpinned curls died silently (flush/brief/enrol) while notifications kept
  working — the pin lives in the config layer so every consumer inherits it.
- **Partial-line safety** — a flush window that ends in an unterminated JSONL line
  is trimmed to the last complete line and the remainder is HELD for the next
  firing or the sweep (never advanced past mid-line, so no straddled message is
  lost) — on every event, `SessionEnd` included: a half-written last record is
  a session Claude Code is still writing, and the sweep picks it up once the
  file has been quiet.
- **Bounded state** — `flush-session.log` is truncated to its last 500 lines
  once it exceeds 1 MB; per-session `recall-*` state files older than 7 days are
  pruned; an `.offset` checkpoint is retired ONLY by the hook's own state sweep,
  once a day, when its transcript no longer exists on the box (see the
  lifecycle below — never on an event, never on age alone). All housekeeping is
  confined to the hooks' own `~/.claude/hooks` state/log dirs.
- **Never-silent failures**: the flush hook refuses to POST an empty/invalid
  body (logs `BUG: request body build failed`), and the SessionStart pre-brief
  surfaces any recent flush failure INTO the next session's context so it is
  seen, not buried in a log (item 495's sting was silent loss). A flush firing
  logs `ok bytes=… (offset X/Y)` ONLY when fully drained (`X>=Y`); a firing that
  broke early (a failed chunk, a held partial line, or the per-firing cap) logs a
  truthful `partial bytes=… (offset X/Y) — will resume` instead. The pre-brief
  reads any `partial` (offset `X<Y`) as an unresolved failure — so a partial drop
  on the terminal `SessionEnd`, where there is no next firing, can no longer be
  masked by an earlier chunk's success and vanish silently. LOCAL enqueue logs the
  same `ok bytes=` marker on success, so a one-off transient failure self-clears at
  the next successful firing instead of nagging every new session.
- `settings.json` merge is idempotent (matches both command and args forms) and
  preserves any other hooks you have.
- To disable: remove the entries from `~/.claude/settings.json` `hooks`, or
  delete `~/.claude/kb-flush.env` (the hooks no-op without config).

## Offset checkpoints: never deleted, the trust stamp, seeding, the sweep

Every session's progress lives in ONE file, `~/.claude/hooks/state/<session>.offset`
— the byte offset of the transcript the KB has accepted. Since wave C (2026-09)
that file is **never deleted by an event**: a drained `SessionEnd` leaves it at
`offset == size` (notifications then read `kb 5.2MB synced`), a resumed session
continues from it, and the server refuses a re-send of bytes it already holds for
that session. The only writers are: `+accepted bytes` after each 2xx chunk, `0`
when the transcript shrank (logged `reason=shrank`), a branch seed when a new
transcript turns out to be a copy of a sibling (logged `branch of <parent8>`), and
the owner's `--seed`. A checkpoint is retired only by the hook's daily state
sweep, and only when its transcript is gone from every Claude Code projects dir.

**The trust stamp.** `install.sh` writes `state/.checkpoints-trusted-since`
(ISO-8601 UTC) exactly once and never moves it. A transcript with no checkpoint
whose first record is NEWER than the stamp is a genuinely new session and drains
from 0. One that is OLDER was worked by a pre-wave-C hook that deleted its
checkpoint after draining — sending it from 0 would resend the whole session —
so the hook **refuses** it, writes `<session>.unseeded`, and logs once a day:

```
flush <project> unseeded: refusing to send <bytes> from 0 (transcript predates the
checkpoint stamp) — seed it: kb-flush-backfill.sh --seed <sid> current|<bytes>
```

A **corrupt or empty checkpoint file** takes the very same refusal (same marker,
same once-a-day line) with the reason `offset unreadable: <path>` — a resend from
0 is never automatic (KB 33783). The file is left as it is; `--seed` replaces it.

Notifications show `kb ?/199.0MB unseeded`; the pre-brief's `[KB sweep]` line
counts them. Two clean exits, both owner decisions:

```bash
kb-flush-backfill.sh                          # the table: unseeded / ready / flushed / busy / stalled
kb-flush-backfill.sh --seed <sid> current     # "everything currently on disk is already in the KB"
kb-flush-backfill.sh --seed <sid> <bytes>     # "captured up to this byte" (snapped DOWN to a line end)
KB_FLUSH_ALLOW_UNSEEDED=1                     # or: let it send from 0 deliberately (the server dedupes)
```

`KB_FLUSH_ALLOW_UNSEEDED=1` is safe ONLY once the server's resend guard is live
(wave S) AND these never-delete hooks are installed on that box — an older backfill
resends from byte 0 with no guard behind it, and the server dedupes only chunks cut
on the lattice. Until both hold, seed instead.

Seed LOW when in doubt: a low seed costs at most one overlapping chunk (refused by
the server when it lands on the cut lattice, else one duplicate row); a high seed
loses lines. Never hand-edit or delete an offset file.

**The sweep.** `kb-flush-backfill.sh --auto` walks every transcript on the box and
feeds each unflushed remainder through the real flush hook — it is what captures a
session whose terminal was killed, whose IDE crashed, or whose laptop slept through
`SessionEnd`. Two triggers run it: the hooks' own piggyback (any firing, at most
every `KB_SWEEP_INTERVAL` = 1800 s — every box, no scheduler) and, on Linux, a
`systemd --user` timer `install.sh` renders into
`~/.config/systemd/user/kb-flush-sweep.{service,timer}` (`OnBootSec=10min`,
`OnStartupSec=10min` — re-arms it when a Linger=no user manager restarts at login,
`OnActiveSec=10min` — re-bases it on every start/restart, so an update that changes
the units (and restarts the timer) re-arms it on a manager that has been up for weeks,
`OnUnitActiveSec=<KB_SWEEP_INTERVAL>s`, `Persistent=true`; a backfill that predates
`--auto` is not scheduled until the next install; the installer prints one `sweep:`
line saying which). **The off switches:** `KB_SWEEP_INTERVAL=0` in
`~/.claude/kb-flush.env` stops the piggyback (the hooks source that file) AND, at the
next `install.sh`/`update.sh`, keeps the timer off — the installer greps the file
(never sources it), and if the timer is enabled or active it runs
`systemctl --user disable --now kb-flush-sweep.timer`; the `sweep:` line then reads
`not scheduled (KB_SWEEP_INTERVAL=0 in kb-flush.env; the sweep is off …)` — every
`not scheduled` line names an interval of 0 this way, never "the piggyback still
runs". A non-numeric interval (`off`, `no`, `false`, …) is OFF too — the hooks'
piggyback stops on it, so the installer does not fall back to 1800: it names it
`<non-numeric>` (never echoing the file) and disables the timer the same way. An
interval of 1–59 is NOT off: the timer is not scheduled (60 s is its floor, and a
running one is disabled), but the piggyback still sweeps every N s, and the line
says `… is under the timer's 60s floor; the hooks' piggyback trigger still runs the
sweep every Ns`. `KB_SWEEP_TIMER=0` (in the file, or exported for one install) turns off only the
timer — the piggyback still runs; any value but `1` is off, a word such as `off` in the
file included. An exported value always wins over the file, through `update.sh` too
(it restores the caller's `KB_SWEEP_*` after sourcing the file). With no user bus the
installer cannot disable anything: if the timer's `timers.target.wants` link is still
there it prints one `hint:` line — run `systemctl --user disable --now
kb-flush-sweep.timer` from a login session. Until the next install/update the
file line does not touch a timer that is already running (the timer never reads
`kb-flush.env`): disable it by hand to stop it at once. A sweep never
touches a transcript modified in the last **5 min** (`KB_SWEEP_QUIET_SECONDS` = 300),
holds a trailing half-line like the hook, and leaves one line in `state/.sweep-last`.
The timer runs the sweep with the **`KB_SWEEP_*` defaults**: the unit passes only
`PATH`, and the sweep never sources `kb-flush.env` — to tune it, edit the unit or
export the knob in the timer's environment (`systemctl --user edit kb-flush-sweep.service`,
`[Service] Environment=KB_SWEEP_QUIET_SECONDS=600`). On a **LOCAL** box (`KB_DIR`)
the hook enqueues whole transcripts and writes no offsets, so the sweep has nothing
to account for and is a no-op there. When the hook finds a session's lock held on a
`Backfill` firing it logs `flush <project> Backfill skipped (lock held) …`, which the
sweep reports as `busy` (a fact, never a failure). Its health is visible
without opening a log: notifications append `sweep:never` / `sweep:26h stale` /
`sweep:STALLED n` / `sweep:unknown n` only when something is wrong, and the
SessionStart pre-brief prints one `[KB sweep]` line with the action (`stalled`
means nothing has triggered a sweep for over 6 h — the timer is not firing).
Windows (Git Bash) has the piggyback trigger only; SHARING.md documents an
optional, manual `schtasks` line.

### State files (`~/.claude/hooks/state/`, proposal_v2 Appendix I)

Per session (`<okey>` = the sanitised session id): `<okey>.offset` (the checkpoint),
`<okey>.offset.lock` (flock) or `<okey>.offset.lock.d/pid` (the mkdir fallback where
`flock` is missing — only the lock keeps the `.offset` infix), `<okey>.last` (when a
drain last started; the Stop gate's clock), `<okey>.gate` (when the Stop gate last
opened), `<okey>.retry` = `<epoch> <n> <code> <end>` (after a failed REST chain: attempt
count, HTTP code, and **`<end>` = the failed chunk's END offset, `offset + bytes`,
always `<= size`**, so the retry re-cuts the same window; a SessionEnd that met a
held lock leaves `<epoch> 0 lock <size>` — nothing failed, `<end>` is the
transcript size at deferral — so the resumed session finishes the tail),
`<okey>.project` (`<slug>\t<how>`, the session pin), `<okey>.unseeded` (refused: pre-stamp
transcript or unreadable checkpoint), `<okey>.sweep` (the sweep's stall record),
`<okey>.seen` (empty: some firing found the transcript — even a closed-gate Stop —
so a later SessionEnd with the file missing is a BUG, not a prompt-less skip; KB 51863;
retired by the state sweep with the offset, or — a session that never drained — alone
after 90 days with a loss note; a LOCAL box never runs that sweep),
`<okey>.stalecopy` (the stale-copy line's 24 h rate-limit stamp),
`<okey>.diverged` / `<okey>.behind` (KB 49064: ONE line `<iso> <path> size=<n>
checkpoint=<n>[ cover=<copy>] frozen=<n>` naming the refused copy; `frozen=` is
its size when first named, kept on every rewrite — where a resumed copy's unsent
records begin; written by the hook on each refusing firing and by the sweep's
`--push`/`--auto` (`<marker>.t` is the atomic-write temp), cleared only by
`--seed`, by a firing or sweep pass that finds the named path gone, with the
offset by the state sweep, or — `.diverged` only — by the hook, under the session
lock, once the named path again carries the checkpoint holder's bytes just below the
checkpoint it drains from (a session resumed there after the copy was moved out)) and their 24 h log rate-limit stamps
`<okey>.diverged.stamp` / `<okey>.behind.stamp`.
Box-level: `.checkpoints-trusted-since`, `.sweep.lock`, `.sweep.trigger`, `.sweep-last`,
`.state-sweep.last`, the hourly rate-limit stamps `.notoken-<project>.stamp`,
`.notconfigured.stamp`, `.nojson-<tag>.stamp`, `.tripwire.stamp`, and the dispatcher's
`.dispatch-124-<child>-<EV>.stamp` (one epoch per child/event, e.g.
`.dispatch-124-flush-session-to-kb-Stop.stamp`: the `dispatch … hit its 3s budget
(rc 124)` line is logged at most once an hour per child/event). Box-level stamps are
never per-session files, so the daily state sweep never retires them; `.backfill-done`
is an inert relic.

## Keeping the hooks current

The pre-brief compares `~/.claude/hooks/.setup-version` with the server's
`/api/setup/manifest.json` once per session and prints an `OUT OF DATE` banner
when they differ. `bash ~/.claude/hooks/update.sh` then fetches every asset the
manifest lists into `~/.claude/kb-setup`, verifies each file's sha256 and re-runs
`install.sh` (`--dry-run` fetches and verifies only). The file list comes from the
server, never from a hand-kept paste, so it can be neither short nor long.
