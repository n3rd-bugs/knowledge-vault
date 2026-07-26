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
bash tests/hookrig/run.sh --clean   # also delete the built image afterwards
KB_SKILL_DIR=contrib/claude/kb-session-flush bash tests/hookrig/run.sh
```

Exit status is 0 only when every case passes (`XFAIL` — a pinned known defect —
counts as green; see [Known defect](#known-defect-xfail)).

## What it is

| file | role |
| --- | --- |
| `Dockerfile` | clean debian-slim image: bash, curl, jq, python3, git, gzip/coreutils, setsid. **No** Claude Code, **no** `uv`, **no** `kb` CLI — the build fails if any appear. Non-root `tester` with its own `HOME`. |
| `stub_server.py` | dependency-free (stdlib) stub serving **both** transports and recording every request to JSONL. Decodes `transcript_gz` so assertions see the real payload bytes. |
| `stub_contract.py` | the submission contract the stub enforces, mirrored from `kbcore`/`kbrest` with the source line cited against each constant — so a drift between stub and server is a reviewable diff, not a silently permissive test. |
| `lib.sh` | assertions plus the fakes each transport precondition needs (`fake_uv`, `fake_claude`, `fake_claude_json`, `fake_kb_repo`) and the event builders. |
| `run.sh` | builds the image, starts the stub, runs each case in a **fresh** container, prints the PASS/FAIL table, cleans up. |
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
| `12_incomplete_config_silence` | **known defect**, see below | — |

## Known defect (XFAIL)

`12_incomplete_config_silence` asserts the behaviour we *want* and currently
fails, on purpose:

> `install.sh`'s REMOTE fallback writes `KB_REST_URL=https://api.example.com` and
> an **empty** `KB_TOKEN`, then tells the operator once, on stdout, to fill it in.
> Until they do, every flush falls through `flush-session-to-kb.sh:58` (which
> requires a non-empty `KB_TOKEN`), exits 0, and writes **nothing** to
> `flush-session.log`. The pre-brief hook's failure surfacing greps that same log,
> so it stays quiet too. The machine captures nothing, forever, with no signal
> anywhere. The same hole exists for `KB_MCP_URL` with an empty token.

It is marked `# EXPECT: xfail` in the case header, so the runner reports `XFAIL`
and the suite stays green. **When the hook is fixed the case starts passing, the
runner reports `XPASS` (a failure), and you delete the marker** — that is the
moment the KB fact gets corrected too.

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
