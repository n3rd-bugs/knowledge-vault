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
| `lib.sh` | assertions plus the fakes each transport precondition needs (`fake_uv`, `fake_claude`, `fake_claude_json`, `fake_kb_repo`), the Windows fakes (`fake_uname_windows`, `fake_cygpath`, `fake_win32_timeout`, `record_nohup`, `expose_setsid`), `extract_block`, and the event builders. Uses `$RIG_JQ`/`$RIG_PY`, never a bare `jq`/`python3`. |
| `run.sh` | builds the base image + every variant a case asks for, starts the stub, runs each case in a **fresh** container of its image, prints the PASS/FAIL table, cleans up. The skill under test defaults to the repo copy `contrib/claude/kb-session-flush` (override with `KB_SKILL_DIR`). |
| `cases/*.sh` | one file per assertion group. Every case header names the KB fact it pins. |

The stub speaks:

* `POST /mcp` — enough MCP streamable-HTTP for `kb-mcp-call.sh`: `initialize`
  (replies with the `Mcp-Session-Id` **response header**),
  `notifications/initialized`, `tools/call` (`kb_ingest_session`, `kb_search`).
* `POST /api/ingest-session` — the real REST contract, mirrored from
  `src/kbrest/server.py:156`, `src/kbcore/ingest_queue.py:22` and
  `src/kbcore/compress.py:21-22`: `401` (missing/invalid bearer), `422`
  (malformed body), `400` (empty transcript, over 1,000,000 bytes, bad kind,
  both transcript forms, undecodable gzip), else `202`
  `{queue_id,duplicate,requeued,permanently_failed}`.
* `GET /api/search` — the REMOTE-mode recall endpoint, mirroring
  `src/kbrest/server.py:93` / `SearchHit` closely enough to exercise the hook's
  url-encoding and bullet formatting (bearer-gated: `401` without it).
* `GET /__requests` (the JSONL record), `POST /__reset`, `GET /__health`.

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
| `20_no_jq_python_fallback` *(image: `nojq`)* | a box with **no jq** (and no sudo): install succeeds on the **python3** backend, the mode chain (incl. MCP auto-discovery from `~/.claude.json`) still resolves, four events register, MCP **and** REST flushes really submit; and install.sh's jq merge vs its python merge produce a **byte-identical** `settings.json` from the same seeded input (foreign hooks, unicode, mixed types preserved) | **1173**, **1152** |
| `21_no_json_tool_loud` *(image: `nojson`)* | **no jq and no python3**: install **fails loudly** with a sudo-free remedy (names `~/.local/bin`, the co-location path and `KB_JQ`; never `apt install`) and writes no `settings.json`; a flush **and** a brief fired in that state exit 0, print nothing, and each append a rate-limited `NOT CONFIGURED` line the pre-brief lifts into the next session | **1174** |
| `22_colocated_jq_and_kb_jq` *(image: `nojson`)* | backend-chain links 2 and 3 on a box with no JSON tool at all: a jq **beside the hooks** and `KB_JQ=/path/to/jq` each make install + flush + recall work, and the installer propagates that jq into `~/.claude/hooks/` so the box survives losing `KB_JQ` | **496** |
| `23_windows_simulation` *(image: `winsim`)* | faked `uname -s`=MINGW + `cygpath` + a Win32 `timeout.exe`: hooks register as `bash.exe` + `args[0]` for all four events (never a bare `.sh`), a `C:\Users\First Last\...` transcript/cwd converts and submits, the Win32 `timeout` is probed-then-**refused** (recall still returns hits), and detach falls back to `nohup` — with `setsid` restored as the positive control | **1153**, **496**, **1152** |
| `24_paths_with_spaces` | a `HOME` **and** a project dir containing spaces: install, jq co-location, PreCompact flush, SessionEnd delta flush, offset file, per-prompt recall and pre-brief all work; `"kb hook rig"` slugifies to `kb-hook-rig` | — |
| `25_no_cygpath_fallback` *(image: `winsim`)* | Windows **without cygpath**: the pure-shell `C:\` -> `/c/` conversion keeps the flush working, `install.sh` still registers a spawnable command (`$EXEPATH`-derived, else bare `bash.exe`), plus unit assertions on `kb_winpath` extracted from the *installed* hook | **1153** |
| `26_portable_block_identity` | the shared portable block appears exactly once per script and is **byte-identical** across all four, is sourceable standalone, and its jq/python3 backends agree primitive by primitive (field read, object build incl. a file-sourced value, validation, URL encoding) | — |

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
| `winsim` | `setsid`; adds a writable `/c` and `/d` | `util-linux` is Essential (un-removable via apt), and a converted `C:\…` path must resolve to a real file |

Each variant **moves** the tool to `/rigbin` instead of deleting it, and the
image sets `RIG_JQ`/`RIG_PY` to those paths: `/rigbin` is on no `PATH`, so the
harness can still build fixtures and assert while the code under test can find
nothing. `lib.sh` therefore uses `$RIG_JQ`/`$RIG_PY` and never a bare `jq`.
A case that deliberately re-exposes a tool (case 22 copying a jq next to the
hooks, case 23's `expose_setsid` positive control) does it explicitly, in the
case source.

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

OPEN / not yet pinned: on a 57 KB transcript only the FIRST ~24 KB chunk landed
inside the wait window and the flush log was empty at that point, though the
submission had succeeded. Whether the remaining chunks relay on the next firing
(by design, per KB_CONNECTOR_MAX) or the detached relay was simply still in flight
when the container exited is UNRESOLVED — do not claim connector chunking works
until a case pins it.

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
