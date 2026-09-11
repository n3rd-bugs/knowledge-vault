# hookrig — a hermetic clean-room for the Claude Code ⇄ KB session hooks

Everything we "knew" about the `kb-session-flush` hooks was field folklore: facts
gathered by poking live boxes and filed as unverified KB items. An audit found
roughly two thirds of it wrong — a fabricated stdout contract, an inverted mode
priority, the wrong number of registered events. This rig turns the **corrected**
facts into executable assertions so they cannot silently regress, and gives us a
place to change hook behaviour without touching a production machine.

**A hook fact is only "known" once it is a case in here.** A KB item that no case
pins is a hypothesis. If you learn something new about the hooks, the fact is not
recorded by writing it down — it is recorded by adding a case (see
[Adding a case](#adding-a-case)).

```
make hooktest                       # everything
bash tests/hookrig/run.sh 05 07     # only cases whose name contains 05 or 07
bash tests/hookrig/run.sh --clean   # also delete the built image(s) afterwards
KB_SKILL_DIR=~/.claude/skills/kb-session-flush bash tests/hookrig/run.sh   # test an INSTALLED copy
```

Exit status is 0 only when every case passes (`XFAIL` — a pinned known defect —
counts as green; see [Known defect](#known-defect-xfail)).

## What it is

| file | role |
| --- | --- |
| `Dockerfile` | clean debian-slim image: bash, curl, jq, python3, git, gzip/coreutils, setsid. **No** Claude Code, **no** `uv`, **no** `kb` CLI — the build fails if any appear. Non-root `tester` with its own `HOME`. |
| `stub_server.py` | dependency-free (stdlib) stub serving **both** transports and recording every request to JSONL. Decodes `transcript_gz` so assertions see the real payload bytes. |
| `stub_contract.py` | the submission contract the stub enforces, mirrored from `kbcore`/`kbrest` with the source line cited against each constant — so a drift between stub and server is a reviewable diff, not a silently permissive test. |
| `Dockerfile.<variant>` | a deliberately worse-equipped box, built FROM the base image, selected per case with a `# IMAGE: <variant>` header — see [Image variants](#image-variants). |
| `lib.sh` | assertions plus the fakes each transport precondition needs (`fake_uv`, `fake_claude`, `fake_claude_json`, `fake_kb_repo`), the Windows fakes (`fake_uname_windows`, `fake_cygpath`, `fake_win32_timeout`, `record_nohup`, `expose_setsid`, `expose_flock`), the stub knobs (`stub_fail`, `stub_delay`), the sweep clock (`pin_sweep_clock` / `unpin_sweep_clock` → `KB_BACKFILL_NOW`), `extract_block`, and the event builders. Fixtures (`mk_transcript`, `mk_big_transcript`) carry a first-record `"timestamp"` like a real transcript, 1 s ahead of the case clock so a fixture built in the same second as `install.sh`'s `.checkpoints-trusted-since` stamp still reads as *newer*; `RIG_FIXTURE_TS=2020-01-01T00:00:00.000Z` builds a PRE-stamp one. Uses `$RIG_JQ`/`$RIG_PY`, never a bare `jq`/`python3`. |
| `run.sh` | builds the base image + every variant a case asks for, starts the stub, runs each case in a **fresh** container of its image, prints the PASS/FAIL table, cleans up. The skill under test defaults to the repo copy `contrib/claude/kb-session-flush` (override with `KB_SKILL_DIR`). |
| `cases/*.sh` | one file per assertion group. Every case header names the KB fact it pins. |

The stub speaks:

* `POST /mcp` — enough MCP streamable-HTTP for `kb-mcp-call.sh`: `initialize`
  (replies with the `Mcp-Session-Id` **response header**),
  `notifications/initialized`, `tools/call` (`kb_ingest_session`, `kb_search`).
* `POST /api/ingest-session` — the real REST contract, mirrored from
  `src/kbrest/server.py:168`, `src/kbcore/ingest_queue.py:22` and
  `src/kbcore/compress.py:21-22`: `401` (missing/invalid bearer), `422`
  (malformed body, a non-string `session_uri`, or a malformed `flush_*` stamp,
  `src/kbrest/schemas.py:80-87` — string stamps are `fullmatch`ed so a trailing
  newline is refused like the server's anchored pattern; the int check is
  deliberately *stricter* than pydantic lax: `12.0`/`true` are 422 here),
  `400` (empty transcript, over 1,000,000 bytes, bad kind, both transcript
  forms, undecodable gzip), else `202` `{queue_id,duplicate,requeued,permanently_failed}`.
  **Dedupe is keyed like the wave-S server** (KB 33783/34161, `stub_contract.py`
  `Queue.take`): a `kind=session` chunk with a non-degenerate `session_uri`
  starting `session:` is identified by `(session_uri, content_hash)` across
  *every* project — a resend under another project answers `duplicate:true`
  with a null `queue_id`, byte-identical to a same-project repeat (no oracle);
  everything else falls back to the legacy `(kind, project, content_hash)`
  arbiter. The record carries harness-only `dedupe_key` (`session`|`legacy`),
  `duplicate_kind` (`same-project`|`cross-project`|null) and
  `session_uri_normalised`. The eight `flush_*` telemetry fields are recorded
  **verbatim** (a number stays a number, a string a string) plus
  `flush_consistent` (`src/kbcore/models.py:268-274` — recorded, never a refusal).
* `GET /api/search` — the REMOTE-mode recall endpoint, mirroring
  `src/kbrest/server.py:93` / `SearchHit` closely enough to exercise the hook's
  url-encoding and bullet formatting (bearer-gated: `401` without it).
* `GET /__requests` (the JSONL record), `POST /__reset`, `GET /__health`.
* `POST /__fail {"times": N, "code": C}` — the next N ingest-session answers
  (REST, and an MCP `kb_ingest_session` tool call alike) return HTTP C; `C = 0`
  closes the connection with **no** response, which curl reports as `000`.
  Every injected hit is recorded (`status: C`, `injected: C`) so a case can
  count what a retry chain did; an injected failure never consumes the dedupe
  key, so the retried chunk lands as a new row. The gate applies **before the
  bearer check as well**, so an unauthenticated request spends a slot too —
  count `.injected` hits, not `.auth`. Cleared by `/__reset`. A malformed body
  (non-int `times`/`code`) is a `400`, and `lib.sh`'s `stub_fail` turns that
  into a red check on the spot (it checks the HTTP code) rather than a silent no-op.
* `POST /__delay {"ms": N}` — every ingest answer is held N ms; the env
  `RIG_DELAY_MS` on the stub sets the default. The Windows lane's case 08 arms
  its 12 s think-time per step with `stub_delay 12000` (an endpoint call) and
  restores the default with a bare `stub_delay` afterwards. `{}` (= `lib.sh`
  `stub_delay` with **no** argument) and `/__reset` restore that env default;
  `stub_delay 0` is a literal zero and removes a lane's think-time until reset.
  Malformed `ms` → `400` → `stub_delay` fails loudly like `stub_fail`.

## The hermetic guarantee

This is the part that is not negotiable — hook folklore was gathered against
production, and a sub-agent once truncated the live schema. So:

1. **No egress.** Case containers run on a `docker network create --internal`
   network. There is no default route: not to `kb.n3rd-bugs.dev`,
   `api.n3rd-bugs.dev`, `127.0.0.1:8321/8322/8323`, the live Postgres, or the
   internet. The only reachable service is the stub, started by `run.sh`.
   The live KB hostnames are additionally pinned to `127.0.0.1` via `--add-host`.
2. **No host mutation.** The host's `~/.claude` is never mounted. `install.sh`
   runs **inside** the container against a container-local `HOME`, so no host
   `settings.json`, `hooks/`, or `kb-flush.env` is ever created or edited.
   `run.sh` fingerprints those host files before and after the run and fails
   loudly if anything changed.
3. **Read-only skill.** The skill directory is mounted `:ro`, as is the rig
   itself. Case 00 asserts both, plus 1 and 2, and runs first — if it fails, no
   other result in the run means anything.
4. **Fresh HOME per case**, and fresh sub-`HOME`s within a case, so every
   `install.sh` is a clean-room install. Nothing carries over between cases
   except the stub's record, which each case resets.
5. **No database, ever.** The stub holds its record in a tmpfs file. The rig
   never runs `make migrate`, `docker compose up`, or `pytest`.

## The cases and what they pin

| case | pins | KB item |
| --- | --- | --- |
| `00_hermetic_isolation` | the guarantees above, as assertions | — (rig fact) |
| `01_mode_priority` | `install.sh` picks **LOCAL → MCP → CONNECTOR → REMOTE**, first precondition wins; LOCAL needs *both* a `pyproject.toml` dir *and* `uv`; MCP auto-discovery only accepts a `~/.claude.json` server whose **key** matches `/kb\|knowledge/i` *and* carries a `Bearer`; REMOTE is an unconditional placeholder fallback that ignores `KB_REST_URL` in the environment; an existing config is never re-authored | **1151** (corrects **519**, which claimed CONNECTOR was the default) |
| `02_four_events` | exactly **four** events — `PreCompact` + `SessionEnd` → `flush-session-to-kb.sh`, `SessionStart` (matcher `startup\|clear`) + `UserPromptSubmit` (empty matcher) → `kb-session-brief.sh`; two scripts, declared timeouts | **1152** (corrects **509**, which said three) |
| `03_idempotent_install` | three installs leave exactly one KB entry per event; pre-existing non-KB hooks, matchers and unrelated settings survive; an existing `kb-flush.env` is not clobbered | — |
| `04_flush_no_json_contract` | the flush hook prints **nothing** to stdout — no `{"status"`, no `"queued"`, no JSON at all. Success is a *log line* in `flush-session.log`, written by the detached child | correction to the fabricated **1073** |
| `05_mcp_transport` | `initialize` → `notifications/initialized` → `tools/call`; session id arrives as a **response header** and is echoed on later requests; tool is `kb_ingest_session`; payload travels as `transcript_gz` and decodes byte-identical; project = slugified `cwd` basename; a wrong bearer fails loudly and does not advance the offset | — |
| `06_chunking_row_cap` | a >1 MB delta is split into sequential ≤900,000-byte windows, each cut on a **line** boundary, each individually valid JSONL, each under the 1,000,000-byte row cap; concatenation reproduces the input byte for byte; one MCP session per chunk | **1154** |
| `07_offset_checkpoint` | second flush of an appended transcript sends only the appended bytes; a flush with nothing new makes **no** request at all; the offset key is the `session_id`; a corrupt offset restarts rather than wedging | — |
| `08_remote_contract` | REST mode end to end: 202 happy path, 401 on a wrong bearer (offset not advanced, session unharmed), empty transcript stopped by the hook itself; plus the server contract asserted directly (401/422/400/202 and the 202 body keys) | — |
| `09_recall_noise_controls` | `kb-session-brief.sh` skips slash commands, <12-char prompts and stopword-only prompts ("Yes please review and update"), fires on a substantive one, suppresses a repeat within the 600 s window (normalised query), honours `KB_PROMPT_RECALL=0`; `SessionStart` briefs on `startup` but not `resume`/`compact` | — |
| `10_never_silent_failure` | an unreachable MCP/REST endpoint → exit 0, empty stdout, an explanatory line in `flush-session.log`, offset untouched — and the next `SessionStart` surfaces that failure; garbage input is an inert no-op | — |
| `11_guard_always_on` | `KB_HOOK_GUARD` is checked on line 14 of **both** hooks, before the config is even read: guarded firings do no work in **MCP and LOCAL** modes and on both brief events. Every assertion is paired with a positive control | correction to the inverted **885** |
| `12_incomplete_config_silence` | an incomplete transport config (`KB_REST_URL`/`KB_MCP_URL` with an empty `KB_TOKEN`) logs a `NOT CONFIGURED` line instead of being a permanent silent no-op, and the next `SessionStart` surfaces it | **1174** |
| `13_connector_chunking` | CONNECTOR relay byte contract, with a **recorder standing in for the model** (`CLAUDE_BIN` is just a path): a 60 KB delta relays as sequential `KB_CONNECTOR_CHUNK` windows, each cut on a **line** boundary and individually valid JSONL, the chunks reassemble **byte for byte**, the offset reaches EOF, the firing logs a truthful `ok bytes=` (never `partial`), the child is invoked headless with a tool allowlist and no permission bypass, and a drained transcript re-sends nothing | — |
| `14_connector_caps_and_resume` | every way a CONNECTOR firing stops EARLY still leaves the session **resumable, never truncated**: `KB_CONNECTOR_MAX` caps a `PreCompact` firing (logged `partial` + `per-firing cap reached`, never `ok`) and later firings drain the rest with no re-send or loss; `SessionEnd` **ignores** the cap and **keeps the offset file at the transcript size** (flipped 2026-09-11 — it used to assert the file was retired, on the false "recycled id" premise; session UUIDs are resumed, never recycled, and the deletion is what made every resume re-send from 0); a refused chunk advances the offset by exactly the accepted bytes and is retried verbatim; `Error`/`NOTOOL`/no-id replies all count as failure; a still-being-written partial line is held on `PreCompact` but **sent** at `SessionEnd` (connector arm) | **33783**, **34161** |
| `15_large_transcript` | the flush loop at scale over REST: a 12 MB delta drains in one firing byte-for-byte across ~30 chunks each ≤ 1,000,000 bytes; `KB_FLUSH_MAX` caps a `PreCompact` firing (`partial`, never `ok`) and repeated firings drain the rest exactly once; `SessionEnd` lifts the cap and **keeps the offset file at the transcript size** (flipped 2026-09-11, same fact as case 14); a single 2 MB line does not stall the loop; growth after a flush is picked up next firing; shipped defaults are `KB_FLUSH_CHUNK` 700,000 / `KB_FLUSH_SEND_MAX` 950,000 / `KB_FLUSH_MAX` 200 MB (C2) | **33783**, **34161** |
| `20_no_jq_python_fallback` *(image: `nojq`)* | a box with **no jq** (and no sudo): install succeeds on the **python3** backend, the mode chain (incl. MCP auto-discovery from `~/.claude.json`) still resolves, four events register, MCP **and** REST flushes really submit; and install.sh's jq merge vs its python merge produce a **byte-identical** `settings.json` from the same seeded input (foreign hooks, unicode, mixed types preserved) | **1173**, **1152** |
| `21_no_json_tool_loud` *(image: `nojson`)* | **no jq and no python3**: install **fails loudly** with a sudo-free remedy (names `~/.local/bin`, the co-location path and `KB_JQ`; never `apt install`) and writes no `settings.json`; a flush **and** a brief fired in that state exit 0, print nothing, and each append a rate-limited `NOT CONFIGURED` line the pre-brief lifts into the next session | **1174** |
| `22_colocated_jq_and_kb_jq` *(image: `nojson`)* | backend-chain links 2 and 3 on a box with no JSON tool at all: a jq **beside the hooks** and `KB_JQ=/path/to/jq` each make install + flush + recall work, and the installer propagates that jq into `~/.claude/hooks/` so the box survives losing `KB_JQ` | **496** |
| `23_windows_simulation` *(image: `winsim`)* | faked `uname -s`=MINGW + `cygpath` + a Win32 `timeout.exe`: hooks register as `bash.exe` + `args[0]` for all four events (never a bare `.sh`), a `C:\Users\First Last\...` transcript/cwd converts and submits, the Win32 `timeout` is probed-then-**refused** (recall still returns hits), and detach falls back to `nohup` — with `setsid` restored as the positive control | **1153**, **496**, **1152** |
| `24_paths_with_spaces` | a `HOME` **and** a project dir containing spaces: install, jq co-location, PreCompact flush, SessionEnd delta flush, offset file, per-prompt recall and pre-brief all work; `"kb hook rig"` slugifies to `kb-hook-rig` | — |
| `25_no_cygpath_fallback` *(image: `winsim`)* | Windows **without cygpath**: the pure-shell `C:\` -> `/c/` conversion keeps the flush working, `install.sh` still registers a spawnable command (`$EXEPATH`-derived, else bare `bash.exe`), plus unit assertions on `kb_winpath` extracted from the *installed* hook | **1153** |
| `29_offset_lifecycle` | the offset lifecycle (C1/C8, App. B writer invariant): SIZE=0 is *unreadable*, not shrank (no clobber, no drain, bearer and CONNECTOR arms); a trailing partial line is HELD on every bearer event incl. SessionEnd; `0 < SIZE < offset` logs `reason=shrank`, restarts at 0 and the 0 LANDS on disk; a drained SessionEnd KEEPS the offset at SIZE and a resume sends only the delta; `kb_state_sweep` retires a session's state only when the offset is > 30 d old AND the transcript is gone under BOTH `$HOME/.claude/projects` and `$CLAUDE_CONFIG_DIR/projects` (a `.retry` beside it -> a loss note); an unseeded pre-stamp transcript is refused (0 requests, `<okey>.unseeded`, one line per 24 h) and `KB_FLUSH_ALLOW_UNSEEDED=1` sends it; a stamp-less box gets a stamp created as *now*; **a corrupt/empty offset file is refused as `unseeded … (offset unreadable: <path>)`** — same marker, same rate limit, the file untouched, Stop closed, never `restarting from 0` — and `kb-flush-backfill.sh --seed <sid> 0` makes it flow (stage 3 A1) | **33783**, **33784** |
| `35_branch_seed` | the branch/fork seed (C1, App. C): a fork = parent bytes + N lines seeds at the parent size and sends ONLY its own lines (one `branch of <parent8>: seeded offset=` line, `flush_offset` = the seed); a pre-stamp parent still seeds (the branch check runs before the timestamp rule); a divergent fork under `LC_ALL=C` (cmp says `char N`) seeds at the last newline BEFORE the divergence; the seed is capped at what the parent checkpointed (an unflushed parent seeds nothing -> the timestamp rule); **a shared prefix whose last newline lies > 1 MB behind it** (a 1.3 MB record, diverging 1.2 MB in) seeds at THAT newline — the snap is the backfill's `bf_snap` arithmetic (newline count + `head -n N`), never `tail -n1`, which uutils coreutils 0.8.0 truncates to 983040 bytes (stage 3 A2) | **33784** |
| `26_portable_block_identity` | the shared portable block appears exactly once per script and is **byte-identical** across all four, is sourceable standalone, and its jq/python3 backends agree primitive by primitive (field read, object build incl. a file-sourced value, validation, URL encoding) | — |
| `31_stop_gate` | the Stop gate (C5, hybrid_v1 §2.2, App. D): an idle Stop makes **0 requests and no log line** (no pin, no `.gate`, nothing below the gate runs); an OPEN Stop with a cached pin makes **no `git` call in the parent** (a `git` shim records the caller's session id: gate → cached pin → token file → detach; the git context, `hook_version` and the log rotation are the detached child's); the size path opens at 4,000,000 pending bytes + 300 s since `.last` and a second open inside the interval is blocked; the age path opens at 65,536 bytes + 1800 s (1000 s does not; 10 KB never); `.gate` is stamped by the parent, `.last` by the child at drain start; a `.gate` newer than `.last` (parent killed before the detach) closes the gate for one interval; a `.retry` marker at give-up (n >= 20) suppresses the age path, below give-up with its back-off elapsed it opens the gate and the drain removes it, inside its back-off it holds even 4 MB; a refused (unseeded, pre-stamp) session refuses once on its first open Stop and every later Stop is closed by `.unseeded` (zero requests, no line, no `.gate`); `.sweep.trigger` is touched on a closed Stop once per `KB_SWEEP_INTERVAL` and ONE `kb-flush-backfill.sh --auto` run is detached with it (an `--auto`-aware stand-in backfill records the runs and logs nothing, so the case is green with or without C7's backfill); **no token auto-mint on Stop/SessionEnd** — with no token on the owner box those firings log `no token yet: the auto-mint is deferred to the next PreCompact/Backfill firing`, never NEEDS TOKEN / NOT CONFIGURED — and on PreCompact the mint runs in the detached child (parent < 1.5 s, minted bearer on the wire); a `subagents/` transcript is ignored on every event; dispatcher `Stop) flush-session-to-kb.sh:3 notify-complete.sh:11` (14 < 15, slot unchanged), a child killed at its budget leaves one `rc 124` line per hour (rate-limited by the BOX-level stamp `state/.dispatch-124-<child>-<event>.stamp`, beside `.notoken-*.stamp` / `.notconfigured.stamp` — never a per-session file, never swept) and the next child still runs | **33784** |
| `32_transport_retry` | the transport line + retry marker (C6, App. D + G; KB 1195): `stub_fail 1 502` is absorbed by curl's `--retry` (1 extra hit, no FAILED, no marker); `stub_fail 9 502` -> one `chunk FAILED (502) … retry#1 ev= sid=` line, <= 4 hits (`KB_FLUSH_CURL_RETRY=1`: 2 attempts x 2 chains), `.retry` = `<epoch> 1 502 <end>` (field 4 = the FAILED CHUNK'S END, offset + bytes, always `<= size` — never the file size), n=2 on the next failed drain, removed by a clean drain that reaches that end; a dropped connection (000) is on the transient set; 401 is one hit, no retry, no marker; 429 -> marker + the LONG back-off honoured by the next Stop (0 requests at once AND with the marker 1000 s old — open under the exponential 300 s, closed under the 3600 s bound), a 4000 s-old marker opens it; lost 202 then growth -> the first window is capped at the marker end so the re-cut is byte-identical (`duplicate:true`, `dup=1`), the new rows reassemble to exactly the appended bytes; a capped retry drain that FAILS again keeps the same end in the marker (not the grown size), so after a second growth the next retry still re-cuts `[P, E1)` byte-identically and the new rows still equal exactly the appended bytes; source pins: the served ingest curl carries `--max-time`/`--retry`/`--retry-delay 3`/`--retry-max-time`/`$KB_CURL_RETRY_ALL`, no `--connect-timeout` in flush/kb-mcp-call/brief/update.sh, no `--fail` | **33784**, **1195** |
| `27_backfill_sweep` | `kb-flush-backfill.sh` on **offset accounting** (C7): exactly the bytes past each session's checkpoint leave -- a seeded transcript sends only its tail, byte for byte, under its own session identity; a transcript with **no checkpoint that the hook's own rule would not send from 0** (undatable: no timestamp, or pre-stamp) is `unknown`, listed and never sent by a bare `--push` (`--push --include-unknown` is the explicit opt-in, the positive control; a post-stamp one is `ready` at 0 -- case 33); `flushed` sends nothing; the old `.backfill-done` ledger is **inert** (a ledger claiming a session done changes nothing and is never rewritten); dry run by default; a second `--push` is a no-op; the hook is fed `hook_event_name=Backfill` (log `ev=Backfill`, never SessionEnd); an unrecoverable cwd is `skipped` with its reason; `--limit`/`--project` work; a bad flag exits 1; no token in the output | **33783**, **33784** |
| `33_sweep_auto` | the sweep the timer / piggyback run (C7): `--auto` has **empty stdout** and the exit-code contract (0 ran, 2 tripwire / refused `--seed`, 3 sweep lock held -- silently); the **quiet window** (`pin_sweep_clock`) skips a `live` transcript and drains it once the clock moves, `.sweep-last` written with the pinned `ts=`, `.sweep.trigger` consumed; a **held session flock** is `busy` (nothing sent, no `NOT CAPTURED`, no stall increment, `busy=` counted); **the HOOK meeting a held lock on a `Backfill` firing** (a stand-in hook takes the flock, then runs the real hook) logs exactly one `flush <project> Backfill skipped (lock held) ev=Backfill sid=<sid8>` — the byte-for-byte token the sweep's verdict grep reads as `busy (the hook found the session lock held)` — while PreCompact under the same lock stays silent and SessionEnd keeps its deferred line (stage 3 A3); a fork of a drained sibling reads `branch` and only its own bytes leave; `KB_SWEEP_BUDGET_BYTES` / `KB_SWEEP_MAX_TRANSCRIPTS` defer the rest oldest-first; a `Backfill` event **holds the trailing partial line** and the checkpoint **survives** (`flush_event=Backfill`, no SessionEnd stamped or logged); six no-progress runs log exactly **one `STALLED`** line, the transcript backs off `KB_SWEEP_STALL_BACKOFF` and is retried after it; the **tripwire** (no line-2 lineage marker, or `rm -f "$OFFF"` present) prints one `BUG: hook lineage mismatch` line, exits 2, zero requests; `--seed` snaps DOWN to a line end (`seeded <sid8> offset=N (snapped from M)`, logged) and is refused past the size / under a held lock / under `--auto` / for an unknown sid; an **off-lattice seed costs exactly one new row** then `duplicate:true` (`dup == N - 1`); **the hook's own rule for no-checkpoint transcripts**: a post-stamp first record is `ready` at 0 and `--auto` drains it, a pre-stamp one is `unknown` and `--auto` never fires it even with `--include-unknown` (`.sweep-last` `unknown=`), `KB_FLUSH_BRANCH_SCAN=0` turns a `branch` row into what the hook would do without the scan; **fd hygiene**: the hook is not handed the sweep's flock fd 8 / index fd 3, so a detached drain that outlives the run cannot keep `.sweep.lock` held (`--status` says `free`, the next `--auto` is not locked out); the tripwire's **log copy is rate-limited** (one an hour, `state/.tripwire.stamp`) while its stdout line is per run; the **stall back-off gates `--auto` only** -- an explicit `--push` inside it fires the transcript and refreshes `.sweep-last` (`mode=push`); `--seed` with a missing operand is a clean usage error; `--projects` / `--timeout` are exercised | **33783**, **33784** |
| `37_install_timer` | install.sh (C9): the `.checkpoints-trusted-since` stamp is written once, in UTC, BEFORE any hook is copied and never overwritten; the `kb-flush-sweep` user timer is rendered AFTER `.setup-version`, byte-identical across runs, and gated (Windows, `KB_SWEEP_TIMER=0`, no `--auto` in the served backfill, missing / degraded / `enable`-failing `systemctl`) — every path completes the install and prints exactly ONE `sweep:` line. |
| `38_rig_stub_plumbing` | the rig's OWN wave-C plumbing (C10), stub-only so a failure is a rig defect: dedupe keyed on `(session_uri, content_hash)` across projects (cross-project resend → `duplicate:true`, null id, four keys only, never names the canonical project) with the legacy `(kind, project, hash)` fallback for no/degenerate uri; the eight `flush_*` fields recorded verbatim, `flush_consistent` recorded and never a refusal, malformed stamps → 422 (fullmatch; a non-string `session_uri` is a 422, never a crashed handler); `POST /__fail` (N × code, `0` = dropped connection → curl `000`, hits recorded, dedupe key not consumed, MCP tool calls too, cleared by reset; a malformed knob body is a loud `stub_fail`/`stub_delay` failure); `POST /__delay` (`{}` restores the env default); fixtures carry a first-record timestamp (`RIG_FIXTURE_TS` for a pre-stamp one); `pin_sweep_clock`; `expose_flock` | **33783**, **34161** (rig fact) |
| `39_notify_brief_state` | notify-complete.sh / kb-session-brief.sh (C9): the `kb <flushed>/<size>` progress keeps its shape and gains ` synced`, ` retry#N`, `?/<size> unseeded` and (unhealthy only) ` sweep:<age>` from the per-session state files; a CORRUPT offset beside `<okey>.unseeded` reads `?/<size> unseeded` (alone: omitted); the marker probe reads the single Appendix I spelling (`<okey>.retry`, never `<okey>.offset.retry`) (stage 3 A1/D); the brief prints a `[KB sweep]` line only when the sweep is stale/stalled/never ran, and its staleness scanner matches `sid=<sid8>` before falling back to the project. |
| `wincases/05_real_flush_end_to_end` *(lane: Windows)* | the real-Git-Bash end-to-end flush with native `C:\…` paths (as before) **plus wave C on the real box**: a drained SessionEnd keeps the offset at the transcript size; a closed-gate Stop makes 0 requests and no log line (`KB_SWEEP_INTERVAL=0` keeps the piggyback sweep out of these bounds); an open-gate Stop's *parent* wall time is printed and asserted < 3 s (the dispatcher's Stop budget: no git, no mint, no rotation in the parent) while its child drains; the `nohup`-detached drain survives a 1 s `kb_timeout` killing its parent (rc 124); delta records carry a `timestamp` | **33784** |
| `wincases/08_real_large_post` *(lane: Windows)* | a >= 1,000,000-byte REST POST (the hook's plaintext fallback on a 999 KB incompressible transcript; the gzip path of the same bytes is ~0.94 MB and is sent too) through `stub_delay 12000` is answered **202** on real Git Bash/Schannel — one request, no retry of a request still in flight, offset at EOF; the served ingest curl carries no `--connect-timeout` | **1195** |

## Image variants

Some facts can only be pinned on a differently-equipped box: one with no `jq`, or
no `python3` either, or no `setsid`. A case declares that in its header:

```sh
# IMAGE: nojq
```

`run.sh` then builds `Dockerfile.<variant>` **FROM the base image**
(`--build-arg BASE=…`) and runs that case in `kb-hookrig:1-<variant>`. No header
line = the base image. An unknown variant aborts the run rather than silently
falling back. `--clean` removes the variants too.

| variant | what it removes | why it cannot be faked in `$HOME` |
| --- | --- | --- |
| `nojq` | `jq` (keeps `python3`) | `command -v jq` must really find nothing |
| `nojson` | `jq` **and** `python3` | same, for both |
| `winsim` | `setsid` **and `flock`** (Git Bash has neither); adds a writable `/c` and `/d` | `util-linux` is Essential (un-removable via apt), `command -v flock` must really find nothing for the hooks' mkdir lock path (`<okey>.offset.lock.d/pid`, proposal_v2 App. B) to run, and a converted `C:\…` path must resolve to a real file. The build fails if either binary is still resolvable. |

Each variant **moves** the tool to `/rigbin` instead of deleting it, and the
image sets `RIG_JQ`/`RIG_PY` to those paths: `/rigbin` is on no `PATH`, so the
harness can still build fixtures and assert while the code under test can find
nothing. `lib.sh` therefore uses `$RIG_JQ`/`$RIG_PY` and never a bare `jq`.
A case that deliberately re-exposes a tool (case 22 copying a jq next to the
hooks, case 23's `expose_setsid` positive control, case 36's `expose_flock`)
does it explicitly, in the case source.

## Known defect (XFAIL)

There is currently none: `12_incomplete_config_silence` was the last one and is
now a green regression gate (the hook logs `NOT CONFIGURED` and the pre-brief
surfaces it). The mechanism stays available — mark a case `# EXPECT: xfail` and
write the defect out in the header; when it starts passing the runner reports
`XPASS` (a failure) and you delete the marker, which is the moment the KB fact
gets corrected too.

## Adding a case

1. Create `cases/NN_name.sh`. The header comment must state **which fact it pins**
   and cite the `kb-system` item id when there is one, plus the source line in the
   skill (`flush-session-to-kb.sh:81-96`) or the repo (`src/kbcore/compress.py:21`).
   A case with no stated fact is a test; a case with one is knowledge.
2. `. /rig/lib.sh`, use `new_home <name>` for a clean-room `HOME`, one of the
   `cfg_*` writers for the transport, then `install_hooks`.
3. Fire events with `fire`/`fire_guarded` + `flush_event`/`prompt_event`/`start_event`.
   The flush hook **detaches**, so assert with `wait_stub`/`wait_log`, never with a
   bare `sleep`. For "nothing should happen", call `settle` twice and assert
   `stub_total` is 0.
4. Pair every negative assertion with a **positive control** in the same case. "No
   request was made" proves nothing unless the same setup without the suppressor
   does make one.
5. Run `bash tests/hookrig/run.sh NN` until green, then the whole suite.

If a new case fails because the **hook** is wrong, do not weaken the assertion.
Either fix the hook, or mark the case `# EXPECT: xfail` with the defect written
out in the header — and say so in the KB.

## The CONNECTOR lane (opt-in, NOT hermetic)

`tests/hookrig/run-connector.sh` is the one lane that cannot be stubbed: CONNECTOR
mode relays through a headless `claude -p` and the KB tool lives on the SERVER side
of the claude.ai account, so proving it works means real egress, real Claude usage,
and a real row in the LIVE ingest queue. It is therefore NOT part of `make hooktest`
and refuses to run without `KB_HOOKRIG_LIVE=1`.

    KB_HOOKRIG_LIVE=1 bash tests/hookrig/run-connector.sh

Containment: a disposable project (`kb-hookrig-live`), and the script DELETES the
queue rows it creates so the nightly drain can never mint knowledge from a test.
It also asserts afterwards that the project owns zero knowledge items.

Requirements: a `claude` binary (found the same way install.sh finds it) and
`~/.claude/.credentials.json`, both mounted read-only. It runs on the rig image —
the container is non-root, so packages must be baked in at build time, not apt-got
at runtime.

VERIFIED 2026-07-26: install selects CONNECTOR from a mounted binary alone; the
relay reaches the claude.ai KB connector and a real submission lands in the live
ingest queue (23,827 bytes); the offset advances (resume-not-resend); cleanup
leaves nothing behind.

RESOLVED 2026-07-27 — connector chunking is now pinned, hermetically. The open
question was whether the bytes beyond the first ~24 KB chunk relay at all: on a
57 KB transcript only the first chunk had landed inside the live lane's wait
window. The live lane could never settle it cheaply, because every chunk there is
a real `claude -p` costing real usage and tens of seconds.

`CLAUDE_BIN` is just a path to an executable (flush-session-to-kb.sh:498), so
cases 13 and 14 stand a RECORDER in place of the model (`fake_claude_relay` in
lib.sh) and assert the byte contract directly — free, deterministic, no egress.
Between them they pin: chunk sizing and line-boundary cutting, byte-for-byte
reassembly of the delta, offset advance per ACCEPTED chunk, the KB_CONNECTOR_MAX
per-firing budget, SessionEnd draining in full (it is terminal, so honouring the
budget would strand the tail forever), the partial-line hold and its SessionEnd
exception, every failure classification leaving the offset untouched so the
refused chunk is retried verbatim, and the truthful ok/partial/cap logging.

The answer to the original question: the remaining chunks DO relay, on the next
firing, exactly as KB_CONNECTOR_MAX intends. The live lane's empty log was the
detached relay still in flight when the container exited, not lost content.
run-connector.sh keeps its narrower job — proving the real relay reaches
claude.ai — and no longer has to carry the byte contract.

## The WINDOWS lane (opt-in, a REAL box over WinRM)

The container has a `winsim` variant, but it *simulates* Windows: a stubbed
`uname -s`, a shell-script `cygpath`, a hand-written `timeout.exe`. It pins the
logic and cannot falsify the platform assumptions underneath it.
`tests/hookrig/run-windows.sh` runs the **same skill and the same assertion
library** (`lib.sh`, via the thin `winlib.sh` shim) on an actual Windows box:

    make hooktest-win                       # all windows cases
    bash tests/hookrig/run-windows.sh 02    # only matching cases
    bash tests/hookrig/run-windows.sh --keep  # leave the remote sandbox for poking

Credentials come from the repo-root `.windows` (gitignored):
`WIN_IP` / `WIN_USER` / `WIN_PASSWORD`. They are never printed and never placed
on a remote command line. Needs `pywinrm` on the driving host (`apt install
python3-winrm`) and Git Bash at `C:\Program Files\Git\bin\bash.exe` on the target.

Containment, which matters because this is somebody's real machine:

* every case runs with `HOME` inside a per-run sandbox under the remote `%TEMP%`;
* the host's real `%USERPROFILE%\.claude` is hashed before and after — a change
  fails the run (the same tripwire the container lane gets for free);
* the stub runs on the Windows box's own **loopback**, so the hooks under test
  still talk to a hermetic KB. Only the control plane crosses the network;
* teardown tree-kills the stub and removes the sandbox, and startup sweeps
  sandboxes orphaned by a killed run (older than 6h, so it can never touch a
  concurrent one).

### What this lane caught that the simulation could not

* **The `timeout.exe` trap is only reproducible on a specific PATH.** Git Bash
  puts its own coreutils `/usr/bin/timeout` *ahead* of System32, so the default
  Git Bash PATH does not reproduce the trap at all — but Claude Code spawns hooks
  through CreateProcess with the system PATH, where System32 wins. Case 02 now
  forces a System32-first PATH and asserts against the genuine Microsoft binary
  (which really does answer `ERROR: Invalid syntax` instead of running the
  command). Testing only the default PATH would have declared this safe while
  shipping the exact configuration that breaks.
* **`kb_timeout` memoises its choice in a global**, so probing it inside `$(...)`
  discards the result and leaves any assertion about the chosen backend
  inspecting an empty variable. Cases must probe in the current shell.
* Project slugification is real and load-bearing on Windows: a cwd of
  `C:\...\My Project` submits as `my-project`, and spaces in paths are the norm
  there, not an edge case.

### Rig-side traps worth remembering

Three of these cost real debugging time and are designed around, not worked around:

* `Start-Process` **blocks forever** under WinRM; a detached background process
  needs `Win32_Process.Create`.
* A case's stdout must be redirected to a FILE inside the wrapper. If it were a
  PowerShell pipe, the hooks' detached `nohup` child would inherit the write
  handle and `Out-String` would hang long after the case exited.
* PowerShell's `-like` is a **wildcard** operator, not regex: `\\` there is two
  literal characters. A cleanup filter written with escaped backslashes silently
  matches nothing. Kill by PID — and with `taskkill /T`, because
  `Win32_Process.Create` hands back the `cmd.exe` wrapper, not the python child.

VERIFIED 2026-07-26 on Windows 11 Pro / Git Bash 5.2.26: 6 cases, 78 checks,
green; run concurrently with three container lanes with zero interference and
zero leaked resources on either side.
