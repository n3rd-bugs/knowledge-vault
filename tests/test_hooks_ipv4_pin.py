"""The IPv4 pin at the shared curl layer (review #6, hooks package).

On this network the AAAA record for the KB host resolves first and IPv6 egress
fails. Only the ntfy push pinned ``-4``, so during an AAAA-only transient every
other hook curl died: flushes failed (recovered later by the backfill), briefs
skipped silently at their reachability probe, and an enrolment could strand a
brand-new box with no credential. The fix lives at the ONE layer every
credentialed request already flows through — the curl-config emitters that feed
``curl -K -`` — so every consumer (mcp-call, brief, flush, install/enrol)
inherits the pin from a single edit, and the two bare reachability probes carry
``-4`` so the gate tests the same address family as the call it gates.

Structural tests derive the guarded set mechanically (every EXECUTED curl must
be family-pinned, via config or flag) so the class is guarded, not the found
instances. Behavioural tests run the real emitter functions and the real flush
script against the local recording stub.

The reship rider is covered here too: the flush hook stamps ``hook_version``
(the 12-hex manifest digest install.sh recorded in ``.setup-version``) into the
REST body only — the MCP tool signature is closed and would error on an unknown
argument — and the value is shape-checked, so junk in the stamp file never
reaches the wire. Absence is meaningful (old code / unverified install).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.test_capture_funnel import (
    BRIEF,
    FAKE_ENROL_CODE,
    FAKE_PROJECT_TOKEN,
    FLUSH,
    INSTALL,
    MCPCALL,
    NOTIFY,
    _executed_curl_lines,
    _hook_home,
    _Recorder,
    _run_hook,
    _transcript,
    requires_curl,
)

REPO = Path(__file__).resolve().parents[1]
CONTRIB = REPO / "contrib" / "claude" / "kb-session-flush"

# (script, emitter function) — every named curl-config source in the assets.
EMITTERS = [
    (MCPCALL, "kb_curl_auth_config"),
    (FLUSH, "kb_curl_auth_config"),
    (BRIEF, "kb_curl_auth_config"),
    (INSTALL, "setup_auth_config"),
]


def _function_block(text: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?^\}}", text, re.M | re.S)
    assert m, f"{name}() not found"
    return m.group(0)


def _invoked_curl_lines(text: str) -> list[str]:
    """`_executed_curl_lines` minus the lines that only MENTION curl (an emitter
    definition's trailing comment, a `command -v curl` probe): a real invocation
    always passes at least one flag, so require `curl -`."""
    # A `curl --help …` / `curl --version` capability probe (wave C: the
    # --retry-all-errors probe in the flush child) opens no connection and so
    # needs no address-family pin.
    return [
        ln
        for ln in _executed_curl_lines(text)
        if re.search(r"curl\s+-", ln) and not re.search(r"curl\s+--(help|version)\b", ln)
    ]


# ---------------------------------------------------------------------------
# Structural: the pin is in the shared layer, unconditionally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "func"), EMITTERS, ids=lambda v: getattr(v, "name", v))
def test_every_config_emitter_pins_ipv4_before_the_bearer_guard(path: Path, func: str):
    """`ipv4` must be the FIRST thing the config says, ahead of the empty-bearer
    early return — an unauthenticated request should fail as a loud 401 over
    IPv4, never as a doomed IPv6 connect that looks like a network fault."""
    block = _function_block(path.read_text(), func)
    pin = block.find("printf 'ipv4\\n'")
    guard = block.find("|| return 0")
    assert pin != -1, f"{path.name}:{func}() no longer emits the ipv4 pin"
    assert guard != -1, f"{path.name}:{func}() lost its empty-bearer guard"
    assert pin < guard, (
        f"{path.name}:{func}() emits ipv4 only when a bearer exists — an "
        "unauthenticated call would silently lose the pin"
    )


def test_the_inline_enrolment_configs_carry_the_same_pin():
    """The enrol path builds its config inline (the code is a credential that
    exists before any emitter function does), so each of those printfs must
    carry the pin itself — pre-manifest, pre-notify, and the redeem POST."""
    text = INSTALL.read_text()
    shape = 'printf \'ipv4\\nheader = "Authorization: Bearer %s"\\n\' "$KB_ENROL_CODE"'
    inline = text.count(shape)
    assert inline == 3, (
        f"expected the 3 inline enrolment configs to pin ipv4, found {inline} — "
        "an unpinned enrol dies on the doomed IPv6 connect and the box comes up "
        "with no credential at all"
    )


@pytest.mark.parametrize(
    "path",
    sorted(CONTRIB.glob("*.sh"), key=lambda p: p.name),
    ids=lambda p: p.name,
)
def test_every_executed_curl_is_family_pinned(path: Path):
    """The class guard: every curl these scripts EXECUTE either reads a config
    from stdin (`-K -`, whose emitters the tests above pin) or carries an
    explicit `-4`. A future bare curl cannot silently reintroduce the
    AAAA-transient failure mode."""
    offenders = [
        ln
        for ln in _invoked_curl_lines(path.read_text())
        if "-K -" not in ln and not re.search(r"(^|\s)-4(\s|$)", ln)
    ]
    assert not offenders, f"{path.name} runs curl with no address-family pin: {offenders}"


def test_the_ntfy_push_keeps_its_explicit_ipv4_flag():
    """`verify notify-complete.sh's existing -4 stays consistent` — the push
    predates the shared layer and pins on the command line; it must not lose
    that while the config layer takes over everywhere else."""
    curls = _invoked_curl_lines(NOTIFY.read_text())
    assert curls, "the notify hook no longer invokes curl at all"
    assert all(re.search(r"(^|\s)-4(\s|$)", ln) for ln in curls), (
        f"the ntfy push lost its -4 pin: {curls}"
    )


def test_the_brief_reachability_probes_pin_the_same_family_as_the_calls_they_gate():
    """A probe on the wrong address family is a false gate: it was exactly the
    'briefs skip silently' half of the transient. Both bare probes (MCP and
    REST) must carry -4 to match the pinned real call behind them."""
    probes = [
        ln
        for ln in _invoked_curl_lines(BRIEF.read_text())
        # Wave C (KB 1195): no hook curl carries --connect-timeout; the probes
        # are the two bare `--max-time 3` GETs against the transport URLs.
        if re.search(r"--max-time\s+\d+", ln) and "-K -" not in ln
    ]
    assert len(probes) == 2, f"expected the two reachability probes, found: {probes}"
    for ln in probes:
        assert re.search(r"(^|\s)-4(\s|$)", ln), f"unpinned reachability probe: {ln}"


# ---------------------------------------------------------------------------
# Behavioural: the emitters really emit the pin (and stay valid curl config)
# ---------------------------------------------------------------------------


def _emit(path: Path, func: str, env: dict[str, str]) -> list[str]:
    block = _function_block(path.read_text(), func)
    res = subprocess.run(
        ["bash", "-c", f"{block}\n{func}\n"],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.splitlines()


@pytest.mark.parametrize(("path", "func"), EMITTERS, ids=lambda v: getattr(v, "name", v))
def test_the_emitted_config_pins_first_and_authenticates_second(path: Path, func: str):
    bearer = FAKE_ENROL_CODE if func == "setup_auth_config" else FAKE_PROJECT_TOKEN
    var = "KB_ENROL_CODE" if func == "setup_auth_config" else "KB_TOKEN"
    assert _emit(path, func, {var: bearer}) == [
        "ipv4",
        f'header = "Authorization: Bearer {bearer}"',
    ]
    # No bearer: the pin alone — the request still goes out and fails LOUDLY
    # (401) instead of being skipped or dying on an IPv6 connect.
    assert _emit(path, func, {}) == ["ipv4"]


@requires_curl
@pytest.mark.parametrize(("path", "func"), EMITTERS, ids=lambda v: getattr(v, "name", v))
def test_real_curl_accepts_the_pinned_config(path: Path, func: str):
    """`ipv4` must be a keyword curl's config parser actually knows — a typo'd
    keyword is only a warning to some curls and a hard error to others, and
    either way it is this test's job to notice, not a fleet's."""
    bearer = FAKE_ENROL_CODE if func == "setup_auth_config" else FAKE_PROJECT_TOKEN
    var = "KB_ENROL_CODE" if func == "setup_auth_config" else "KB_TOKEN"
    cfg = "\n".join(_emit(path, func, {var: bearer})) + "\n"
    res = subprocess.run(
        ["curl", "-q", "-K", "-", "-sS", "-o", os.devnull, "file:///dev/null"],
        input=cfg,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"curl rejected the emitted config: {res.stderr}"


# ---------------------------------------------------------------------------
# The reship rider: hook_version in the flush payload
# ---------------------------------------------------------------------------

STAMP = "abc123def456"  # 12-hex, the manifest-version shape install.sh records


def test_hook_version_rides_only_the_rest_body():
    """The REST request model ignores unknown fields (harmless today, fleet
    data once the server learns the field); the MCP tool signature is CLOSED —
    an unknown argument errors the call — so the stamp must never reach the
    MCP arm, and the child that sends it must actually receive HOOKVER."""
    text = FLUSH.read_text()
    append = (
        '[ -z "$KB_MCP_URL" ] && [ -n "${HOOKVER:-}" ] && set -- "$@" hook_version s "$HOOKVER"'
    )
    assert append in text, (
        "the REST-only hook_version append is gone (or no longer gated on the transport)"
    )
    mcp_builds = [ln for ln in text.splitlines() if "kb_json_build" in ln and "$TMPC.args" in ln]
    assert mcp_builds, "the MCP arg builders moved — update this guard"
    assert all("hook_version" not in ln for ln in mcp_builds), (
        "hook_version leaked into the MCP tool arguments — current servers "
        "reject unknown arguments, which would break every MCP-mode flush"
    )
    assert re.search(r"^\s*export .*\bHOOKVER\b", text, re.M), (
        "HOOKVER is read inside the detached child but never exported into it"
    )


@requires_curl
def test_a_rest_flush_carries_the_stamped_hook_version(tmp_path):
    home = _hook_home(tmp_path)
    (home / ".claude" / "hooks" / ".setup-version").write_text(STAMP + "\n")
    work = tmp_path / "verproj"
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
                "session_id": "ver-session",
                "hook_event_name": "SessionEnd",
            },
            home,
        )
        assert res.returncode == 0, res.stderr
        assert rec.wait(1), "no submission reached the stub; flush log: " + (
            home / ".claude/hooks/flush-session.log"
        ).read_text(errors="replace")
        payload = rec.requests[0]["payload"]
    assert payload.get("hook_version") == STAMP
    # The stamp is context, not identity — it must not displace the fields the
    # server already keys on.
    assert payload.get("session_uri") == "session:ver-session"
    assert payload.get("project") == "verproj"


@requires_curl
@pytest.mark.parametrize(
    "stamp",
    [
        None,  # old code / unverified install: no stamp file at all
        "v2.1 (local)",  # freeform junk — not a manifest digest
        "abc123def45",  # 11 hex: shape-checked, LENGTH included
    ],
    ids=["missing", "junk", "short"],
)
def test_a_missing_or_malformed_stamp_sends_no_hook_version(tmp_path, stamp):
    """Absence is the honest signal (old code and unverified installs never
    stamp), and the stamp file is disk-controlled data — anything that is not a
    12-hex digest stays off the wire entirely, never as an empty field."""
    home = _hook_home(tmp_path)
    if stamp is not None:
        (home / ".claude" / "hooks" / ".setup-version").write_text(stamp + "\n")
    work = tmp_path / "nostamp"
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
                "session_id": "nostamp-session",
                "hook_event_name": "SessionEnd",
            },
            home,
        )
        assert res.returncode == 0, res.stderr
        assert rec.wait(1), "no submission reached the stub; flush log: " + (
            home / ".claude/hooks/flush-session.log"
        ).read_text(errors="replace")
        payload = rec.requests[0]["payload"]
    assert "hook_version" not in payload, (
        f"a non-digest stamp {stamp!r} reached the wire as {payload.get('hook_version')!r}"
    )
