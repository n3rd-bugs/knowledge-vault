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
    ["flush-session-to-kb.sh", "kb-session-brief.sh", "install.sh", "notify-complete.sh"],
)
def test_contrib_and_shipped_copies_stay_identical(name: str):
    """contrib/ is canonical, setup_assets/ is what the container serves. A box
    that fetches from /api/setup/ must get the script that was tested."""
    assert (CONTRIB / name).read_bytes() == (SHIPPED / name).read_bytes(), (
        f"{name} drifted from its setup_assets twin — run `make sync-assets`"
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
