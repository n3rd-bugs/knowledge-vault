"""Wave C.1 (KB 40411): the hooks survive a NON-LOGIN Windows spawn.

On the owner's Windows box the hooks were registered as the raw MSYS
``usr\\bin\\bash.exe``, which Claude Code starts as a non-login shell on the
WINDOWS PATH: no ``/usr/bin``, no ``/mingw64/bin``, a native GnuWin32 ``tr``
first. kb_winpath's no-cygpath fallback piped through ``tr '\\\\' '/'``; MSYS
rewrites that lone ``/`` argument for a native program into
``C:/Program Files/Git/``, every transcript path came out garbled, and the flush
hook's ``[ -f "$T" ] || exit 0`` left the box capturing nothing for two weeks
without one log line.

Everything below is RUN, source-extracted from the shipped scripts:

* the portable block repairs PATH itself under an msys/cygwin ``$OSTYPE`` (bash's
  own variable -- ``uname`` is one of the missing binaries, so it cannot be the
  guard) and ``kb_is_windows`` consults ``$OSTYPE`` first;
* every kb_winpath / kb_posixpath fallback is PURE BASH: a hostile ``tr``/``cut``
  first on PATH cannot touch the result, cygpath still wins when it answers, and
  UNC / already-POSIX input passes through;
* the dispatcher hands its children the repaired PATH;
* a named-but-missing transcript leaves ONE rate-limited ``BUG:`` line;
* a CRLF-written session pin never ships ``kbproject\\r`` on the wire;
* the sweep calls a fresh NEEDS TOKEN stamp ``refused`` (not ``no-progress``);
* the pre-brief's ``[KB sweep]`` line NAMES the stalled sessions.

The Windows-only halves (MSYS argument conversion itself, the launcher at
``<git root>\\bin\\bash.exe``) are the hookrig's winsim cases; these are the
Linux-runnable pins.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_capture_funnel import (
    BACKFILL,
    BRIEF,
    DISPATCH,
    FAKE_PROJECT_TOKEN,
    FLUSH,
    INSTALL,
    MCPCALL,
    NOTIFY,
    _flush_okey,
    _hook_home,
    _recall_curl,
    _recall_env,
    _recall_home,
    _Recorder,
    _run_hook,
    _shell_fn,
    _start,
    _transcript,
    requires_curl,
)

CARRIERS = [INSTALL, FLUSH, BRIEF, MCPCALL, BACKFILL]
_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}


def _block(path: Path) -> str:
    """The portable block, begin marker through end marker, as shipped."""
    text = path.read_text()
    start = text.index("# >>> KB PORTABLE BLOCK")
    end = text.index("\n", text.index("# <<< END KB PORTABLE BLOCK")) + 1
    return text[start:end]


def _bash(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script, "-", *args],
        env=env or dict(_ENV),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _hostile_bin(tmp_path: Path, keep: tuple[str, ...] = ()) -> Path:
    """A PATH dir standing in for the Windows PATH: `tr` and `cut` are native
    look-alikes whose output is garbage (the GnuWin32 tr after MSYS rewrote its
    `/` argument), and there is no cygpath. ``keep`` symlinks real tools in."""
    d = tmp_path / "winpath-bin"
    d.mkdir(exist_ok=True)
    for name in ("tr", "cut"):
        shim = d / name
        shim.write_text("#!/bin/sh\ncat >/dev/null 2>&1\nprintf 'C:/Program Files/Git/GARBLED'\n")
        shim.chmod(0o755)
    for name in keep:
        real = shutil.which(name)
        assert real, f"{name} is needed on the test box"
        (d / name).symlink_to(real)
    return d


def _fake_cygpath(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "cygpath-bin"
    d.mkdir(exist_ok=True)
    cyg = d / "cygpath"
    cyg.write_text("#!/bin/sh\n" + body + "\n")
    cyg.chmod(0o755)
    return d


# ---------------------------------------------------------------------------
# The block: PATH repair and platform detection
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not Path("/usr/bin").is_dir(), reason="needs a /usr/bin to prepend")
@pytest.mark.parametrize(
    ("ostype", "expected"),
    [("msys", "/usr/bin:/bin"), ("cygwin", "/usr/bin:/bin"), ("linux-gnu", "/bin")],
)
def test_the_block_puts_the_msys_dirs_back_on_PATH_only_under_msys(ostype: str, expected: str):
    """The raw usr\\bin\\bash.exe spawn has no /usr/bin on PATH, so uname, cut,
    tr, awk and grep all miss. The block restores it BEFORE its first external
    call, keyed on bash's own $OSTYPE, exported so every child inherits it; on
    Linux/macOS it is a no-op. /mingw64/bin is added only when it exists."""
    for path in CARRIERS:
        res = _bash(
            f"OSTYPE={ostype}\nPATH=/bin\n{_block(path)}\n"
            'printf \'%s\\n\' "$PATH"\nbash -c \'printf "%s\\n" "$PATH"\'\n'
        )
        assert res.returncode == 0, res.stderr
        mine, childs = res.stdout.splitlines()
        assert mine == expected, f"{path.name}: PATH after the block is {mine!r}"
        assert childs == expected, f"{path.name}: the repaired PATH is not exported: {childs!r}"


@pytest.mark.parametrize(
    ("ostype", "uname_s", "windows"),
    [
        ("msys", "", True),  # the raw spawn: uname missing, $OSTYPE still msys
        ("cygwin", "", True),
        ("linux-gnu", "", False),
        ("darwin23", "", False),
        ("linux-gnu", "MINGW64_NT-10.0-22631", True),  # the rig's faked uname still counts
    ],
)
def test_kb_is_windows_consults_OSTYPE_before_uname(
    tmp_path: Path, ostype: str, uname_s: str, windows: bool
):
    """uname is itself a /usr/bin binary, so under the raw spawn KB_UNAME read
    `unknown` and a uname-only kb_is_windows said NO on Windows -- the guard the
    doc proposed was circular. $OSTYPE is decided by bash itself."""
    env = dict(_ENV)
    if uname_s:
        d = tmp_path / "uname-bin"
        d.mkdir()
        (d / "uname").write_text(f'#!/bin/sh\n[ "$1" = -s ] && echo {uname_s} && exit 0\nexit 1\n')
        (d / "uname").chmod(0o755)
        env["PATH"] = f"{d}:{env['PATH']}"
    res = _bash(
        f"OSTYPE={ostype}\n{_block(FLUSH)}\nkb_is_windows && echo yes || echo no\n",
        env=env,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == ("yes" if windows else "no"), (ostype, uname_s, res.stdout)


# ---------------------------------------------------------------------------
# Every path fallback is pure bash
# ---------------------------------------------------------------------------

_WINPATH_CASES = [
    (r"C:\Users\x\a b.jsonl", "/c/Users/x/a b.jsonl"),
    ("c:/mixed/p", "/c/mixed/p"),
    (r"D:\tmp/mixed\sep", "/d/tmp/mixed/sep"),
    (r"\\srv\share\t.jsonl", r"\\srv\share\t.jsonl"),  # UNC: passes through
    ("/c/already", "/c/already"),  # already POSIX: passes through
    (r"relative\p", r"relative\p"),
    ("", ""),
]


# Every copy of kb_winpath: the block in each of its five carriers, and the
# dispatcher's trimmed copy (it deliberately does not carry the block).
_SOURCES = {f"block:{p.name}": _block(p) for p in CARRIERS}
_SOURCES["dispatcher"] = _shell_fn(DISPATCH, "kb_winpath")


@pytest.mark.parametrize("sid", sorted(_SOURCES))
def test_the_winpath_fallback_calls_no_external_program(tmp_path: Path, sid: str):
    """THE DEFECT: the fallback was `cut | tr '\\\\' '/'`, and a native tr whose
    `/` argument MSYS rewrote turned C:\\Users\\... into /cCUsersC.... With a
    garbage-printing tr and cut FIRST on PATH (and no cygpath), the conversion
    must still be exact -- it may not spawn anything at all."""
    hostile = _hostile_bin(tmp_path)
    ins = [c[0] for c in _WINPATH_CASES]
    loop = 'for a in "$@"; do kb_winpath "$a"; printf "\\n"; done'
    res = _bash(f'{_SOURCES[sid]}\nPATH="{hostile}"\n{loop}\n', *ins)
    assert res.returncode == 0, res.stderr
    got = res.stdout.split("\n")[: len(ins)]
    assert got == [c[1] for c in _WINPATH_CASES], f"{sid}: {list(zip(ins, got, strict=True))}"
    assert "GARBLED" not in res.stdout


@pytest.mark.parametrize("sid", sorted(_SOURCES))
def test_cygpath_still_answers_first(tmp_path: Path, sid: str):
    """cygpath stays FIRST: when it answers, its mapping wins (mounts, 8.3 names);
    only an EMPTY answer falls back to the pure-bash form."""
    src = _SOURCES[sid]
    good = _fake_cygpath(tmp_path, "printf '/from-cygpath'")
    res = _bash(f'{src}\nPATH="{good}:$PATH"\nkb_winpath "$1"\n', "C:\\x")
    assert res.stdout == "/from-cygpath", f"{sid}: cygpath was not consulted: {res.stdout!r}"
    shutil.rmtree(good)
    _fake_cygpath(tmp_path, "exit 1")
    res = _bash(f'{src}\nPATH="{good}:$PATH"\nkb_winpath "$1"\n', "C:\\x\\y")
    assert res.stdout == "/c/x/y", f"{sid}: a failing cygpath did not fall back: {res.stdout!r}"


def test_notify_posixpath_fallback_is_pure_bash_after_the_collapse(tmp_path: Path):
    """The seventh copy (notify-complete.sh kb_posixpath). Its event paths are
    still JSON-escaped (doubled backslashes), collapsed by sed first; after
    that nothing but bash may touch the path."""
    hostile = _hostile_bin(tmp_path, keep=("sed",))
    cases = [
        (r"C:\\Users\\x\\a b.jsonl", "/c/Users/x/a b.jsonl"),
        ("c:/mixed/p", "/c/mixed/p"),
        ("/home/u/di\\r/t.jsonl", "/home/u/di\\r/t.jsonl"),
    ]
    src = _shell_fn(NOTIFY, "kb_posixpath")
    res = _bash(
        f'{src}\nPATH="{hostile}"\nfor a in "$@"; do kb_posixpath "$a"; printf "\\n"; done\n',
        *[c[0] for c in cases],
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.split("\n")[: len(cases)] == [c[1] for c in cases], res.stdout


# ---------------------------------------------------------------------------
# The dispatcher repairs PATH for its children
# ---------------------------------------------------------------------------


def _ostype_env_honoured() -> bool:
    res = subprocess.run(
        ["bash", "-c", 'printf %s "$OSTYPE"'],
        env={**_ENV, "OSTYPE": "msys"},
        capture_output=True,
        text=True,
    )
    return res.stdout == "msys"


@pytest.mark.skipif(not Path("/usr/bin").is_dir(), reason="needs a /usr/bin to prepend")
@pytest.mark.parametrize(("ostype", "expected"), [("msys", "/usr/bin:/bin"), ("linux-gnu", "/bin")])
def test_the_dispatcher_hands_its_children_the_repaired_PATH(
    tmp_path: Path, ostype: str, expected: str
):
    """The dispatcher is the process Claude Code spawns, so its first statement
    after EV= repairs PATH -- with bash only: kb_is_windows is not defined there.
    Every child (the flush, the brief, notify) then inherits it."""
    if not _ostype_env_honoured():
        pytest.skip("this bash does not take OSTYPE from the environment")
    hookdir = tmp_path / "hookdir"
    hookdir.mkdir()
    shutil.copy2(DISPATCH, hookdir / DISPATCH.name)
    (hookdir / "turn-start.sh").write_text('printf "%s" "$PATH" >"$KB_REC/path"\n')
    rec = tmp_path / "rec"
    rec.mkdir()
    home = _hook_home(tmp_path)
    res = subprocess.run(
        ["bash", str(hookdir / DISPATCH.name), "UserPromptSubmit"],
        input="{}",
        env={
            "HOME": str(home),
            "PATH": "/bin",
            "LANG": "C.UTF-8",
            "OSTYPE": ostype,
            "KB_REC": str(rec),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    assert (rec / "path").read_text() == expected


# ---------------------------------------------------------------------------
# The flush hook: a missing transcript is a BUG line, a CRLF pin is clean
# ---------------------------------------------------------------------------


def _flush_env_file(home: Path, url: str = "http://127.0.0.1:9") -> None:
    cfg = home / ".claude" / "kb-flush.env"
    cfg.write_text(f"KB_REST_URL={url}\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n")


def _log_lines(home: Path, needle: str) -> list[str]:
    log = home / ".claude" / "hooks" / "flush-session.log"
    text = log.read_text(errors="replace") if log.exists() else ""
    return [ln for ln in text.splitlines() if needle in ln]


def test_a_named_transcript_that_is_not_a_file_leaves_ONE_rate_limited_BUG_line(tmp_path: Path):
    """The silent `exit 0` sat BEFORE LOG/STATE and before the sweep trigger, so a
    garbled conversion looked exactly like a quiet box. Now: one line in the
    literal log path, a box-level hourly stamp, still exit 0 -- and the pre-brief
    lifts it into the next session (BUG: is one of its tokens)."""
    home = _hook_home(tmp_path)
    _flush_env_file(home)
    event = {
        "transcript_path": r"C:\Users\nobody\gone.jsonl",
        "cwd": r"C:\Users\nobody",
        "session_id": "0123456789abcdef",
        "hook_event_name": "Stop",
    }
    for _ in range(2):
        res = _run_hook(FLUSH, event, home)
        assert res.returncode == 0, res.stderr
    lines = _log_lines(home, "transcript not found")
    assert len(lines) == 1, f"expected ONE rate-limited line, got {lines}"
    assert re.fullmatch(
        r"\S+ flush BUG: transcript not found: /c/Users/nobody/gone\.jsonl "
        r"\(path conversion\?\) ev=Stop sid=01234567",
        lines[0],
    ), lines[0]
    stamp = home / ".claude" / "hooks" / "state" / ".badpath.stamp"
    assert stamp.read_text().strip().isdigit(), "the box-level stamp was not written"
    stamp.write_text(f"{int(time.time()) - 7200}\n")
    assert _run_hook(FLUSH, event, home).returncode == 0
    assert len(_log_lines(home, "transcript not found")) == 2, "an hour later it must log again"
    state = sorted(p.name for p in (home / ".claude/hooks/state").iterdir())
    assert state == [".badpath.stamp"], f"a missing transcript must leave no session state: {state}"


def test_an_absent_transcript_path_stays_silent(tmp_path: Path):
    """No path at all is not a conversion bug (an event without one): unchanged."""
    home = _hook_home(tmp_path)
    _flush_env_file(home)
    event = {"cwd": str(tmp_path), "session_id": "s", "hook_event_name": "Stop"}
    res = _run_hook(FLUSH, event, home)
    assert res.returncode == 0, res.stderr
    assert _log_lines(home, "BUG:") == []


@requires_curl
def test_a_CRLF_written_pin_never_ships_a_carriage_return(tmp_path: Path):
    """A pin edited on Windows (Notepad) is `slug<TAB>how\\r\\n`. Read with plain
    cut, `kbproject\\r` went out as flush_project_by and the REST schema
    (^[a-z-]+$) 422'd every chunk of the session."""
    home = _hook_home(tmp_path)
    work = tmp_path / "crlfwork"
    transcript = _transcript(work / "t.jsonl")
    sid = "crlf-pin-session"
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True)
    (state / f"{_flush_okey(sid)}.project").write_bytes(b"pinned\tkbproject\r\n")
    with _Recorder() as rec:
        _flush_env_file(home, rec.url)
        res = _run_hook(
            FLUSH,
            {
                "transcript_path": str(transcript),
                "cwd": str(work),
                "session_id": sid,
                "hook_event_name": "SessionEnd",
            },
            home,
        )
        assert res.returncode == 0, res.stderr
        assert rec.wait(1), "no submission reached the stub"
        payload = rec.requests[0]["payload"]
    assert payload.get("project") == "pinned", payload.get("project")
    assert payload.get("flush_project_by") == "kbproject", repr(payload.get("flush_project_by"))


# ---------------------------------------------------------------------------
# The sweep: a fresh NEEDS TOKEN stamp is a refusal, by name
# ---------------------------------------------------------------------------

_LINEAGE = "# KB-FLUSH-LINEAGE: never-delete-offsets v2"


@pytest.mark.parametrize(
    ("stamp_age", "verdict"),
    [
        (60, "refused"),
        (3599, "refused"),
        (3600, "no-progress"),
        (7200, "no-progress"),
        (None, "no-progress"),
    ],
)
def test_a_fresh_notoken_stamp_is_labelled_refused_not_no_progress(
    tmp_path: Path, stamp_age: int | None, verdict: str
):
    """The hook's NEEDS TOKEN line is hourly and the sweep half-hourly, so every
    other run saw a silent refusal and booked it `no-progress` -- walking a
    tokenless project into STALLED with nothing saying why. A stamp younger
    than an hour IS that refusal; its detail names the token, never a value."""
    home = _hook_home(tmp_path)
    (home / ".claude" / "kb-flush.env").write_text("KB_REST_URL=http://127.0.0.1:9\n")
    hooks = home / ".claude" / "hooks"
    state = hooks / "state"
    state.mkdir()
    (state / ".checkpoints-trusted-since").write_text("2000-01-01T00:00:00Z\n")
    work = tmp_path / "work"
    work.mkdir()
    projdir = home / ".claude" / "projects" / "-work-tokless"
    projdir.mkdir(parents=True)
    sid = "aaaaaaaa-1111-2222-3333-444444444444"
    t = projdir / f"{sid}.jsonl"
    t.write_text(
        "".join(
            f'{{"type":"user","timestamp":"2026-09-11T00:00:00.000Z","cwd":"{work}","i":{i}}}\n'
            for i in range(5)
        )
    )
    os.utime(t, (1700000000, 1700000000))
    (state / f"{sid}.offset").write_text("0")
    (state / f"{sid}.project").write_text("tokless\tgitfirst\n")
    now = 1800000000
    if stamp_age is not None:
        (state / ".notoken-tokless.stamp").write_text(f"{now - stamp_age}\n")
    standin = tmp_path / "standin.sh"
    standin.write_text(f"#!/usr/bin/env bash\n{_LINEAGE}\ncat >/dev/null\nexit 0\n")
    standin.chmod(0o755)
    res = subprocess.run(
        ["bash", str(BACKFILL), "--push", "--hook", str(standin), "--timeout", "20"],
        env={**_ENV, "HOME": str(home), "KB_BACKFILL_NOW": str(now)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    sweep = (state / f"{sid}.sweep").read_text()
    assert f"verdict={verdict}" in sweep, f"{sweep!r}\n{res.stdout}"
    logged = _log_lines(home, "aaaaaaaa tokless")
    assert any(f" {verdict} " in ln for ln in logged), logged
    named = "NEEDS TOKEN (rate-limited): no KB_TOKEN_TOKLESS on this box; see "
    if verdict == "refused":
        assert named in res.stdout and any(named in ln for ln in logged), res.stdout
    else:
        assert "NEEDS TOKEN (rate-limited)" not in res.stdout
    assert "kbg_" not in res.stdout + "".join(logged)


# ---------------------------------------------------------------------------
# The pre-brief names the stalled sessions on its ONE [KB sweep] line
# ---------------------------------------------------------------------------


def _hostname() -> str:
    res = subprocess.run(["hostname"], capture_output=True, text=True, check=False)
    return res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else "this-machine"


def _stall(state: Path, okey: str, fails: int, verdict: str, pin: bytes | None) -> None:
    (state / f"{okey}.sweep").write_text(f"fails={fails} last=1800000000 verdict={verdict}\n")
    if pin is not None:
        (state / f"{okey}.project").write_bytes(pin)


def _brief_sweep_line(tmp_path: Path, home: Path, work: Path) -> tuple[list[str], str]:
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    res = _run_hook(
        BRIEF,
        _start(work),
        home,
        _recall_env(
            binp,
            rec_dir,
            CURL_SEARCH_BODY="[]",
            KB_VERSION_CHECK="0",
            KB_RECALL_BREAKER_SECS="0",
        ),
    )
    assert res.returncode == 0, res.stderr
    return [ln for ln in res.stdout.splitlines() if ln.startswith("[KB sweep]")], res.stdout


def test_the_sweep_line_names_each_stalled_session_and_the_token_remedy(tmp_path: Path):
    """A bare `N transcript(s) STALLED` sent the owner through the log to learn
    which session, which project and why. On the SAME line (one line, ASCII):
    `<slug> (<sid8>) <verdict>` per stalled .sweep, and the kb_project_token
    remedy ONLY where the hook left a .notoken stamp for that slug."""
    home, work = _recall_home(tmp_path)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir()
    (state / ".sweep-last").write_text(
        f"ts={int(time.time()) - 600} rc=0 mode=auto ready=0 pushed=0 failed=2 deferred=0 "
        "stalled=2 unknown=0 live=0 unflushed=0 busy=0 partial=0\n"
    )
    _stall(state, "aaaaaaaa-0000-0000-0000-000000000001", 6, "refused", b"tokless\tfiring\r\n")
    _stall(state, "bbbbbbbb-0000-0000-0000-000000000002", 9, "no-progress", b"other\tgitfirst\n")
    _stall(state, "cccccccc-0000-0000-0000-000000000003", 2, "failed", b"below\tgitfirst\n")
    # D: a TRANSPORT stall (verdict=failed) beside a two-day-old .notoken stamp --
    # stamps are never removed, so the remedy must NOT be printed for it.
    _stall(state, "dddddddd-0000-0000-0000-000000000004", 7, "failed", b"stale\tgitfirst\n")
    (state / ".notoken-stale.stamp").write_text(f"{1800000000 - 172800}\n")
    (state / ".notoken-tokless.stamp").write_text("1800000000\n")
    lines, out = _brief_sweep_line(tmp_path, home, work)
    assert len(lines) == 1, out
    line = lines[0]
    assert line.isascii(), line
    assert "2 transcript(s) STALLED" in line, line
    assert (
        f'tokless (aaaaaaaa) refused -- kb_project_token(project="tokless", '
        f'machine="{_hostname()}")'
    ) in line, line
    assert "other (bbbbbbbb) no-progress" in line, line
    assert 'project="other"' not in line, "a remedy without a .notoken stamp"
    assert "stale (dddddddd) failed" in line, line
    assert 'project="stale"' not in line, "a remedy on a stale stamp beside a transport stall"
    assert "cccccccc" not in line and "below" not in line, "a session under the threshold was named"
    assert "kbg_" not in out and FAKE_PROJECT_TOKEN not in out


def test_the_sweep_line_caps_the_named_sessions(tmp_path: Path):
    home, work = _recall_home(tmp_path)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir()
    (state / ".sweep-last").write_text(f"ts={int(time.time()) - 600} rc=0 stalled=5 unknown=0\n")
    for i in range(5):
        pin = f"p{i}\tgitfirst\n".encode()
        _stall(state, f"{str(i) * 8}-sess", 6, "no-progress", pin)
    lines, out = _brief_sweep_line(tmp_path, home, work)
    assert len(lines) == 1, out
    assert "p0 (00000000) no-progress" in lines[0] and "p2 (22222222)" in lines[0], lines[0]
    assert "p3 (" not in lines[0] and lines[0].endswith(", +2 more"), lines[0]


# ---------------------------------------------------------------------------
# install.sh: the launcher is chosen only when it really exists
# ---------------------------------------------------------------------------


def _bashw_region() -> str:
    text = INSTALL.read_text()
    start = text.index('BASHW=""\nif kb_is_windows; then\n')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return text[start:end]


@pytest.mark.parametrize(
    ("cygpath_root", "exepath"),
    [
        ("Q:\\no-such-kb-root\\", ""),  # cygpath answers, no launcher there
        (None, ""),  # neither cygpath nor EXEPATH: rig case 25's bare bash.exe
        (None, "Q:\\no-such-kb-root"),  # EXEPATH only, no launcher there
        (None, "\\\\srv\\share\\Git"),  # a UNC root is never guessed at
    ],
)
def test_install_keeps_todays_interpreter_when_no_launcher_exists(
    tmp_path: Path, cygpath_root: str | None, exepath: str
):
    """<git root>\\bin\\bash.exe is registered only when it EXISTS (tested in the
    drive form, never through cygpath -u's mount table, where /bin is /usr/bin);
    otherwise the value is kb_nativepath's, exactly as before. The positive path
    needs a /c drive root and lives in the winsim rig."""
    env = dict(_ENV)
    if cygpath_root is not None:
        d = _fake_cygpath(tmp_path, f"printf '%s' '{cygpath_root}'")
        env["PATH"] = f"{d}:{env['PATH']}"
    if exepath:
        env["EXEPATH"] = exepath
    res = _bash(
        "set -euo pipefail\nkb_is_windows() { return 0; }\n"
        "kb_nativepath() { printf 'TODAYS-VALUE'; }\n"
        f"{_bashw_region()}printf '%s' \"$BASHW\"\n",
        env=env,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "TODAYS-VALUE", res.stdout


def test_the_launcher_preference_lives_outside_the_shared_block():
    """kb_nativepath is block code shared by five scripts and stays as it was;
    the choice of interpreter is install.sh's own, after the block."""
    text = INSTALL.read_text()
    assert "\\\\bin\\\\bash.exe" in _bashw_region(), "the launcher candidate is gone"
    assert text.index(_bashw_region()) > text.index("# <<< END KB PORTABLE BLOCK")
    native = _shell_fn(INSTALL, "kb_nativepath")
    assert '_kbn="${EXEPATH%\\\\}\\\\usr\\\\bin\\\\bash.exe"' in native, (
        "kb_nativepath's EXEPATH fallback changed -- rig case 25 pins it"
    )
