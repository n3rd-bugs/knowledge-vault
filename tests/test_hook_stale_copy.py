"""Wave C.2 (KB 49064): a session resumed from another directory leaves a STALE COPY.

Claude Code keeps TWO transcript files with ONE session id when a session is
resumed from a different working directory: the frozen copy in the old project
dir and the live one in the new. Every checkpoint is keyed by the SESSION id, so
both share ``state/<okey>.offset``; the live file's flushes carried it past the
frozen file's size, the sweep then read the frozen file as ``shrank`` and the
hook restarted it from byte 0 (rm1, 2026-10-08: 411 rows / 282 MB re-sent,
~3,750 duplicate items).

Everything below RUNS the shipped scripts under a throwaway HOME. Nothing leaves
the box: the hook's transport is a local stub that must receive NOTHING on the
stale paths (or a refused local port for the control), and the sweep is run dry
or with a stand-in hook that only records the events it is handed.

* the hook skips a stale copy (a byte-prefix of the copy that reaches the
  checkpoint) silently -- exit 0, offset byte-identical, one rate-limited
  ``stale copy:`` line ``reason=stale-copy``, no marker;
* a DIVERGED copy (it kept growing behind the shared checkpoint) is REFUSED
  visibly: nothing sent, no restart, ``state/<okey>.diverged`` (re)written on
  every firing, notify says ``kb ?/<size> diverged``, the sweep tables it
  ``diverged`` and counts it in ``unflushed=`` / ``diverged=``;
* a FROZEN copy whose covering copy is gone (older than the checkpoint's last
  write) is ``behind``: refused, ``state/<okey>.behind``, one line naming
  ``--seed``; a path a marker names is never restarted even once resumed;
* a genuine shrink (modified after the checkpoint, no copy, no marker) still
  restarts from 0, exactly as before;
* a path a marker names is refused at ANY size -- grown past the checkpoint
  too (that checkpoint is another copy's: a drain from it would start
  mid-record); the marker keeps ``frozen=`` (its size when first named), so a
  resumed ``behind`` copy is tabled REMAIN = size - frozen and seeded exactly
  there (``--seed <sid> <frozen>``), never offered ``current``;
* a diverged copy another copy still covers is settled only by moving it out:
  ``--seed`` says so (``note:``) and the next pass marks it again;
* markers clear only with the offset (state sweep), by ``--seed``, when the
  path they name is gone, or -- a ``.diverged`` one -- when that path now
  carries the checkpoint holder's prefix (moved out, then resumed there: the
  hook, the connector arm and the sweep release it by CONTENT);
* the sweep's push loop never books a refused copy as pushed nor charges its
  budget with the other copy's checkpoint -- nor a shrank row whose hook
  stopped before it decided (busy, NEEDS TOKEN, silent);
* the sweep lists the stale copy as ``stale-copy`` (never fired, never counted as
  unflushed, ``stale=`` in ``.sweep-last``) and ``--seed`` takes the LARGEST copy;
* the copies helper is ONE block, byte-identical in the hook and the sweep;
* (the sweep timer's rendered keys and the off switch are pinned in
  tests/test_hook_sweep_timer.py).
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_capture_funnel import (
    BACKFILL,
    FAKE_PROJECT_TOKEN,
    FLUSH,
    _connector_home,
    _flush_okey,
    _hook_home,
    _notify_message,
    _recall_home,
    _Recorder,
    _recording_curl,
    _run_hook,
    _run_notify,
    requires_curl,
)
from tests.test_hook_windows_spawn import _brief_sweep_line

_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
_LINEAGE = "# KB-FLUSH-LINEAGE: never-delete-offsets v2"
SID = "bd162e9f-0000-4000-8000-000000000049"
OLD = 1700000000  # transcript mtime: far outside the sweep's quiet window
NOW = 1800000000  # the sweep's pinned clock


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _lines(work: Path, n: int, start: int = 0, tag: str = "") -> bytes:
    """``n`` JSONL records (~200 bytes each) carrying the cwd the sweep needs."""
    out = []
    for i in range(start, start + n):
        rec = {
            "type": "user",
            "timestamp": "2026-10-08T06:00:00.000Z",
            "cwd": str(work),
            "i": i,
            "text": f"{tag}line {i} " + "x" * 120,
        }
        out.append(json.dumps(rec) + "\n")
    return "".join(out).encode()


def _projects(home: Path) -> Path:
    return home / ".claude" / "projects"


def _state(home: Path) -> Path:
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (OLD, OLD))
    return path


def _resumed_session(
    home: Path, work: Path, *, diverged: bool = False, stale_dir: str = "-home-u"
) -> tuple[Path, Path]:
    """The incident's shape: a frozen copy (30 records) in the old project dir
    and the live one (40 records) in the new. ``diverged`` rewrites the frozen
    copy's last record in the LIVE file (same length, other bytes), so the
    frozen copy is no longer a byte-prefix of it."""
    head = _lines(work, 30)
    live_body = head + _lines(work, 10, start=30)
    if diverged:
        last = _lines(work, 1, start=29)
        other = last.replace(b"line 29", b"LINE 29")
        assert len(other) == len(last) and other != last
        live_body = head[: -len(last)] + other + _lines(work, 10, start=30)
    stale = _write(_projects(home) / stale_dir / f"{SID}.jsonl", head)
    live = _write(_projects(home) / "-home-u-binary-rewriter" / f"{SID}.jsonl", live_body)
    assert stale.stat().st_size > 4096, "the tail check must run on a window inside the file"
    return stale, live


def _offset(home: Path, value: int) -> Path:
    off = _state(home) / f"{_flush_okey(SID)}.offset"
    off.write_text(str(value))
    return off


def _flush_cfg(home: Path, url: str) -> None:
    (home / ".claude" / "kb-flush.env").write_text(
        f"KB_REST_URL={url}\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n"
    )


def _fire(home: Path, transcript: Path, work: Path, ev: str = "Backfill", **env: str):
    event = {
        "transcript_path": str(transcript),
        "cwd": str(work),
        "session_id": SID,
        "hook_event_name": ev,
    }
    return _run_hook(
        FLUSH,
        event,
        home,
        extra_env={"KB_FLUSH_FOREGROUND": "1", "KB_FLUSH_CURL_RETRY": "0", **env},
    )


def _log(home: Path) -> str:
    log = home / ".claude" / "hooks" / "flush-session.log"
    return log.read_text(errors="replace") if log.exists() else ""


def _stale_lines(home: Path) -> list[str]:
    return [ln for ln in _log(home).splitlines() if " stale copy: " in ln]


def _marker(home: Path, kind: str) -> Path:
    """``state/<okey>.diverged`` / ``.behind`` (and ``.diverged.stamp`` ...)."""
    return _state(home) / f"{_flush_okey(SID)}.{kind}"


_ISO = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"


def _assert_marker(
    home: Path,
    kind: str,
    path: Path,
    checkpoint: int,
    cover: Path | None,
    frozen: int | None = None,
):
    """The ONE marker line; ``frozen=`` (LAST) defaults to the path's size -- a
    marker first written now. A copy resumed since keeps its first size there."""
    text = _marker(home, kind).read_text()
    size = path.stat().st_size
    want = f"{path} size={size} checkpoint={checkpoint}"
    if cover is not None:
        want += f" cover={cover}"
    want += f" frozen={size if frozen is None else frozen}"
    assert re.fullmatch(_ISO + " " + re.escape(want) + "\n", text), text


def _no_markers(home: Path) -> None:
    for kind in ("diverged", "behind", "diverged.stamp", "behind.stamp"):
        assert not _marker(home, kind).exists(), f"an unexpected {kind} marker"


def _grow(path: Path, work: Path, n: int = 3) -> None:
    """The frozen copy RESUMED in its first directory: records appended (mtime now)."""
    with path.open("ab") as fh:
        fh.write(_lines(work, n, start=30, tag="resumed "))


def _age(path: Path, when: int) -> None:
    os.utime(path, (when, when))


# ---------------------------------------------------------------------------
# (b) the hook skips a stale copy (a true byte-prefix) silently
# ---------------------------------------------------------------------------


def test_the_hook_skips_a_stale_copy_and_never_restarts(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size - 150  # the live copy's flushes went past the frozen one
    assert checkpoint > stale.stat().st_size
    off = _offset(home, checkpoint)
    before = off.read_bytes()
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(home, stale, work)
        assert res.returncode == 0, res.stderr
        assert res.stdout == "", res.stdout
        assert off.read_bytes() == before, "a stale copy must never write the checkpoint"
        lines = _stale_lines(home)
        assert len(lines) == 1, _log(home)
        assert re.fullmatch(
            r"\S+ flush \S+ stale copy: "
            + re.escape(f"{stale} ({stale.stat().st_size}) is behind the checkpoint {checkpoint}; ")
            + re.escape(f"a newer copy covers it ({live}) reason=stale-copy ")
            + r"— skipped, no restart ev=Backfill sid=bd162e9f",
            lines[0],
        ), lines[0]
        assert "restarting from 0" not in _log(home)
        assert "reason=shrank" not in _log(home)
        _no_markers(home)  # a true prefix is silent: no owner-visible marker
        stamp = _state(home) / f"{_flush_okey(SID)}.stalecopy"
        assert stamp.exists(), "the 24 h rate-limit stamp was not written"

        # A second firing inside 24 h: still skipped, silently.
        assert _fire(home, stale, work, ev="PreCompact").returncode == 0
        assert len(_stale_lines(home)) == 1, _log(home)
        assert off.read_bytes() == before

        # A day later the line comes back (rate-limited, never muted for good).
        os.utime(stamp, (OLD, OLD))
        assert _fire(home, stale, work).returncode == 0
        assert len(_stale_lines(home)) == 2, _log(home)
        assert off.read_bytes() == before
        assert rec.requests == [], "a stale copy sent bytes"


def _refused_url() -> str:
    """A local port nothing listens on: every send fails at once, on the box."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@requires_curl
@pytest.mark.parametrize("copy", ["none", "smaller"])
def test_a_genuine_shrink_still_restarts_from_zero(tmp_path: Path, copy: str):
    """Control (e): a transcript REWRITTEN smaller after the checkpoint's last
    write (its mtime is newer), with no copy -- or a copy that does NOT reach the
    checkpoint -- and no marker, is today's shrink: the restart line, offset 0
    (the send then fails on a refused local port, so the checkpoint stays at the
    restart value)."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    if copy == "smaller":
        _write(_projects(home) / "-home-u-other" / f"{SID}.jsonl", _lines(work, 20))
    off = _offset(home, size + 500)
    _age(off, OLD - 3600)  # the checkpoint was written BEFORE the rewrite
    _flush_cfg(home, _refused_url())
    res = _fire(home, t, work)
    assert res.returncode == 0, res.stderr
    log = _log(home)
    assert (
        f"transcript shrank ({size} < offset {size + 500}) reason=shrank — restarting from 0" in log
    ), log
    assert _stale_lines(home) == []
    assert "shrink check skipped" not in log
    _no_markers(home)
    assert off.read_text() == "0", off.read_text()


def _shim_bin(tmp_path: Path) -> Path:
    """stat/date that cannot report an mtime (every other use passes through)."""
    b = tmp_path / "shim"
    b.mkdir()
    real_stat = shutil.which("stat")
    real_date = shutil.which("date")
    (b / "stat").write_text(
        f'#!/bin/sh\nfor a in "$@"; do case "$a" in *%Y* | *%m*) exit 1 ;; esac; done\n'
        f'exec {real_stat} "$@"\n'
    )
    (b / "date").write_text(
        f'#!/bin/sh\nfor a in "$@"; do [ "$a" = -r ] && exit 1; done\nexec {real_date} "$@"\n'
    )
    for f in b.iterdir():
        f.chmod(0o755)
    return b


@requires_curl
def test_an_unreadable_mtime_keeps_the_restart_and_says_the_check_was_skipped(tmp_path: Path):
    """D4: when either mtime cannot be read the frozen check does not run --
    today's restart applies, and ONE line says the check was skipped."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    off = _offset(home, size + 500)  # newer than the transcript: frozen, IF it could be read
    _flush_cfg(home, _refused_url())
    shim = _shim_bin(tmp_path)
    res = _fire(home, t, work, PATH=f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    assert res.returncode == 0, res.stderr
    log = _log(home)
    assert (
        f"shrink check skipped: an mtime could not be read ({t} or {off}) — the frozen-copy "
        "check did not run, the shrink rules apply ev=Backfill sid=bd162e9f"
    ) in log, log
    assert "reason=shrank — restarting from 0" in log, log
    _no_markers(home)
    assert off.read_text() == "0", off.read_text()


def test_a_copy_under_CLAUDE_CONFIG_DIR_covers_too(tmp_path: Path):
    """The second root the state sweep probes is a root here as well."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    cfgdir = tmp_path / "cfgroot"
    stale = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    live = _write(cfgdir / "projects" / "-home-u-elsewhere" / f"{SID}.jsonl", _lines(work, 40))
    off = _offset(home, live.stat().st_size)
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(home, stale, work, CLAUDE_CONFIG_DIR=str(cfgdir))
        assert res.returncode == 0, res.stderr
        assert rec.requests == []
    assert off.read_text() == str(live.stat().st_size)
    (line,) = _stale_lines(home)
    assert f"({live}) reason=stale-copy " in line, line


@pytest.mark.parametrize("scenario", ["stale-copy", "diverged", "behind", "genuine-shrink"])
def test_the_connector_arm_skips_a_stale_copy_too(tmp_path: Path, scenario: str):
    """(h) The CONNECTOR arm had its own shrank restart: the same verdict guards
    it -- a stale copy is skipped, a diverged or behind one refused with its
    marker (nothing relayed, offset untouched); a genuine shrink still restarts."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = stale.stat().st_size + 300
    if scenario == "diverged":
        checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    if scenario == "diverged":
        _grow(stale, work)
    if scenario in ("behind", "genuine-shrink"):
        live.unlink()
        _age(off, OLD + 3600 if scenario == "behind" else OLD - 3600)
    size = stale.stat().st_size
    assert size < checkpoint
    res = _fire(home, stale, work, ev="SessionEnd", RELAY_REC=str(rec_dir))
    assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
    log = _log(home)
    if scenario == "genuine-shrink":
        assert "connector " in log and "reason=shrank — restarting from 0" in log, log
        assert _stale_lines(home) == []
        _no_markers(home)
        assert (rec_dir / ".n").exists(), "the genuine shrink was not re-relayed"
        return
    assert off.read_text() == str(checkpoint), log
    assert not (rec_dir / ".n").exists(), f"a {scenario} copy was relayed"
    assert "restarting from 0" not in log and "reason=shrank" not in log, log
    (line,) = _stale_lines(home)
    if scenario == "stale-copy":
        assert f"({live}) reason=stale-copy — skipped, no restart " in line, line
        _no_markers(home)
    elif scenario == "diverged":
        assert f"({live}) but its tail differs reason=stale-copy-diverged — refused" in line, line
        _assert_marker(home, "diverged", stale, checkpoint, live)
    else:
        assert "moved after this file last changed reason=behind — refused" in line, line
        assert f"kb-flush-backfill.sh --seed {SID} current|<bytes>" in line, line
        _assert_marker(home, "behind", stale, checkpoint, None)


# ---------------------------------------------------------------------------
# the copies helper: one block, identical semantics in both scripts
# ---------------------------------------------------------------------------


def _copies_block(path: Path) -> str:
    text = path.read_text()
    start = text.index("# >>> KB SESSION COPIES v1")
    end = text.index("\n", text.index("# <<< KB SESSION COPIES v1")) + 1
    return text[start:end]


def test_the_copies_block_is_byte_identical_in_the_hook_and_the_sweep():
    assert _copies_block(FLUSH) == _copies_block(BACKFILL)
    for path in (FLUSH, BACKFILL):
        text = path.read_text()
        assert text.index("# >>> KB SESSION COPIES v1") > text.index(
            "# <<< END KB PORTABLE BLOCK"
        ), f"{path.name}: the copies helper must live OUTSIDE the portable block"


@pytest.mark.parametrize("script", [FLUSH, BACKFILL], ids=["hook", "sweep"])
def test_kb_session_copies_lists_every_other_copy_once(tmp_path: Path, script: Path):
    home = tmp_path / "h"
    cfg = tmp_path / "cfg"
    extra = tmp_path / "extra"
    judged = _write(_projects(home) / "-a" / f"{SID}.jsonl", b"a\n")
    want = {
        _write(_projects(home) / "-b" / f"{SID}.jsonl", b"bb\n"),
        _write(cfg / "projects" / "-c" / f"{SID}.jsonl", b"ccc\n"),
        _write(extra / "-d" / f"{SID}.jsonl", b"dddd\n"),
    }
    # Never a copy: two levels down, another session, a sub-agent transcript.
    _write(_projects(home) / "-b" / "deeper" / f"{SID}.jsonl", b"no\n")
    _write(_projects(home) / "-b" / "other-session.jsonl", b"no\n")
    _write(_projects(home) / "-b" / SID / "subagents" / f"{SID}.jsonl", b"no\n")

    def run(sid: str, cfgdir: str, extra_root: str) -> list[str]:
        res = subprocess.run(
            [
                "bash",
                "-c",
                _copies_block(script) + 'kb_session_copies "$@"',
                "-",
                sid,
                str(judged),
                extra_root,
            ],
            env={**_ENV, "HOME": str(home), "CLAUDE_CONFIG_DIR": cfgdir},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0 and res.stderr == "", res.stderr
        return res.stdout.splitlines()

    got = run(SID, str(cfg), str(extra))
    assert sorted(got) == sorted(f"{p.stat().st_size} {p}" for p in want), got
    # The SAME root reached twice (CLAUDE_CONFIG_DIR=~/.claude and --projects=its projects): once.
    twice = run(SID, str(home / ".claude") + "/", str(_projects(home)))
    assert twice == [f"3 {_projects(home) / '-b' / f'{SID}.jsonl'}"], twice
    # An id that is not a plain session id never becomes a glob.
    assert run("*", str(cfg), str(extra)) == []
    assert run("../-b/" + SID, str(cfg), str(extra)) == []


def test_the_tail_check_survives_a_tail_that_panics_on_EPIPE(tmp_path: Path):
    """uutils' tail panics when head closes the pipe: its stderr is dropped and
    the verdict still comes from the bytes."""
    shim = tmp_path / "bin"
    shim.mkdir()
    real = subprocess.run(
        ["bash", "-c", "command -v tail"], capture_output=True, text=True
    ).stdout.strip()
    (shim / "tail").write_text(
        f'#!/bin/sh\n{real} "$@"\necho "thread \'main\' panicked: Broken pipe" >&2\nexit 101\n'
    )
    (shim / "tail").chmod(0o755)
    work = tmp_path / "w"
    stale = _write(tmp_path / "s.jsonl", _lines(work, 30))
    live = _write(tmp_path / "l.jsonl", _lines(work, 40))
    other = _write(tmp_path / "o.jsonl", _lines(work, 40, tag="Q"))
    env = {**_ENV, "PATH": f"{shim}:{_ENV['PATH']}"}
    for copy, expect in ((live, "0"), (other, "1")):
        res = subprocess.run(
            [
                "bash",
                "-c",
                _copies_block(FLUSH) + 'kb_tail_matches "$@"; echo $?',
                "-",
                str(stale),
                str(stale.stat().st_size),
                str(copy),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.stdout.strip() == expect and res.stderr == "", (res.stdout, res.stderr)


# ---------------------------------------------------------------------------
# (d) the sweep: stale-copy, never fired, never unflushed
# ---------------------------------------------------------------------------


def _sweep(home: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(BACKFILL), *args],
        env={**_ENV, "HOME": str(home), "KB_BACKFILL_NOW": str(NOW), **env},
        capture_output=True,
        text=True,
        timeout=180,
    )


def _sweep_last(home: Path) -> dict[str, str]:
    return dict(kv.split("=", 1) for kv in (_state(home) / ".sweep-last").read_text().split())


def _recording_standin(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in hook that only records the events the sweep hands it."""
    fired = tmp_path / "fired"
    standin = tmp_path / "standin.sh"
    standin.write_text(
        f'#!/usr/bin/env bash\n{_LINEAGE}\ncat >>"{fired}"\necho >>"{fired}"\nexit 0\n'
    )
    standin.chmod(0o755)
    return standin, fired


def _fired_paths(fired: Path) -> list[str]:
    if not fired.exists():
        return []
    events, text, dec = [], fired.read_text(), json.JSONDecoder()
    while text.strip():
        event, end = dec.raw_decode(text.lstrip())
        events.append(event["transcript_path"])
        text = text.lstrip()[end:]
    return events


def _rows(out: str) -> list[tuple[list[str], str]]:
    """(columns, reason) for every transcript row of the dry-run table."""
    rows: list[tuple[list[str], str]] = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) == 6 and cols[1] == SID:
            rows.append((cols, ""))
        elif rows and line.lstrip().startswith("-- ") and not rows[-1][1]:
            rows[-1] = (rows[-1][0], line.strip())
    return rows


@pytest.mark.parametrize("live_state", ["ready", "flushed"])
def test_the_sweep_lists_a_stale_copy_and_counts_it_nowhere(tmp_path: Path, live_state: str):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    last = len(_lines(work, 1, start=39))
    checkpoint = live.stat().st_size - (last if live_state == "ready" else 0)
    _offset(home, checkpoint)
    res = _sweep(home)
    assert res.returncode == 0, res.stdout + res.stderr
    rows = {int(cols[2]): (cols, reason) for cols, reason in _rows(res.stdout)}
    assert set(rows) == {stale.stat().st_size, live.stat().st_size}, res.stdout
    cols, reason = rows[stale.stat().st_size]
    assert cols[5] == "stale-copy" and cols[4] == "0", res.stdout
    assert str(live.parent) in reason and "no restart" in reason, reason
    assert "a byte-prefix of that copy" in reason, reason
    lcols, _ = rows[live.stat().st_size]
    assert lcols[5] == live_state, res.stdout
    remain = live.stat().st_size - checkpoint
    assert (
        f"2 transcript(s): {1 if remain else 0} with unflushed bytes ({remain} bytes)" in res.stdout
    ), res.stdout
    assert "1 stale copy(ies)" in res.stdout, res.stdout
    assert "restarts from 0" not in res.stdout


def test_the_sweep_still_says_shrank_without_a_covering_copy(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    _write(_projects(home) / "-home-u-other" / f"{SID}.jsonl", _lines(work, 20))
    off = _offset(home, t.stat().st_size + 500)
    _age(off, OLD - 3600)  # rewritten after the checkpoint: a genuine shrink
    res = _sweep(home)
    rows = {int(cols[2]): cols for cols, _ in _rows(res.stdout)}
    assert rows[t.stat().st_size][5] == "shrank", res.stdout
    assert "stale copy" not in res.stdout
    assert "the hook restarts from 0 (reason=shrank)" in res.stdout, res.stdout


def test_a_push_never_fires_the_stale_copy_and_records_stale_in_sweep_last(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (home / ".claude" / "kb-flush.env").write_text("KB_REST_URL=http://127.0.0.1:9\n")
    stale, live = _resumed_session(home, work)
    last = len(_lines(work, 1, start=39))
    _offset(home, live.stat().st_size - last)
    fired = tmp_path / "fired"
    standin = tmp_path / "standin.sh"
    standin.write_text(
        f'#!/usr/bin/env bash\n{_LINEAGE}\ncat >>"{fired}"\necho >>"{fired}"\nexit 0\n'
    )
    standin.chmod(0o755)
    res = _sweep(home, "--push", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0, res.stdout + res.stderr
    events, text, dec = [], fired.read_text(), json.JSONDecoder()
    while text.strip():
        event, end = dec.raw_decode(text.lstrip())
        events.append(event)
        text = text.lstrip()[end:]
    assert [e["transcript_path"] for e in events] == [str(live)], events
    sweep_last = (_state(home) / ".sweep-last").read_text()
    assert sweep_last.rstrip("\n").endswith(" stale=1 diverged=0 behind=0"), sweep_last
    assert f" unflushed={last} " in sweep_last, sweep_last
    _no_markers(home)  # a pure prefix never earns an owner-visible marker
    # Every existing reader keys on its own ` <name>=`: ` stalled=` is untouched.
    for key, want in (
        ("stalled", "0"),
        ("unknown", "0"),
        ("stale", "1"),
        ("diverged", "0"),
        ("behind", "0"),
    ):
        got = subprocess.run(
            ["sed", "-n", rf"s/.* {key}=\([0-9]*\).*/\1/p"],
            input=sweep_last,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert got == want, (key, got, sweep_last)


# ---------------------------------------------------------------------------
# (e) --seed takes the LARGEST copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", [SID, SID[:8]], ids=["full-id", "prefix"])
def test_seed_current_takes_the_largest_copy(tmp_path: Path, given: str):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    # The frozen copy sorts FIRST, so a first-match seed would have used it.
    stale, live = _resumed_session(home, work, stale_dir="-a-first")
    res = _sweep(home, "--seed", given, "current")
    assert res.returncode == 0, res.stdout + res.stderr
    off = _state(home) / f"{_flush_okey(SID)}.offset"
    assert off.read_text() == str(live.stat().st_size), off.read_text()
    assert (
        f"copies: 2 transcripts carry session bd162e9f -- seeding from the largest, {live} "
        f"({live.stat().st_size} bytes)" in res.stdout
    ), res.stdout


def test_seed_still_refuses_a_prefix_shared_by_two_sessions(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _write(_projects(home) / "-a" / "bd162e9f-aaaa.jsonl", _lines(work, 3))
    _write(_projects(home) / "-b" / "bd162e9f-bbbb.jsonl", _lines(work, 5))
    res = _sweep(home, "--seed", "bd162e9f", "current")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "matches 2 transcripts" in res.stderr, res.stderr


# ---------------------------------------------------------------------------
# MUST-FIX 1: a DIVERGED copy is refused VISIBLY and counted unflushed
# ---------------------------------------------------------------------------


def _notify_kb(tmp_path: Path, home: Path, transcript: Path, work: Path) -> str:
    """The notify ping for <transcript> of SID, through a recording curl."""
    rec_dir = tmp_path / f"notify-{len(list(tmp_path.glob('notify-*')))}"
    binp = _recording_curl(home, rec_dir)
    (home / ".claude" / "notify.env").write_text(
        "NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=some-topic\nNTFY_MACHINE=testbox\n"
    )
    event = {
        "session_id": SID,
        "cwd": str(work),
        "hook_event_name": "Stop",
        "transcript_path": str(transcript),
    }
    res = _run_notify(home, binp, rec_dir, event)
    assert res.returncode == 0, res.stderr
    return _notify_message(rec_dir)


@pytest.mark.parametrize("flavor", ["grown", "rewritten"])
def test_a_diverged_copy_is_refused_visibly_and_counted_unflushed(tmp_path: Path, flavor: str):
    """(a) The copy behind the checkpoint GROWS: the session was resumed back in
    its first directory after the other copy carried the shared checkpoint past
    it ("grown"), or the copy that reaches the checkpoint is not a superset of
    it ("rewritten"). Its records past the shared prefix were never sent, so a
    silent skip would lose them while everything reads `synced`: refused, no
    restart, the marker on every firing, notify and the sweep say so."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work, diverged=flavor == "rewritten")
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    if flavor == "grown":
        _grow(stale, work)
    size = stale.stat().st_size
    assert size < checkpoint
    before = off.read_bytes()
    okey = _flush_okey(SID)
    # A silent stale-copy day (its own fresh stamp) never mutes the diverged line.
    stalecopy = _state(home) / f"{okey}.stalecopy"
    stalecopy.touch()
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        for ev in ("PreCompact", "SessionEnd"):
            res = _fire(home, stale, work, ev=ev)
            assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
            assert off.read_bytes() == before, "a diverged copy must never write the checkpoint"
        assert rec.requests == [], "a diverged copy sent bytes"
    log = _log(home)
    assert "restarting from 0" not in log and "reason=shrank" not in log, log
    (line,) = _stale_lines(home)  # two firings, ONE line (24 h)
    assert re.fullmatch(
        r"\S+ flush \S+ stale copy: "
        + re.escape(
            f"{stale} ({size}) is behind the checkpoint {checkpoint}; a newer copy reaches it "
            f"({live}) but its tail differs reason=stale-copy-diverged — refused, nothing sent, "
            f"no restart: records past the shared prefix may be unflushed (state/{okey}.diverged) "
            "ev=PreCompact sid=bd162e9f"
        ),
        line,
    ), line
    _assert_marker(home, "diverged", stale, checkpoint, live)
    assert _marker(home, "diverged.stamp").exists()
    assert not _marker(home, "behind").exists()
    assert stalecopy.exists(), "the kinds share no stamp"

    # EVERY firing rewrites the marker; the line stays once per 24 h.
    _age(_marker(home, "diverged"), OLD)
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        assert _fire(home, stale, work, ev="PreCompact").returncode == 0
        assert rec.requests == []
    assert _marker(home, "diverged").stat().st_mtime > OLD
    assert len(_stale_lines(home)) == 1, _log(home)

    # notify: never `synced` while the marker exists -- the frozen copy and the
    # live one share the session, and the session's capture is in doubt.
    for transcript in (stale, live):
        msg = _notify_kb(tmp_path, home, transcript, work)
        assert re.search(r"\(kb \?/\d+(\.\d)?[KMG]?B diverged\)$", msg), msg
        assert "synced" not in msg, msg

    # The sweep tables it `diverged` (REMAIN = the whole file), never fires it,
    # and counts it in unflushed= and diverged=.
    res = _sweep(home)
    assert res.returncode == 0, res.stdout + res.stderr
    rows = {int(cols[2]): (cols, reason) for cols, reason in _rows(res.stdout)}
    cols, reason = rows[size]
    assert cols[5] == "diverged" and cols[4] == str(size), res.stdout
    assert "NOT sent" in reason, reason
    # Review 2 MUST-FIX 2: while the live copy reaches the checkpoint a --seed
    # cannot settle it (the next pass re-marks it): the remedy is a move.
    assert (
        "Owner: a --seed cannot settle this while a copy reaches the checkpoint (the next "
        f"--push/--auto or firing marks it again, and a seed below {checkpoint} re-sends the "
        "live copy). To accept the loss, move this copy out of ~/.claude/projects (the marker "
        "then clears); to keep its records, leave it in place until the per-path checkpoint wave"
    ) in reason, reason
    assert "kb-flush-backfill.sh --seed" not in reason, reason
    assert "1 diverged copy(ies)" in res.stdout, res.stdout
    standin, fired = _recording_standin(tmp_path)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and res.stdout == "", res.stdout + res.stderr
    assert _fired_paths(fired) == [], "--auto fired a diverged copy"
    last = _sweep_last(home)
    assert (last["diverged"], last["unflushed"], last["stale"], last["behind"]) == (
        "1",
        str(size),
        "0",
        "0",
    ), last
    assert off.read_bytes() == before


def test_the_brief_counts_diverged_and_behind_as_failures(tmp_path: Path):
    """D6: diverged=/behind= in .sweep-last, or the markers, reach the ONE
    [KB sweep] line (ASCII); the hook's refusal lines are failure tokens."""
    home, work = _recall_home(tmp_path)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir()
    (state / ".sweep-last").write_text(
        f"ts={int(time.time()) - 600} rc=0 mode=auto ready=0 pushed=0 failed=0 deferred=0 "
        "stalled=0 unknown=0 live=0 unflushed=9000 busy=0 partial=0 stale=1 diverged=2 behind=0\n"
    )
    (state / "x-sess.behind").write_text("2026-10-11T00:00:00Z /gone/x.jsonl size=1 checkpoint=2\n")
    okey = _flush_okey(SID)
    (home / ".claude" / "hooks" / "flush-session.log").write_text(
        "2026-10-11T00:00:00+00:00 flush recallproj stale copy: /p/x.jsonl (10) is behind the "
        "checkpoint 20; a newer copy reaches it (/q/x.jsonl) but its tail differs "
        "reason=stale-copy-diverged — refused, nothing sent, no restart: records past the "
        f"shared prefix may be unflushed (state/{okey}.diverged) ev=PreCompact sid=bd162e9f\n"
    )
    lines, out = _brief_sweep_line(tmp_path, home, work)
    assert len(lines) == 1, out
    line = lines[0]
    assert line.isascii(), line
    assert (
        "2 diverged transcript copy(ies): records a resumed copy holds past the shared "
        "checkpoint were NOT sent"
    ) in line, line
    assert (
        "1 transcript copy(ies) behind the checkpoint with no copy reaching it need an owner "
        "decision: run kb-flush-backfill.sh and use the exact --seed its row names (a copy "
        "resumed since it was marked seeds at its frozen= size, never current)"
    ) in line, line
    # Never a generic `current` seed nor `frozen` asserted for a resumed copy.
    assert "<bytes|current>" not in line and "(frozen, never restarted)" not in line, line
    assert "KB session-flush reported a recent failure" in out, out
    assert "reason=stale-copy-diverged" in out, out


# ---------------------------------------------------------------------------
# MUST-FIX 2: a FROZEN copy whose covering copy is gone is BEHIND, never restarted
# ---------------------------------------------------------------------------


def test_a_frozen_copy_whose_live_copy_is_gone_is_behind_never_restarted(tmp_path: Path):
    """(c) The incident again, minus the covering copy: the live copy was deleted
    and the frozen one is older than the checkpoint's last write. (d) Then the
    frozen copy is RESUMED (appended: its mtime is now newer) -- the marker names
    it, so it is still never restarted from 0."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    live.unlink()
    _age(off, OLD + 3600)  # the checkpoint moved AFTER the frozen copy last changed
    before = off.read_bytes()
    okey = _flush_okey(SID)
    size = stale.stat().st_size
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(home, stale, work, ev="PreCompact")
        assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
        assert rec.requests == [], "a behind copy sent bytes"
    assert off.read_bytes() == before
    log = _log(home)
    assert "restarting from 0" not in log and "reason=shrank" not in log, log
    (line,) = _stale_lines(home)
    assert re.fullmatch(
        r"\S+ flush \S+ stale copy: "
        + re.escape(
            f"{stale} ({size}) is behind the checkpoint {checkpoint}; no copy reaches it and the "
            "checkpoint moved after this file last changed reason=behind — refused, nothing sent, "
            f"no restart: an owner decision, seed it: kb-flush-backfill.sh --seed {SID} "
            f"current|<bytes> (state/{okey}.behind) ev=PreCompact sid=bd162e9f"
        ),
        line,
    ), line
    _assert_marker(home, "behind", stale, checkpoint, None)
    assert not _marker(home, "diverged").exists()
    msg = _notify_kb(tmp_path, home, stale, work)
    assert re.search(r"\(kb \?/\d+(\.\d)?[KMG]?B behind\)$", msg) and "synced" not in msg, msg

    # The sweep: `behind`, an owner decision -- never fired, not unflushed.
    res = _sweep(home)
    assert res.returncode == 0, res.stdout + res.stderr
    ((cols, reason),) = _rows(res.stdout)
    assert cols[5] == "behind" and cols[4] == "0", res.stdout
    assert "frozen" in reason and f"--seed {SID} current" in reason, reason
    assert "1 behind the checkpoint" in res.stdout, res.stdout
    standin, fired = _recording_standin(tmp_path)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _fired_paths(fired) == [], "--auto fired a behind copy"
    last = _sweep_last(home)
    assert (last["behind"], last["unflushed"], last["diverged"]) == ("1", "0", "0"), last
    res = _sweep(home, "--push", "--include-unknown", "--no-quiet", "--hook", str(standin))
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout

    # (d) RESUMED in its first directory: newer than the checkpoint now, still
    # behind it, and the marker names it -- refused, never a restart (D2).
    _grow(stale, work, 2)
    assert stale.stat().st_mtime > off.stat().st_mtime
    assert stale.stat().st_size < checkpoint
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        assert _fire(home, stale, work, ev="SessionEnd").returncode == 0
        assert rec.requests == []
    assert off.read_bytes() == before
    assert "restarting from 0" not in _log(home), _log(home)
    assert len(_stale_lines(home)) == 1, "the behind line is once per 24 h"
    # Refreshed with the new size; frozen= keeps where its unsent records begin.
    _assert_marker(home, "behind", stale, checkpoint, None, frozen=size)
    _age(_marker(home, "behind.stamp"), OLD)
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        assert _fire(home, stale, work, ev="PreCompact").returncode == 0
        assert rec.requests == []
    second = _stale_lines(home)[-1]
    assert (
        f"no copy reaches it and state/{okey}.behind names this file reason=behind — refused"
    ) in second, second
    assert (
        f"resumed since it was marked: its records past {size} were NOT sent -- "
        f"kb-flush-backfill.sh --seed {SID} {size} sends exactly them (state/{okey}.behind)"
    ) in second, second
    assert "current" not in second, second
    res = _sweep(home)
    ((cols, reason),) = _rows(res.stdout)
    assert cols[5] == "behind" and f"state/{okey}.behind names this file" in reason, res.stdout
    assert cols[4] == str(stale.stat().st_size - size), res.stdout
    assert off.read_bytes() == before


def test_the_sweep_marks_a_frozen_copy_so_its_later_resume_is_no_restart(tmp_path: Path):
    """Nothing ever FIRES a frozen copy (behind is never fireable), so the sweep
    pass (--auto/--push, never a dry run) writes the marker itself: a later
    resume of that copy -- its mtime newer than the checkpoint, no marker from
    a hook firing -- is then refused instead of re-sent from 0."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    live.unlink()
    _age(off, OLD + 3600)
    before = off.read_bytes()
    _flush_cfg(home, _refused_url())
    assert _sweep(home).returncode == 0
    assert not _marker(home, "behind").exists(), "a dry run wrote state"
    standin, fired = _recording_standin(tmp_path)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout + res.stderr
    _assert_marker(home, "behind", stale, checkpoint, None)
    _grow(stale, work, 2)  # resumed: newer than the checkpoint
    assert _fire(home, stale, work, ev="SessionEnd").returncode == 0
    assert "restarting from 0" not in _log(home), _log(home)
    assert off.read_bytes() == before
    (line,) = _stale_lines(home)
    assert "names this file reason=behind — refused" in line, line


# ---------------------------------------------------------------------------
# D3: markers clear only with the offset, by --seed, or when their path is gone
# ---------------------------------------------------------------------------


def _plant_markers(home: Path, path: Path, checkpoint: int) -> None:
    for kind in ("diverged", "behind"):
        _marker(home, kind).write_text(
            f"2026-10-11T00:00:00Z {path} size=100 checkpoint={checkpoint}\n"
        )
        _marker(home, f"{kind}.stamp").touch()


def test_a_marker_naming_a_removed_copy_is_cleared_by_the_next_firing(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    live = _write(_projects(home) / "-home-u-binary-rewriter" / f"{SID}.jsonl", _lines(work, 40))
    checkpoint = live.stat().st_size - len(_lines(work, 1, start=39))
    off = _offset(home, checkpoint)
    gone = _projects(home) / "-home-u" / f"{SID}.jsonl"  # the owner removed that copy
    _plant_markers(home, gone, checkpoint)
    okey = _flush_okey(SID)
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(home, live, work)
        assert res.returncode == 0, res.stderr
        assert rec.requests, "the live copy's delta was not sent"
    _no_markers(home)
    log = _log(home)
    for kind in ("diverged", "behind"):
        assert (
            f"flush work marker cleared: state/{okey}.{kind} named {gone}, which no longer "
            "exists ev=Backfill sid=bd162e9f"
        ) in log, log
    assert off.read_text() == str(live.stat().st_size)


def test_a_sweep_pass_clears_a_marker_whose_copy_is_gone_and_a_dry_run_does_not(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    live = _write(_projects(home) / "-home-u-binary-rewriter" / f"{SID}.jsonl", _lines(work, 40))
    _offset(home, live.stat().st_size)
    gone = _projects(home) / "-home-u" / f"{SID}.jsonl"
    _plant_markers(home, gone, live.stat().st_size)
    _flush_cfg(home, "http://127.0.0.1:9")
    assert _sweep(home).returncode == 0
    assert _marker(home, "diverged").exists() and _marker(home, "behind").exists()
    standin, _fired = _recording_standin(tmp_path)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0, res.stdout + res.stderr
    _no_markers(home)
    okey = _flush_okey(SID)
    for kind in ("diverged", "behind"):
        assert (
            f"backfill bd162e9f ? marker cleared: state/{okey}.{kind} named {gone}, which no "
            "longer exists"
        ) in _log(home), _log(home)
    # A live marker (its path exists) survives a sweep pass untouched.
    _plant_markers(home, live, live.stat().st_size)
    assert _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20").returncode == 0
    assert _marker(home, "diverged").exists() and _marker(home, "behind").exists()


def test_seed_clears_the_sessions_markers_and_says_so(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    _offset(home, live.stat().st_size)
    _plant_markers(home, stale, live.stat().st_size)
    res = _sweep(home, "--seed", SID, "current")
    assert res.returncode == 0, res.stdout + res.stderr
    _no_markers(home)
    okey = _flush_okey(SID)
    for kind in ("diverged", "behind"):
        assert (
            f"cleared: state/{okey}.{kind} (it named {stale}) -- the seed is the owner "
            "decision for this session"
        ) in res.stdout, res.stdout
    assert "cleared: diverged behind" in _log(home), _log(home)


def test_the_state_sweep_retires_the_markers_with_the_offset(tmp_path: Path):
    """kb_state_sweep (a retired session: offset > 30 days old, transcript gone
    under both roots) takes the markers and their stamps with the offset."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    state = _state(home)
    okey = _flush_okey(SID)
    old = int(time.time()) - 45 * 86400
    retired = [
        state / f"{okey}.{suffix}"
        for suffix in (
            "offset",
            "diverged",
            "behind",
            "diverged.stamp",
            "behind.stamp",
            "stalecopy",
            "seen",  # KB 51863: the session's breadcrumb goes with its offset
        )
    ]
    for f in retired:
        f.write_text("123\n" if f.suffix == ".offset" else "x\n")
        _age(f, old)
    # Any other session's drain runs the daily state sweep in its detached child.
    (state / ".checkpoints-trusted-since").write_text("2000-01-01T00:00:00Z\n")
    other = work / "other.jsonl"
    other.write_bytes(_lines(work, 5))
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        event = {
            "transcript_path": str(other),
            "cwd": str(work),
            "session_id": "aaaaaaaa-0000-4000-8000-000000000001",
            "hook_event_name": "PreCompact",
        }
        res = _run_hook(FLUSH, event, home, extra_env={"KB_FLUSH_FOREGROUND": "1"})
        assert res.returncode == 0, res.stderr
    assert f"flush state sweep: retired sid={okey} transcript gone" in _log(home), _log(home)
    left = [f.name for f in retired if f.exists()]
    assert left == [], left


# ---------------------------------------------------------------------------
# MUST-FIX 5: a row the hook refuses at fire time is never booked as pushed
# ---------------------------------------------------------------------------


def _racing_standin(
    tmp_path: Path, home: Path, kind: str, t: Path, checkpoint: int, other: Path | None = None
) -> tuple[Path, Path]:
    """The race: the LIVE copy covers the row between discovery and firing. For
    SID this stand-in advances the SHARED checkpoint past the copy and logs the
    hook's refusal line (``silent``: only touches the stale-copy stamp, as a
    rate-limited firing does; ``silent-diverged`` / ``silent-behind``: writes
    the marker naming the copy in kb_marker_write's exact format and touches
    that kind's stamp, logging nothing -- the hook's rate-limited refusal); any
    other session is drained to its size."""
    okey = _flush_okey(SID)
    live = _projects(home) / "-home-u-binary-rewriter" / f"{SID}.jsonl"
    why = {
        "stale-copy": f"a newer copy covers it ({live}) reason=stale-copy — skipped, no restart",
        "diverged": (
            f"a newer copy reaches it ({live}) but its tail differs reason=stale-copy-diverged "
            "— refused, nothing sent, no restart: records past the shared prefix may be "
            f"unflushed (state/{okey}.diverged)"
        ),
        "behind": (
            "no copy reaches it and the checkpoint moved after this file last changed "
            "reason=behind — refused, nothing sent, no restart: an owner decision, seed it: "
            f"kb-flush-backfill.sh --seed {SID} current|<bytes> (state/{okey}.behind)"
        ),
    }
    state = _state(home)
    logf = home / ".claude" / "hooks" / "flush-session.log"
    linef = tmp_path / "line.txt"
    if kind == "silent":
        action = f'touch "{state}/{okey}.stalecopy"'
    elif kind.startswith("silent-"):
        mk = kind.split("-", 1)[1]
        size = t.stat().st_size
        action = (
            "printf '%s %s size=%s checkpoint=%s frozen=%s\\n' "
            f'"$(date -u +%Y-%m-%dT%H:%M:%SZ)" "{t}" {size} {checkpoint} {size} '
            f'>"{state}/{okey}.{mk}"\ntouch "{state}/{okey}.{mk}.stamp"'
        )
    else:
        linef.write_text(
            f"2026-10-11T00:00:00+00:00 flush work stale copy: {t} ({t.stat().st_size}) is "
            f"behind the checkpoint {checkpoint}; {why[kind]} ev=Backfill sid=bd162e9f\n"
        )
        action = f'cat "{linef}" >>"{logf}"'
    fired = tmp_path / "fired"
    other_line = ""
    if other is not None:
        other_line = (
            f'printf %s {other.stat().st_size} >"{state}/{_flush_okey(other.stem)}.offset"\n'
        )
    standin = tmp_path / "racing.sh"
    standin.write_text(
        f"#!/usr/bin/env bash\n{_LINEAGE}\nev=$(cat)\n"
        f'case "$ev" in\n*{SID}*)\n'
        f'printf %s {checkpoint} >"{state}/{okey}.offset"\n{action}\n;;\n'
        f'*)\nprintf \'%s\\n\' "$ev" >>"{fired}"\n'
        f'printf \'KB_FLUSH_MAX=%s\\n\' "$KB_FLUSH_MAX" >>"{tmp_path}/fired.env"\n'
        f"{other_line};;\nesac\nexit 0\n"
    )
    standin.chmod(0o755)
    return standin, fired


_KINDS = [
    ("stale-copy", "skipped", "stale"),
    ("silent", "skipped", "stale"),
    ("diverged", "refused", "diverged"),
    ("behind", "refused", "behind"),
    ("silent-diverged", "refused", "diverged"),
    ("silent-behind", "refused", "behind"),
]
_FIELD = {"stale-copy": "stale", "silent-diverged": "diverged", "silent-behind": "behind"}


@pytest.mark.parametrize(("kind", "verdict", "field"), _KINDS, ids=[k[0] for k in _KINDS])
def test_a_row_refused_at_fire_time_is_never_booked_as_pushed(
    tmp_path: Path, kind: str, verdict: str, field: str
):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _flush_cfg(home, "http://127.0.0.1:9")
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    base = size - len(_lines(work, 1, start=29))
    _offset(home, base)  # `ready` at discovery
    checkpoint = size + 100000  # the live copy's checkpoint, far past this file
    standin, _fired = _racing_standin(tmp_path, home, kind, t, checkpoint)
    res = _sweep(home, "--push", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0, res.stdout + res.stderr
    assert f" ... {verdict} (reason=" in res.stdout, res.stdout
    assert " ... ok" not in res.stdout and "FAILED" not in res.stdout, res.stdout
    counts = {"stale": 0, "diverged": 0, "behind": 0, field: 1}
    assert (
        "kb-flush-backfill: pushed=0 partial=0 failed=0 deferred=0 busy=0 skipped=0 bytes=0 "
        f"stale={counts['stale']} diverged={counts['diverged']} behind={counts['behind']}"
    ) in res.stdout, res.stdout
    last = _sweep_last(home)
    assert (last["pushed"], last["failed"], last[field]) == ("0", "0", "1"), last
    assert f"backfill bd162e9f work {verdict} base={base} size={size} sent=0 took=" in _log(home)
    assert not (_state(home) / f"{_flush_okey(SID)}.sweep").exists(), "a refusal booked a stall"


@pytest.mark.parametrize(
    "kind", ["stale-copy", "diverged", "behind", "silent-diverged", "silent-behind"]
)
def test_a_refused_firing_never_charges_the_sweep_budget(tmp_path: Path, kind: str):
    """--auto: the other copy's checkpoint (CUR-BASE ~100 KB) charged to a 50 KB
    budget would defer every later row; a refusal sent nothing and costs 0."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _flush_cfg(home, "http://127.0.0.1:9")
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    base = size - len(_lines(work, 1, start=29))
    _offset(home, base)
    other_sid = "aaaaaaaa-0000-4000-8000-000000000001"
    other = _write(_projects(home) / "-home-u-other" / f"{other_sid}.jsonl", _lines(work, 20))
    _age(other, OLD + 10)  # fired AFTER the racing row (oldest first)
    (_state(home) / f"{_flush_okey(other_sid)}.offset").write_text(
        str(other.stat().st_size - len(_lines(work, 1, start=19)))
    )
    budget = 50000
    standin, fired = _racing_standin(tmp_path, home, kind, t, size + 100000, other)
    res = _sweep(
        home,
        "--auto",
        "--hook",
        str(standin),
        "--timeout",
        "20",
        KB_SWEEP_BUDGET_BYTES=str(budget),
    )
    assert res.returncode == 0 and res.stdout == "", res.stdout + res.stderr
    assert _fired_paths(fired) == [str(other)], "the later row was deferred"
    assert (tmp_path / "fired.env").read_text() == f"KB_FLUSH_MAX={budget}\n"
    last = _sweep_last(home)
    field = _FIELD.get(kind, kind)
    assert (last["pushed"], last["deferred"], last[field]) == ("1", "0", "1"), last
    assert " sent=0 took=" in next(
        ln for ln in _log(home).splitlines() if "backfill bd162e9f work " in ln
    )


def test_the_marker_helpers_parse_a_path_with_spaces(tmp_path: Path):
    marker = tmp_path / "s.behind"
    target = tmp_path / "a dir with spaces" / "x size=y.jsonl"
    script = _copies_block(FLUSH) + (
        'kb_marker_write "$1" "$2" 10 20 "/c/o v.jsonl"; cat "$1"; '
        'kb_marker_path "$1"; echo; kb_marker_names "$(dirname "$1")" s "$2"; echo " rc=$?"'
    )
    res = subprocess.run(
        ["bash", "-c", script, "-", str(marker), str(target)],
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=30,
    )
    line, path, names = res.stdout.splitlines()
    want = f" {target} size=10 checkpoint=20 cover=/c/o v.jsonl frozen=10"
    assert re.fullmatch(_ISO + re.escape(want), line), line
    # A path that itself contains " size=" is cut at its FIRST occurrence: never a
    # match for another file (the marker then names nothing that exists).
    assert path == str(target).split(" size=")[0], path
    assert names == " rc=1", names
    plain = tmp_path / "a dir with spaces" / "x.jsonl"
    res = subprocess.run(
        ["bash", "-c", script, "-", str(marker), str(plain)],
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.stdout.splitlines()[1:] == [str(plain), "behind rc=0"], res.stdout


def test_the_marker_keeps_the_frozen_size_of_the_path_it_first_named(tmp_path: Path):
    """frozen= (LAST field) is the size a path had when a marker FIRST named it,
    carried by every rewrite naming the same path; a marker written before the
    field existed lends its size=; another path starts afresh."""
    marker = tmp_path / "s.behind"
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    script = _copies_block(BACKFILL) + (
        'kb_marker_write "$1" "$2" 10 20; kb_marker_write "$1" "$2" 15 25; cat "$1"; '
        'kb_marker_frozen "$1"; echo; '
        'kb_marker_write "$1" "$3" 7 25; cat "$1"; '
        'printf "%s %s size=42 checkpoint=50\\n" 2026-10-11T00:00:00Z "$2" >"$1"; '
        'kb_marker_frozen "$1"; echo; kb_marker_write "$1" "$2" 60 70; cat "$1"; '
        'printf "%s %s size=5 checkpoint=6 frozen=x\\n" 2026-10-11T00:00:00Z "$2" >"$1"; '
        'kb_marker_frozen "$1"; echo; kb_marker_frozen "$1.none"; echo " rc=$?"'
    )
    res = subprocess.run(
        ["bash", "-c", script, "-", str(marker), str(a), str(b)],
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=30,
    )
    got = res.stdout.splitlines()
    assert len(got) == 7, res.stdout + res.stderr
    assert re.fullmatch(_ISO + re.escape(f" {a} size=15 checkpoint=25 frozen=10"), got[0]), got
    assert got[1] == "10", got
    assert re.fullmatch(_ISO + re.escape(f" {b} size=7 checkpoint=25 frozen=7"), got[2]), got
    assert got[3] == "42", got  # a pre-frozen marker: its size= stands in
    assert re.fullmatch(_ISO + re.escape(f" {a} size=60 checkpoint=70 frozen=42"), got[4]), got
    assert got[5] == "5", got  # a garbled frozen= falls back to size=
    assert got[6] == " rc=1", got


# ---------------------------------------------------------------------------
# Review 2 MUST-FIX 1: a path a marker names is refused at ANY size (frozen=)
# ---------------------------------------------------------------------------


def _grow_past(path: Path, work: Path, checkpoint: int) -> None:
    """The marked copy keeps growing: records appended until it is PAST the
    shared checkpoint -- where the old hook drained it from that checkpoint,
    starting mid-record."""
    rec = len(_lines(work, 1, start=100, tag="past "))
    n = (checkpoint - path.stat().st_size) // rec + 3
    with path.open("ab") as fh:
        fh.write(_lines(work, n, start=100, tag="past "))
    assert path.stat().st_size > checkpoint


def _received(rec: _Recorder) -> list[tuple[int, bytes]]:
    """(flush_offset, chunk bytes) per request the stub KB took, in offset order."""
    out = []
    for r in rec.requests:
        body = r["payload"]
        if "transcript_gz" in body:
            data = gzip.decompress(base64.b64decode(body["transcript_gz"]))
        else:
            data = body["transcript"].encode()
        out.append((int(body["flush_offset"]), data))
    return sorted(out)


@requires_curl
def test_a_resumed_behind_copy_is_refused_at_any_size_and_seeded_at_its_frozen_size(
    tmp_path: Path,
):
    """D2's own scenario: the live copy tidied away, the frozen copy resumed in
    its directory. (i) Below the checkpoint the marker keeps frozen=F: tabled
    behind with REMAIN = size - F, the remedy names --seed <sid> F, never
    `current`. (ii) Grown PAST the checkpoint it is still refused on every
    event -- a drain from that (another copy's) checkpoint would start
    mid-record and lose [F, checkpoint) for good. (iii) --seed <sid> F then
    sends exactly the records written since the resume."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    live.unlink()
    _age(off, OLD + 3600)  # frozen: the checkpoint moved after this copy last changed
    before = off.read_bytes()
    frozen = stale.stat().st_size
    okey = _flush_okey(SID)
    _flush_cfg(home, _refused_url())
    standin, fired = _recording_standin(tmp_path)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout + res.stderr
    _assert_marker(home, "behind", stale, checkpoint, None)  # frozen= its size now

    # (i) RESUMED, still below the checkpoint.
    _grow(stale, work, 3)
    grown = stale.stat().st_size
    assert frozen < grown < checkpoint
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(home, stale, work, ev="PreCompact", KB_SWEEP_INTERVAL="0")
        assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
        assert rec.requests == [], "a behind copy sent bytes"
    assert off.read_bytes() == before
    _assert_marker(home, "behind", stale, checkpoint, None, frozen=frozen)
    (line,) = _stale_lines(home)
    assert re.fullmatch(
        r"\S+ flush \S+ stale copy: "
        + re.escape(
            f"{stale} ({grown}) is behind the checkpoint {checkpoint}; no copy reaches it and "
            f"state/{okey}.behind names this file reason=behind — refused, nothing sent, no "
            f"restart: resumed since it was marked: its records past {frozen} were NOT sent -- "
            f"kb-flush-backfill.sh --seed {SID} {frozen} sends exactly them "
            f"(state/{okey}.behind) ev=PreCompact sid=bd162e9f"
        ),
        line,
    ), line
    res = _sweep(home)
    ((cols, reason),) = _rows(res.stdout)
    assert cols[5] == "behind" and cols[4] == str(grown - frozen), res.stdout
    assert (
        f"resumed since it was marked: its records past {frozen} were NOT sent -- "
        f"kb-flush-backfill.sh --seed {SID} {frozen} sends exactly them"
    ) in reason, reason
    assert "its bytes were sent" not in reason and f"--seed {SID} current" not in reason, reason
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout + res.stderr
    last = _sweep_last(home)
    assert (last["behind"], last["unflushed"]) == ("1", str(grown - frozen)), last
    _assert_marker(home, "behind", stale, checkpoint, None, frozen=frozen)  # the sweep kept it

    # (ii) GROWN PAST the checkpoint: still refused, on every event.
    _grow_past(stale, work, checkpoint)
    past = stale.stat().st_size
    _age(_marker(home, "behind.stamp"), OLD)  # the day's line is due again
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        for ev in ("Stop", "PreCompact", "SessionEnd"):
            # The Stop gate OPEN (bytes AND interval: the PreCompact leg above
            # wrote .last, and the 300 s default would close it), so Stop alone
            # reaches the refusal -- never a no-op leg.
            res = _fire(
                home,
                stale,
                work,
                ev=ev,
                KB_FLUSH_STOP_MIN_BYTES="1",
                KB_FLUSH_STOP_MIN_INTERVAL="0",
                KB_SWEEP_INTERVAL="0",
            )
            assert res.returncode == 0 and res.stdout == "", (ev, res.stdout, res.stderr)
            assert off.read_bytes() == before, ev
            if ev == "Stop":
                # Stop ALONE rewrote the marker: size=<past>, frozen=F kept.
                _assert_marker(home, "behind", stale, checkpoint, None, frozen=frozen)
                assert _stale_lines(home)[-1].endswith(" ev=Stop sid=bd162e9f"), _log(home)
        assert rec.requests == [], "a marked copy past the checkpoint was drained"
    _assert_marker(home, "behind", stale, checkpoint, None, frozen=frozen)
    log = _log(home)
    assert "restarting from 0" not in log and " ok bytes=" not in log, log
    lines = _stale_lines(home)
    assert len(lines) == 2, log  # one new line for three firings (24 h)
    assert re.fullmatch(
        r"\S+ flush \S+ stale copy: "
        + re.escape(
            f"{stale} ({past}) has grown to the checkpoint {checkpoint} or past it; "
            f"state/{okey}.behind names this file and that checkpoint is another copy's "
            "(draining from it would start mid-record) reason=behind — refused, nothing sent, "
            f"no restart: resumed since it was marked: its records past {frozen} were NOT "
            f"sent -- kb-flush-backfill.sh --seed {SID} {frozen} sends exactly them "
            f"(state/{okey}.behind) ev=Stop sid=bd162e9f"
        ),
        lines[1],
    ), lines[1]
    res = _sweep(home)
    ((cols, reason),) = _rows(res.stdout)
    assert cols[5] == "behind" and cols[4] == str(past - frozen), res.stdout
    assert f"has grown to checkpoint {checkpoint} or past it" in reason, reason
    assert f"--seed {SID} {frozen} sends exactly them" in reason, reason
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], "--auto fired a marked copy"
    last = _sweep_last(home)
    assert (last["behind"], last["unflushed"]) == ("1", str(past - frozen)), last
    assert off.read_bytes() == before

    # (iii) The owner's exact seed: the records written since the resume, no more.
    data = stale.read_bytes()
    assert data[frozen - 1 : frozen] == b"\n", "frozen= is a record boundary"
    res = _sweep(home, "--seed", SID, str(frozen))
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"seeded bd162e9f offset={frozen} (snapped from {frozen})" in res.stdout, res.stdout
    assert off.read_text() == str(frozen)
    _no_markers(home)
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(home, stale, work, ev="SessionEnd", KB_SWEEP_INTERVAL="0")
        assert res.returncode == 0, res.stderr
        got = _received(rec)
    assert got and got[0][0] == frozen, [o for o, _ in got]
    assert b"".join(chunk for _, chunk in got) == data[frozen:]
    assert off.read_text() == str(len(data))
    res = _sweep(home)
    ((cols, _reason),) = _rows(res.stdout)
    assert cols[5] == "flushed", res.stdout


@pytest.mark.parametrize("kind", ["behind", "diverged"])
def test_the_connector_arm_refuses_a_marked_copy_grown_past_the_checkpoint(
    tmp_path: Path, kind: str
):
    """(ii) on the CONNECTOR arm: nothing relayed, the offset untouched, the
    marker still naming the copy with its frozen= kept."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    frozen = stale.stat().st_size
    if kind == "behind":
        live.unlink()
    _marker(home, kind).write_text(
        f"2026-10-11T00:00:00Z {stale} size={frozen} checkpoint={checkpoint} frozen={frozen}\n"
    )
    _grow_past(stale, work, checkpoint)
    res = _fire(home, stale, work, ev="SessionEnd", RELAY_REC=str(rec_dir), KB_SWEEP_INTERVAL="0")
    assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
    assert off.read_text() == str(checkpoint), _log(home)
    assert not (rec_dir / ".n").exists(), f"a marked {kind} copy was relayed"
    _assert_marker(home, kind, stale, checkpoint, None, frozen=frozen)
    okey = _flush_okey(SID)
    (line,) = _stale_lines(home)
    label = "stale-copy-diverged" if kind == "diverged" else "behind"
    assert (
        f"stale copy: {stale} ({stale.stat().st_size}) has grown to the checkpoint {checkpoint} "
        f"or past it; state/{okey}.{kind} names this file and that checkpoint is another "
        f"copy's (draining from it would start mid-record) reason={label} — refused, nothing "
        "sent, no restart"
    ) in line, line


@requires_curl
def test_a_diverged_copy_grown_past_the_checkpoint_is_refused_while_the_live_copy_exists(
    tmp_path: Path,
):
    """(iv) The diverged copy keeps growing past the checkpoint the LIVE copy
    carried, and the live copy is still there: refused, nothing sent, the
    offset unchanged; the sweep tables it diverged and names the move."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    before = off.read_bytes()
    okey = _flush_okey(SID)
    _grow(stale, work)  # resumed in its first directory: its tail differs from the live copy
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        assert _fire(home, stale, work, ev="PreCompact", KB_SWEEP_INTERVAL="0").returncode == 0
        frozen = stale.stat().st_size
        _assert_marker(home, "diverged", stale, checkpoint, live)
        _grow_past(stale, work, checkpoint)
        _age(_marker(home, "diverged.stamp"), OLD)
        for ev in ("PreCompact", "SessionEnd"):
            res = _fire(home, stale, work, ev=ev, KB_SWEEP_INTERVAL="0")
            assert res.returncode == 0 and res.stdout == "", (ev, res.stdout, res.stderr)
            assert off.read_bytes() == before, ev
        assert rec.requests == [], "a diverged copy past the checkpoint was drained"
    _assert_marker(home, "diverged", stale, checkpoint, None, frozen=frozen)
    lines = _stale_lines(home)
    assert len(lines) == 2, _log(home)
    assert (
        f"stale copy: {stale} ({stale.stat().st_size}) has grown to the checkpoint {checkpoint} "
        f"or past it; state/{okey}.diverged names this file and that checkpoint is another "
        "copy's (draining from it would start mid-record) reason=stale-copy-diverged — "
        "refused, nothing sent, no restart: records past the shared prefix may be unflushed "
        f"(state/{okey}.diverged) ev=PreCompact sid=bd162e9f"
    ) in lines[1], lines[1]
    size = stale.stat().st_size
    res = _sweep(home)
    rows = {int(cols[2]): (cols, reason) for cols, reason in _rows(res.stdout)}
    cols, reason = rows[size]
    assert cols[5] == "diverged" and cols[4] == str(size), res.stdout
    assert f"has grown to checkpoint {checkpoint} or past it" in reason, reason
    assert "a --seed cannot settle this while a copy reaches the checkpoint" in reason, reason
    assert "kb-flush-backfill.sh --seed" not in reason, reason
    standin, fired = _recording_standin(tmp_path)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout + res.stderr
    last = _sweep_last(home)
    assert (last["diverged"], last["unflushed"]) == ("1", str(size)), last
    assert off.read_bytes() == before


# ---------------------------------------------------------------------------
# Review 2 MUST-FIX 2: a covered diverged copy is settled only by moving it out
# ---------------------------------------------------------------------------


def test_a_covered_diverged_copy_is_settled_only_by_moving_it_out(tmp_path: Path):
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    _grow(stale, work)  # behind the checkpoint, its tail differs; the live copy reaches it
    size = stale.stat().st_size
    okey = _flush_okey(SID)
    _flush_cfg(home, "http://127.0.0.1:9")
    standin, fired = _recording_standin(tmp_path)
    assert _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20").returncode == 0
    _assert_marker(home, "diverged", stale, checkpoint, live)

    # --seed <sid> <BASE>: clears the marker, and SAYS it will not stick.
    res = _sweep(home, "--seed", SID, str(checkpoint))
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"cleared: state/{okey}.diverged (it named {stale})" in res.stdout, res.stdout
    assert (
        f"note: {stale} is still behind the checkpoint and another copy reaches it -- the next "
        "sweep or firing marks it again; move it out of ~/.claude/projects to accept the loss"
    ) in res.stdout.splitlines(), res.stdout
    assert off.read_text() == str(checkpoint)
    _no_markers(home)

    # The documented re-mark: the verdict reads the files, not the marker.
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout + res.stderr
    _assert_marker(home, "diverged", stale, checkpoint, live)
    assert _sweep_last(home)["diverged"] == "1"
    res = _sweep(home)
    rows = {int(cols[2]): (cols, reason) for cols, reason in _rows(res.stdout)}
    cols, reason = rows[size]
    assert cols[5] == "diverged", res.stdout
    assert "To accept the loss, move this copy out of ~/.claude/projects" in reason, reason
    assert "kb-flush-backfill.sh --seed" not in reason, reason

    # Moving it out is what settles it: one `marker cleared` line, diverged=0.
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    stale.rename(quarantine / stale.name)
    res = _sweep(home, "--auto", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0 and _fired_paths(fired) == [], res.stdout + res.stderr
    _no_markers(home)
    cleared = [ln for ln in _log(home).splitlines() if " marker cleared: " in ln]
    assert len(cleared) == 1, cleared
    assert (
        f"backfill bd162e9f ? marker cleared: state/{okey}.diverged named {stale}, which no "
        "longer exists"
    ) in cleared[0], cleared
    assert _sweep_last(home)["diverged"] == "0"
    msg = _notify_kb(tmp_path, home, live, work)
    assert "diverged" not in msg and "synced" in msg, msg
    assert off.read_text() == str(checkpoint)


# ---------------------------------------------------------------------------
# Review 3 MUST 1: a .diverged marker RELEASES once its path carries the
# checkpoint holder's prefix -- the copy was moved out (the remedy) and the
# session later resumed from that directory, so Claude Code wrote the live
# transcript there again. A content check, never an inode one.
# ---------------------------------------------------------------------------

_HOLDER = "which now carries the checkpoint holder's prefix"


def _recreate_moved_copy(tmp_path: Path, stale: Path, live: Path, work: Path, flip: bool) -> bytes:
    """The owner moves the marked copy S out of ~/.claude/projects; the session
    is later resumed from S's directory: Claude Code copies the LIVE transcript
    to S's path and appends. ``flip`` changes ONE byte just below the checkpoint
    (a near miss: then S does NOT carry the holder's prefix)."""
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    stale.rename(quarantine / stale.name)
    data = bytearray(live.read_bytes())
    if flip:
        i = data.rindex(b"x")
        assert len(data) - i < 4096, "the flipped byte must sit inside the judged window"
        data[i] = ord("y")
    data += _lines(work, 5, start=200, tag="resumed-here ")
    _write(stale, bytes(data))
    assert len(data) > live.stat().st_size
    return bytes(data)


def _cleared_lines(home: Path) -> list[str]:
    return [ln for ln in _log(home).splitlines() if " marker cleared: " in ln]


@requires_curl
@pytest.mark.parametrize("flavor", ["holder-prefix", "near-miss"])
def test_a_diverged_marker_releases_once_its_path_carries_the_checkpoint_holders_prefix(
    tmp_path: Path, flavor: str
):
    """A covered diverged copy S is marked; S is moved out and its path then
    holds the live transcript plus new records (past the checkpoint). Stop
    alone (gate open) releases the marker -- marker and stamp gone, ONE
    `marker cleared: ... which now carries the checkpoint holder's prefix`
    line -- and drains exactly [checkpoint, size) in one request. The near
    miss (one byte differs just below the checkpoint) stays refused: nothing
    sent, the marker rewritten with frozen= kept, no clear line."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work, diverged=True)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    frozen = stale.stat().st_size
    okey = _flush_okey(SID)
    _flush_cfg(home, _refused_url())
    res = _fire(home, stale, work, ev="PreCompact", KB_SWEEP_INTERVAL="0")
    assert res.returncode == 0, res.stderr
    _assert_marker(home, "diverged", stale, checkpoint, live)
    assert _marker(home, "diverged.stamp").exists()
    data = _recreate_moved_copy(tmp_path, stale, live, work, flip=flavor == "near-miss")
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _fire(
            home,
            stale,
            work,
            ev="Stop",
            KB_FLUSH_STOP_MIN_BYTES="1",
            KB_FLUSH_STOP_MIN_INTERVAL="0",
            KB_SWEEP_INTERVAL="0",
        )
        assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
        got = _received(rec)
        sent = len(rec.requests)
    log = _log(home)
    assert "restarting from 0" not in log and "reason=shrank" not in log, log
    if flavor == "near-miss":
        assert sent == 0, "a copy that does not carry the holder's prefix was drained"
        assert off.read_text() == str(checkpoint), log
        assert _cleared_lines(home) == [], log
        _assert_marker(home, "diverged", stale, checkpoint, None, frozen=frozen)
        return
    _no_markers(home)
    cleared = _cleared_lines(home)
    assert len(cleared) == 1, log
    assert re.fullmatch(
        r"\S+ "
        + re.escape(
            f"flush work marker cleared: state/{okey}.diverged named {stale}, {_HOLDER} "
            "ev=Stop sid=bd162e9f"
        ),
        cleared[0],
    ), cleared[0]
    assert sent == 1 and got == [(checkpoint, data[checkpoint:])], [o for o, _ in got]
    assert off.read_text() == str(len(data))
    assert len(_stale_lines(home)) == 1, log  # only the line that first marked S
    # The table agrees: S flushed, the old live copy a byte-prefix of it.
    res = _sweep(home)
    states = {int(cols[2]): cols[5] for cols, _reason in _rows(res.stdout)}
    assert states == {len(data): "flushed", checkpoint: "stale-copy"}, res.stdout


def test_the_connector_arm_releases_a_diverged_marker_whose_path_carries_the_holders_prefix(
    tmp_path: Path,
):
    """The same release on the CONNECTOR arm: the marker and its stamp go with
    ONE line (ev=SessionEnd), and exactly [checkpoint, size) is relayed."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work, diverged=True)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    okey = _flush_okey(SID)
    res = _fire(home, stale, work, ev="PreCompact", RELAY_REC=str(rec_dir), KB_SWEEP_INTERVAL="0")
    assert res.returncode == 0, res.stderr
    _assert_marker(home, "diverged", stale, checkpoint, live)
    assert not (rec_dir / ".n").exists(), "the diverged copy was relayed"
    data = _recreate_moved_copy(tmp_path, stale, live, work, flip=False)
    res = _fire(home, stale, work, ev="SessionEnd", RELAY_REC=str(rec_dir), KB_SWEEP_INTERVAL="0")
    assert res.returncode == 0 and res.stdout == "", (res.stdout, res.stderr)
    _no_markers(home)
    cleared = _cleared_lines(home)
    assert len(cleared) == 1, _log(home)
    assert cleared[0].endswith(
        f" flush work marker cleared: state/{okey}.diverged named {stale}, {_HOLDER} "
        "ev=SessionEnd sid=bd162e9f"
    ), cleared[0]
    assert off.read_text() == str(len(data)), _log(home)
    n = int((rec_dir / ".n").read_text())
    relayed = b"".join((rec_dir / f"chunk.{i}").read_bytes() for i in range(1, n + 1))
    assert relayed == data[checkpoint:], (n, len(relayed), len(data) - checkpoint)


@requires_curl
def test_the_sweep_releases_a_diverged_marker_whose_path_carries_the_holders_prefix(
    tmp_path: Path,
):
    """The sweep reads the same content check: a dry run tables the recreated
    path `ready` (REMAIN = size - checkpoint, never `diverged`) and writes no
    state; --auto clears the marker with ONE line and its real-hook firing
    drains exactly [checkpoint, size)."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work, diverged=True)
    checkpoint = live.stat().st_size
    off = _offset(home, checkpoint)
    okey = _flush_okey(SID)
    _flush_cfg(home, _refused_url())
    assert _fire(home, stale, work, ev="PreCompact", KB_SWEEP_INTERVAL="0").returncode == 0
    _assert_marker(home, "diverged", stale, checkpoint, live)
    data = _recreate_moved_copy(tmp_path, stale, live, work, flip=False)
    size = len(data)
    marker = _marker(home, "diverged").read_bytes()

    res = _sweep(home)
    assert res.returncode == 0, res.stdout + res.stderr
    rows = {int(cols[2]): (cols, reason) for cols, reason in _rows(res.stdout)}
    cols, reason = rows[size]
    assert cols[5] == "ready" and cols[4] == str(size - checkpoint), res.stdout
    assert "diverged" not in reason and "move this copy out" not in reason, reason
    assert rows[checkpoint][0][5] == "flushed", res.stdout
    assert "diverged copy(ies)" not in res.stdout, res.stdout
    assert _marker(home, "diverged").read_bytes() == marker, "a dry run wrote state"
    assert _marker(home, "diverged.stamp").exists()
    assert _cleared_lines(home) == []

    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _sweep(
            home,
            "--auto",
            "--hook",
            str(FLUSH),
            "--timeout",
            "60",
            KB_FLUSH_FOREGROUND="1",
            KB_FLUSH_CURL_RETRY="0",
        )
        got = _received(rec)
    assert res.returncode == 0, res.stdout + res.stderr
    _no_markers(home)
    cleared = _cleared_lines(home)
    assert len(cleared) == 1, _log(home)
    # The SWEEP never releases at discovery (the transcript is past the checkpoint):
    # it tables the row ready and fires the hook, which releases under the session
    # lock at the OFFSET it drains from -- so the one clear line is the HOOK's.
    assert cleared[0].endswith(
        f" flush work marker cleared: state/{okey}.diverged named {stale}, {_HOLDER}"
        " ev=Backfill sid=bd162e9f"
    ), cleared[0]
    assert got == [(checkpoint, data[checkpoint:])], [o for o, _ in got]
    assert off.read_text() == str(size)
    last = _sweep_last(home)  # unflushed= is the discovery-time remainder: S's new records
    assert (last["ready"], last["pushed"], last["diverged"], last["unflushed"]) == (
        "1",
        "1",
        "0",
        str(size - checkpoint),
    ), last


def test_a_diverged_copy_no_copy_reaches_is_offered_current_only_as_accepting_the_loss(
    tmp_path: Path,
):
    """bf_diverged_owner's no-cover branch, below and past the checkpoint: the
    live copy is gone, so `--seed <sid> current` is named -- as ACCEPTING the
    loss of the unsent records, never as `its bytes were sent`."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work)
    checkpoint = live.stat().st_size
    _offset(home, checkpoint)
    _grow(stale, work)  # its tail differs from the live copy's: covered, diverged
    _flush_cfg(home, _refused_url())
    assert _fire(home, stale, work, ev="PreCompact", KB_SWEEP_INTERVAL="0").returncode == 0
    _assert_marker(home, "diverged", stale, checkpoint, live)
    live.unlink()
    for leg in ("below", "past"):
        if leg == "past":
            _grow_past(stale, work, checkpoint)
        size = stale.stat().st_size
        res = _sweep(home)
        assert res.returncode == 0, res.stdout + res.stderr
        ((cols, reason),) = _rows(res.stdout)
        assert cols[5] == "diverged" and cols[4] == str(size), (leg, res.stdout)
        assert (
            f"Owner: kb-flush-backfill.sh --seed {SID} current ACCEPTS the loss of those "
            "records (it clears the marker; it does not mean they were sent)"
        ) in reason, (leg, reason)
        assert "its bytes were sent" not in reason, (leg, reason)
        assert "a --seed cannot settle this" not in reason, (leg, reason)


# ---------------------------------------------------------------------------
# Review 2 should-fixes: the real hook's refusal in a push, the shrank row that
# stops before deciding, D4's equal second, the brief's behind token, D3(c) on
# the connector arm
# ---------------------------------------------------------------------------


@requires_curl
@pytest.mark.parametrize("stamp", ["no-stamp", "fresh-stamp"])
def test_a_real_hook_refusal_during_a_push_is_booked_refused(tmp_path: Path, stamp: str):
    """MF5/D8 with the REAL hook: both copies are `ready` at discovery (the
    checkpoint sits below both). The live copy fires first (older) and its
    drain carries the shared checkpoint past the other copy, which kept
    growing behind it -- the hook then refuses that copy as diverged. Logged,
    or silent (a fresh stamp: the marker the hook just wrote and that stamp
    name the refusal), it is booked `refused`: never pushed, sent=0."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    head = _lines(work, 30)
    live = _write(
        _projects(home) / "-home-u-binary-rewriter" / f"{SID}.jsonl",
        head + _lines(work, 10, start=30),
    )
    stale = _write(
        _projects(home) / "-home-u" / f"{SID}.jsonl",
        head + _lines(work, 3, start=30, tag="resumed "),
    )
    _age(stale, OLD + 10)  # fired AFTER the live copy (oldest first)
    checkpoint = len(_lines(work, 20))
    off = _offset(home, checkpoint)
    okey = _flush_okey(SID)
    if stamp == "fresh-stamp":
        _marker(home, "diverged.stamp").touch()
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        res = _sweep(
            home,
            "--push",
            "--hook",
            str(FLUSH),
            "--timeout",
            "60",
            KB_FLUSH_FOREGROUND="1",
            KB_FLUSH_CURL_RETRY="0",
        )
        got = _received(rec)
    assert res.returncode == 0, res.stdout + res.stderr
    lsize = live.stat().st_size
    assert off.read_text() == str(lsize), _log(home)
    sent = b"".join(chunk for _, chunk in got)
    assert sent == live.read_bytes()[checkpoint:], "only the live copy's new bytes went out"
    assert (
        f" ... refused (reason=stale-copy-diverged: behind checkpoint {lsize} and its tail "
        "differs from the copy that reaches it -- nothing sent, no restart"
    ) in res.stdout, res.stdout
    assert " ... ok" in res.stdout and res.stdout.count(" ... ok") == 1, res.stdout
    assert (
        "kb-flush-backfill: pushed=1 partial=0 failed=0 deferred=0 busy=0 skipped=0 "
        f"bytes={lsize - checkpoint} stale=0 diverged=1 behind=0"
    ) in res.stdout, res.stdout
    last = _sweep_last(home)
    assert (last["pushed"], last["failed"], last["diverged"]) == ("1", "0", "1"), last
    _assert_marker(home, "diverged", stale, lsize, live)
    assert len(_stale_lines(home)) == (1 if stamp == "no-stamp" else 0), _log(home)
    assert (
        f"backfill bd162e9f work refused base={checkpoint} size={stale.stat().st_size} sent=0 took="
    ) in _log(home), _log(home)
    # The session's stall record is the live copy's progress, untouched by the refusal.
    record = (_state(home) / f"{okey}.sweep").read_text()
    assert record.startswith("fails=0 ") and "verdict=pushed" in record, record


def _stopping_standin(
    tmp_path: Path, home: Path, line: str | None, other: Path
) -> tuple[Path, Path]:
    """The hook stops BEFORE it decides the checkpoint for SID (a held lock, a
    NEEDS TOKEN refusal, a silent rate-limited one): it logs ``line`` (or
    nothing) and leaves the offset exactly as it was; any other session is
    drained to its size."""
    state = _state(home)
    logf = home / ".claude" / "hooks" / "flush-session.log"
    action = ":"
    if line is not None:
        linef = tmp_path / "line.txt"
        linef.write_text(line + "\n")
        action = f'cat "{linef}" >>"{logf}"'
    fired = tmp_path / "fired"
    standin = tmp_path / "stopping.sh"
    standin.write_text(
        f"#!/usr/bin/env bash\n{_LINEAGE}\nev=$(cat)\n"
        f'case "$ev" in\n*{SID}*)\n{action}\n;;\n'
        f'*)\nprintf \'%s\\n\' "$ev" >>"{fired}"\n'
        f'printf \'KB_FLUSH_MAX=%s\\n\' "$KB_FLUSH_MAX" >>"{tmp_path}/fired.env"\n'
        f'printf %s {other.stat().st_size} >"{state}/{_flush_okey(other.stem)}.offset"\n'
        ";;\nesac\nexit 0\n"
    )
    standin.chmod(0o755)
    return standin, fired


_STOPS = {
    "needs-token": (
        "refused",
        "2026-10-11T00:00:00+00:00 flush work NEEDS TOKEN: no KB_TOKEN_WORK in /x/kb-tokens.env "
        "and no shared KB_TOKEN, so this session was NOT captured.",
    ),
    "lock-held": (
        "busy",
        "2026-10-11T00:00:00+00:00 flush work Backfill skipped (lock held) ev=Backfill "
        "sid=bd162e9f",
    ),
    "silent": ("no-progress", None),
}


@pytest.mark.parametrize("stop", list(_STOPS))
def test_a_shrank_row_whose_hook_stops_before_deciding_is_never_pushed_or_charged(
    tmp_path: Path, stop: str
):
    """MF5/D8 residual: a `shrank` row (BASE 0) whose hook exits before
    kb_decide_offset leaves the OLD checkpoint above the file. CUR-0 is that
    checkpoint, never bytes sent: never booked pushed, never charged to the
    --auto budget (the later row still gets all of it); the busy / NEEDS TOKEN
    / no-progress greps name the firing."""
    verdict, line = _STOPS[stop]
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _flush_cfg(home, "http://127.0.0.1:9")
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    off = _offset(home, size + 100000)
    _age(off, OLD - 3600)  # the file changed after the checkpoint: a genuine shrink, fireable
    other_sid = "aaaaaaaa-0000-4000-8000-000000000001"
    other = _write(_projects(home) / "-home-u-other" / f"{other_sid}.jsonl", _lines(work, 20))
    _age(other, OLD + 10)  # fired AFTER the shrank row (oldest first)
    (_state(home) / f"{_flush_okey(other_sid)}.offset").write_text(
        str(other.stat().st_size - len(_lines(work, 1, start=19)))
    )
    res = _sweep(home)
    rows = {int(cols[2]): cols for cols, _ in _rows(res.stdout)}
    assert rows[size][5] == "shrank", res.stdout
    budget = 50000
    standin, fired = _stopping_standin(tmp_path, home, line, other)
    res = _sweep(
        home,
        "--auto",
        "--hook",
        str(standin),
        "--timeout",
        "20",
        KB_SWEEP_BUDGET_BYTES=str(budget),
    )
    assert res.returncode == 0 and res.stdout == "", res.stdout + res.stderr
    assert _fired_paths(fired) == [str(other)], "the later row was deferred"
    assert (tmp_path / "fired.env").read_text() == f"KB_FLUSH_MAX={budget}\n"
    last = _sweep_last(home)
    assert (last["pushed"], last["deferred"]) == ("1", "0"), last
    assert f"backfill bd162e9f work {verdict} base=0 size={size} sent=0 took=" in _log(home), _log(
        home
    )
    # An owner --push books it the same way: never `ok`, bytes=0.
    res = _sweep(home, "--push", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0, res.stdout + res.stderr
    assert " ... ok" not in res.stdout and f"{verdict}" in res.stdout, res.stdout
    assert " pushed=0 " in res.stdout and " bytes=0 " in res.stdout, res.stdout
    assert off.read_text() == str(size + 100000)


@requires_curl
def test_a_checkpoint_written_in_the_transcripts_own_second_is_a_genuine_shrink(tmp_path: Path):
    """D4 is STRICTLY older: a transcript and its checkpoint in the SAME epoch
    second (a rewrite racing the checkpoint write) is a genuine shrink --
    tabled shrank, restarted from 0, no marker -- never `behind`."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    off = _offset(home, size + 500)
    _age(off, OLD)
    assert int(t.stat().st_mtime) == int(off.stat().st_mtime) == OLD
    res = _sweep(home)
    ((cols, _reason),) = _rows(res.stdout)
    assert cols[5] == "shrank", res.stdout
    _flush_cfg(home, _refused_url())
    res = _fire(home, t, work, KB_SWEEP_INTERVAL="0")
    assert res.returncode == 0, res.stderr
    log = _log(home)
    assert (
        f"transcript shrank ({size} < offset {size + 500}) reason=shrank — restarting from 0" in log
    ), log
    assert _stale_lines(home) == []
    _no_markers(home)
    assert off.read_text() == "0", off.read_text()


def test_the_brief_surfaces_a_behind_refusal_line_on_its_own(tmp_path: Path):
    """D6: `reason=behind` is a failure token by itself -- a log holding ONLY
    the hook's behind line (no sweep record, no marker) still warns."""
    home, work = _recall_home(tmp_path)
    (home / ".claude" / "hooks" / "state").mkdir()
    okey = _flush_okey(SID)
    (home / ".claude" / "hooks" / "flush-session.log").write_text(
        "2026-10-11T00:00:00+00:00 flush recallproj stale copy: /p/x.jsonl (10) is behind the "
        "checkpoint 20; no copy reaches it and the checkpoint moved after this file last "
        "changed reason=behind — refused, nothing sent, no restart: an owner decision, seed "
        f"it: kb-flush-backfill.sh --seed {SID} current|<bytes> (state/{okey}.behind) "
        "ev=PreCompact sid=bd162e9f\n"
    )
    _lines_, out = _brief_sweep_line(tmp_path, home, work)
    assert "KB session-flush reported a recent failure" in out, out
    assert "reason=behind" in out, out


def test_the_connector_arm_clears_a_marker_whose_copy_is_gone(tmp_path: Path):
    """D3(c) on the CONNECTOR arm: the next firing clears a marker whose path
    no longer exists, one line per kind."""
    home, rec_dir = _connector_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    live = _write(_projects(home) / "-home-u-binary-rewriter" / f"{SID}.jsonl", _lines(work, 40))
    checkpoint = live.stat().st_size - len(_lines(work, 1, start=39))
    _offset(home, checkpoint)
    gone = _projects(home) / "-home-u" / f"{SID}.jsonl"
    _plant_markers(home, gone, checkpoint)
    okey = _flush_okey(SID)
    res = _fire(home, live, work, ev="SessionEnd", RELAY_REC=str(rec_dir), KB_SWEEP_INTERVAL="0")
    assert res.returncode == 0, res.stderr
    _no_markers(home)
    log = _log(home)
    for kind in ("diverged", "behind"):
        assert (
            f"flush work marker cleared: state/{okey}.{kind} named {gone}, which no longer "
            "exists ev=SessionEnd sid=bd162e9f"
        ) in log, log


@pytest.mark.parametrize("kind", ["behind", "diverged"])
def test_a_firing_marked_by_a_race_at_its_own_size_is_booked_refused(tmp_path: Path, kind: str):
    """MUST-FIX 1 (d), the push loop's widened test: a `ready` row (checkpoint
    below the file) whose firing finds a marker naming it -- written between
    discovery and the firing -- is refused by the hook at its own size, here
    silently (the kind's stamp is fresh). Booked `refused` and counted, never
    failed / no-progress, no stall; the checkpoint is untouched."""
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    _flush_cfg(home, "http://127.0.0.1:9")
    t = _write(_projects(home) / "-home-u" / f"{SID}.jsonl", _lines(work, 30))
    size = t.stat().st_size
    base = size - len(_lines(work, 1, start=29))
    off = _offset(home, base)
    state = _state(home)
    okey = _flush_okey(SID)
    standin = tmp_path / "marking.sh"
    standin.write_text(
        f"#!/usr/bin/env bash\n{_LINEAGE}\nev=$(cat)\n"
        f'case "$ev" in\n*{SID}*)\n'
        "printf '%s %s size=%s checkpoint=%s frozen=%s\\n' "
        f'"$(date -u +%Y-%m-%dT%H:%M:%SZ)" "{t}" {size} {base} {base} >"{state}/{okey}.{kind}"\n'
        f'touch "{state}/{okey}.{kind}.stamp"\n;;\nesac\nexit 0\n'
    )
    standin.chmod(0o755)
    res = _sweep(home, "--push", "--hook", str(standin), "--timeout", "20")
    assert res.returncode == 0, res.stdout + res.stderr
    label = "stale-copy-diverged" if kind == "diverged" else "behind"
    assert (
        f" ... refused (reason={label}: state/{okey}.{kind} names this copy and checkpoint {base} "
        "is another copy's -- nothing sent, no restart; "
    ) in res.stdout, res.stdout
    if kind == "behind":
        assert (
            f"resumed since it was marked: its records past {base} were NOT sent -- "
            f"kb-flush-backfill.sh --seed {SID} {base} sends exactly them (state/{okey}.behind))"
        ) in res.stdout, res.stdout
    assert "FAILED" not in res.stdout and " ... ok" not in res.stdout, res.stdout
    counts = {"diverged": 0, "behind": 0, kind: 1}
    assert (
        "kb-flush-backfill: pushed=0 partial=0 failed=0 deferred=0 busy=0 skipped=0 bytes=0 "
        f"stale=0 diverged={counts['diverged']} behind={counts['behind']}"
    ) in res.stdout, res.stdout
    assert _sweep_last(home)[kind] == "1"
    assert f"backfill bd162e9f work refused base={base} size={size} sent=0 took=" in _log(home)
    assert not (state / f"{okey}.sweep").exists(), "a refusal booked a stall"
    assert off.read_text() == str(base)


@pytest.mark.parametrize("fn", ["kb_window_matches", "kb_tail_matches"])
@pytest.mark.parametrize("carrier", ["hook", "sweep"])
def test_a_failed_window_read_never_counts_as_a_match(tmp_path: Path, fn: str, carrier: str):
    """cksum of NOTHING equals cksum of NOTHING: a head/tail that fails (or reads
    short) on both files must never make two DIFFERENT files 'match' -- for
    kb_window_matches that would release a diverged marker and drain mid-record,
    for kb_tail_matches it would hide a diverged copy as a silent stale-copy."""
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(os.urandom(9000))
    b.write_bytes(os.urandom(9000))
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "tail").write_text("#!/bin/sh\nexit 1\n")  # both sides then read NOTHING
    (shim / "tail").chmod(0o755)
    call = f'{fn} "$1" 8000 "$2"'
    script = _copies_block(FLUSH if carrier == "hook" else BACKFILL) + (
        f'{call}; echo "real rc=$?"; PATH="$3:$PATH"; {call}; echo "shim rc=$?"; '
        f'{fn} "$1" 8000 "$1"; echo "self rc=$?"'
    )
    res = subprocess.run(
        ["bash", "-c", script, "-", str(a), str(b), str(shim)],
        env=_ENV,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.stdout.splitlines() == ["real rc=1", "shim rc=1", "self rc=1"], (
        res.stdout + res.stderr
    )


@requires_curl
def test_the_sweep_never_releases_a_diverged_marker_it_does_not_drain(tmp_path: Path):
    """The verification's TOCTOU (round 3): a sweep that released the marker at
    DISCOVERY (no lock, judged at BASE) for a row it then did not fire (the
    recreated path is still being written: 'live') left the path unmarked; the
    other fork then moved the checkpoint and the path's next Stop drained from
    it MID-RECORD. Now the sweep only tables the row; the hook re-judges the
    window at the checkpoint it actually drains from and refuses."""
    gate = dict(KB_FLUSH_STOP_MIN_BYTES="1", KB_FLUSH_STOP_MIN_INTERVAL="0", KB_SWEEP_INTERVAL="0")
    home = _hook_home(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    stale, live = _resumed_session(home, work, diverged=True)
    cp0 = live.stat().st_size
    off = _offset(home, cp0)
    _age(off, OLD + 3600)
    _flush_cfg(home, _refused_url())
    _fire(home, stale, work, ev="PreCompact", KB_SWEEP_INTERVAL="0")
    assert _marker(home, "diverged").exists()
    quarantine = tmp_path / "q"
    quarantine.mkdir()
    shutil.move(str(stale), str(quarantine / stale.name))
    stale.write_bytes(live.read_bytes() + _lines(work, 3, start=200, tag="A-fork "))
    os.utime(stale, (NOW - 5, NOW - 5))  # being written right now: the sweep's 'live' gate
    res = _sweep(
        home,
        "--auto",
        "--hook",
        str(FLUSH),
        "--timeout",
        "60",
        KB_FLUSH_FOREGROUND="1",
        KB_FLUSH_CURL_RETRY="0",
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert _marker(home, "diverged").exists(), "a discovery-time release of an unfired row"
    assert _cleared_lines(home) == [], _log(home)
    with _Recorder() as rec:
        _flush_cfg(home, rec.url)
        with live.open("ab") as fh:  # the OTHER fork moves the shared checkpoint on
            fh.write(_lines(work, 12, start=300, tag="B-fork-with-a-longer-tag "))
        _fire(home, live, work, ev="Stop", **gate)
        cp1 = int(off.read_text())
        assert cp1 > cp0
        n0 = len(rec.requests)
        with stale.open("ab") as fh:
            fh.write(_lines(work, 20, start=400, tag="A-fork "))
        _fire(home, stale, work, ev="Stop", **gate)
        assert len(rec.requests) == n0, (
            "the recreated fork drained from the other fork's checkpoint"
        )
    assert int(off.read_text()) == cp1
    assert _marker(home, "diverged").exists()
