"""The capture funnel: the hook scripts that feed the queue, and the queue's
grouping of what they feed it.

Everything here guards a failure mode that was SILENT in production. The rig was
green, the logs said "ok bytes=", the queue accepted every row — and the session
identity on the wire had been the empty string on every machine for weeks,
because a variable was computed in the parent shell and never exported into the
detached child that used it. Nothing asserted the identity, so nothing noticed.

The tests are therefore of three kinds:

* **Structural** — derived mechanically from the script source, so they guard the
  whole CLASS of a defect rather than the one instance that was found (every
  variable a detached child reads must be exported; no credential may reach a
  curl command line).
* **Behavioural** — the real scripts, really executed, against a local stub or a
  recording ``curl`` on PATH. These are the tripwires: they assert what actually
  goes on the wire.
* **Queue-side** — real-DB tests for the coalescing key and the owner repair that
  recovers rows the missing identity already poisoned.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTRIB = REPO / "contrib" / "claude" / "kb-session-flush"
SHIPPED = REPO / "src" / "kbmcp" / "setup_assets"

FLUSH = CONTRIB / "flush-session-to-kb.sh"
BRIEF = CONTRIB / "kb-session-brief.sh"
INSTALL = CONTRIB / "install.sh"
NOTIFY = CONTRIB / "notify-complete.sh"
BACKFILL = CONTRIB / "kb-flush-backfill.sh"

# Fake, shaped like the real thing so the scrubber and every "is this a token?"
# assertion has something realistic to bite on. Never a live credential.
FAKE_PROJECT_TOKEN = "kbg_" + "a" * 43
FAKE_ENROL_CODE = "kbe_" + "b" * 28
FAKE_REDEEMED = "kbg_" + "c" * 43
FAKE_NTFY_TOKEN = "tk_" + "d" * 29


# ---------------------------------------------------------------------------
# Structural: the detached-child export contract
# ---------------------------------------------------------------------------
#
# THE BUG THIS EXISTS FOR. flush-session-to-kb.sh does its network work in
# `setsid bash -c '<single-quoted script>'`. That child is a FRESH shell: it
# inherits the ENVIRONMENT, not the parent's shell variables. A name the parent
# merely assigned expands to the empty string there — no error, no warning, and
# `set -u` in the parent cannot see it because the text is single-quoted.
#
# OKEY was such a name. It is the session identity, interpolated as
# "session:$OKEY", so every MCP/REST flush from every machine sent
# session_uri='session:'. Asserting "OKEY is exported" would only guard the
# instance; this derives the reference set from the script itself, so any FUTURE
# variable that slips out of an export list fails here.

_SHELL_PROVIDED = {
    # Set by the shell/OS in any child, so they need no export.
    "HOME", "PATH", "PWD", "OLDPWD", "IFS", "RANDOM", "LINENO", "SECONDS",
    "TMPDIR", "USER", "SHELL", "BASH_ENV", "EXEPATH", "BASH_SOURCE",
}  # fmt: skip

_VAR_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
# An assignment can follow `;`, `&&`, `||`, `then`, `do`, `else` or `(` as well as
# starting a line — `PART=$(...); KEEP=$((...))` is one line with two of them.
_ASSIGN = re.compile(
    r"(?:^|[;&|(]|\bthen\b|\bdo\b|\belse\b)\s*(?:export\s+|local\s+)?([A-Za-z_][A-Za-z0-9_]*)="
)
_EXPORT = re.compile(r"^\s*export\s+(.*)$")


def _detached_children(script: str) -> list[tuple[int, list[str], list[str]]]:
    """Every ``"$DETACH" bash -c '...'`` block: (start line, body, preceding lines)."""
    lines = script.splitlines()
    blocks: list[tuple[int, list[str], list[str]]] = []
    inside: set[int] = set()
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith('"$DETACH" bash -c'):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("' </dev/null"):
                j += 1
            blocks.append((i + 1, lines[i + 1 : j], lines[:i]))
            inside.update(range(i, j + 1))
            i = j
        i += 1
    # Drop export statements that live INSIDE an earlier child — they export into
    # that child's environment, not into a later sibling's.
    return [
        (start, body, [ln for n, ln in enumerate(pre) if n not in inside])
        for start, body, pre in blocks
    ]


def _exported_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    for raw in lines:
        m = _EXPORT.match(raw)
        if not m:
            continue
        rest = m.group(1)
        if rest.startswith("-f"):  # exported FUNCTIONS, a separate namespace
            continue
        for tok in rest.split():
            name = tok.split("=", 1)[0]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                names.add(name)
    return names


def _assigned_names(body: list[str]) -> set[str]:
    names: set[str] = set()
    for raw in body:
        names.update(_ASSIGN.findall(raw))
        # `set -- ...` / for-loops / read
        m2 = re.match(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", raw)
        if m2:
            names.add(m2.group(1))
        for m3 in re.finditer(r"\bread\s+(?:-r\s+)?([A-Za-z_][A-Za-z0-9_]*)", raw):
            names.add(m3.group(1))
    return names


def test_every_variable_a_detached_child_reads_is_exported():
    """The class-level guard. A name the child reads must be exported, assigned
    inside the child, or provided by the shell — anything else is empty at
    runtime, silently, which is exactly how session_uri became 'session:'."""
    script = FLUSH.read_text()
    blocks = _detached_children(script)
    assert len(blocks) >= 3, "the detached-child blocks are no longer recognisable"
    problems: list[str] = []
    for start, body, preceding in blocks:
        available = _exported_names(preceding) | _assigned_names(body) | _SHELL_PROVIDED
        referenced = {n for ln in body for n in _VAR_REF.findall(ln)}
        missing = sorted(referenced - available)
        if missing:
            problems.append(f"child at line {start}: {missing}")
    assert not problems, (
        "variables are read inside a DETACHED child but never exported into it, so "
        "they expand to the empty string on every machine: " + "; ".join(problems)
    )


def test_the_session_identity_is_exported_and_guarded():
    """The specific instance, pinned by name as well as by class — it is the one
    that cost six projects a cross-project extraction."""
    script = FLUSH.read_text()
    assert re.search(r"^\s*export .*\bOKEY\b", script, re.M), (
        "OKEY (the session identity) is not exported — the detached child will "
        "stamp every chunk with 'session:'"
    )
    assert '[ -n "${OKEY:-}" ] && set -- "$@" session_uri s "session:$OKEY"' in script, (
        "the child no longer refuses to put a degenerate session_uri on the wire"
    )


def _connector_child(script: str) -> list[str]:
    """The body of the ``claude -p`` relay child, identified by CLAUDE_BIN."""
    blocks = [b for b in _detached_children(script) if any("CLAUDE_BIN" in ln for ln in b[1])]
    assert len(blocks) == 1, f"expected exactly one connector relay child, found {len(blocks)}"
    return blocks[0][1]


def test_the_export_class_guard_actually_covers_the_connector_child():
    """The class test asserts ``>= 3`` detached children, which stays true even if
    the connector block stops being recognised (its opener changes, its terminator
    moves). Name it explicitly, or the one transport whose relay is a MODEL could
    drift out of the guard without a single test going red."""
    blocks = _detached_children(FLUSH.read_text())
    assert any(any("CLAUDE_BIN" in ln for ln in body) for _s, body, _p in blocks), (
        "no detached child mentions CLAUDE_BIN — the connector relay is no longer "
        "covered by the unexported-variable guard"
    )


def test_the_connector_relay_names_the_identity_and_git_fields():
    """THE DEFECT. OKEY/REPO/BRANCH/COMMIT were already exported into the relay
    child; the prompt simply never mentioned them, so every connector chunk
    reached the queue with no source_uri and no code_ref anchor — one 14B
    extraction per 24 KB instead of one per session. The export-class guard cannot
    see this: an exported-but-unused variable is not a missing one."""
    body = "\n".join(_connector_child(FLUSH.read_text()))
    for name in ("OKEY", "REPO", "BRANCH", "COMMIT"):
        assert f"${name}" in body or f"${{{name}" in body, (
            f"the connector relay never reads {name} — a connector session will "
            "fragment into degenerate singleton rows"
        )
    assert '[ -n "${OKEY:-}" ] && set -- "$@" session_uri s "session:$OKEY"' in body, (
        "the connector child no longer builds the session identity the way the "
        "bearer branches do (and so may put a bare 'session:' on the wire)"
    )
    for field in ("repo", "branch", "commit"):
        assert f'set -- "$@" {field} s ' in body, f"the connector relay omits {field}"


def test_the_connector_relay_lets_the_json_encoder_do_the_escaping():
    """The prompt is assembled in shell and has to survive being turned back into
    JSON by a model. A repo directory or branch holding a space, a quote or a
    backslash would break a hand-written ``key='value'`` instruction, so the
    fields go through the same builder the bearer branches use."""
    body = "\n".join(_connector_child(FLUSH.read_text()))
    assert "kb_json_build -" in body, (
        "the connector relay hand-writes its extra arguments instead of letting "
        "kb_json_build escape them"
    )
    assert "$METAI" in body, "the built object never reaches the relay prompt"


# ---------------------------------------------------------------------------
# Structural: no credential on a command line
# ---------------------------------------------------------------------------


def _executed_curl_lines(script: str) -> list[str]:
    """Lines that INVOKE curl, excluding comments and printed instructions."""
    out = []
    for raw in script.splitlines():
        line = raw.strip()
        if line.startswith("#") or not line:
            continue
        if line.startswith(("printf ", "echo ")):  # user-facing text, not an invocation
            continue
        if re.search(r"(^|[|&;(\s])curl\b", line):
            out.append(line)
    return out


@pytest.mark.parametrize("path", [NOTIFY, BRIEF, INSTALL], ids=lambda p: p.name)
def test_no_credential_is_passed_on_a_curl_command_line(path: Path):
    """argv is world-readable (/proc/<pid>/cmdline, `ps`). An ntfy admin password
    already leaked into a transcript this way — and a transcript is precisely
    what these hooks flush into the KB. Credentials travel on stdin via
    `curl -K -`, never as arguments."""
    offenders = [
        ln
        for ln in _executed_curl_lines(path.read_text())
        if re.search(r"(^|\s)-u\s", ln) or "Authorization:" in ln
    ]
    assert not offenders, f"{path.name} puts a credential in argv: {offenders}"


@pytest.mark.parametrize("path", [NOTIFY, BRIEF, INSTALL], ids=lambda p: p.name)
def test_credentialed_requests_read_their_auth_from_stdin(path: Path):
    """The other half: having removed the argv form, the scripts must still
    authenticate. Every one of them builds a curl config and pipes it in."""
    text = path.read_text()
    assert "-K -" in text, f"{path.name} no longer feeds curl a config on stdin"
    assert 'header = "Authorization: Bearer' in text, (
        f"{path.name} builds no Authorization config — its requests are anonymous"
    )


def test_the_stale_hook_check_runs_after_the_token_resolver():
    """Ordering IS the bug. The version check is bearer-authenticated; sitting
    above the per-project token resolver it read a bare $KB_TOKEN under `set -u`,
    which on a default-enrolled box (no shared KB_TOKEN anywhere) killed its own
    subshell on 'unbound variable'. The warning could then never fire, which is
    how KB item 1193's bug came back in a second place."""
    text = BRIEF.read_text()
    resolver = text.index('PROJ_TOKEN=$(eval "printf')
    check = text.index("/api/setup/manifest.json")
    assert resolver < check, (
        "the stale-hook version check runs BEFORE the per-project token resolver "
        "again — on a default-enrolled box it will have no bearer at all"
    )
    assert '-H "Authorization: Bearer $KB_TOKEN"' not in _joined(_executed_curl_lines(text)), (
        "the version check still interpolates a bare $KB_TOKEN"
    )


def _joined(lines: list[str]) -> str:
    return "\n".join(lines)


def test_install_names_the_credential_it_fetches_with():
    """The manifest/notify fetches used to fall back to $KB_ENROL_CODE — a code
    the redemption POST directly above has already SPENT (enrolment is single
    use), so they authenticated with a dead credential and every enrolled box
    ended up with no version stamp and no notify.env. The token the run just
    redeemed is the one credential it is guaranteed to hold."""
    text = INSTALL.read_text()
    assert 'KB_SETUP_BEARER="$ENROL_TOK"' in text, (
        "install.sh does not carry the redeemed token in-process"
    )
    assert "${KB_SETUP_BEARER:-${KB_TOKEN:-${KB_ENROL_CODE:-}}}" in text, (
        "the setup-fetch bearer chain no longer prefers the redeemed credential"
    )


@pytest.mark.parametrize(
    "name",
    [
        "flush-session-to-kb.sh",
        "kb-session-brief.sh",
        "install.sh",
        "notify-complete.sh",
        "kb-flush-backfill.sh",
    ],
)
def test_contrib_and_shipped_copies_stay_identical(name: str):
    """contrib/ is canonical, setup_assets/ is what the container serves. A box
    that fetches from /api/setup/ must get the script that was tested."""
    assert (CONTRIB / name).read_bytes() == (SHIPPED / name).read_bytes(), (
        f"{name} drifted from its setup_assets twin — run `make sync-assets`"
    )


# ---------------------------------------------------------------------------
# Structural: an asset that is shipped but not listed is an asset no remote box
# can ever get
# ---------------------------------------------------------------------------
#
# THE GAP CLASS. notify-complete.sh and turn-start.sh existed, were installed by
# install.sh and worked on the owner's box for weeks — while every REMOTE box got
# no notifications at all, because the two scripts were missing from the REST
# allowlist and from the bootstrap fetch loop. Nothing failed; the files simply
# were not there. The set of served assets and the set of names in every fetch
# list have to be the same set, and the only way to keep them so is to derive
# both mechanically.

_ASSET_LISTS = {
    "kbrest allowlist": (REPO / "src/kbrest/server.py", "_SETUP_SCRIPTS"),
    "hookrig stub": (REPO / "tests/hookrig/stub_server.py", "SETUP_SCRIPTS"),
}
_FETCH_LOOPS = {
    "kb_setup bootstrap block": REPO / "src/kbmcp/tools_setup.py",
    "rig case 00 (skill completeness)": REPO / "tests/hookrig/cases/00_hermetic_isolation.sh",
    "rig case 17 (enrolment bootstrap)": REPO / "tests/hookrig/cases/17_enrolment_bootstrap.sh",
}


def _frozenset_literal(text: str, name: str) -> str:
    """The braced body of ``<name> = frozenset({...})``, so a mention of a script
    in a nearby comment cannot pass for membership."""
    i = text.index(f"{name} = frozenset(")
    j = text.index("{", i)
    depth = 0
    for k in range(j, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[j : k + 1]
    raise AssertionError(f"{name} literal is unterminated")


def _fetch_loop_bodies(text: str) -> str:
    bodies = re.findall(r"for f in (.*?);\s*do", text, re.S)
    assert bodies, "no `for f in ...; do` asset loop found"
    return "\n".join(bodies)


@pytest.mark.parametrize("asset", sorted(p.name for p in SHIPPED.glob("*.sh")))
def test_every_served_asset_appears_in_every_fetch_and_allowlist(asset: str):
    """One parametrised case per shipped script, across every list that has to
    name it. A new asset that lands in setup_assets/ and nowhere else fails here
    instead of failing silently on someone else's machine."""
    missing = []
    for label, (path, symbol) in _ASSET_LISTS.items():
        if f'"{asset}"' not in _frozenset_literal(path.read_text(), symbol):
            missing.append(label)
    for label, path in _FETCH_LOOPS.items():
        if asset not in _fetch_loop_bodies(path.read_text()):
            missing.append(label)
    assert not missing, (
        f"{asset} is served from setup_assets/ but is not named in: {', '.join(missing)} "
        "— a remote box will never receive it, and nothing will report that"
    )


# ---------------------------------------------------------------------------
# Structural: the backfill sweep only ever FEEDS THE HOOK
# ---------------------------------------------------------------------------
#
# kb-flush-backfill.sh walks every transcript on a box and pushes each one's
# unflushed remainder into the KB. The whole safety argument for it is that it
# owns none of the transport: offsets, chunking, the per-project token, the
# scrubber and the wire format all stay in flush-session-to-kb.sh, and the sweep
# only decides WHICH transcripts still have bytes and hands the hook an event.
# A second implementation of the submission — even a small one — would be a
# second place for 'session:' to be reinvented, on the one path that touches
# EVERY project on the machine at once.


def _backfill_code() -> str:
    """The sweep's OWN executable lines: the shared portable block removed (it is
    tested as one unit elsewhere) and comment lines dropped — so a script merely
    NAMED in a comment can never look like a script being CALLED."""
    text = BACKFILL.read_text()
    start = text.index("# >>> KB PORTABLE BLOCK")
    end = text.index("# <<< END KB PORTABLE BLOCK")
    own = text[:start] + text[end:]
    return "\n".join(ln for ln in own.splitlines() if not ln.lstrip().startswith("#"))


def test_the_backfill_never_puts_anything_on_the_wire_itself():
    """No socket, no credential, no request body. If any of these ever appear,
    the sweep has grown its own transport and the guarantees above are void."""
    text = _backfill_code()
    assert not _executed_curl_lines(text), "the backfill invokes curl directly"
    for forbidden in (
        "KB_TOKEN",
        "KB_MCP_URL",
        "KB_REST_URL",
        "kb_ingest_session",
        "kb-mcp-call.sh",
        "transcript_gz",
        "session_uri",
    ):
        assert forbidden not in text, (
            f"the backfill references {forbidden} — it is reimplementing the "
            "submission instead of feeding the hook"
        )


def test_the_only_json_the_backfill_builds_is_the_HOOK_EVENT():
    """And it is built by kb_json_build, not by hand: a cwd or session id holding
    a quote, a backslash or a newline has to reach the hook exactly as Claude
    Code would have delivered it."""
    body = _backfill_code()
    calls = re.findall(r"kb_json_build\b(?:[^\n]*\\\n)*[^\n]*", body)
    assert len(calls) == 1, f"expected exactly one JSON object to be built, found {len(calls)}"
    call = " ".join(calls[0].split())
    for field in ("transcript_path", "cwd", "session_id", "hook_event_name"):
        assert f"{field} s " in call, f"the hook event omits {field}: {call}"
    assert "SessionEnd" in call, "the event is not SessionEnd-shaped"
    for wire_key in ("project", "transcript ", "kind", "repo", "branch", "commit"):
        assert wire_key not in call, (
            f"the built object carries {wire_key!r} — that is a SUBMISSION, not a hook event"
        )
    assert not re.search(r"""printf\s+['"]\{""", body), "the backfill hand-writes JSON somewhere"


def test_the_backfill_fires_the_REAL_hook_and_waits_for_it():
    """Sequential, foreground, and pointed at the installed hook. Backgrounding
    the firings would put N detached drains on one box, racing for the same
    per-session locks — the very thing the hook's own lock exists to prevent."""
    text = BACKFILL.read_text()
    assert 'BF_HOOK="${KB_BACKFILL_HOOK:-$HOOKS/flush-session-to-kb.sh}"' in text, (
        "the sweep no longer defaults to the installed flush hook"
    )
    fire = [ln for ln in text.splitlines() if 'bash "$BF_HOOK"' in ln]
    assert len(fire) == 1, f"expected exactly one hook firing, found {fire}"
    assert fire[0].rstrip().endswith("2>&1"), f"the firing was changed: {fire[0]}"
    assert not fire[0].rstrip().endswith("&"), "the sweep backgrounds the hook instead of waiting"


def test_the_backfill_clears_the_hook_recursion_guard():
    """KB_HOOK_GUARD is the hooks' anti-recursion flag and the flush hook exits 0
    on sight of it. The sweep is invoked by a PERSON, quite possibly from inside a
    Claude Code session that exported it — inheriting it would make every firing
    a silent no-op, which is the exact failure the sweep exists to repair."""
    assert re.search(r"^unset KB_HOOK_GUARD$", BACKFILL.read_text(), re.M), (
        "the backfill no longer clears KB_HOOK_GUARD"
    )


def _portable_block(path: Path) -> str:
    text = path.read_text()
    start = text.index("# >>> KB PORTABLE BLOCK")
    end = text.index("# <<< END KB PORTABLE BLOCK")
    return text[start:end]


def test_the_backfill_carries_the_SAME_portable_block():
    """Shell has no import, so the block is duplicated verbatim — which is only
    safe while a test proves the copies match. Rig case 26 diffs four copies and
    does not know about this fifth one; case 27 does, but the rig needs docker.
    This is the copy of that check that runs in the ordinary suite."""
    assert _portable_block(BACKFILL) == _portable_block(FLUSH), (
        "kb-flush-backfill.sh's portable block has drifted from the hooks' — the "
        "platform/timeout/JSON primitives are no longer defined once"
    )


def test_the_backfill_exports_whatever_any_detached_child_would_read():
    """The OKEY class guard, applied here BEFORE there is anything to guard. The
    sweep has no detached child today; the day one is added (a parallel mode, a
    background reporter) it inherits only the environment, and a name that was
    merely assigned expands to the empty string with no error at all."""
    problems: list[str] = []
    for start, body, preceding in _detached_children(BACKFILL.read_text()):
        available = _exported_names(preceding) | _assigned_names(body) | _SHELL_PROVIDED
        missing = sorted({n for ln in body for n in _VAR_REF.findall(ln)} - available)
        if missing:
            problems.append(f"child at line {start}: {missing}")
    assert not problems, (
        "the backfill reads variables inside a DETACHED child without exporting "
        "them, so they are empty at runtime: " + "; ".join(problems)
    )


# ---------------------------------------------------------------------------
# Behavioural: a real hook run, with the payload inspected on the wire
# ---------------------------------------------------------------------------


class _Recorder:
    """A tiny stub KB. Records every request; answers ingest with a queue id."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):  # keep pytest output clean
                pass

            def _read(self) -> bytes:
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_POST(self):  # noqa: N802
                raw = self._read()
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except ValueError:
                    payload = {}
                with recorder.lock:
                    recorder.requests.append(
                        {
                            "path": self.path,
                            "bearer": self.headers.get("Authorization", ""),
                            "payload": payload,
                        }
                    )
                body = json.dumps({"queue_id": 1, "status": "queued"}).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Recorder:
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def wait(self, n: int = 1, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.requests) >= n:
                    return True
            time.sleep(0.1)
        return False


def _hook_home(tmp_path: Path) -> Path:
    """A throwaway HOME. Belt and braces: the hooks write into ~/.claude, and a
    test that leaked into the developer's real one would rewrite live hooks."""
    home = tmp_path / "home"
    assert str(home) != os.path.expanduser("~")
    (home / ".claude" / "hooks").mkdir(parents=True)
    return home


def _transcript(path: Path, lines: int = 20) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(lines):
            fh.write(json.dumps({"type": "user", "i": i, "text": f"line {i} of the session"}))
            fh.write("\n")
    return path


def _run_hook(script: Path, event: dict, home: Path, extra_env: dict | None = None):
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env=env,
        timeout=90,
    )


requires_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")


@requires_curl
def test_a_flushed_session_arrives_with_a_NON_DEGENERATE_identity(tmp_path):
    """THE TRIPWIRE. Nothing in the rig or the suite ever looked at the session
    identity that reached the server, so 'session:' — a wildcard grouping key
    that coalesced six projects into one extraction — travelled unnoticed on
    every flush from every machine. Assert the value, not merely that a request
    arrived."""
    home = _hook_home(tmp_path)
    work = tmp_path / "myproj"
    transcript = _transcript(work / "t.jsonl")
    with _Recorder() as rec:
        (home / ".claude" / "kb-flush.env").write_text(
            f"KB_REST_URL={rec.url}\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n"
        )
        res = _run_hook(
            FLUSH,
            {
                "transcript_path": str(transcript),
                "cwd": str(work),
                "session_id": "abc123-session",
                "hook_event_name": "SessionEnd",
            },
            home,
        )
        assert res.returncode == 0, res.stderr
        assert rec.wait(1), "no submission reached the stub; flush log: " + (
            home / ".claude/hooks/flush-session.log"
        ).read_text(errors="replace")
        payload = rec.requests[0]["payload"]

    uri = payload.get("session_uri")
    assert uri, "the flush sent NO session identity at all"
    assert uri not in ("session:", "session::"), (
        f"the flush sent a DEGENERATE session identity {uri!r} — every session that "
        "ever sends one coalesces with every other, across projects"
    )
    assert uri == "session:abc123-session", f"unexpected session identity {uri!r}"
    assert payload.get("project") == "myproj"


@requires_curl
def test_the_flush_bearer_never_appears_in_the_hook_log(tmp_path):
    """The pre-brief lifts flush-log lines into the next session's context, and
    the flush hook writes that log — a credential landing there would be read
    back into a transcript and captured into the KB."""
    home = _hook_home(tmp_path)
    work = tmp_path / "logproj"
    transcript = _transcript(work / "t.jsonl")
    with _Recorder() as rec:
        (home / ".claude" / "kb-flush.env").write_text(
            f"KB_REST_URL={rec.url}\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n"
        )
        _run_hook(
            FLUSH,
            {
                "transcript_path": str(transcript),
                "cwd": str(work),
                "session_id": "s-log",
                "hook_event_name": "SessionEnd",
            },
            home,
        )
        assert rec.wait(1)
    log = (home / ".claude/hooks/flush-session.log").read_text(errors="replace")
    assert FAKE_PROJECT_TOKEN not in log


# ---------------------------------------------------------------------------
# Behavioural: the CONNECTOR relay, whose wire format is a prompt
# ---------------------------------------------------------------------------
#
# CONNECTOR is the zero-config transport: no bearer, no URL, just the claude.ai
# connector bridged into Claude Code. Its data path is a headless `claude -p`, so
# "what goes on the wire" is the text of a prompt — and for a long time that text
# named only project/kind/transcript. Every chunk therefore reached the queue with
# no session identity and no git context: N degenerate singleton rows, N separate
# 14B extractions, nothing anchored to code. Exactly the shape the OKEY export
# closed for MCP/REST, left open on the one transport nothing asserted against.
#
# CLAUDE_BIN is just a path to an executable, so the relay is exercisable with a
# recorder standing in for the model — no egress, no Claude usage.

_RELAY_RECORDER = r"""#!/usr/bin/env bash
# Stand-in for the headless `claude -p` relay. Records the chunk it was handed on
# stdin and its argv (NUL-separated, so a prompt containing spaces or newlines is
# recovered exactly), then answers the way the model is asked to: a bare queue id.
D="$RELAY_REC"
mkdir -p "$D"
N=$(( $(cat "$D/.n" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$N" >"$D/.n"
cat >"$D/chunk.$N"
: >"$D/argv.$N"
for a in "$@"; do printf '%s\0' "$a" >>"$D/argv.$N"; done
echo $((4000 + N))
"""

_META_MARKER = "EXTRA ARGUMENTS (JSON): "

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _relay_recorder(home: Path, rec_dir: Path) -> Path:
    binp = home / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    relay = binp / "claude"
    relay.write_text(_RELAY_RECORDER)
    relay.chmod(0o755)
    rec_dir.mkdir(parents=True, exist_ok=True)
    return relay


def _wait_relay(rec_dir: Path, n: int = 1, timeout: float = 60.0) -> bool:
    """The relay runs in the DETACHED child, so it lands after the hook exits."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if int((rec_dir / ".n").read_text()) >= n:
                return True
        except (FileNotFoundError, ValueError):
            pass
        time.sleep(0.1)
    return False


def _relay_prompt(rec_dir: Path, n: int = 1) -> str:
    args = [a.decode("utf-8") for a in (rec_dir / f"argv.{n}").read_bytes().split(b"\0") if a]
    assert "-p" in args, f"the relay was not invoked in headless print mode: {args}"
    return args[args.index("-p") + 1]


def _relay_meta(prompt: str) -> dict:
    """The extra tool arguments, parsed back out of the prompt.

    Parsing is the point: the hook interpolates shell values into natural
    language, and the model has to turn them back into a JSON tool call. If the
    object does not survive ``json.loads`` here, it will not survive the model
    either — which is how a repo path with a quote in it would silently strip the
    session identity off every chunk.
    """
    if _META_MARKER not in prompt:
        return {}
    obj, _end = json.JSONDecoder().raw_decode(
        prompt, prompt.index(_META_MARKER) + len(_META_MARKER)
    )
    return obj


def _connector_home(tmp_path: Path) -> tuple[Path, Path]:
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "relay"
    relay = _relay_recorder(home, rec_dir)
    (home / ".claude" / "kb-flush.env").write_text(f"KB_CONNECTOR=1\nCLAUDE_BIN={relay}\n")
    return home, rec_dir


def _git(path: Path, *args: str, **kw) -> subprocess.CompletedProcess:
    identity = ["-c", "user.email=rig@example.invalid", "-c", "user.name=rig"]
    return subprocess.run(
        ["git", "-C", str(path), *identity, "-c", "commit.gpgsign=false", *args],
        capture_output=True,
        text=True,
        check=True,
        **kw,
    )


def _git_repo(path: Path, branch: str) -> str:
    """A real repo on a named branch with one commit -> a real repo/branch/commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return _git(path, "rev-parse", "--short", "HEAD").stdout.strip()


@requires_git
def test_a_connector_flush_conveys_the_session_identity_and_git_context(tmp_path):
    """THE TRIPWIRE for this transport. The repo name and branch are deliberately
    hostile — a space, an apostrophe — because the fields travel through a
    natural-language instruction assembled in shell. Asserting the parsed VALUES
    (not merely that the words appear) is what proves the escaping holds."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "we ird's repo"
    sha = _git_repo(work, "feat/o'brien")
    _transcript(work / "t.jsonl")

    res = _run_hook(
        FLUSH,
        {
            "transcript_path": str(work / "t.jsonl"),
            "cwd": str(work),
            "session_id": "conn-abc123",
            "hook_event_name": "SessionEnd",
        },
        home,
        {"RELAY_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr
    assert _wait_relay(rec_dir), "the connector relay was never invoked; flush log: " + (
        home / ".claude/hooks/flush-session.log"
    ).read_text(errors="replace")

    meta = _relay_meta(_relay_prompt(rec_dir))
    assert meta.get("session_uri") == "session:conn-abc123", (
        f"the connector relay carried {meta.get('session_uri')!r} — a chunk with no "
        "identity is extracted as its own singleton, so the session fragments"
    )
    assert meta.get("repo") == "we ird's repo", f"repo did not survive escaping: {meta!r}"
    assert meta.get("branch") == "feat/o'brien", f"branch did not survive escaping: {meta!r}"
    assert meta.get("commit") == sha


@requires_git
def test_every_connector_chunk_of_one_session_carries_the_same_identity(tmp_path):
    """A session relayed as N chunks must be ONE cohort. A per-chunk (or missing)
    identity is what turned a large session into N unrelated 14B extractions."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "chunky"
    _git_repo(work, "main")
    _transcript(work / "t.jsonl", lines=400)

    res = _run_hook(
        FLUSH,
        {
            "transcript_path": str(work / "t.jsonl"),
            "cwd": str(work),
            "session_id": "multi-chunk",
            "hook_event_name": "SessionEnd",
        },
        home,
        {"RELAY_REC": str(rec_dir), "KB_CONNECTOR_CHUNK": "4000"},
    )
    assert res.returncode == 0, res.stderr
    assert _wait_relay(rec_dir, 3), "the transcript was not relayed in several chunks"
    time.sleep(1.5)
    n = int((rec_dir / ".n").read_text())
    assert n >= 3
    uris = {_relay_meta(_relay_prompt(rec_dir, i)).get("session_uri") for i in range(1, n + 1)}
    assert uris == {"session:multi-chunk"}, f"chunks disagreed about the session: {uris}"


def test_a_connector_flush_without_git_sends_NO_empty_keys(tmp_path):
    """kbcore normalises a degenerate identity away at enqueue, and the hook must
    not lean on that. A field with nothing to say is OMITTED — not sent blank for
    someone downstream to remember to ignore."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "plaindir"  # deliberately not a git repo
    _transcript(work / "t.jsonl")

    res = _run_hook(
        FLUSH,
        {
            "transcript_path": str(work / "t.jsonl"),
            "cwd": str(work),
            "session_id": "s-nogit",
            "hook_event_name": "SessionEnd",
        },
        home,
        {"RELAY_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr
    assert _wait_relay(rec_dir)

    prompt = _relay_prompt(rec_dir)
    meta = _relay_meta(prompt)
    assert meta == {"session_uri": "session:s-nogit"}, (
        f"an absent git context was sent as empty keys instead of omitted: {meta!r}"
    )
    assert '"session:"' not in prompt, "a degenerate identity reached the relay prompt"


# ---------------------------------------------------------------------------
# Behavioural: credentials must not reach argv (recording curl on PATH)
# ---------------------------------------------------------------------------

_RECORDING_CURL = r"""#!/usr/bin/env bash
# A `curl` that records the argv it was given and the config it was fed on
# stdin, then answers from a canned reply table. Never talks to a network.
D="$CURL_REC"
mkdir -p "$D"
N=$(( $(cat "$D/.n" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$N" >"$D/.n"
printf '%s\n' "$*" >"$D/argv.$N"
: >"$D/stdin.$N"
for a in "$@"; do
  if [ "$a" = "-K" ]; then cat >"$D/stdin.$N"; break; fi
done
URL=""
for a in "$@"; do case "$a" in http://*|https://*) URL="$a" ;; esac; done
printf '%s\n' "$URL" >"$D/url.$N"
case "$URL" in
  *"/api/enrol") printf '%s' "$CURL_ENROL_REPLY" ;;
  *"/api/setup/manifest.json")
    printf '{"version":"%s","files":{},"count":0}' "${CURL_VERSION:-deadbeef}" ;;
  *"/api/setup/notify.env") exit 22 ;;
  *) : ;;
esac
exit 0
"""


def _recording_curl(home: Path, rec_dir: Path) -> Path:
    binp = home / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    curl = binp / "curl"
    curl.write_text(_RECORDING_CURL)
    curl.chmod(0o755)
    rec_dir.mkdir(parents=True, exist_ok=True)
    return binp


def _recorded(rec_dir: Path, kind: str) -> list[str]:
    n = int((rec_dir / ".n").read_text()) if (rec_dir / ".n").exists() else 0
    return [(rec_dir / f"{kind}.{i}").read_text() for i in range(1, n + 1)]


def test_the_notify_hook_keeps_its_credential_off_the_command_line(tmp_path):
    """An ntfy admin password leaked into a transcript through `-u` on a curl
    command line once already. Every credentialed request in this hook now feeds
    curl its auth on stdin."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    (home / ".claude" / "notify.env").write_text(
        f"NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=some-topic\nNTFY_TOKEN={FAKE_NTFY_TOKEN}\n"
    )
    res = _run_hook(
        NOTIFY,
        {"session_id": "s1", "cwd": str(tmp_path / "proj"), "hook_event_name": "Stop"},
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr
    argvs = _recorded(rec_dir, "argv")
    stdins = _recorded(rec_dir, "stdin")
    assert argvs, "the notify hook never invoked curl"
    assert not any(FAKE_NTFY_TOKEN in a for a in argvs), (
        f"the ntfy credential is on the curl command line: {argvs}"
    )
    assert any(FAKE_NTFY_TOKEN in s for s in stdins), (
        "the credential is not on argv, but it is not on stdin either — the push "
        "is now unauthenticated"
    )


def test_the_notify_hook_keeps_basic_auth_off_the_command_line_too(tmp_path):
    """The owner box uses NTFY_USER/NTFY_PASSWORD, which is the pair that
    actually leaked. `-u user:pass` is argv; `user = "..."` is stdin."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    password = "s3cr3t-" + "e" * 20
    (home / ".claude" / "notify.env").write_text(
        "NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=some-topic\n"
        f"NTFY_USER=admin\nNTFY_PASSWORD={password}\n"
    )
    res = _run_hook(
        NOTIFY,
        {"session_id": "s2", "cwd": str(tmp_path / "proj"), "hook_event_name": "Stop"},
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr
    argvs = _recorded(rec_dir, "argv")
    assert not any(password in a for a in argvs), f"the password is in argv: {argvs}"
    assert not any(re.search(r"(^|\s)-u\s", a) for a in argvs), (
        "curl was invoked with -u, which puts user:pass in /proc/<pid>/cmdline"
    )
    assert any(password in s for s in _recorded(rec_dir, "stdin"))


# ---------------------------------------------------------------------------
# Behavioural: "how much of this session is in the KB?" in the completion ping
# ---------------------------------------------------------------------------
#
# THE MISATTRIBUTION CLASS. The obvious source for "bytes flushed" is the shared
# flush log — and it is the wrong one: every session on the box appends its
# "ok bytes=" lines there, so the newest line usually belongs to a NEIGHBOUR.
# The backfill sweep shipped with exactly that bug. The number must come from
# this session's OWN offset file, whose name IS the session identity, and the
# tests below plant neighbour evidence to prove nothing else is consulted.
#
# The other half is knowing what cannot be known: LOCAL-mode flushes never write
# an offset, and the flush hook DELETES the offset after a terminal drain. A
# missing number is reported by SAYING NOTHING, never by printing a zero or a
# question mark that a human would read as "the session was lost".

# 5452595 B / 1048576 = 5.19… -> "5.2MB"; 4299161 B -> "4.1MB". Hardcoded on
# purpose: these pin the human formatting (shared unit, one decimal at MB), not
# merely the arithmetic.
_KB_SIZE = 5452595
_KB_FLUSHED = 4299161


def _notify_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A throwaway HOME with a recording curl and a configured ntfy channel."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    # NTFY_MACHINE pins the one part of the message that would otherwise be this
    # developer's hostname — the "no kb segment" assertions read the whole body.
    (home / ".claude" / "notify.env").write_text(
        "NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=some-topic\nNTFY_MACHINE=testbox\n"
    )
    (home / ".claude" / "hooks" / "state").mkdir(parents=True, exist_ok=True)
    return home, rec_dir, binp


def _run_notify(home: Path, binp: Path, rec_dir: Path, event: dict):
    return _run_hook(
        NOTIFY,
        event,
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
    )


def _notify_message(rec_dir: Path) -> str:
    """The exact notification body, lifted back out of the recorded curl argv."""
    argvs = _recorded(rec_dir, "argv")
    assert argvs, "the notify hook never invoked curl"
    m = re.search(r"-d (.*?) https?://", argvs[0])
    assert m, f"no -d payload in the recorded argv: {argvs[0]!r}"
    return m.group(1)


def _okey_pipeline(script: Path, var: str) -> str:
    """The offset-key derivation AS WRITTEN in <script>, as a runnable pipeline.

    Derived from the source rather than restated, so the two scripts are compared
    against each other and never against this test's idea of the sanitisation.
    """
    m = re.search(
        rf"""{var}=\$\(printf '%s' "\$\{{SID:-\$\w+\}}"\s*(\|[^)]*)\)""",
        script.read_text(),
    )
    assert m, f"the {var} derivation in {script.name} no longer has the expected shape"
    return m.group(1)


def _flush_okey(sid: str) -> str:
    """The offset-file key for <sid>, computed by the FLUSH hook's own code."""
    pipeline = _okey_pipeline(FLUSH, "OKEY")
    out = subprocess.run(
        ["bash", "-c", f'printf %s "$SID" {pipeline}'],
        env={"SID": sid, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def _sized_transcript(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "user", "text": "x" * 200}) + "\n"
    path.write_text((line * (size // len(line) + 1))[:size], encoding="utf-8")
    assert path.stat().st_size == size
    return path


def test_the_notify_message_reports_this_sessions_own_flushed_bytes(tmp_path):
    """Offset file present -> "kb <flushed>/<size>", read from THIS session's
    checkpoint. A neighbour's finished flush is planted in the shared log and in
    the ledger; neither may influence the number."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-mine')}.offset").write_text(str(_KB_FLUSHED))
    # A neighbour session that IS fully flushed, in both shared records.
    (state / f"{_flush_okey('s-neighbour')}.offset").write_text(str(_KB_SIZE))
    (state / ".backfill-done").write_text(f"{_flush_okey('s-neighbour')}\t{_KB_SIZE}\n")
    (home / ".claude" / "hooks" / "flush-session.log").write_text(
        "2026-08-04T09:00:00+00:00 flush other ok bytes=5452595 (offset 5452595/5452595)\n"
    )

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-mine",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert "kb 4.1/5.2MB" in msg, f"the flushed/total figure is wrong or absent: {msg!r}"
    assert msg.endswith("(kb 4.1/5.2MB)"), f"unexpected message shape: {msg!r}"


def test_kb_progress_survives_a_transcript_path_with_spaces(tmp_path):
    """Claude Code's project dirs are derived from real working directories, and
    those have spaces in them. An unquoted path here would stat the wrong file
    (or nothing) and silently drop the figure."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "my project dir"
    transcript = _sized_transcript(work / "a session.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-spaces')}.offset").write_text(str(_KB_FLUSHED))

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-spaces",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    assert "kb 4.1/5.2MB" in _notify_message(rec_dir)


def test_a_fully_drained_session_reports_synced_from_the_backfill_ledger(tmp_path):
    """The flush hook DELETES the offset after a terminal SessionEnd drain, so
    "no offset file" does not mean "nothing was captured". The backfill ledger is
    the durable record that survives it — a hit for THIS key means fully in."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    assert not (state / f"{_flush_okey('s-done')}.offset").exists()
    (state / ".backfill-done").write_text(
        f"{_flush_okey('s-other')}\t42\n{_flush_okey('s-done')}\t{_KB_SIZE}\n"
    )

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-done",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert "kb 5.2MB synced" in msg, f"a fully drained session was not reported: {msg!r}"


def test_the_kb_part_is_OMITTED_when_this_session_has_no_state_of_its_own(tmp_path):
    """LOCAL-mode flushes write no offset at all. With no checkpoint and no
    ledger entry for THIS session the honest report is silence — and the
    neighbour evidence sitting right next to it must not be borrowed."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-neighbour')}.offset").write_text(str(_KB_SIZE))
    (state / ".backfill-done").write_text(f"{_flush_okey('s-neighbour')}\t{_KB_SIZE}\n")
    (home / ".claude" / "hooks" / "flush-session.log").write_text(
        "2026-08-04T09:00:00+00:00 flush other ok bytes=5452595 (offset 5452595/5452595)\n"
    )

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-unknown",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert "kb" not in msg, (
        f"a neighbour session's bytes were attributed to this one: {msg!r} — this is "
        "the misattribution the backfill sweep already had to fix"
    )
    assert msg == "Claude finished - proj @ testbox", f"the base message changed: {msg!r}"


def test_a_corrupt_offset_file_degrades_to_omission_and_still_exits_zero(tmp_path):
    """A non-numeric checkpoint means "we cannot tell", which is NOT the same as
    zero: printing "kb 0.0/5.2MB" would report a lost session. Say nothing, and
    never let the parse failure reach the hook's exit status."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-corrupt')}.offset").write_text("not-a-number\n")

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-corrupt",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert "kb" not in msg, f"a corrupt checkpoint was rendered as a figure: {msg!r}"
    assert msg == "Claude finished - proj @ testbox", f"the base message changed: {msg!r}"


@pytest.mark.parametrize(
    "sid",
    [
        "0f3c9a12-4b7e-4d21-9a55-6c2e8d10ab77",  # the ordinary shape
        "sess/with spaces:and*stars",  # every character the hook rewrites
        "x" * 300,  # longer than the 120-byte cap
        "",  # absent id -> both fall back to the transcript path
    ],
)
def test_the_notify_hook_derives_the_offset_key_exactly_like_the_flush_hook(sid: str):
    """The key is the JOIN between the two scripts: notify reads the file flush
    writes. Approximating the sanitisation (it is `tr -c` based and length-capped)
    would read a path that never exists for any id containing a rewritten
    character, and the figure would silently never appear. Both derivations are
    extracted from their own source and RUN, so agreement is proved, not assumed.
    """
    flush_pipe = _okey_pipeline(FLUSH, "OKEY")
    notify_pipe = _okey_pipeline(NOTIFY, "_okey")
    env = {"SID": sid, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}

    def run(pipeline: str) -> str:
        return subprocess.run(
            ["bash", "-c", f'printf %s "${{SID:-/home/u/.claude/projects/p/t.jsonl}}" {pipeline}'],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert run(flush_pipe) == run(notify_pipe) != "", (
        f"the two offset-key derivations disagree for {sid!r}: "
        f"{run(flush_pipe)!r} vs {run(notify_pipe)!r}"
    )


def test_the_published_body_is_pure_ascii_in_its_fullest_shape(tmp_path):
    """KB 12885: an em dash in the message broke every push from the Windows box —
    the MSYS->native argv conversion rewrites non-ASCII through the ANSI codepage
    (em dash -> CP-1252 0x97, invalid UTF-8), ntfy classifies the body as a binary
    attachment and rejects it (400/40014), and the hook's exit-0 contract makes
    the loss silent. It was patched locally four times and reintroduced by every
    release. ASCII is immune to every codepage, so the WHOLE body must be ASCII —
    asserted on the recorded wire body, in the shape with every segment present
    (duration + kb progress), so a fancy dash in ANY segment fails here."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-ascii')}.offset").write_text(str(_KB_FLUSHED))
    mark_dir = tmp_path / "claude-turns"
    mark_dir.mkdir()
    (mark_dir / "s-ascii").write_text(str(int(time.time()) - 200))

    res = _run_hook(
        NOTIFY,
        {
            "session_id": "s-ascii",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
        home,
        {
            "PATH": f"{binp}:{os.environ.get('PATH', '')}",
            "CURL_REC": str(rec_dir),
            "TMPDIR": str(tmp_path),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert "kb " in msg and "m" in msg, f"expected the fullest shape, got {msg!r}"
    assert msg.isascii(), (
        f"a non-ASCII byte is back in the published body — this is the Windows-killing "
        f"regression KB 12885 documents: {msg!r}"
    )


def _shell_fn(script: Path, name: str) -> str:
    """The function <name> AS WRITTEN in <script>, extracted for execution.

    Source-extracted rather than restated, so the test proves what the shipped
    script does, never what this file believes it does.
    """
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", script.read_text(), re.S | re.M)
    assert m, f"{name}() not found in {script.name}"
    return m.group(0)


@pytest.mark.parametrize(
    ("escaped", "native"),
    [
        # The JSON-escaped native form the notify hook's grep/sed keeps, next to
        # the unescaped form the flush hook's JSON backend hands to kb_winpath.
        (r"C:\\Users\\Usama Masood\\.claude\\t.jsonl", r"C:\Users\Usama Masood\.claude\t.jsonl"),
        # Some builds emit forward slashes — no escaping involved, same file.
        ("C:/Users/u/.claude/projects/p/t.jsonl", "C:/Users/u/.claude/projects/p/t.jsonl"),
    ],
)
def test_a_windows_transcript_maps_exactly_like_the_flush_hooks_winpath(escaped: str, native: str):
    """Windows is the one box where the two hooks see DIFFERENT spellings of the
    same transcript: notify gets the JSON-escaped drive path, flush gets the
    unescaped one. Both must land on the same POSIX path, or notify stats a file
    flush is checkpointing under another name and the figure never appears."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}

    def run(fn_src: str, fn: str, arg: str) -> str:
        return subprocess.run(
            ["bash", "-c", f'{fn_src}\n{fn} "$1"', "-", arg],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    ours = run(_shell_fn(NOTIFY, "kb_posixpath"), "kb_posixpath", escaped)
    theirs = run(_shell_fn(FLUSH, "kb_winpath"), "kb_winpath", native)
    assert ours == theirs != "", f"path mapping diverged: {ours!r} vs {theirs!r}"
    assert ours.startswith("/c/"), f"expected a POSIX drive path, got {ours!r}"


def test_a_posix_path_passes_through_kb_posixpath_untouched():
    """Only drive-letter paths are rewritten. A Linux path — even one containing
    a literal backslash, which is a legal filename byte — must come out
    byte-identical, or the hook corrupts the very path it is about to stat."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    for weird in ("/home/u/.claude/projects/p/t.jsonl", r"/home/u/di\r/t.jsonl"):
        out = subprocess.run(
            ["bash", "-c", f'{_shell_fn(NOTIFY, "kb_posixpath")}\nkb_posixpath "$1"', "-", weird],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out == weird, f"a POSIX path was rewritten: {weird!r} -> {out!r}"


# ---------------------------------------------------------------------------
# Behavioural: the stale-hook detector on a DEFAULT-ENROLLED box
# ---------------------------------------------------------------------------

# An ADVERSARIAL curl: it refuses the manifest whenever the request carries no
# bearer. That is what the real endpoint does, and it is the only way to tell
# "the check ran and compared versions" apart from "the check ran with an empty
# credential and silently got nothing" — the state the ordering bug produced.
_STRICT_CURL = r"""#!/usr/bin/env bash
D="$CURL_REC"
mkdir -p "$D"
N=$(( $(cat "$D/.n" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$N" >"$D/.n"
printf '%s\n' "$*" >"$D/argv.$N"
: >"$D/stdin.$N"
for a in "$@"; do
  if [ "$a" = "-K" ]; then cat >"$D/stdin.$N"; break; fi
done
URL=""
for a in "$@"; do case "$a" in http://*|https://*) URL="$a" ;; esac; done
printf '%s\n' "$URL" >"$D/url.$N"
case "$URL" in
  *"/api/setup/manifest.json")
    if grep -q 'Authorization: Bearer .' "$D/stdin.$N"; then
      printf '{"version":"%s"}' "${CURL_VERSION:-beef1234cafe}"
      exit 0
    fi
    exit 22 ;;   # unauthenticated -> what a real 401 does under curl -f
  *) exit 0 ;;
esac
"""


def _strict_curl(home: Path, rec_dir: Path) -> Path:
    binp = home / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    curl = binp / "curl"
    curl.write_text(_STRICT_CURL)
    curl.chmod(0o755)
    rec_dir.mkdir(parents=True, exist_ok=True)
    return binp


def test_a_stale_DEFAULT_ENROLLED_box_is_told_its_hooks_are_out_of_date(tmp_path):
    """Default enrolment writes a PROJECT-SCOPED token to kb-tokens.env and
    deliberately writes NO shared KB_TOKEN. The version check used to run above
    the resolver that reads that file, referencing a bare $KB_TOKEN under
    `set -u`: the subshell died on 'unbound variable', so the warning was dead on
    exactly the machines set up the recommended way — the same class of failure
    as KB item 1193."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _strict_curl(home, rec_dir)
    work = tmp_path / "repeater"
    work.mkdir()
    (home / ".claude" / "kb-flush.env").write_text("KB_REST_URL=http://kb.invalid\n")
    (home / ".claude" / "kb-tokens.env").write_text(f"KB_TOKEN_REPEATER={FAKE_PROJECT_TOKEN}\n")
    (home / ".claude" / "hooks" / ".setup-version").write_text("oldversion0\n")

    res = _run_hook(
        BRIEF,
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(work),
            "session_id": "s-brief",
        },
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr
    assert "OUT OF DATE" in res.stdout, (
        "a stale default-enrolled box was NOT warned; stdout=" + repr(res.stdout)
    )
    assert "oldversion0" in res.stdout and "beef1234cafe" in res.stdout

    # And it got there with a real bearer, on stdin, not on argv.
    stdins = _recorded(rec_dir, "stdin")
    argvs = _recorded(rec_dir, "argv")
    assert any(FAKE_PROJECT_TOKEN in s for s in stdins), (
        "the version check authenticated with something other than the project token"
    )
    assert not any(FAKE_PROJECT_TOKEN in a for a in argvs), "the token reached argv"


def test_a_box_with_no_credential_at_all_still_exits_cleanly(tmp_path):
    """The other half of `set -u` safety: with nothing to authenticate with, the
    check must degrade to "no answer, no warning" rather than abort the hook."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _strict_curl(home, rec_dir)
    work = tmp_path / "nocred"
    work.mkdir()
    (home / ".claude" / "kb-flush.env").write_text("KB_REST_URL=http://kb.invalid\n")

    res = _run_hook(
        BRIEF,
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(work),
            "session_id": "s-nocred",
        },
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr
    assert "OUT OF DATE" not in res.stdout
    assert "unbound variable" not in res.stderr


# ---------------------------------------------------------------------------
# Behavioural: install.sh fetches with the credential it just redeemed
# ---------------------------------------------------------------------------


def test_install_fetches_setup_assets_BEFORE_the_code_is_spent(tmp_path):
    """The manifest and notify.env fetches must use the ENROLMENT CODE, while it
    is still live — not the redeemed grantee token.

    /api/setup/notify.env admits the owner or an UNREDEEMED code and gives a
    project-scoped grantee the same byte-identical 404, so fetching after
    redemption returns nothing and the box enrols with no channel and no version
    stamp. A GET does not spend the code (only POST /api/enrol redeems it), so
    the ordering costs nothing — but it has to actually BE the ordering.
    """
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    res = _run_hook(
        INSTALL,
        {},
        home,
        {
            "PATH": f"{binp}:{os.environ.get('PATH', '')}",
            "CURL_REC": str(rec_dir),
            "KB_REST_URL": "http://127.0.0.1:9",
            "KB_ENROL_CODE": FAKE_ENROL_CODE,
        },
    )
    # Exit 3 is CORRECT here: the stub curl refuses the enrol POST, and an
    # installer that could not configure the machine must fail loudly (rig case
    # 17). What this test pins is the ORDERING, which happens before that point.
    assert res.returncode in (0, 3), res.stderr

    argvs = _recorded(rec_dir, "argv")
    stdins = _recorded(rec_dir, "stdin")
    setup_idx = [n for n, a in enumerate(argvs) if "/api/setup/" in a]
    assert setup_idx, "install.sh never fetched a setup asset"
    for n in setup_idx:
        assert FAKE_ENROL_CODE in stdins[n], (
            "a setup fetch did not carry the live enrolment code: " + repr(stdins[n])
        )
    enrol_idx = [n for n, a in enumerate(argvs) if "/api/enrol" in a]
    if enrol_idx:
        assert min(setup_idx) < min(enrol_idx), (
            "setup assets were fetched AFTER redemption had spent the code"
        )


def _age(uri: str) -> None:
    """Push a session past the quiet window so it is eligible to coalesce."""
    from kbcore.db import transaction

    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET created_at = now() - interval '2 hours' "
            "WHERE meta->>'source_uri' = %s",
            (uri,),
        )


def test_the_same_session_uri_in_two_projects_makes_TWO_groups(kb):
    """The confidentiality property. The consumer concatenates a group and stamps
    the result with rows[0]'s project, so a key shared across projects files one
    project's content under another's name — which is exactly what happened to
    137 rows across six projects, on NDA material."""
    from kbcore import ingest_queue

    a = ingest_queue.enqueue("proj-a", "alpha content\n", "session", source_uri="session:shared")
    b = ingest_queue.enqueue("proj-b", "beta content\n", "session", source_uri="session:shared")
    assert a.id and b.id
    _age("session:shared")

    groups = ingest_queue.pending_groups(limit=200, quiet_minutes=0)
    mine = [g for g in groups if g["source_uri"] == "session:shared"]
    assert len(mine) == 2, f"two projects coalesced into {len(mine)} group(s)"
    assert {g["project"] for g in mine} == {"proj-a", "proj-b"}
    assert len({g["uri"] for g in mine}) == 2, (
        "the two groups share a cohort key — one group's prefetch would feed the other"
    )
    for g in mine:
        assert len({r["project"] for r in g["rows"]}) == 1, "a group mixes projects"


@pytest.mark.parametrize("degenerate", ["session:", "session://", "   ", "file:/"])
def test_a_degenerate_session_uri_can_NEVER_coalesce(kb, degenerate: str):
    """Second, independent guard: even if a client ships a valueless identity,
    the queue refuses to use it as a grouping key. Each row falls back to its own
    queue:<kind>:<id>."""
    from kbcore import ingest_queue

    ids = [
        ingest_queue.enqueue(
            "degen-proj", f"row {i} content\n", "session", source_uri=degenerate
        ).id
        for i in range(3)
    ]
    assert all(ids)
    groups = ingest_queue.pending_groups(limit=200, quiet_minutes=0)
    mine = [g for g in groups if any(r["id"] in ids for r in g["rows"])]
    assert len(mine) == 3, f"a degenerate uri {degenerate!r} coalesced {len(mine)} group(s)"
    for g in mine:
        assert g["source_uri"] is None
        assert g["uri"].endswith(f"queue:session:{g['rows'][0]['id']}")


def test_a_degenerate_uri_is_not_even_stored(kb):
    """Dropped at the door, so nothing downstream has to remember to ignore it."""
    from kbcore import ingest_queue
    from kbcore.db import connect

    r = ingest_queue.enqueue("degen-proj", "one row\n", "session", source_uri="session:")
    with connect() as conn:
        meta = conn.execute("SELECT meta FROM ingest_queue WHERE id=%s", (r.id,)).fetchone()[0]
    assert "source_uri" not in (meta or {}), f"a degenerate identity was stored: {meta}"


def test_a_row_stored_with_a_degenerate_uri_still_cannot_coalesce(kb):
    """The 137 poisoned rows predate the enqueue guard, so the GROUPING side must
    refuse them too — otherwise requeueing them for the recovery night would
    recreate the very cross-project extraction we are repairing."""
    from kbcore import ingest_queue
    from kbcore.db import transaction

    ids = [
        ingest_queue.enqueue("legacy-a", "legacy row one\n", "session").id,
        ingest_queue.enqueue("legacy-a", "legacy row two\n", "session").id,
    ]
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET meta = jsonb_set(coalesce(meta,'{}'::jsonb), "
            "'{source_uri}', '\"session:\"') , created_at = now() - interval '2 hours' "
            "WHERE id = ANY(%s)",
            (ids,),
        )
    groups = ingest_queue.pending_groups(limit=200, quiet_minutes=0)
    mine = [g for g in groups if any(r["id"] in ids for r in g["rows"])]
    assert len(mine) == 2, "legacy rows carrying 'session:' still coalesce"


def test_normal_per_project_coalescing_still_works(kb):
    """The regression guard for the fix itself: one session's chunks, in one
    project, must still become ONE unit of work in the hook's byte order."""
    from kbcore import ingest_queue

    ids = [
        ingest_queue.enqueue(
            "coalesce-ok", f"chunk {i} body\n", "session", source_uri="session:one"
        ).id
        for i in range(5)
    ]
    _age("session:one")
    groups = ingest_queue.pending_groups(limit=200, quiet_minutes=0)
    mine = [g for g in groups if g["source_uri"] == "session:one"]
    assert len(mine) == 1, f"the session did not coalesce: {len(mine)} groups"
    assert [r["id"] for r in mine[0]["rows"]] == sorted(ids)
    assert mine[0]["uri"] == ingest_queue.group_key("coalesce-ok", "session:one")


# --------------------------------------------------- the owner repair


def _zero_yield(project: str, body: str) -> int:
    """A row in the exact state the bug left: extracted, nothing saved."""
    from kbcore import ingest_queue

    qid = ingest_queue.enqueue(project, body, "session").id
    assert qid
    ingest_queue.mark_extracted(qid, saved=0, rejected=0)
    return qid


def test_requeue_extracted_reopens_only_ZERO_YIELD_rows(kb):
    """'extracted' with saved_count=0 is not mined content, it is content that
    produced nothing — terminal, and unreachable by every automatic path. A row
    that DID save items must never be re-opened: re-draining it duplicates
    knowledge."""
    from kbcore import ingest_queue
    from kbcore.db import connect

    empty = _zero_yield("repair-proj", "the cross-project extraction saved nothing")
    productive = ingest_queue.enqueue("repair-proj", "this one yielded knowledge", "session").id
    ingest_queue.mark_extracted(productive, saved=3, rejected=1)

    ids = ingest_queue.requeue_extracted(project="repair-proj", reason="coalescing bug")
    assert empty in ids
    assert productive not in ids

    with connect() as conn:
        rows = dict(
            conn.execute(
                "SELECT id, status FROM ingest_queue WHERE id = ANY(%s)", ([empty, productive],)
            ).fetchall()
        )
    assert rows[empty] == "pending"
    assert rows[productive] == "extracted"
    assert any(r["id"] == empty for r in ingest_queue.pending(limit=100))


def test_requeue_extracted_resets_attempts_and_records_the_reason(kb):
    """Same contract as requeue_failed: the attempts were spent against a broken
    grouping key, and a status flip with no audit trail is indistinguishable from
    a bug."""
    from kbcore import ingest_queue
    from kbcore.db import connect, transaction

    qid = _zero_yield("repair-proj", "row that will be repaired")
    with transaction() as conn:
        conn.execute(
            "UPDATE ingest_queue SET attempts = 2, error = 'earlier note' WHERE id=%s", (qid,)
        )

    ingest_queue.requeue_extracted(ids=[qid], reason="9f2c1ab cross-project coalescing")

    with connect() as conn:
        status, attempts, err, processed = conn.execute(
            "SELECT status, attempts, error, processed_at FROM ingest_queue WHERE id=%s", (qid,)
        ).fetchone()
    assert (status, attempts, processed) == ("pending", 0, None)
    assert "requeued (0 items saved)" in err and "9f2c1ab" in err
    assert "earlier note" in err, "the original record was overwritten"


def test_requeue_extracted_can_be_scoped_to_explicit_ids(kb):
    """The recovery night is supervised: the owner names the rows."""
    from kbcore import ingest_queue

    a = _zero_yield("repair-a", "row a")
    b = _zero_yield("repair-b", "row b")
    ids = ingest_queue.requeue_extracted(ids=[a], reason="just this one")
    assert ids == [a]
    assert b not in ingest_queue.requeue_extracted(ids=[a], reason="idempotent-ish")


def test_requeue_extracted_refuses_to_run_unscoped(kb):
    """Without a selector this is a whole-table replay of every empty extraction
    the KB has ever done — a nightly budget spent on nothing."""
    from kbcore import ingest_queue
    from kbcore.errors import InvalidOperation

    _zero_yield("repair-proj", "some row")
    with pytest.raises(InvalidOperation):
        ingest_queue.requeue_extracted(reason="everything")


def test_requeue_extracted_never_touches_failed_or_pending_rows(kb):
    """It is one narrow repair, not a general un-do: requeue_failed owns the
    failed set, and a live row must never be disturbed."""
    from kbcore import ingest_queue
    from kbcore.db import connect

    failed = ingest_queue.enqueue("repair-proj", "a failed row", "session").id
    ingest_queue.mark_failed(failed, "boom")
    live = ingest_queue.enqueue("repair-proj", "a live row", "session").id

    ids = ingest_queue.requeue_extracted(project="repair-proj", reason="repair")
    assert failed not in ids and live not in ids
    with connect() as conn:
        status = conn.execute("SELECT status FROM ingest_queue WHERE id=%s", (failed,)).fetchone()[
            0
        ]
    assert status == "failed"
