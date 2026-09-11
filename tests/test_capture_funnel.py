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

import fnmatch
import hashlib
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
DISPATCH = CONTRIB / "kb-hook-dispatch.sh"
MCPCALL = CONTRIB / "kb-mcp-call.sh"
NOTIFY_FAIL = REPO / "systemd" / "kb-notify-fail.sh"

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


@pytest.mark.parametrize("path", [NOTIFY, BRIEF, INSTALL, FLUSH, MCPCALL], ids=lambda p: p.name)
def test_no_credential_is_passed_on_a_curl_command_line(path: Path):
    """argv is world-readable (/proc/<pid>/cmdline, `ps`). An ntfy admin password
    already leaked into a transcript this way — and a transcript is precisely
    what these hooks flush into the KB. Credentials travel on stdin via
    `curl -K -`, never as arguments. FLUSH and MCPCALL joined the family last
    (review #3 wave D): the transcript-capture path was still holding the
    bearer — on a single-token box, the OWNER master token — in argv."""
    offenders = [
        ln
        for ln in _executed_curl_lines(path.read_text())
        if re.search(r"(^|\s)-u\s", ln) or "Authorization:" in ln
    ]
    assert not offenders, f"{path.name} puts a credential in argv: {offenders}"


@pytest.mark.parametrize("path", [NOTIFY, BRIEF, INSTALL, FLUSH, MCPCALL], ids=lambda p: p.name)
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


@pytest.mark.parametrize("name", sorted(p.name for p in SHIPPED.iterdir() if p.is_file()))
def test_contrib_and_shipped_copies_stay_identical(name: str):
    """contrib/ is canonical, setup_assets/ is what the container serves. A box
    that fetches from /api/setup/ must get the script that was tested.

    DERIVED from the shipped directory, never a hand-written list: the list this
    replaced had gone stale in the quiet direction (kb-mcp-call.sh, turn-start.sh
    and SKILL.md were shipped but named nowhere here), so those three could drift
    from their twins with nothing to report it."""
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
    # The allowlist moved to kbcore.setup_manifest (wave S4, KB 33784) so the nightly
    # digest can compare a box's hook_version against the CURRENT manifest; the
    # kbrest routes re-export it (test_fleet_flush pins the two names equal).
    "kbrest allowlist": (REPO / "src/kbcore/setup_manifest.py", "SETUP_SCRIPTS"),
    "hookrig stub": (REPO / "tests/hookrig/stub_server.py", "SETUP_SCRIPTS"),
}
_FETCH_LOOPS = {
    "kb_setup bootstrap block": REPO / "src/kbmcp/tools_setup.py",
    "rig case 00 (skill completeness)": REPO / "tests/hookrig/cases/00_hermetic_isolation.sh",
    "rig case 17 (enrolment bootstrap)": REPO / "tests/hookrig/cases/17_enrolment_bootstrap.sh",
    "win rig case 07 (enrolment bootstrap)": (
        REPO / "tests/hookrig/wincases/07_enrolment_bootstrap.sh"
    ),
    # The SELF-UPDATE list. The pre-brief's staleness banner is the only fetch
    # list a box ever sees AFTER it is installed — it is what an out-of-date
    # machine copies to catch up — and it was not checked here, so it went stale
    # exactly as the gap class predicts (2026-08-08: it named 7 of the 8 assets,
    # omitting kb-hook-dispatch.sh, which install.sh installs UNCONDITIONALLY;
    # the paste therefore died at `install -m 0755 … kb-hook-dispatch.sh`).
    "session-brief staleness banner": BRIEF,
    # The README's enrolment bootstrap block is the same paste for a human
    # reader, and had gone stale the same way (4 of 8).
    "README enrolment bootstrap block": REPO / "README.md",
    # SHARING.md 2.5 is the operator guide's copy of the same paste. It named 4 of
    # 8 assets until review #8 (install.sh died at the dispatcher), because it was
    # the one fetch list this registry did not derive.
    "SHARING.md 2.5 enrolment bootstrap block": REPO / "SHARING.md",
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


def _banner_fetch_list(text: str) -> set[str]:
    """The filenames the pre-brief's staleness banner tells a box to fetch,
    lifted out of the `for f in …; do` line it PRINTS (printf quoting and all)."""
    start = text.index("Your hook scripts are OUT OF DATE")
    end = text.index("This is FYI", start)
    loop = re.search(r"for f in (.*?);\s*do", text[start:end], re.S)
    assert loop, "the staleness banner no longer prints a `for f in …; do` fetch list"
    return set(re.findall(r"[A-Za-z0-9_.-]+\.sh", loop.group(1)))


def test_the_staleness_banner_names_exactly_the_asset_set():
    """SET EQUALITY, not membership — the banner is the self-update path, and it
    is the one list a box reads after install, so it must be neither short nor
    long. Short is how this broke: it named 7 of 8 assets and install.sh, which
    stats kb-hook-dispatch.sh unconditionally, aborted on the missing 8th — the
    deliberate loud failure, but reached from a banner that promised a working
    paste. Long is just as bad: a name that is no longer served 404s the curl
    (-fsS, so a hard failure) and the same paste dies before the installer.

    The parametrised case above now covers the banner too; this asserts the other
    direction, which membership tests structurally cannot."""
    printed = _banner_fetch_list(BRIEF.read_text())
    assert printed == {p.name for p in SHIPPED.glob("*.sh")}, (
        "the staleness banner's fetch list has drifted from the served asset set: "
        f"extra={sorted(printed - {p.name for p in SHIPPED.glob('*.sh')})} "
        f"missing={sorted({p.name for p in SHIPPED.glob('*.sh')} - printed)}"
    )


def test_the_portable_block_carriers_are_named_the_same_everywhere():
    """The same gap class, one layer down. The shared block is duplicated verbatim
    into several scripts, and that is only safe because a rig case diffs the
    copies — so the LIST of carriers is load-bearing. It is written in two places
    (the block's own header sentence, and rig case 26's SCRIPTS) and neither was
    derived from the files: case 26 still named four when kb-flush-backfill.sh had
    become the fifth carrier, so that copy could drift with nothing diffing it."""
    carriers = {p.name for p in SHIPPED.glob("*.sh") if "# >>> KB PORTABLE BLOCK" in p.read_text()}

    header = re.search(r"BYTE-IDENTICAL in (.*?)\. ", INSTALL.read_text(), re.S)
    assert header, "the portable block no longer says which scripts carry it"
    assert set(re.findall(r"[A-Za-z0-9_.-]+\.sh", header.group(1))) == carriers, (
        "the block header's carrier sentence disagrees with the scripts that carry it"
    )

    case26 = REPO / "tests/hookrig/cases/26_portable_block_identity.sh"
    rig = re.search(r'^SCRIPTS="([^"]*)"', case26.read_text(), re.M)
    assert rig, "rig case 26 no longer declares a SCRIPTS list"
    assert set(rig.group(1).split()) == carriers, (
        "rig case 26 does not diff every copy of the portable block: "
        f"unchecked={sorted(carriers - set(rig.group(1).split()))}"
    )


def test_the_staleness_banner_runs_the_installer_on_what_it_fetched():
    """Fetching the scripts is only half the update: they are inert in
    ~/.claude/kb-setup until install.sh copies them into ~/.claude/hooks."""
    text = BRIEF.read_text()
    banner = text[text.index("Your hook scripts are OUT OF DATE") : text.index("This is FYI")]
    assert "bash install.sh" in banner, "the banner fetches new scripts but never installs them"


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
    # Wave C (C7, KB 34161): the sweep hands the hook a `Backfill` event — never a synthetic
    # SessionEnd, which used to make the hook retire the live session's checkpoint.
    assert "Backfill" in call, "the event is not Backfill-shaped"
    assert "SessionEnd" not in call, "the sweep must never synthesise SessionEnd (KB 33783)"
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


def _run_hook(
    script: Path,
    event: dict | str,
    home: Path,
    extra_env: dict | None = None,
    args: tuple[str, ...] = (),
):
    """``event`` is normally the hook event object; a raw STRING is written to
    stdin verbatim, which is how the empty-stdin case is exercised. ``args`` are
    the script's own argv (the dispatcher takes the event name there)."""
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        input=event if isinstance(event, str) else json.dumps(event),
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


def test_a_fully_drained_session_reports_synced_from_its_kept_offset(tmp_path):
    """Wave C (KB 33783 / 33784): the flush hook NEVER deletes the offset — a
    drained SessionEnd leaves it at offset == size, and THAT is the `synced`
    signal notify reads. The backfill ledger (`.backfill-done`) is an inert
    relic and is not planted here: a session with a kept checkpoint at its size
    reports synced with no ledger at all."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-done')}.offset").write_text(str(_KB_SIZE))
    assert not (state / ".backfill-done").exists()

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


def test_a_corrupt_offset_beside_the_unseeded_marker_reads_unseeded(tmp_path):
    """Wave C stage 3 (A1, KB 33783): the hook REFUSES a corrupt checkpoint as
    unseeded — never a from-0 resend — and leaves `<okey>.unseeded` beside it.
    Notify then says exactly what it says for a missing checkpoint, `?/<size>
    unseeded`, instead of the silent omission of the not-yet-ruled case above."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    state = home / ".claude" / "hooks" / "state"
    (state / f"{_flush_okey('s-corrupt')}.offset").write_text("not-a-number\n")
    (state / f"{_flush_okey('s-corrupt')}.unseeded").touch()

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
    assert msg.endswith("(kb ?/5.2MB unseeded)"), (
        f"a refused corrupt checkpoint was not reported: {msg!r}"
    )


def test_the_notify_marker_probe_reads_the_single_appendix_I_spelling():
    """Both hooks write `<okey>.retry` / `<okey>.unseeded` (proposal_v2 Appendix
    I); the second-guess `<okey>.offset.<ext>` probe is gone, so a marker can
    only ever be read from the name the writer uses (rig case 39 plants it)."""
    fn = _shell_fn(NOTIFY, "kb_marker")
    assert '"$1/$2.$3"' in fn, "kb_marker no longer probes <okey>.<ext>"
    assert ".offset.$3" not in fn, "kb_marker still probes the retired <okey>.offset.<ext> spelling"


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


def test_a_network_box_with_no_capture_record_reports_the_honest_zero(tmp_path):
    """Owner report 2026-08-07: young sessions on the Windows box (flush fires at
    PreCompact/SessionEnd, so nothing has flushed yet) pushed pings with NO kb
    segment — indistinguishable from the feature being broken. On a NETWORK-mode
    box, no offset and no ledger genuinely means zero bytes captured, so the
    segment now says so instead of hiding."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    (home / ".claude" / "kb-flush.env").write_text(
        "KB_REST_URL=http://kb.invalid\nKB_TOKEN=kbg_" + "0" * 40 + "\n"
    )

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-young",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert msg.endswith("(kb 0.0/5.2MB)"), f"expected the honest zero, got {msg!r}"
    # The flush config was sourced in a subshell to learn the transport; its
    # credential must not leak into the push body or any argv.
    assert "kbg_" not in msg
    recorded = "".join(p.read_text() for p in rec_dir.glob("argv.*"))
    assert "kbg_" not in recorded, "the flush credential leaked into curl argv"


def test_a_LOCAL_box_with_no_capture_record_still_omits_the_segment(tmp_path):
    """LOCAL mode writes neither offsets nor ledger — absence is genuinely
    unknowable there, so the original omission stands."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)
    (home / ".claude" / "kb-flush.env").write_text("KB_DIR=/somewhere\nUV=/usr/bin/uv\n")

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-local",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert msg == "Claude finished - proj @ testbox", f"LOCAL mode must keep omitting: {msg!r}"


def test_no_flush_config_at_all_still_omits_the_segment(tmp_path):
    """A box with notifications but no flush install: nothing is knowable."""
    home, rec_dir, binp = _notify_home(tmp_path)
    work = tmp_path / "proj"
    transcript = _sized_transcript(work / "t.jsonl", _KB_SIZE)

    res = _run_notify(
        home,
        binp,
        rec_dir,
        {
            "session_id": "s-noflush",
            "cwd": str(work),
            "hook_event_name": "Stop",
            "transcript_path": str(transcript),
        },
    )
    assert res.returncode == 0, res.stderr
    msg = _notify_message(rec_dir)
    assert msg == "Claude finished - proj @ testbox", f"no config must keep omitting: {msg!r}"


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
    # The RENDERED list, not just the source: a warning whose paste cannot finish
    # is a warning that strands the box it warned. (It named 7 of 8 assets until
    # 2026-08-08, and install.sh stats kb-hook-dispatch.sh unconditionally.)
    assert _banner_fetch_list(res.stdout) == {p.name for p in SHIPPED.glob("*.sh")}, (
        "the banner a user actually sees does not fetch the whole asset set: " + repr(res.stdout)
    )

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
# Behavioural: recall is never FAIL-SILENT (KB item 12885)
# ---------------------------------------------------------------------------
#
# THE BUG, reported by the owner from another box. kb-session-brief.sh serves
# BOTH hook events, and when its search timed out or errored it emitted exactly
# what a genuinely empty KB emits — nothing. A struggling server was therefore
# invisible while it charged its whole timeout to every single prompt, and the
# owner's local patch for it died on every asset refetch.
#
# The fix is deliberately narrow, and these tests pin all three halves of it:
# a detected failure writes ONE log line and trips a short breaker; while that
# breaker is hot the remote call is SKIPPED (asserted against a recording stub
# that would have recorded it) and one ASCII line says so; and a search that
# answered with zero rows is a SUCCESS that must touch neither. That last one is
# the false positive that would turn a quiet KB into a permanently disabled one.

# A recording curl for the recall paths. Every invocation is written down, which
# is the only way to prove a hot breaker made NO call rather than a fast one.
# CURL_SEARCH_* drive the search response; CURL_PROBE_RC drives the cheap
# reachability probe the hook makes first.
_RECALL_CURL = r"""#!/usr/bin/env bash
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
WANT_W=0
for a in "$@"; do
  case "$a" in http://*|https://*) URL="$a" ;; esac
  [ "$a" = "-w" ] && WANT_W=1
done
printf '%s\n' "$URL" >"$D/url.$N"
case "$URL" in
  *"/api/setup/manifest.json") exit 22 ;;   # unversioned box: nothing to report
  *"/api/search"*)
    printf '%s' "${CURL_SEARCH_BODY-}"
    # CURL_SEARCH_NO_TRAILER simulates curl dying before the -w trailer is
    # written (signal, crash, a shim) — stdout ends on the body's own last line.
    if [ "$WANT_W" = 1 ] && [ -z "${CURL_SEARCH_NO_TRAILER-}" ]; then
      printf '\n%s' "${CURL_SEARCH_STATUS:-200}"
    fi
    exit "${CURL_SEARCH_RC:-0}" ;;
  *) exit "${CURL_PROBE_RC:-0}" ;;          # the reachability probe
esac
"""

# One row, in the shape /api/search really answers with.
_ONE_HIT = json.dumps(
    [
        {
            "type": "decision",
            "project": "recallproj",
            "trust": "verified",
            "title": "the drain deadline",
            "snippet": "bounded refills, measured 2026-07",
        }
    ]
)


def _recall_curl(home: Path, rec_dir: Path) -> Path:
    binp = home / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    curl = binp / "curl"
    curl.write_text(_RECALL_CURL)
    curl.chmod(0o755)
    rec_dir.mkdir(parents=True, exist_ok=True)
    return binp


def _recall_home(tmp_path: Path, url: str = "http://kb.invalid") -> tuple[Path, Path]:
    """A throwaway HOME configured for the REST transport, plus a work dir whose
    basename is the project."""
    home = _hook_home(tmp_path)
    (home / ".claude" / "kb-flush.env").write_text(
        f"KB_REST_URL={url}\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n"
    )
    work = tmp_path / "recallproj"
    work.mkdir(exist_ok=True)
    return home, work


def _breaker(home: Path) -> Path:
    return home / ".claude" / "hooks" / "state" / ".recall-breaker.stamp"


def _hooklog(home: Path) -> str:
    log = home / ".claude" / "hooks" / "flush-session.log"
    return log.read_text(errors="replace") if log.exists() else ""


def _recall_lines(home: Path) -> list[str]:
    return [ln for ln in _hooklog(home).splitlines() if "RECALL UNAVAILABLE" in ln]


def _calls(rec_dir: Path) -> int:
    stamp = rec_dir / ".n"
    return int(stamp.read_text()) if stamp.exists() else 0


def _prompt(text: str, work: Path, sid: str = "s-recall") -> dict:
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": text,
        "cwd": str(work),
        "session_id": sid,
    }


def _start(work: Path, sid: str = "s-recall") -> dict:
    return {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(work),
        "session_id": sid,
    }


# A prompt that clears every noise gate: long enough, not a slash command, and
# carrying well over two content words.
_REAL_PROMPT = "why does the drain refill stall after the deadline expires"


def _recall_env(binp: Path, rec_dir: Path, **kw: str) -> dict:
    env = {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)}
    env.update(kw)
    return env


def _assert_ascii(text: str, what: str) -> None:
    for line in text.splitlines():
        assert line.isascii(), (
            f"{what} emitted a non-ASCII line: {line!r} — the Windows ANSI-codepage "
            "argv conversion mangles any non-ASCII byte (KB 12885)"
        )


@requires_curl
def test_a_timed_out_recall_is_logged_and_trips_the_breaker(tmp_path):
    """THE TRIPWIRE for 12885. A search that times out must stop being
    indistinguishable from an empty KB: one line in the hook family's log, and a
    breaker marker recording when it happened."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_RC="28"),  # 28 = curl's own timeout
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "", "a failed recall must still print nothing into the prompt"

    lines = _recall_lines(home)
    assert len(lines) == 1, f"expected exactly one failure line, got {lines!r}"
    line = lines[0]
    assert "event=UserPromptSubmit" in line, line
    assert "transport=rest" in line, line
    assert "/api/search" in line, line
    assert "rc=28" in line, line
    _assert_ascii(line, "the failure log line")

    marker = _breaker(home)
    assert marker.exists(), "the failure did not trip the breaker"
    assert marker.read_text().strip().isdigit(), (
        f"the breaker marker must record WHEN it tripped, got {marker.read_text()!r}"
    )


@requires_curl
def test_a_five_hundred_is_a_failure_even_though_curl_exits_zero(tmp_path):
    """`curl -sS` (no -f, which would discard the body) exits 0 on a 503 exactly
    as on a 200. Without the status the error page is simply a body with no rows
    in it — the fail-silent bug wearing a different hat."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(
            binp,
            rec_dir,
            CURL_SEARCH_RC="0",
            CURL_SEARCH_STATUS="503",
            CURL_SEARCH_BODY="<html>Service Unavailable</html>",
        ),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    lines = _recall_lines(home)
    assert len(lines) == 1, f"a 503 was swallowed as 'no knowledge': {_hooklog(home)!r}"
    assert "status=503" in lines[0], lines[0]
    assert _breaker(home).exists()


@requires_curl
def test_an_auth_rejection_is_logged_but_NEVER_trips_the_breaker(tmp_path):
    """Rig case 18's catch. A 401/403 is answered instantly (there is no dead
    wait for the breaker to protect the prompt from), it will not heal within a
    breaker window, and the hot-breaker note's 'kb_search still works' would be
    untrue precisely when the credential is bad. So: a loud log line, no stamp —
    and the NEXT prompt must still reach the KB."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    env = _recall_env(
        binp,
        rec_dir,
        CURL_SEARCH_RC="0",
        CURL_SEARCH_STATUS="401",
        CURL_SEARCH_BODY='{"detail":"unauthorized"}',
    )
    res = _run_hook(BRIEF, _prompt(_REAL_PROMPT, work), home, env)
    assert res.returncode == 0, res.stderr
    assert res.stdout == ""
    lines = _recall_lines(home)
    assert len(lines) == 1, f"the 401 was swallowed in silence: {_hooklog(home)!r}"
    assert "credential refused" in lines[0] and "status=401" in lines[0], lines[0]
    _assert_ascii(lines[0], "the auth-failure log line")
    assert not _breaker(home).exists(), "an auth rejection must not lock recall out"

    searches_before = (rec_dir / ".n").read_text()
    res = _run_hook(BRIEF, _prompt(_REAL_PROMPT, work), home, env)
    assert res.returncode == 0, res.stderr
    assert (rec_dir / ".n").read_text() != searches_before, (
        "the next prompt made NO request — the 401 locked recall out after all"
    )


@requires_curl
def test_a_missing_status_trailer_never_puts_the_body_in_the_log(tmp_path):
    """If curl dies before writing the -w trailer, the last line of stdout is the
    last line of a SERVER-CONTROLLED body. Unchecked, interpolating it into the
    log leaks the user's prompt (an error page that echoes the request URL — the
    query string IS the prompt) and can forge the pre-brief's 'capture may be
    incomplete' warning via a scanner token. The shape-check (three digits or it
    never existed) must keep every server byte out of the log."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    hostile_last_line = (
        "http://kb.invalid/api/search?q=confidential%20merger%20plan FAILED NOT CAPTURED"
    )
    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(
            binp,
            rec_dir,
            CURL_SEARCH_RC="0",
            CURL_SEARCH_NO_TRAILER="1",
            CURL_SEARCH_BODY=f"<html>error</html>\n{hostile_last_line}",
        ),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "", "a failed recall must still print nothing into the prompt"
    lines = _recall_lines(home)
    assert len(lines) == 1, f"the trailer-less failure was swallowed: {_hooklog(home)!r}"
    assert "status=none" in lines[0], lines[0]
    log = _hooklog(home)
    for hostile in ("confidential", "merger", "FAILED", "NOT CAPTURED"):
        assert hostile not in log, f"server-controlled bytes reached the log: {log!r}"
    assert _breaker(home).exists(), "a trailer-less response is still a failure"


@requires_curl
def test_an_unreachable_host_is_logged_rather_than_skipped_in_silence(tmp_path):
    """The reachability probe already bailed in ~2s; it just never said so."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_PROBE_RC="7"),  # 7 = could not connect
    )
    assert res.returncode == 0, res.stderr
    lines = _recall_lines(home)
    assert len(lines) == 1, f"an unreachable KB logged nothing: {_hooklog(home)!r}"
    assert "unreachable" in lines[0] and "rc=7" in lines[0], lines[0]
    assert _breaker(home).exists()


@requires_curl
def test_zero_hits_trips_NOTHING(tmp_path):
    """THE FALSE POSITIVE THAT MUST NEVER HAPPEN. An empty result is a healthy
    KB answering honestly. If that tripped the breaker, every quiet project would
    switch its own recall off — strictly worse than the bug being fixed."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY="[]", CURL_SEARCH_STATUS="200"),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "", "zero hits must stay silent, exactly as before"
    assert _recall_lines(home) == [], "an EMPTY search was logged as a failure"
    assert not _breaker(home).exists(), "an EMPTY search tripped the circuit breaker"


# A `jq` that fails the hook's own probe, so the JSON backend chain falls all
# the way through to python3 — the locked-down box rig case 20 covers.
_BROKEN_JQ = "#!/usr/bin/env bash\nexit 1\n"


def _shadow_jq(binp: Path) -> None:
    fake = binp / "jq"
    fake.write_text(_BROKEN_JQ)
    fake.chmod(0o755)


@requires_curl
@pytest.mark.parametrize("backend", ["jq", "python"])
def test_zero_hits_stays_a_SUCCESS_on_EITHER_json_backend(tmp_path, backend: str):
    """The two backends must agree about what an empty KB looks like, or the
    false positive comes back on exactly the boxes that have no jq."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    if backend == "python":
        _shadow_jq(binp)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY="[]", CURL_SEARCH_STATUS="200"),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "", res.stdout
    assert _recall_lines(home) == [], f"{backend} backend logged an empty search"
    assert not _breaker(home).exists(), f"{backend} backend tripped on an empty search"
    # Proof the backend under test really parsed the event rather than falling
    # back to the SessionStart branch: per-prompt recall asks for k=3, the
    # pre-brief for k=5.
    assert any("k=3" in u for u in _recorded(rec_dir, "url")), (
        f"the {backend} backend never parsed the event: {_recorded(rec_dir, 'url')}"
    )


@requires_curl
@pytest.mark.parametrize("backend", ["jq", "python"])
def test_an_unparseable_200_is_a_FAILURE_on_either_json_backend(tmp_path, backend: str):
    """A 200 carrying something that is not the search array — a proxy error
    page, a truncated body — is a failure, not an empty KB. The python branch
    used to swallow the parse error as `sys.exit(0)` and so was the quiet one;
    jq has always exited non-zero."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    if backend == "python":
        _shadow_jq(binp)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(
            binp,
            rec_dir,
            CURL_SEARCH_BODY="<html>gateway had a bad day</html>",
            CURL_SEARCH_STATUS="200",
        ),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout == "", res.stdout
    lines = _recall_lines(home)
    assert len(lines) == 1, f"{backend} backend read a broken body as an empty KB"
    assert "unparseable" in lines[0], lines[0]
    # Same proof as above: only a working backend can name the event.
    assert "event=UserPromptSubmit" in lines[0], (
        f"the {backend} backend never parsed the event: {lines[0]}"
    )
    assert _breaker(home).exists()


@requires_curl
def test_a_hot_breaker_makes_NO_call_and_says_so_in_one_ascii_line(tmp_path):
    """The point of the breaker: while it is hot the hook must not pay the
    remote call at all. Asserted against the recording stub — a fast failure
    would still show up there."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    _breaker(home).write_text(f"{int(time.time())}\n")

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY=_ONE_HIT),
    )
    assert res.returncode == 0, res.stderr
    assert _calls(rec_dir) == 0, (
        f"a hot breaker still called out {_calls(rec_dir)} time(s): {_recorded(rec_dir, 'url')}"
    )
    assert res.stdout.count("\n") == 1, f"expected ONE context line, got {res.stdout!r}"
    assert res.stdout.startswith("[KB recall] skipped:"), res.stdout
    assert "breaker hot" in res.stdout and "retry after" in res.stdout, res.stdout
    assert "kb_search still works" in res.stdout, res.stdout
    _assert_ascii(res.stdout, "the hot-breaker context line")


@requires_curl
def test_the_two_events_SHARE_one_breaker(tmp_path):
    """SessionStart and UserPromptSubmit hit the same server, so a failure on
    either is a failure for both — one marker, not two."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    tripped = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_RC="28"),
    )
    assert tripped.returncode == 0, tripped.stderr
    assert _breaker(home).exists()

    before = _calls(rec_dir)
    res = _run_hook(BRIEF, _start(work, "s-next"), home, _recall_env(binp, rec_dir))
    assert res.returncode == 0, res.stderr
    assert _calls(rec_dir) == before, (
        "SessionStart ignored the breaker the prompt hook tripped and called out anyway"
    )
    assert res.stdout.startswith("[KB recall] skipped:"), res.stdout
    _assert_ascii(res.stdout, "the hot-breaker context line on SessionStart")


@requires_curl
def test_an_expired_breaker_retries_and_a_SUCCESS_clears_the_marker(tmp_path):
    """The breaker is a pause, never a verdict: the next prompt after the window
    tries again, and a search that answers removes the marker."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    _breaker(home).write_text(f"{int(time.time()) - 1000}\n")  # long expired

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY=_ONE_HIT, CURL_SEARCH_STATUS="200"),
    )
    assert res.returncode == 0, res.stderr
    assert _calls(rec_dir) >= 1, "an EXPIRED breaker still suppressed the call"
    assert "[KB recall] skipped" not in res.stdout, res.stdout
    assert "[KB recall" in res.stdout, f"the retry printed no hits: {res.stdout!r}"
    assert "the drain deadline" in res.stdout, res.stdout
    assert not _breaker(home).exists(), "a successful search left the breaker armed"


@requires_curl
def test_the_breaker_window_is_configurable(tmp_path):
    """KB_RECALL_BREAKER_SECS follows the family's config convention (a KB_* key
    in kb-flush.env or the environment), and 0 disables the breaker outright."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    _breaker(home).write_text(f"{int(time.time()) - 30}\n")  # 30s old

    # Default window (300s): still hot at 30s.
    hot = _run_hook(BRIEF, _prompt(_REAL_PROMPT, work), home, _recall_env(binp, rec_dir))
    assert hot.stdout.startswith("[KB recall] skipped:"), hot.stdout
    assert _calls(rec_dir) == 0

    # A 10s window: the same 30s-old marker is cold.
    short = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work, "s-short"),
        home,
        _recall_env(binp, rec_dir, KB_RECALL_BREAKER_SECS="10", CURL_SEARCH_BODY=_ONE_HIT),
    )
    assert short.stdout.startswith("[KB recall"), short.stdout
    assert "skipped" not in short.stdout, short.stdout
    assert _calls(rec_dir) >= 1, "a 10s window still treated a 30s-old marker as hot"


@requires_curl
def test_a_garbage_marker_can_never_become_a_PERMANENT_skip(tmp_path):
    """A corrupt stamp must fail OPEN. A breaker that cannot prove it is fresh
    and still suppresses would silently disable recall forever — the one outcome
    worse than the bug."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    _breaker(home).write_text("not-a-number\n")

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY=_ONE_HIT),
    )
    assert res.returncode == 0, res.stderr
    assert _calls(rec_dir) >= 1, "a garbage marker suppressed the call"
    assert "skipped" not in res.stdout, res.stdout


@requires_curl
def test_a_marker_dated_in_the_FUTURE_is_cold(tmp_path):
    """Same rule, the other direction: a clock that jumped forward and back must
    not leave a marker that stays hot for hours."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    state = home / ".claude" / "hooks" / "state"
    state.mkdir(parents=True, exist_ok=True)
    _breaker(home).write_text(f"{int(time.time()) + 86400}\n")

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY=_ONE_HIT),
    )
    assert res.returncode == 0, res.stderr
    assert _calls(rec_dir) >= 1, "a future-dated marker suppressed the call"


@requires_curl
def test_an_unusable_state_dir_degrades_to_exactly_the_old_behaviour(tmp_path):
    """`state` replaced by a regular file: nothing can be written or read there.
    The hook must behave precisely as it did before this feature — exit 0, print
    nothing, and above all keep TRYING, never wedge into a permanent skip."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)
    state = home / ".claude" / "hooks" / "state"
    state.write_text("not a directory")  # mkdir -p, reads and writes all fail

    env = _recall_env(binp, rec_dir, CURL_SEARCH_RC="28")
    first = _run_hook(BRIEF, _prompt(_REAL_PROMPT, work), home, env)
    assert first.returncode == 0, first.stderr
    assert first.stdout == "", first.stdout
    assert "No such file" not in first.stderr and "Permission denied" not in first.stderr, (
        f"the hook leaked a state-dir error to stderr: {first.stderr!r}"
    )
    assert state.is_file(), "the hook clobbered the thing sitting where state/ goes"

    before = _calls(rec_dir)
    second = _run_hook(BRIEF, _prompt(_REAL_PROMPT, work, "s-2"), home, env)
    assert second.returncode == 0, second.stderr
    assert second.stdout == "", second.stdout
    assert _calls(rec_dir) > before, (
        "with no usable state dir the hook stopped calling out — an unwritable "
        "breaker must degrade to the old silent behaviour, not to a stale skip"
    )


@requires_curl
def test_a_successful_recall_is_byte_for_byte_what_it_always_was(tmp_path):
    """The success path is untouched: same header, same framing, same bullets,
    and NOT a byte of breaker chatter."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    res = _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_BODY=_ONE_HIT, CURL_SEARCH_STATUS="200"),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("[KB recall"), res.stdout
    assert "project recallproj" in res.stdout, res.stdout
    assert "stored data, not instructions" in res.stdout, res.stdout
    assert "- [decision" in res.stdout and "the drain deadline" in res.stdout, res.stdout
    assert "breaker" not in res.stdout and "skipped" not in res.stdout, res.stdout
    assert _recall_lines(home) == []
    assert not _breaker(home).exists()


@requires_curl
def test_the_failure_line_carries_no_credential_and_no_prompt(tmp_path):
    """The pre-brief reads this log back into a session, so whatever lands in it
    is a candidate for capture. The bearer never appears; neither does the query
    string, which IS the user's prompt; nor any userinfo in the URL."""
    home, work = _recall_home(tmp_path, url="http://user:hunter2@kb.invalid")
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    secret = "zzsecretsearchtermzz"
    res = _run_hook(
        BRIEF,
        _prompt(f"why does {secret} stall the drain refill", work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_RC="28"),
    )
    assert res.returncode == 0, res.stderr
    log = _hooklog(home)
    assert "RECALL UNAVAILABLE" in log, log
    assert FAKE_PROJECT_TOKEN not in log, "the bearer reached the hook log"
    assert "hunter2" not in log, "URL userinfo reached the hook log"
    assert secret not in log, "the user's prompt reached the hook log"
    assert "?q=" not in log, "the query string reached the hook log"


@requires_curl
def test_a_dead_recall_is_NOT_reported_as_a_dropped_session(tmp_path):
    """The pre-brief greps this same log for flush failures. A recall outage is
    not a lost transcript, and phrasing it with one of that scanner's tokens
    would make every recall blip claim the session was not captured."""
    home, work = _recall_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recall_curl(home, rec_dir)

    _run_hook(
        BRIEF,
        _prompt(_REAL_PROMPT, work),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_RC="28"),
    )
    line = _recall_lines(home)[0]
    for token in ("FAILED", "BUG:", "NOT CONFIGURED", "NOT CAPTURED", "NEEDS TOKEN"):
        assert token not in line, (
            f"the recall failure line contains {token!r}, which the pre-brief's "
            "flush-failure scanner treats as a session that was never captured"
        )

    # And prove it end to end, with the breaker disabled so the skip line cannot
    # stand in for the warning we are asserting is absent.
    res = _run_hook(
        BRIEF,
        _start(work, "s-after"),
        home,
        _recall_env(binp, rec_dir, CURL_SEARCH_RC="28", KB_RECALL_BREAKER_SECS="0"),
    )
    assert res.returncode == 0, res.stderr
    assert "session-flush reported a recent failure" not in res.stdout, res.stdout


def test_the_breaker_marker_is_not_swept_by_the_recall_dedup_prune():
    """The prune deletes `recall-*.last` older than 7 days. The breaker stamp
    must not be named into that pattern — and it is short-lived anyway."""
    text = BRIEF.read_text()
    assert "-name 'recall-*.last'" in text, "the dedup prune pattern changed"
    marker = re.search(r'BRK_FILE="\$BRK_DIR/([^"]+)"', text)
    assert marker, "the breaker marker is no longer a single named file"
    assert not fnmatch.fnmatch(marker.group(1), "recall-*.last"), (
        f"the breaker marker {marker.group(1)!r} is swept by the dedup prune"
    )


def test_every_line_the_breaker_paths_can_emit_is_ascii_by_construction():
    """Structural half of the ASCII rule: the two lines this feature can print
    are ASCII in the SOURCE, so no locale or code path can smuggle a wide
    character into an argv that Windows will mangle (KB 12885's other lesson)."""
    text = BRIEF.read_text()
    for needle in ("[KB recall] skipped:", "RECALL UNAVAILABLE:"):
        idx = text.index(needle)
        line = text[text.rindex("\n", 0, idx) + 1 : text.index("\n", idx)]
        assert line.isascii(), f"the {needle!r} line is not ASCII in the source: {line!r}"


# ---------------------------------------------------------------------------
# Behavioural: ONE hook per event — the stdin fan-out dispatcher
# ---------------------------------------------------------------------------
#
# THE BUG THIS EXISTS FOR (verified on the real Windows box, KB item 14508).
# Claude Code on Windows has an OPEN upstream bug (GH #77078 + siblings): when
# MORE THAN ONE hook is registered on ONE event, only one of the spawned
# processes receives the event JSON on stdin — the others read an empty stream.
# There is no env-var fallback carrying the fields, and no fixed version.
#
# Our install used to put TWO hooks on UserPromptSubmit (turn-start + the recall)
# and one or more on Stop, so on Windows the recall regularly ran with no prompt
# and no cwd: it printed nothing, which is byte-for-byte what a healthy empty KB
# prints. The workaround the docs recommend is ONE hook per event that reads
# stdin once and fans out itself, which is kb-hook-dispatch.sh.
#
# These tests are the tripwire for that fan-out: same bytes to every child, in
# the right order, stdout passing through, and never a non-zero exit.

# A stand-in child. Records the stdin it was really handed and appends its own
# name to a shared order file, so "both children got the same bytes, in this
# order" is an assertion about observed behaviour rather than about the source.
# OUT_<TAG> / RC_<TAG> in the environment drive what it prints and returns.
_CHILD_RECORDER = """#!/usr/bin/env bash
D="$KB_DISPATCH_REC"
mkdir -p "$D"
TAG="__TAG__"
cat >"$D/stdin.$TAG"
printf '%s\\n' "$TAG" >>"$D/order"
: >"$D/args.$TAG"
for a in "$@"; do printf '%s\\n' "$a" >>"$D/args.$TAG"; done
_o="OUT_$TAG"
_r="RC_$TAG"
[ -n "${!_o:-}" ] && printf '%s' "${!_o}"
exit "${!_r:-0}"
"""

# The event map, as a test fixture rather than a re-reading of the script.
_DISPATCH_TAGS = {
    "flush-session-to-kb.sh": "FLUSH",
    "kb-session-brief.sh": "BRIEF",
    "turn-start.sh": "TURN",
    "notify-complete.sh": "NOTIFY",
}
_DISPATCH_MAP = {
    "PreCompact": ["FLUSH"],
    "SessionEnd": ["FLUSH"],
    "SessionStart": ["BRIEF"],
    "UserPromptSubmit": ["TURN", "BRIEF"],
    # Wave C (C5): the flush runs FIRST on Stop (its gate is 2 stats + a few cats
    # under a 3 s budget), then notify reads the freshest offset.
    "Stop": ["FLUSH", "NOTIFY"],
}


def _dispatch_dir(tmp_path: Path, omit: set[str] = frozenset()) -> Path:
    """A hooks directory holding the REAL dispatcher beside recorder children —
    the dispatcher resolves them relative to itself, so this is the whole of the
    resolution path under test."""
    hookdir = tmp_path / "hookdir"
    hookdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DISPATCH, hookdir / DISPATCH.name)
    for name, tag in _DISPATCH_TAGS.items():
        if name in omit:
            continue
        child = hookdir / name
        child.write_text(_CHILD_RECORDER.replace("__TAG__", tag))
        child.chmod(0o755)
    return hookdir


def _fired(rec_dir: Path) -> list[str]:
    order = rec_dir / "order"
    return order.read_text().split() if order.exists() else []


def _run_dispatch(tmp_path: Path, event: str, payload, home: Path, omit=frozenset(), **env):
    hookdir = _dispatch_dir(tmp_path, omit)
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir(exist_ok=True)
    res = _run_hook(
        hookdir / "kb-hook-dispatch.sh",
        payload,
        home,
        {"KB_DISPATCH_REC": str(rec_dir), **env},
        args=(event,),
    )
    return res, rec_dir


def test_the_dispatcher_gives_BOTH_prompt_children_byte_identical_stdin(tmp_path):
    """THE TRIPWIRE for 14508. UserPromptSubmit is the event that carried two
    hooks, so it is the one the Windows bug ate. Both children must see the same
    bytes — the cheap turn stamp FIRST, the 35 s recall second — and the recall's
    stdout must reach Claude Code unchanged, because that stdout IS the injected
    context."""
    home = _hook_home(tmp_path)
    event = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "why does the drain refill stall",
        "cwd": str(tmp_path / "proj"),
        "session_id": "s-dispatch",
    }
    res, rec = _run_dispatch(
        tmp_path, "UserPromptSubmit", event, home, OUT_BRIEF="[KB recall] one hit\n"
    )
    assert res.returncode == 0, res.stderr

    assert _fired(rec) == ["TURN", "BRIEF"], (
        f"fan-out order is {_fired(rec)!r} — the 5 s stamp must not queue behind the 35 s recall"
    )
    turn = (rec / "stdin.TURN").read_bytes()
    brief = (rec / "stdin.BRIEF").read_bytes()
    assert turn == brief, "the two children received DIFFERENT bytes — the bug, reimplemented"
    assert turn == json.dumps(event).encode(), (
        f"the event was altered on the way to the children: {turn!r}"
    )
    assert res.stdout == "[KB recall] one hit\n", (
        f"the brief's stdout did not pass through untouched: {res.stdout!r}"
    )


@pytest.mark.parametrize("event", sorted(_DISPATCH_MAP))
def test_each_event_reaches_exactly_the_children_it_maps_to(tmp_path, event: str):
    """Stop must not fire a flush, PreCompact must not fire recall: a mis-routed
    event would be a session captured on every keystroke, or notifications on
    compaction."""
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(tmp_path, event, {"hook_event_name": event}, home)
    assert res.returncode == 0, res.stderr
    assert _fired(rec) == _DISPATCH_MAP[event], (
        f"{event} fired {_fired(rec)!r}, expected {_DISPATCH_MAP[event]!r}"
    )


def test_an_unknown_event_runs_nothing_at_all(tmp_path):
    """Claude Code gains events; an unrecognised one must be a no-op, not a
    guess. It still exits 0 and prints nothing into whatever context it fired
    from."""
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(tmp_path, "PostToolUse", {"hook_event_name": "PostToolUse"}, home)
    assert res.returncode == 0, res.stderr
    assert _fired(rec) == [], f"an unknown event ran {_fired(rec)!r}"
    assert res.stdout == "", res.stdout


def test_an_EMPTY_event_still_runs_the_children_and_is_logged_exactly_once(tmp_path):
    """The residual case: Claude Code hands the ONE registered hook an empty
    stream anyway. The children still run — each degrades gracefully, and a
    stdin-less notify still pushes a basic ping — but the hit is made COUNTABLE
    with one line in the log the pre-brief already reads."""
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(tmp_path, "UserPromptSubmit", "", home)
    assert res.returncode == 0, res.stderr
    assert _fired(rec) == ["TURN", "BRIEF"], (
        f"an empty event skipped the children entirely: {_fired(rec)!r}"
    )
    assert (rec / "stdin.TURN").read_bytes() == b""
    assert (rec / "stdin.BRIEF").read_bytes() == b""

    lines = [ln for ln in _hooklog(home).splitlines() if "arrived EMPTY" in ln]
    assert len(lines) == 1, f"expected exactly one EMPTY line, got {lines!r}"
    line = lines[0]
    assert "hook event arrived EMPTY: event=UserPromptSubmit" in line, line
    _assert_ascii(line, "the empty-event log line")

    # The two-sided pin, exactly as for a dead recall: this log is also the
    # pre-brief's flush-failure scanner input, and an empty event is not a lost
    # transcript. Phrasing it with one of those tokens would make every residual
    # Windows hit claim the session was never captured.
    for token in ("FAILED", "BUG:", "NOT CONFIGURED", "NOT CAPTURED", "NEEDS TOKEN"):
        assert token not in line, (
            f"the empty-event line contains {token!r}, which the pre-brief's "
            "flush-failure scanner treats as a session that was never captured"
        )


def test_a_failing_child_stops_neither_the_next_child_nor_the_exit_code(tmp_path):
    """Sequential fan-out means one bad child could take the rest of the event
    with it. It must not: turn-start crashing has to leave recall intact, and a
    hook that exits non-zero can block a Claude Code prompt outright."""
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(
        tmp_path,
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit"},
        home,
        RC_TURN="7",
        RC_BRIEF="3",
        OUT_BRIEF="[KB recall] still injected\n",
    )
    assert res.returncode == 0, f"the dispatcher propagated a child failure: {res!r}"
    assert _fired(rec) == ["TURN", "BRIEF"], (
        f"a failing first child swallowed the rest of the event: {_fired(rec)!r}"
    )
    assert res.stdout == "[KB recall] still injected\n", res.stdout


def test_a_child_that_is_not_installed_is_simply_skipped(tmp_path):
    """notify-complete.sh and turn-start.sh are OPTIONAL installs (`[ -f ]` in
    install.sh), and every event is registered regardless. A missing child must
    be a no-op, not a shell error on every prompt."""
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(
        tmp_path,
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit"},
        home,
        omit={"turn-start.sh"},
        OUT_BRIEF="[KB recall] hit\n",
    )
    assert res.returncode == 0, res.stderr
    assert _fired(rec) == ["BRIEF"], _fired(rec)
    assert res.stdout == "[KB recall] hit\n", res.stdout
    assert res.stderr == "", f"a missing child leaked an error: {res.stderr!r}"


# ---------------------------------------------------------------------------
# Behavioural: install.sh MIGRATES a box off the old per-script registration
# ---------------------------------------------------------------------------
#
# The migration is the dangerous half. If a re-install leaves the OLD
# kb-session-brief.sh entry beside the new dispatcher entry, UserPromptSubmit
# holds two hooks again — which both re-arms the Windows stdin bug AND fires
# recall TWICE per prompt (two searches, two injected blocks, two budgets).

_LEGACY_EVENTS = {
    "PreCompact": [("flush-session-to-kb.sh", "", 15)],
    "SessionEnd": [("flush-session-to-kb.sh", "", 15)],
    "SessionStart": [("kb-session-brief.sh", "startup|clear", 35)],
    # THE SHAPE THAT MATTERS: two entries on one event.
    "UserPromptSubmit": [("kb-session-brief.sh", "", 35), ("turn-start.sh", "", 5)],
    "Stop": [("notify-complete.sh", "", 15)],
}


def _legacy_settings(home: Path) -> Path:
    """A settings.json in the shape install.sh wrote before 2026-08-06, plus a
    foreign hook that must survive the migration untouched."""
    hooks_dir = home / ".claude" / "hooks"
    settings = home / ".claude" / "settings.json"
    data = {
        "model": "opus",
        "hooks": {
            ev: [
                {
                    "matcher": matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": str(hooks_dir / script),
                            "timeout": timeout,
                        }
                    ],
                }
                for script, matcher, timeout in entries
            ]
            for ev, entries in _LEGACY_EVENTS.items()
        },
    }
    data["hooks"]["PreCompact"].insert(
        0,
        {"matcher": "", "hooks": [{"type": "command", "command": "/usr/local/bin/user-pre.sh"}]},
    )
    settings.write_text(json.dumps(data, indent=2) + "\n")
    return settings


def _entry_commands(settings: Path, event: str) -> list[str]:
    """The command each entry on ``event`` would really run — the args[0] form on
    Windows, the command string everywhere else."""
    data = json.loads(settings.read_text())
    out = []
    for item in data["hooks"].get(event, []):
        inner = item["hooks"][0]
        out.append(inner["args"][0] if inner.get("args") else inner["command"])
    return out


def test_install_migrates_the_old_two_entry_shape_to_ONE_dispatcher(tmp_path):
    """Exactly one dispatcher entry per event, zero legacy entries, foreign hooks
    kept — and byte-identical on the second run, because the installer is meant
    to be re-run on every asset refresh."""
    home = _hook_home(tmp_path)
    settings = _legacy_settings(home)
    dispatch = str(home / ".claude" / "hooks" / "kb-hook-dispatch.sh")

    res = _run_hook(INSTALL, {}, home)
    assert res.returncode == 0, res.stderr

    legacy_paths = {str(home / ".claude" / "hooks" / s) for s in _DISPATCH_TAGS}
    for event in _LEGACY_EVENTS:
        cmds = _entry_commands(settings, event)
        ours = [c for c in cmds if c == dispatch or c.startswith(dispatch + " ")]
        assert len(ours) == 1, f"{event} holds {len(ours)} dispatcher entries: {cmds!r}"
        left = [c for c in cmds if c in legacy_paths]
        assert left == [], (
            f"{event} still holds legacy per-script entries {left!r} — a leftover "
            "brief entry double-fires recall on every prompt"
        )

    assert _entry_commands(settings, "PreCompact")[0] == "/usr/local/bin/user-pre.sh", (
        "the user's own hook was dropped by the migration"
    )
    data = json.loads(settings.read_text())
    assert data["model"] == "opus", "unrelated settings were lost"
    ups = data["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert ups["timeout"] == 40, (
        f"UserPromptSubmit timeout is {ups['timeout']} — one process now runs the "
        "5 s stamp and the 35 s recall"
    )

    first = settings.read_bytes()
    res2 = _run_hook(INSTALL, {}, home)
    assert res2.returncode == 0, res2.stderr
    assert settings.read_bytes() == first, "a second install rewrote settings.json"


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


# ---------------------------------------------------------------------------
# Behavioural: install.sh verifies what it INSTALLED before stamping the version
# ---------------------------------------------------------------------------
#
# THE TRUST GAP (review #3 P1, KB 19663). install.sh stamped .setup-version from
# the fetched manifest without ever hashing the files it had just written — so a
# partial or corrupted install (truncated copy, Git Bash autocrlf rewriting every
# LF) recorded itself as current, and the stamp SILENCED the staleness banner,
# the one channel that reports a broken box every session. The server already
# publishes a sha256 per served file; the stamp must now be EARNED by a full
# per-file match, and any mismatch must leave the box unversioned (stripping a
# stale stamp too), which the banner reports honestly.

_MANIFEST_CURL = r"""#!/usr/bin/env bash
# curl stand-in for the installer-verification tests: answers the manifest URL
# with the JSON in $CURL_MANIFEST (empty -> hard failure, like an unreachable or
# older server), refuses notify.env, ignores everything else. Never a network.
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
case "$URL" in
  *"/api/setup/manifest.json")
    [ -n "${CURL_MANIFEST:-}" ] || exit 22
    printf '%s' "$CURL_MANIFEST" ;;
  *"/api/setup/notify.env") exit 22 ;;
  *) : ;;
esac
exit 0
"""


def _shipped_manifest(drop: tuple[str, ...] = ()) -> tuple[str, str]:
    """The manifest EXACTLY as src/kbrest/server.py derives it, computed from the
    canonical contrib/ scripts — the same derivation, never a restatement of what
    this test believes the hashes are."""
    files = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(CONTRIB.glob("*.sh"))
    }
    for name in drop:
        files.pop(name)
    version = hashlib.sha256(
        "".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()
    ).hexdigest()[:12]
    return version, json.dumps({"version": version, "files": files, "count": len(files)})


def _verify_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A configured REMOTE box with the manifest-serving curl first on PATH."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = home / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    curl = binp / "curl"
    curl.write_text(_MANIFEST_CURL)
    curl.chmod(0o755)
    rec_dir.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "kb-flush.env").write_text(
        f"KB_REST_URL=http://127.0.0.1:9\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n"
    )
    return home, binp, rec_dir


def _run_install_verified(home: Path, binp: Path, rec_dir: Path, manifest: str):
    return _run_hook(
        INSTALL,
        {},
        home,
        {
            "PATH": f"{binp}:{os.environ.get('PATH', '')}",
            "CURL_REC": str(rec_dir),
            "CURL_MANIFEST": manifest,
            "KB_REST_URL": "http://127.0.0.1:9",
            "KB_TOKEN": FAKE_PROJECT_TOKEN,
        },
    )


def _corrupting_install(binp: Path, victim: str) -> None:
    """An `install` that corrupts ONE destination file after the real copy —
    the exact 'survives to the stamp' shape: source intact, installed bytes
    mangled (a partial write, or autocrlf appending its damage)."""
    real = shutil.which("install")
    assert real, "install(1) missing from the test environment"
    (binp / "install").write_text(
        "#!/usr/bin/env bash\n"
        f'"{real}" "$@"\n'
        "rc=$?\n"
        'last=""\n'
        'for a in "$@"; do last="$a"; done\n'
        f'case "$last" in */{victim}) printf \'\\r\\n\' >>"$last" ;; esac\n'
        "exit $rc\n"
    )
    (binp / "install").chmod(0o755)


def test_install_stamps_only_after_verifying_the_installed_bytes(tmp_path):
    """The clean path: every installed hook hashes to what the manifest says, so
    the stamp is written — and says it was verified, not merely fetched."""
    home, binp, rec_dir = _verify_home(tmp_path)
    version, manifest = _shipped_manifest()
    res = _run_install_verified(home, binp, rec_dir, manifest)
    assert res.returncode == 0, res.stderr
    stamp = home / ".claude" / "hooks" / ".setup-version"
    assert stamp.read_text() == version + "\n", "the verified version was not stamped"
    assert "verified" in res.stdout
    assert "MISMATCH" not in res.stdout + res.stderr


def test_a_corrupted_install_stays_UNVERSIONED_with_a_loud_per_file_line(tmp_path):
    """One corrupted byte in one installed file: no stamp, a stale stamp from an
    earlier run STRIPPED (a stamp over corrupted files is the silence this whole
    check exists to end), and a loud line naming the file with expected-vs-got
    hash prefixes — 12 hex chars, never the full digests or path noise."""
    home, binp, rec_dir = _verify_home(tmp_path)
    version, manifest = _shipped_manifest()
    _corrupting_install(binp, "kb-session-brief.sh")
    stamp = home / ".claude" / "hooks" / ".setup-version"
    stamp.write_text("stalestamp00\n")  # from an earlier, clean install

    res = _run_install_verified(home, binp, rec_dir, manifest)
    assert res.returncode == 0, res.stderr
    out = res.stdout + res.stderr
    assert not stamp.exists(), "a stale stamp survived a failed verification"
    assert "MISMATCH kb-session-brief.sh" in out, f"no loud per-file line: {out!r}"
    expected = json.loads(manifest)["files"]["kb-session-brief.sh"]
    got = hashlib.sha256(
        (home / ".claude" / "hooks" / "kb-session-brief.sh").read_bytes()
    ).hexdigest()
    assert expected[:12] in out and got[:12] in out, f"expected-vs-got prefixes absent: {out!r}"
    assert expected not in out and got not in out, "full 64-char digests are noise"
    assert "CRLF" in out, "the mismatch hint no longer names the Git Bash CRLF trap"
    assert "UNVERSIONED" in out
    mline = next(ln for ln in out.splitlines() if "MISMATCH kb-session-brief.sh" in ln)
    assert str(home / ".claude" / "hooks") not in mline, "the per-file line carries path noise"


def test_a_hash_absent_from_the_manifest_is_a_MISMATCH_not_a_pass(tmp_path):
    """A file the manifest does not vouch for cannot be verified, and unverified
    must never stamp — treating absence as success would let a truncated
    manifest wave through exactly the files it lost."""
    home, binp, rec_dir = _verify_home(tmp_path)
    _version, manifest = _shipped_manifest(drop=("turn-start.sh",))
    res = _run_install_verified(home, binp, rec_dir, manifest)
    assert res.returncode == 0, res.stderr
    out = res.stdout + res.stderr
    assert not (home / ".claude" / "hooks" / ".setup-version").exists()
    assert "MISMATCH turn-start.sh" in out
    assert "no hash" in out


def test_no_manifest_skips_verification_with_a_note_and_no_stamp(tmp_path):
    """An enrolment-less LOCAL install (or an older/unreachable server) has
    nothing to verify against: the install still succeeds, stamps nothing —
    exactly the pre-verification behavior — and now SAYS so in one line."""
    home, binp, rec_dir = _verify_home(tmp_path)
    res = _run_install_verified(home, binp, rec_dir, "")
    assert res.returncode == 0, res.stderr
    assert not (home / ".claude" / "hooks" / ".setup-version").exists()
    assert "no manifest available" in res.stdout


def test_the_verification_loop_names_every_hook_asset():
    """SET EQUALITY with the shipped assets, minus install.sh (served in the
    manifest but never copied into ~/.claude/hooks — the fetch verifies it). A
    new hook asset that lands in setup_assets/ but not in this loop would
    install unverified under a stamp that vouches for it."""
    m = re.search(r"for _hf in (.*?);\s*do", INSTALL.read_text(), re.S)
    assert m, "install.sh no longer verifies its installed hooks in a loop"
    named = set(re.findall(r"[A-Za-z0-9_.-]+\.sh", m.group(1)))
    shipped = {p.name for p in SHIPPED.glob("*.sh")} - {"install.sh"}
    assert named == shipped, (
        f"the verify loop drifted from the shipped asset set: "
        f"extra={sorted(named - shipped)} missing={sorted(shipped - named)}"
    )


def test_kb_sha256_matches_hashlib_byte_for_byte(tmp_path):
    """Source-extracted and really executed. CRLF is the payload here: the
    digest is over the exact bytes, so the one mangle Git Bash actually performs
    (LF -> CRLF) can never hash equal — that is the point of hashing at all."""
    fn = _shell_fn(INSTALL, "kb_sha256")
    lf = tmp_path / "lf.bin"
    lf.write_bytes(b"line one\nline two\n\x00\xff")
    crlf = tmp_path / "crlf.bin"
    crlf.write_bytes(b"line one\r\nline two\r\n\x00\xff")

    def digest(path: Path) -> str:
        return subprocess.run(
            ["bash", "-c", f'KB_PY_BIN=""\n{fn}\nkb_sha256 "$1"', "-", str(path)],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert digest(lf) == hashlib.sha256(lf.read_bytes()).hexdigest()
    assert digest(crlf) == hashlib.sha256(crlf.read_bytes()).hexdigest()
    assert digest(lf) != digest(crlf), "CRLF mangling did not change the digest"


def test_kb_sha256_python_fallback_agrees_with_sha256sum(tmp_path):
    """A box with no sha256sum (macOS, odd minimal images) hashes through the
    same python3 the JSON fallback already relies on — hashlib is stdlib, so no
    new dependency — and the two backends must agree exactly."""
    fn = _shell_fn(INSTALL, "kb_sha256")
    target = tmp_path / "t.bin"
    target.write_bytes(b"payload\r\nwith bytes\x01")
    bindir = tmp_path / "restricted-bin"
    bindir.mkdir()
    for tool in ("bash", "python3"):
        real = shutil.which(tool)
        assert real, f"{tool} missing from the test environment"
        (bindir / tool).symlink_to(real)
    res = subprocess.run(
        [
            "bash",
            "-c",
            f'command -v sha256sum >/dev/null 2>&1 && exit 97\nKB_PY_BIN=""\n{fn}\nkb_sha256 "$1"',
            "-",
            str(target),
        ],
        env={"PATH": str(bindir), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
    )
    assert res.returncode != 97, "sha256sum leaked into the restricted PATH"
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == hashlib.sha256(target.read_bytes()).hexdigest()


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


# ---------------------------------------------------------------------------
# Behavioural: the LAST bearers still travelling in argv (review #3 wave D)
# ---------------------------------------------------------------------------
#
# The structural tests above prove the SOURCE builds a curl config; these prove
# the processes really invoked put the credential on stdin and nothing else.
# The two scripts here are the transcript-capture path itself — the one family
# member whose bearer, on a single-token box, is the OWNER master token.


def _wait_for(pred, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_the_flush_rest_submission_carries_its_bearer_on_stdin_never_argv(tmp_path):
    """The REST submission runs in the DETACHED child, where nothing else ever
    looked: `-H "Authorization: Bearer $KB_TOKEN"` sat there long after every
    sibling had moved to the stdin config. Assert against the recorded process,
    not the source."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    work = tmp_path / "argvproj"
    transcript = _transcript(work / "t.jsonl")
    (home / ".claude" / "kb-flush.env").write_text(
        f"KB_REST_URL=http://127.0.0.1:9\nKB_TOKEN={FAKE_PROJECT_TOKEN}\n"
    )
    res = _run_hook(
        FLUSH,
        {
            "transcript_path": str(transcript),
            "cwd": str(work),
            "session_id": "s-argv",
            "hook_event_name": "SessionEnd",
        },
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
    )
    assert res.returncode == 0, res.stderr

    def _submitted() -> bool:
        try:
            return any("/api/ingest-session" in u for u in _recorded(rec_dir, "url"))
        except FileNotFoundError:
            return False

    assert _wait_for(_submitted), (
        "the flush child never invoked curl for the submission; flush log: "
        + (home / ".claude/hooks/flush-session.log").read_text(errors="replace")
    )
    urls = _recorded(rec_dir, "url")
    argvs = _recorded(rec_dir, "argv")
    stdins = _recorded(rec_dir, "stdin")
    idx = [n for n, u in enumerate(urls) if "/api/ingest-session" in u]
    assert idx
    for n in idx:
        assert FAKE_PROJECT_TOKEN not in argvs[n], (
            f"the bearer is on the curl command line: {argvs[n]!r}"
        )
        assert "Authorization" not in argvs[n], f"an auth header is built in argv: {argvs[n]!r}"
        assert FAKE_PROJECT_TOKEN in stdins[n], (
            "the bearer is not on argv, but it is not on stdin either — the "
            "submission is now unauthenticated"
        )


_MCP_CURL = r"""#!/usr/bin/env bash
# curl stand-in for the MCP client: records argv + the stdin config, then plays
# the server's part by CALL NUMBER -- 1: the initialize response (the session id
# travels in a header, which `curl -D -` dumps to stdout); 2: the initialized
# notification (no body); 3: the tools/call result; 4: the session DELETE.
# Never a network.
D="$CURL_REC"
mkdir -p "$D"
N=$(( $(cat "$D/.n" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$N" >"$D/.n"
printf '%s\n' "$*" >"$D/argv.$N"
: >"$D/stdin.$N"
for a in "$@"; do
  if [ "$a" = "-K" ]; then cat >"$D/stdin.$N"; break; fi
done
case "$N" in
  1) printf 'HTTP/1.1 200 OK\r\nMcp-Session-Id: sess-argv-test\r\n\r\n' ;;
  3) printf '%s' '{"jsonrpc":"2.0","id":2,"result":{"isError":false,' \
       '"content":[{"type":"text","text":"queued 42"}]}}' ;;
esac
exit 0
"""


def test_the_mcp_helper_carries_its_bearer_on_stdin_on_all_four_requests(tmp_path):
    """kb-mcp-call.sh makes FOUR credentialed requests per chunk (initialize,
    initialized, tools/call, session DELETE), and every one used to interpolate
    the bearer into `-H "$AUTH"` — one chunked flush paraded the token through
    /proc/<pid>/cmdline ~117 times. All four must now read it from stdin."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = home / "bin"
    binp.mkdir(parents=True, exist_ok=True)
    curl = binp / "curl"
    curl.write_text(_MCP_CURL)
    curl.chmod(0o755)
    rec_dir.mkdir(exist_ok=True)
    res = _run_hook(
        MCPCALL,
        "",
        home,
        {
            "PATH": f"{binp}:{os.environ.get('PATH', '')}",
            "CURL_REC": str(rec_dir),
            "KB_MCP_URL": "http://127.0.0.1:9/mcp",
            "KB_TOKEN": FAKE_PROJECT_TOKEN,
        },
        args=("kb_stats", "{}"),
    )
    assert res.returncode == 0, res.stderr + res.stdout
    assert res.stdout.strip() == "queued 42", (
        f"the tool result did not survive the transport change: {res.stdout!r}"
    )
    argvs = _recorded(rec_dir, "argv")
    stdins = _recorded(rec_dir, "stdin")
    assert len(argvs) == 4, f"expected the full 4-request MCP conversation, got {len(argvs)}"
    for n, (argv, stdin) in enumerate(zip(argvs, stdins, strict=True), start=1):
        assert FAKE_PROJECT_TOKEN not in argv, (
            f"request {n} put the bearer on the curl command line: {argv!r}"
        )
        assert "Authorization" not in argv, f"request {n} builds an auth header in argv: {argv!r}"
        assert FAKE_PROJECT_TOKEN in stdin, (
            f"request {n} sent no bearer at all — it is unauthenticated"
        )


# ---------------------------------------------------------------------------
# Behavioural + structural: the dispatcher's per-child timeout and event argv
# ---------------------------------------------------------------------------
#
# Review #3 wave D. (a) The children run SEQUENTIALLY, so before the per-child
# bound one wedged child owned the whole hook slot: a hung turn-start spent the
# entire UserPromptSubmit budget and the recall behind it never ran. (b) The
# children used to re-derive the event name from the stdin JSON — and the one
# case the dispatcher exists for (an EMPTY stdin) is exactly the case where
# stdin says nothing, while the dispatcher's own argv always carries it.


@pytest.mark.parametrize("event", sorted(_DISPATCH_MAP))
def test_every_child_receives_the_event_name_as_its_first_argument(tmp_path, event: str):
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(tmp_path, event, {"hook_event_name": event}, home)
    assert res.returncode == 0, res.stderr
    for tag in _DISPATCH_MAP[event]:
        args = (rec / f"args.{tag}").read_text().splitlines()
        assert args and args[0] == event, (
            f"{tag} received argv {args!r} for {event} — the event name must arrive as $1"
        )


def test_the_event_name_reaches_the_children_even_when_stdin_is_empty(tmp_path):
    """The payoff case: the residual Windows bug hands the hook an empty stream,
    and a child that needed stdin for the event name got NOTHING. Our argv still
    knows the event, and it must arrive."""
    home = _hook_home(tmp_path)
    res, rec = _run_dispatch(tmp_path, "UserPromptSubmit", "", home)
    assert res.returncode == 0, res.stderr
    for tag in ("TURN", "BRIEF"):
        args = (rec / f"args.{tag}").read_text().splitlines()
        assert args and args[0] == "UserPromptSubmit", (
            f"{tag} lost the event name exactly when stdin was empty: {args!r}"
        )


_HUNG_CHILD = """#!/usr/bin/env bash
# A child that wedges: records that it ran, then blocks far past its budget.
# Its stdio is pointed at /dev/null BEFORE the block so the orphaned sleep
# cannot hold the dispatcher's stdout pipe open after the child is killed.
D="$KB_DISPATCH_REC"
mkdir -p "$D"
printf 'HUNG\\n' >>"$D/order"
exec >/dev/null 2>&1 </dev/null
sleep 300
"""


def test_a_hung_child_is_killed_at_its_budget_and_later_children_still_run(tmp_path):
    """turn-start's budget is 5 s; a child that sleeps 300 must cost ~5 s, not
    the whole slot, and the recall behind it must still run and still have its
    stdout injected."""
    home = _hook_home(tmp_path)
    hookdir = _dispatch_dir(tmp_path)
    hung = hookdir / "turn-start.sh"
    hung.write_text(_HUNG_CHILD)
    hung.chmod(0o755)
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir(exist_ok=True)
    t0 = time.monotonic()
    res = _run_hook(
        hookdir / "kb-hook-dispatch.sh",
        {"hook_event_name": "UserPromptSubmit"},
        home,
        {"KB_DISPATCH_REC": str(rec_dir), "OUT_BRIEF": "[KB recall] survived\n"},
        args=("UserPromptSubmit",),
    )
    elapsed = time.monotonic() - t0
    assert res.returncode == 0, res.stderr
    assert _fired(rec_dir) == ["HUNG", "BRIEF"], (
        f"a hung first child swallowed the rest of the event: {_fired(rec_dir)!r}"
    )
    assert res.stdout == "[KB recall] survived\n", res.stdout
    assert elapsed < 60, (
        f"the hung child was not killed at its budget (the dispatch took {elapsed:.0f}s)"
    )


def _dispatcher_budgets() -> dict[str, dict[str, int]]:
    """{event: {child: seconds}} parsed from the dispatcher's own map, so the
    arithmetic below is over what ships, not over what this test remembers."""
    out: dict[str, dict[str, int]] = {}
    for m in re.finditer(r'^\s*([A-Za-z| ]+)\)\s*CHILDREN="([^"]+)"', DISPATCH.read_text(), re.M):
        entries: dict[str, int] = {}
        for tok in m.group(2).split():
            name, sep, budget = tok.partition(":")
            assert sep, f"dispatcher child {tok!r} carries no timeout budget"
            entries[name] = int(budget)
        for ev in m.group(1).split("|"):
            out[ev.strip()] = entries
    return out


def _registered_timeouts() -> dict[str, int]:
    """The per-event hook timeouts install.sh registers, read from the jq branch
    (the python branch is pinned byte-identical to it by rig case 20)."""
    found = dict(re.findall(r'kb_entry (\w+) "[^"]*" (\d+)', INSTALL.read_text()))
    assert set(found) == {"PreCompact", "SessionEnd", "SessionStart", "UserPromptSubmit", "Stop"}
    return {k: int(v) for k, v in found.items()}


def test_the_per_child_budgets_fit_inside_the_registered_hook_slots():
    """The budgets only help if the dispatcher finishes INSIDE its slot instead
    of being killed by it — so per event, the child budgets must SUM below the
    timeout install.sh registers. The flush events are the documented exception:
    that hook detaches its real work in under a second, and its oversized budget
    is a runaway net that must not cut off the 45 s token auto-mint."""
    budgets = _dispatcher_budgets()
    slots = _registered_timeouts()
    assert set(budgets) == set(slots), (
        f"dispatcher events {sorted(budgets)} != registered events {sorted(slots)}"
    )
    for ev, slot in slots.items():
        if ev in ("PreCompact", "SessionEnd"):
            assert budgets[ev] == {"flush-session-to-kb.sh": 300}, (
                f"{ev}: the flush exception changed shape: {budgets[ev]!r}"
            )
            continue
        total = sum(budgets[ev].values())
        assert total < slot, (
            f"{ev}: child budgets sum to {total}s, which does not fit under the "
            f"registered {slot}s slot — a hung child would eat the hook slot again"
        )
    text = DISPATCH.read_text()
    assert 'kb_timeout "$_kbb" "${BASH:-bash}" "$_kbs" "$EV"' in text, (
        "the fan-out no longer bounds each child AND forwards the event as $1"
    )


# ---------------------------------------------------------------------------
# Behavioural: credential files are 0600 from their FIRST byte (install umask)
# ---------------------------------------------------------------------------


def test_enrolment_writes_the_token_file_0600_at_creation_not_via_chmod(tmp_path):
    """The LOW-3 fix, recreated for kb-tokens.env (review #3 wave D). The token
    line travelled through a DEFAULT-UMASK temp (`grep > .t`, then mv): 0644
    with the credential inside until a later chmod, and permanently 0644 if the
    installer died in between. mv PRESERVES the temp's mode, so the fix must be
    at CREATION — proven here by running the installer under umask 022 with
    chmod turned into a no-op: the mode must hold by construction."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    (binp / "chmod").write_text("#!/usr/bin/env bash\nexit 0\n")
    (binp / "chmod").chmod(0o755)
    wrapper = tmp_path / "umask022-install.sh"
    wrapper.write_text(f'#!/usr/bin/env bash\numask 022\nexec bash "{INSTALL}"\n')
    reply = json.dumps(
        {"token": FAKE_REDEEMED, "project": "argvproj", "principal": "proj-argvproj--box"}
    )
    res = _run_hook(
        wrapper,
        {},
        home,
        {
            "PATH": f"{binp}:{os.environ.get('PATH', '')}",
            "CURL_REC": str(rec_dir),
            "KB_REST_URL": "http://127.0.0.1:9",
            "KB_ENROL_CODE": FAKE_ENROL_CODE,
            "CURL_ENROL_REPLY": reply,
        },
    )
    assert res.returncode == 0, res.stderr
    tokens = home / ".claude" / "kb-tokens.env"
    assert tokens.exists(), res.stdout + res.stderr
    assert f"KB_TOKEN_ARGVPROJ={FAKE_REDEEMED}\n" in tokens.read_text()
    assert (tokens.stat().st_mode & 0o777) == 0o600, (
        f"kb-tokens.env is {oct(tokens.stat().st_mode & 0o777)} without chmod's help — "
        "the credential was world-readable at some point of its life"
    )
    cfg = home / ".claude" / "kb-flush.env"
    assert (cfg.stat().st_mode & 0o777) == 0o600, (
        f"kb-flush.env is {oct(cfg.stat().st_mode & 0o777)} without chmod's help"
    )


# ---------------------------------------------------------------------------
# Behavioural: the failure notifier redacts the journal excerpt it ships
# ---------------------------------------------------------------------------


def test_the_failure_notifier_redacts_credentials_from_the_journal_excerpt(tmp_path):
    """kb-notify-fail.sh ships two raw journald lines to an external ntfy server,
    and journald keeps credentials in the clear — log lines are where an ntfy
    admin password already leaked once. A planted kbg_ token (bare AND as a
    Bearer header) must leave the box as ***; the notifier's own credential must
    still travel on stdin, never argv."""
    home = _hook_home(tmp_path)
    rec_dir = tmp_path / "rec"
    binp = _recording_curl(home, rec_dir)
    (binp / "journalctl").write_text(
        "#!/usr/bin/env bash\n"
        f"printf 'auth retry with Authorization: Bearer {FAKE_PROJECT_TOKEN}\\n'\n"
        f"printf 'flush refused for token {FAKE_PROJECT_TOKEN}\\n'\n"
    )
    (binp / "journalctl").chmod(0o755)
    (binp / "systemctl").write_text("#!/usr/bin/env bash\necho exit-code\n")
    (binp / "systemctl").chmod(0o755)
    (home / ".claude" / "notify.env").write_text(
        "NTFY_URL=http://127.0.0.1:9\nNTFY_TOPIC=some-topic\nNTFY_MACHINE=testbox\n"
        f"NTFY_TOKEN={FAKE_NTFY_TOKEN}\n"
    )
    res = _run_hook(
        NOTIFY_FAIL,
        "",
        home,
        {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec_dir)},
        args=("kb-consolidate.service",),
    )
    assert res.returncode == 0, res.stderr
    argvs = _recorded(rec_dir, "argv")
    assert argvs, "the notifier never invoked curl"
    push = argvs[0]
    assert FAKE_PROJECT_TOKEN not in push, (
        f"the planted credential left the box in the push body: {push!r}"
    )
    assert "***" in push, f"nothing was redacted: {push!r}"
    assert "Bearer ***" in push, f"the Bearer value survived: {push!r}"
    assert FAKE_NTFY_TOKEN not in push, "the notifier's own credential reached argv"
    assert any(FAKE_NTFY_TOKEN in s for s in _recorded(rec_dir, "stdin")), (
        "the push is now unauthenticated"
    )


# ---------------------------------------------------------------------------
# Structural: every function that mints a plaintext secret is REGISTERED for
# scrubbing
# ---------------------------------------------------------------------------
#
# CLAUDE.md rule 12: every secret this system issues must be matched by a scrub
# pattern so it cannot survive ingest — and kbg_, kbe_ and tk_ were each missed
# IN TURN because kbcore.scrub.MINTED_SECRETS was tied to nothing but its
# author's memory. The convention now (review #3 wave D): a minter's docstring
# carries the marker sentence, this test greps kbcore for it, and a new minter
# that writes the marker but skips the registry fails the build.

_MINT_MARKER = "returns the plaintext secret exactly once"


def _marked_minters() -> set[str]:
    """Every kbcore function whose DOCSTRING carries the marker, by qualified
    name — resolved with ast so a mention in a comment or a nearby string can
    never count. Whitespace-normalised before matching, so the sentence may
    wrap across docstring lines."""
    import ast

    src_root = REPO / "src"
    marked: set[str] = set()
    for path in sorted((src_root / "kbcore").rglob("*.py")):
        module = ".".join(path.relative_to(src_root).with_suffix("").parts)
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = " ".join((ast.get_docstring(node) or "").split())
                if _MINT_MARKER in doc.lower():
                    marked.add(f"{module}.{node.name}")
    return marked


def test_every_marked_minter_is_registered_in_MINTED_SECRETS():
    from kbcore.scrub import MINTED_SECRETS

    marked = _marked_minters()
    # The floor keeps the tripwire from going vacuous: these four really do
    # return a plaintext credential today, so a lost marker is a red test too.
    # (store_drive_token RECEIVES its secret and returns nothing, so it is
    # registered without the marker — see the note beside MINTED_SECRETS.)
    floor = {
        "kbcore.principals.create_principal",
        "kbcore.project_tokens.issue",
        "kbcore.enrolment.mint_code",
        "kbcore.offsite_config.generate_age_keypair",
    }
    assert floor <= marked, f"minters lost their docstring marker: {sorted(floor - marked)}"
    unregistered = sorted(marked - set(MINTED_SECRETS))
    assert not unregistered, (
        f"functions marked as minting a plaintext secret are MISSING from "
        f"kbcore.scrub.MINTED_SECRETS: {unregistered} — their credential shape "
        "would survive ingest unredacted"
    )


# ---------------------------------------------------------------------------
# Wave C stage 3 pins (root cause KB 33783, proposal KB 33784, wave S KB 34161,
# transport KB 1195): the never-delete lineage, the shared lock / first-record
# blocks, the transport line, the flush_* telemetry body, the install stamp
# order and the Stop dispatcher map — each checked on the canonical contrib
# copy AND the served setup_assets copy where the pin is about what ships.
# ---------------------------------------------------------------------------

_LINEAGE_MARKER = "# KB-FLUSH-LINEAGE: never-delete-offsets v2"
# Every served script that invokes curl — DERIVED from what ships (currently
# install, notify, brief, flush, kb-mcp-call and update.sh), so a new served
# script cannot dodge the KB 1195 transport pins by not being on a list.
_CURL_SERVED = sorted(p.name for p in SHIPPED.glob("*.sh") if "curl" in p.read_text())
_EIGHT_STAMPS = (
    "flush_offset",
    "flush_bytes",
    "flush_size",
    "flush_event",
    "flush_mtime",
    "flush_box",
    "flush_project_by",
    "flush_branch_of",
)


def _both_copies(name: str) -> list[Path]:
    """The canonical contrib copy and the served setup_assets copy of one script."""
    return [CONTRIB / name, SHIPPED / name]


def _copy_id(p: Path) -> str:
    return f"{p.parent.name}/{p.name}"


@pytest.mark.parametrize("path", _both_copies("flush-session-to-kb.sh"), ids=_copy_id)
def test_line_2_of_the_flush_hook_is_the_never_delete_lineage_marker(path: Path):
    """KB 33783: every resume re-sent whole transcripts because the old hook
    DELETED the checkpoint after a drained SessionEnd. The sweep's tripwire
    (kb-flush-backfill.sh bf_lineage_ok) reads exactly line 2 of the hook it is
    about to exec and refuses to feed a hook without this marker."""
    assert path.read_text().splitlines()[1] == _LINEAGE_MARKER, (
        f"{_copy_id(path)}: line 2 is not the lineage marker the sweep's tripwire greps for"
    )


def _command_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


# `rm` in command position: line start, after a separator, after a keyword on
# the same line (`then rm`, `do rm`, `else rm`), by path (`/bin/rm`) or through
# `command rm` — and the offset file spelled `"$OFFF"` or `"${OFFF}"`.
_RM_COMMAND = re.compile(r"(^|[;&|({]\s*|\b(?:then|do|else)\s+|/|\bcommand\s+)rm\s")
_OFFF_ARG = re.compile(r'\$\{?OFFF\}?"')


@pytest.mark.parametrize(
    "path",
    _both_copies("flush-session-to-kb.sh") + _both_copies("kb-flush-backfill.sh"),
    ids=_copy_id,
)
def test_no_script_ever_removes_the_offset_file(path: Path):
    """The writer invariant (proposal_v2 Appendix B): value(t+1) is +accepted,
    0 (shrank, logged), a seed, or a logged CONNECTOR skip — never absent. An
    `rm` of "$OFFF" anywhere (either bearer arm, the connector arm, the sweep)
    is the delete lineage coming back; only `$OFFF.t` (the atomic-write temp)
    may be removed."""
    offenders = [
        ln.strip()
        for ln in _command_lines(path.read_text())
        if _RM_COMMAND.search(ln.strip()) and _OFFF_ARG.search(ln)
    ]
    assert not offenders, f"{_copy_id(path)} removes the offset file: {offenders}"


def _named_block(path: Path, name: str) -> str:
    text = path.read_text()
    start = text.index(f"# >>> KB {name}")
    end = text.index(f"# <<< KB {name}")
    return text[start : end + len(f"# <<< KB {name}")]


@pytest.mark.parametrize("name", ["LOCK TRIO v1", "FIRST RECORD v1"])
def test_the_lock_trio_and_first_record_blocks_are_byte_identical_everywhere(name: str):
    """ONE function set (kb_lock_take/touch/owned/release; kb_first_record_field)
    edited in one place and copied verbatim: the hook and the sweep must take the
    same lock the same way and date a session from the same bytes. Checked
    across the hook and the backfill, in contrib AND the shipped copies."""
    copies = _both_copies("flush-session-to-kb.sh") + _both_copies("kb-flush-backfill.sh")
    blocks = {_copy_id(p): _named_block(p, name) for p in copies}
    reference = blocks[_copy_id(CONTRIB / "flush-session-to-kb.sh")]
    assert reference.strip(), f"the {name} block is empty in the contrib flush hook"
    drifted = [k for k, v in blocks.items() if v != reference]
    assert not drifted, f"the {name} block drifted in {drifted} — edit it in ONE place and copy it"


def _snap_fixture(tmp_path) -> tuple[Path, int, int]:
    """20 short lines, then ONE 1.3 MB record: a last newline that lies more
    than a 1 MB snap window before most of the file."""
    head = b"".join(b'{"type":"user","i":%d,"text":"shared line %d"}\n' % (i, i) for i in range(20))
    giant = b'{"type":"assistant","i":20,"text":"' + b"G" * 1300000 + b'"}\n'
    f = tmp_path / "snap.jsonl"
    f.write_bytes(head + giant)
    return f, len(head), len(head) + len(giant)


def _run_snaps(script: str, offsets: list[int]) -> list[str]:
    res = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert res.returncode == 0, res.stderr
    out = res.stdout.split()
    assert len(out) == len(offsets), (offsets, res.stdout, res.stderr)
    return out


def test_the_hook_seed_snap_and_the_backfill_seed_snap_are_the_same_arithmetic(tmp_path):
    """Wave C stage 3 A2 (KB 33784): kb_snap_down (the hook's branch seed) is a
    hand-copied twin of bf_snap (the owner's --seed) with different temp-file
    plumbing, so the byte-identity pin used for the LOCK TRIO cannot cover it.
    RUN both, source-extracted, on a >1 MB newline-less tail at every offset
    class — 0, 1, mid-line, exactly / one past the 1 MB window, deep inside the
    giant record, EOF — and they must agree byte for byte, with every offset
    inside the giant record snapping BACK to the end of line 20 (never to
    `offset - 983040`, the uutils `tail -n1` misread the old code made)."""
    f, head_len, size = _snap_fixture(tmp_path)
    offsets = [0, 1, head_len + 5, 1048576, 1048577, head_len + 1200000, size - 1, size]
    calls = " ".join(f"{o}" for o in offsets)
    hook_script = "\n".join(
        [
            _shell_fn(FLUSH, "kb_snap_down"),
            f'for o in {calls}; do kb_snap_down "{f}" "$o"; printf " "; done',
        ]
    )
    bf_script = "\n".join(
        [
            _shell_fn(BACKFILL, "bf_size"),
            _shell_fn(BACKFILL, "bf_snap"),
            "BF_TMP=$(mktemp -d); trap 'rm -rf \"$BF_TMP\"' EXIT",
            f'for o in {calls}; do bf_snap "{f}" "$o"; printf " "; done',
        ]
    )
    hook_out = _run_snaps(hook_script, offsets)
    bf_out = _run_snaps(bf_script, offsets)
    assert hook_out == bf_out, f"the two snaps disagree: hook={hook_out} backfill={bf_out}"
    expected = [0, 0, head_len, head_len, head_len, head_len, head_len, size]
    assert [int(x) for x in hook_out] == expected, (
        f"snap results {hook_out} for offsets {offsets}; "
        f"expected {expected} (head={head_len}, size={size})"
    )


def _logical_lines(text: str) -> str:
    """The script with backslash-newline continuations joined, so a curl that
    spans four physical lines is examined as the one command it is."""
    return re.sub(r"\\\n\s*", " ", text)


def _executed_curl_commands(text: str) -> list[str]:
    return _executed_curl_lines(_logical_lines(text))


def test_the_served_curl_set_is_what_this_file_expects():
    """The derived set must at least cover the five scripts the brief named plus
    update.sh; if it shrinks, a script stopped shipping or stopped using curl."""
    assert {
        "install.sh",
        "notify-complete.sh",
        "kb-session-brief.sh",
        "flush-session-to-kb.sh",
        "kb-mcp-call.sh",
        "update.sh",
    } <= set(_CURL_SERVED), _CURL_SERVED


@pytest.mark.parametrize("name", _CURL_SERVED)
def test_no_executed_curl_in_the_served_scripts_carries_a_connect_timeout(name: str):
    """KB 1195: on Git Bash/Schannel `--connect-timeout` aborts healthy slow
    requests (the TLS handshake counts against it), so every bound is a
    `--max-time`. Executed invocations only, continuations joined, both copies,
    over EVERY served script that invokes curl (derived, not listed)."""
    for path in _both_copies(name):
        offenders = [
            c for c in _executed_curl_commands(path.read_text()) if "--connect-timeout" in c
        ]
        assert not offenders, f"{_copy_id(path)} carries --connect-timeout: {offenders}"


@pytest.mark.parametrize("path", _both_copies("flush-session-to-kb.sh"), ids=_copy_id)
def test_the_ingest_curl_line_is_the_appendix_G_transport_line(path: Path):
    """The ONE REST submission (kb_rest_post, proposal_v2 Appendix G): bounded per
    attempt (`--max-time`), a short flap cover (`--retry` + `--retry-max-time`),
    and NEVER `--fail` — a 401/403 must surface as the code, not be retried.
    Safe only because /api/ingest-session dedupes; that is why it is one line."""
    fn = _logical_lines(_shell_fn(path, "kb_rest_post"))
    cmds = [c for c in _executed_curl_lines(fn) if "/api/ingest-session" in c]
    assert len(cmds) == 1, f"{_copy_id(path)}: expected one ingest curl, found {cmds}"
    cmd = cmds[0]
    for flag in ("--max-time", "--retry ", "--retry-max-time"):
        assert flag in cmd, f"{_copy_id(path)}: the ingest curl lost {flag.strip()}: {cmd}"
    assert "--connect-timeout" not in cmd, (
        f"{_copy_id(path)}: --connect-timeout on the ingest curl (KB 1195)"
    )
    assert not re.search(r"(^|\s)(--fail|-f|-fsS|-sSf)(\s|$)", cmd), (
        f"{_copy_id(path)}: --fail on the ingest curl would retry a 401/403: {cmd}"
    )


def _stamp_lines(text: str) -> list[str]:
    """The STAMPS construction, lifted VERBATIM from the bearer child."""
    lines = [
        ln.strip() for ln in text.splitlines() if re.match(r'\s*(\[ -n "\$\w+" \] && )?STAMPS=', ln)
    ]
    assert len(lines) == 5, f"the STAMPS construction changed shape: {lines}"
    return lines


def _rest_body_build_lines(text: str) -> list[str]:
    lines = [ln.strip() for ln in text.splitlines() if 'kb_json_build "$BODYF"' in ln]
    assert len(lines) == 2, f"expected the gz and the plaintext REST body builds, found {lines}"
    return lines


@pytest.mark.parametrize("backend", ["jq", "python3"])
def test_the_rest_body_carries_all_eight_flush_stamps(tmp_path, backend: str):
    """Wave S accepts eight `flush_*` telemetry stamps (src/kbrest/schemas.py
    IngestSessionRequest); the hook builds them as extra kb_json_build triples
    (numbers as `n`, strings as `s`) on the REST body only. RUN, not read: the
    STAMPS lines and the body-build line are lifted from the source and executed
    through the hook's own kb_json_build under EACH JSON backend, and the eight
    keys must land with their types intact."""
    jq = shutil.which("jq")
    if backend == "jq" and jq is None:
        pytest.skip("jq is not installed")
    text = FLUSH.read_text()
    gz_line, plain_line = _rest_body_build_lines(text)
    assert "transcript_gz f" in gz_line and "transcript f" in plain_line, (gz_line, plain_line)
    for ln in (gz_line, plain_line):
        assert ln.endswith('"$@" $STAMPS'), (
            f"the REST body build no longer appends the stamps: {ln}"
        )
    chunk = tmp_path / "chunk"
    chunk.write_text('{"type":"user","text":"x"}\n', encoding="utf-8")
    body = tmp_path / "body.json"
    script = "\n".join(
        [
            _portable_block(FLUSH),
            "KB_JSON_PROBED=1",
            f'KB_JQ_BIN="{jq if backend == "jq" else ""}"',
            f'KB_PY_BIN="{"" if backend == "jq" else "python3"}"',
            "PROJECT=proj OFFSET=1400000 BYTES=700000 SIZE=2100000 MTIME=1757500000",
            "EV=Backfill BOX=box-1 PROJECT_BY=gitfirst BRANCH_OF=abcd1234",
            f'TMPC="{chunk}" BODYF="{body}"',
            "set --",
            *_stamp_lines(text),
            plain_line,
        ]
    )
    res = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    doc = json.loads(body.read_text(encoding="utf-8"))
    assert {k: doc.get(k) for k in _EIGHT_STAMPS} == {
        "flush_offset": 1400000,
        "flush_bytes": 700000,
        "flush_size": 2100000,
        "flush_event": "Backfill",
        "flush_mtime": 1757500000,
        "flush_box": "box-1",
        "flush_project_by": "gitfirst",
        "flush_branch_of": "abcd1234",
    }, f"[{backend}] the REST body's flush_* stamps are wrong or missing: {doc}"
    assert (
        doc["project"] == "proj" and doc["kind"] == "session" and doc["transcript"].startswith("{")
    )


def test_install_writes_the_trust_stamp_before_any_hook_is_installed():
    """The stamp `.checkpoints-trusted-since` is what lets the hook tell a
    genuinely new session (send from 0) from one an older, offset-deleting hook
    already captured (refuse as unseeded). If a hook could run before the stamp
    existed it would create the stamp as `now` itself and refuse every session
    the install was meant to trust — so the write precedes every `install -m`."""
    text = INSTALL.read_text()
    stamp = text.index('STAMP="$STATE/.checkpoints-trusted-since"')
    write = text.index('>"$STAMP"')
    first_install = min(
        m.start() for m in re.finditer(r"^\s*(\[ -f [^\]]*\] && )?install -m ", text, re.M)
    )
    assert stamp < write < first_install, (
        "install.sh installs a hook before it has written .checkpoints-trusted-since"
    )
    assert "set -C" in text[stamp:write], "the stamp write is no longer create-only (noclobber)"


def test_the_stop_dispatcher_map_runs_the_flush_gate_then_notify():
    """Wave C (C5): on Stop the flush hook runs FIRST under a 3 s budget (its
    gate is two stats and a few cats; a closed gate costs nothing), then notify
    reads the freshest offset under 11 s — 14 s inside the registered 15 s slot.
    The map is read from the dispatcher that ships, not restated."""
    stop = _dispatcher_budgets()["Stop"]
    assert list(stop.items()) == [("flush-session-to-kb.sh", 3), ("notify-complete.sh", 11)], (
        f"the Stop dispatcher map changed: {stop}"
    )
