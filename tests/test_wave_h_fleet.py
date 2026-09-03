"""Wave H pkg-fleet (review #8 H11 + H15 tail): the fleet-facing pieces.

* the recall hook's ENVELOPE GATE — machine-shaped prompts (<task-notification>,
  <system-reminder>) never reach a search (structural, and really executed);
* ``update.sh`` — the manifest-driven self-update asset, really executed against a
  recording ``curl`` stand-in (fetch, verify, hand over; any failure leaves the box
  exactly as it was);
* ``fleet_by_box`` gains the per-box ``hook_version`` column (real DB, kb_test);
* docker-compose: journald on every service on the public path;
* every shipped script parses (``bash -n``).

Hermetic: throwaway HOMEs, a fake curl first on PATH, no network, no live KB.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
CONTRIB = REPO / "contrib" / "claude" / "kb-session-flush"
SHIPPED = REPO / "src" / "kbmcp" / "setup_assets"
BRIEF = CONTRIB / "kb-session-brief.sh"
INSTALL = CONTRIB / "install.sh"
UPDATE = CONTRIB / "update.sh"

# Fake, shaped like the real thing; never a live credential.
FAKE_TOKEN = "kbg_" + "a" * 43
FAKE_PROJECT_TOKEN = "kbg_" + "p" * 43

requires_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    assert str(home) != os.path.expanduser("~")
    (home / ".claude" / "hooks").mkdir(parents=True)
    return home


def _run(script: Path, home: Path, *, stdin: str = "", env: dict | None = None, args=()):
    e = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    e.update(env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        input=stdin,
        text=True,
        capture_output=True,
        env=e,
        timeout=90,
    )


# ============================================================================
# H11 — the envelope gate
# ============================================================================

_TRIPWIRE_CURL = """#!/usr/bin/env bash
# Any invocation is a network attempt the gate should have prevented.
mkdir -p "$CURL_REC"
printf '%s\\n' "$*" >>"$CURL_REC/argv"
exit 7
"""

ENVELOPES = [
    "<task-notification>\n<task-id>agent-7</task-id>\n<status>completed</status>\n"
    "<summary>the build is green and every test passes on the branch</summary>\n"
    "</task-notification>",
    "<system-reminder>the todo list changed and the file was modified since you read it"
    "</system-reminder>",
]
SUBSTANTIVE = "why does kb-mcp-call.sh drop the Mcp-Session-Id header on retry"


def _tripwire_box(tmp_path: Path) -> tuple[Path, dict]:
    home = _home(tmp_path)
    rec = tmp_path / "rec"
    binp = home / "bin"
    binp.mkdir()
    (binp / "curl").write_text(_TRIPWIRE_CURL)
    (binp / "curl").chmod(0o755)
    (home / ".claude" / "kb-flush.env").write_text(
        f"KB_REST_URL=http://127.0.0.1:9\nKB_TOKEN={FAKE_TOKEN}\n"
    )
    env = {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec)}
    return home, env


def _prompt_event(prompt: str, cwd: Path) -> str:
    return json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "cwd": str(cwd),
            "session_id": "h",
        }
    )


def test_the_envelope_gate_has_no_backslash_and_sits_before_the_length_gate():
    """The lenses render the prefix as '<\\task-notification>' (a JSON-escape
    artefact); a case pattern written that way matches nothing, which is how a
    'fix' could ship inert. Both twins are byte-identical (test_capture_funnel)."""
    lines = BRIEF.read_text().splitlines()
    gate = [
        i
        for i, ln in enumerate(lines)
        if ln.lstrip().startswith("case") and "task-notification" in ln
    ]
    assert len(gate) == 1, "exactly one envelope case-gate"
    ln = lines[gate[0]]
    assert "'<task-notification>'*" in ln and "'<system-reminder>'*" in ln
    assert "\\task" not in ln and "\\system" not in ln, "the pattern must be the REAL prefix"
    assert ln.rstrip().endswith("exit 0 ;; esac"), "an envelope exits silently, it is not searched"
    length_gate = next(i for i, line in enumerate(lines) if '[ "${#PROMPT}" -ge 12 ]' in line)
    assert gate[0] < length_gate, "before the length gate, so it can never search"


@requires_bash
@pytest.mark.parametrize("prompt", ENVELOPES, ids=["task-notification", "system-reminder"])
def test_an_envelope_prompt_prints_nothing_and_makes_no_call(tmp_path, prompt):
    home, env = _tripwire_box(tmp_path)
    work = tmp_path / "work" / "envelopeproj"
    work.mkdir(parents=True)

    res = _run(BRIEF, home, stdin=_prompt_event(prompt, work), env=env)

    assert res.returncode == 0, res.stderr
    assert res.stdout == "", f"an envelope injected context: {res.stdout!r}"
    argv = Path(env["CURL_REC"]) / "argv"
    assert not argv.exists(), f"an envelope reached the network: {argv.read_text()}"


@requires_bash
def test_the_tripwire_sees_a_substantive_prompt_try_the_network(tmp_path):
    """Control: the harness would catch a call, so the silence above is the gate."""
    home, env = _tripwire_box(tmp_path)
    work = tmp_path / "work" / "envelopeproj"
    work.mkdir(parents=True)

    res = _run(BRIEF, home, stdin=_prompt_event(SUBSTANTIVE, work), env=env)

    assert res.returncode == 0, res.stderr
    assert (Path(env["CURL_REC"]) / "argv").exists(), "a real prompt must at least probe the KB"


# ============================================================================
# H15 (3) — update.sh, really executed
# ============================================================================

_UPDATE_CURL = r"""#!/usr/bin/env bash
# curl stand-in for update.sh: serves the manifest from $CURL_MANIFEST and files
# from $CURL_FILES_DIR/<name>; records argv + stdin (the -K config). Never a network.
D="$CURL_REC"
mkdir -p "$D"
N=$(( $(cat "$D/.n" 2>/dev/null || echo 0) + 1 ))
printf '%s' "$N" >"$D/.n"
printf '%s\n' "$*" >"$D/argv.$N"
: >"$D/stdin.$N"
OUT=""
URL=""
prev=""
for a in "$@"; do
  [ "$prev" = "-o" ] && OUT="$a"
  case "$a" in http://*|https://*) URL="$a" ;; esac
  prev="$a"
done
for a in "$@"; do
  if [ "$a" = "-K" ]; then cat >"$D/stdin.$N"; break; fi
done
emit() { if [ -n "$OUT" ]; then cat >"$OUT"; else cat; fi; }
case "$URL" in
  *"/api/setup/manifest.json")
    [ -n "${CURL_MANIFEST:-}" ] || exit 22
    printf '%s' "$CURL_MANIFEST" | emit ;;
  *"/api/setup/"*)
    name="${URL##*/}"
    [ -f "$CURL_FILES_DIR/$name" ] || exit 22
    emit <"$CURL_FILES_DIR/$name" ;;
  *) exit 22 ;;
esac
exit 0
"""

# A stand-in installer: proves the hand-over happened (and with which env), and
# never touches ~/.claude/hooks — so any hook file appearing would be update.sh's.
STUB_INSTALL = r"""#!/usr/bin/env bash
printf 'stub installer ran url=%s bearer=%s\n' "${KB_REST_URL:-unset}" "${KB_SETUP_BEARER:+set}" \
  >"$HOME/installer.ran"
echo 'stub installer ran'
"""
STUB_BRIEF = "#!/usr/bin/env bash\necho brief\n"


def _manifest_for(digests: dict[str, str]) -> tuple[str, str]:
    """The manifest EXACTLY as kbrest derives it (compact JSON, content version)."""
    version = hashlib.sha256(
        "".join(f"{k}:{v}" for k, v in sorted(digests.items())).encode()
    ).hexdigest()[:12]
    body = json.dumps(
        {"version": version, "files": digests, "count": len(digests)}, separators=(",", ":")
    )
    return version, body


def _serve(tmp_path: Path, files: dict[str, str], *, tamper: dict[str, str] | None = None) -> dict:
    fdir = tmp_path / "served"
    fdir.mkdir(exist_ok=True)
    digests: dict[str, str] = {}
    for name, content in files.items():
        (fdir / name).write_text(content)
        digests[name] = hashlib.sha256(content.encode()).hexdigest()
    digests.update(tamper or {})
    version, manifest = _manifest_for(digests)
    return {"dir": fdir, "manifest": manifest, "version": version, "files": files}


def _update_box(tmp_path: Path, *, cfg: str) -> tuple[Path, dict, Path]:
    home = _home(tmp_path)
    rec = tmp_path / "rec"
    rec.mkdir()
    binp = home / "bin"
    binp.mkdir()
    (binp / "curl").write_text(_UPDATE_CURL)
    (binp / "curl").chmod(0o755)
    (home / ".claude" / "kb-flush.env").write_text(cfg)
    env = {"PATH": f"{binp}:{os.environ.get('PATH', '')}", "CURL_REC": str(rec)}
    return home, env, rec


def _run_update(home: Path, env: dict, served: dict, *args: str):
    full = {**env, "CURL_MANIFEST": served["manifest"], "CURL_FILES_DIR": str(served["dir"])}
    return _run(UPDATE, home, env=full, args=args)


def _recorded(rec: Path, kind: str) -> list[str]:
    n = int((rec / ".n").read_text()) if (rec / ".n").exists() else 0
    return [(rec / f"{kind}.{i}").read_text() for i in range(1, n + 1)]


_THREE = {
    "install.sh": STUB_INSTALL,
    "kb-session-brief.sh": STUB_BRIEF,
    "update.sh": "#!/usr/bin/env bash\n",
}
_CFG_SHARED = f"KB_REST_URL=http://127.0.0.1:9/\nKB_TOKEN={FAKE_TOKEN}\n"


@requires_bash
def test_update_fetches_verifies_and_hands_over_to_the_fetched_installer(tmp_path):
    served = _serve(tmp_path, _THREE)
    home, env, rec = _update_box(tmp_path, cfg=_CFG_SHARED)

    res = _run_update(home, env, served)

    assert res.returncode == 0, res.stderr
    setup = home / ".claude" / "kb-setup"
    for name, content in _THREE.items():
        assert (setup / name).read_text() == content, f"{name} was altered in transit"
        assert (setup / name).stat().st_mode & 0o111, f"{name} is not executable"
    ran = (home / "installer.ran").read_text()
    assert ran.startswith("stub installer ran url=http://127.0.0.1:9 bearer=set"), ran
    assert f"fetched and verified 3 file(s) (server version {served['version']})" in res.stdout
    # The bearer rode the -K config on stdin — never argv — and every config is ipv4-pinned.
    argvs, stdins = _recorded(rec, "argv"), _recorded(rec, "stdin")
    assert len(argvs) == 4, "one manifest fetch + one per served file"
    assert all(FAKE_TOKEN not in a for a in argvs), "a credential reached a curl command line"
    assert all("-K -" in a for a in argvs)
    assert all(s.startswith("ipv4\n") and FAKE_TOKEN in s for s in stdins)
    # update.sh itself never touches the installed hooks, and leaves no staging dir.
    assert not (home / ".claude" / "hooks" / "kb-session-brief.sh").exists()
    assert not list(setup.glob(".update.*"))


@requires_bash
def test_a_digest_mismatch_aborts_before_anything_changes(tmp_path):
    served = _serve(tmp_path, _THREE, tamper={"kb-session-brief.sh": "0" * 64})
    home, env, rec = _update_box(tmp_path, cfg=_CFG_SHARED)
    setup = home / ".claude" / "kb-setup"
    setup.mkdir()
    (setup / "install.sh").write_text("the installer this box already had\n")

    res = _run_update(home, env, served)

    assert res.returncode == 1
    assert "MISMATCH" in res.stderr and "kb-session-brief.sh" in res.stderr
    assert "nothing changed" in res.stderr
    assert (setup / "install.sh").read_text() == "the installer this box already had\n"
    assert sorted(p.name for p in setup.iterdir()) == ["install.sh"], "no partial set was placed"
    assert not (home / "installer.ran").exists(), "the installer must not run on a bad fetch"
    assert not list(setup.glob(".update.*")), "the staging dir is cleaned up"
    assert FAKE_TOKEN not in res.stdout + res.stderr


@requires_bash
def test_a_failed_download_aborts_the_same_way(tmp_path):
    served = _serve(tmp_path, _THREE)
    (served["dir"] / "kb-session-brief.sh").unlink()  # the server 404s one file
    home, env, _rec = _update_box(tmp_path, cfg=_CFG_SHARED)

    res = _run_update(home, env, served)

    assert res.returncode == 1 and "fetch FAILED for kb-session-brief.sh" in res.stderr
    assert not (home / "installer.ran").exists()
    assert not any((home / ".claude" / "kb-setup").iterdir())


@requires_bash
def test_dry_run_stages_and_verifies_but_never_installs(tmp_path):
    served = _serve(tmp_path, _THREE)
    home, env, _rec = _update_box(tmp_path, cfg=_CFG_SHARED)

    res = _run_update(home, env, served, "--dry-run")

    assert res.returncode == 0, res.stderr
    assert "--dry-run" in res.stdout and "NOT run" in res.stdout
    assert (home / ".claude" / "kb-setup" / "install.sh").read_text() == STUB_INSTALL
    assert not (home / "installer.ran").exists()


@requires_bash
def test_without_a_rest_url_it_explains_and_makes_no_call(tmp_path):
    served = _serve(tmp_path, _THREE)
    home, env, rec = _update_box(
        tmp_path, cfg=f"KB_MCP_URL=http://127.0.0.1:9/mcp\nKB_TOKEN={FAKE_TOKEN}\n"
    )

    res = _run_update(home, env, served)

    assert res.returncode == 2
    assert "KB_REST_URL" in res.stderr
    assert not (rec / ".n").exists(), "no network attempt without a REST base"


@requires_bash
def test_a_default_enrolled_box_uses_its_per_project_token(tmp_path):
    """The default enrolment writes NO shared KB_TOKEN — only KB_TOKEN_<PROJECT>
    lines — and any live credential may fetch the setup assets."""
    served = _serve(tmp_path, _THREE)
    home, env, rec = _update_box(tmp_path, cfg="KB_REST_URL=http://127.0.0.1:9\n")
    (home / ".claude" / "kb-tokens.env").write_text(f"KB_TOKEN_MYPROJ={FAKE_PROJECT_TOKEN}\n")

    res = _run_update(home, env, served)

    assert res.returncode == 0, res.stderr
    stdins = _recorded(rec, "stdin")
    assert stdins and all(FAKE_PROJECT_TOKEN in s for s in stdins)
    assert all(FAKE_PROJECT_TOKEN not in a for a in _recorded(rec, "argv"))


@requires_bash
def test_without_any_bearer_it_stops_before_the_network(tmp_path):
    served = _serve(tmp_path, _THREE)
    home, env, rec = _update_box(tmp_path, cfg="KB_REST_URL=http://127.0.0.1:9\n")

    res = _run_update(home, env, served)

    assert res.returncode == 2 and "no bearer" in res.stderr
    assert not (rec / ".n").exists()


@requires_bash
def test_a_hostile_manifest_name_is_not_a_file_to_fetch(tmp_path):
    """The name pattern IS the allowlist: a slash or a leading dot never becomes a
    write target, so a lying server cannot steer bytes outside the staging dir."""
    digest = hashlib.sha256(b"evil").hexdigest()
    _version, manifest = _manifest_for(
        {"../evil.sh": digest, ".bashrc": digest, "/etc/x.sh": digest}
    )
    served = {"dir": tmp_path / "served", "manifest": manifest}
    served["dir"].mkdir()
    home, env, _rec = _update_box(tmp_path, cfg=_CFG_SHARED)

    res = _run_update(home, env, served)

    assert res.returncode == 1 and "no fetchable files" in res.stderr
    assert not (home / ".claude" / "evil.sh").exists() and not (home / ".bashrc").exists()
    setup = home / ".claude" / "kb-setup"
    assert not setup.exists() or not any(setup.iterdir()), "nothing was staged or placed"


# --- structural: the asset is wired everywhere it must be --------------------------


def test_update_sh_is_served_installed_verified_and_pointed_at():
    from kbrest.server import _SETUP_SCRIPTS

    assert "update.sh" in _SETUP_SCRIPTS
    install = INSTALL.read_text()
    assert 'install -m 0755 "$SKILL_DIR/update.sh"' in install, (
        "install.sh must place it beside the hooks"
    )
    verify = re.search(r"for _hf in (.*?);\s*do", install, re.S)
    assert verify and "update.sh" in verify.group(1), "and verify its sha256 like every hook"
    brief = BRIEF.read_text()
    banner = brief[brief.index("OUT OF DATE") : brief.index("This is FYI")]
    assert "bash ~/.claude/hooks/update.sh" in banner, (
        "the staleness banner points at the one command"
    )
    assert (SHIPPED / "update.sh").read_bytes() == UPDATE.read_bytes()


@pytest.mark.parametrize("script", sorted(CONTRIB.glob("*.sh")), ids=lambda p: p.name)
def test_every_shipped_script_parses(script: Path):
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("script", sorted(CONTRIB.glob("*.sh")), ids=lambda p: p.name)
def test_every_shipped_script_is_executable(script: Path):
    assert script.stat().st_mode & 0o111, f"{script.name} is not executable"


# ============================================================================
# H15 (2) — fleet_by_box hook_version column
# ============================================================================


def test_fleet_by_box_reports_each_boxs_latest_hook_version(kb):
    from kbcore import db, fleet_health, ingest_queue, scope

    project = "fleetproj"

    def as_box(name: str):
        return scope.use_principal(
            scope.Principal(
                name=name,
                read_projects=frozenset({project}),
                write_projects=frozenset({project}),
                caps=scope.ALL_CAPS,
            )
        )

    def stamp(qid: int, version: str, age_minutes: int) -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE ingest_queue SET meta = meta "
                "|| jsonb_build_object('hook_version', %s::text), "
                "created_at = now() - make_interval(mins => %s) WHERE id = %s",
                (version, age_minutes, qid),
            )

    with as_box("box-rm1"):
        older = ingest_queue.enqueue(project, "older chunk", source_uri="session:one")
        newer = ingest_queue.enqueue(project, "newer chunk", source_uri="session:two")
    stamp(older.id, "oldversion0a", 120)
    stamp(newer.id, "beef1234cafe", 5)
    ingest_queue.enqueue(project, "owner box, never stamped", source_uri="session:o")

    report = fleet_health.fleet_by_box()

    assert report["box-rm1"] == {"degenerate": 0, "total": 2, "hook_version": "beef1234cafe"}, (
        "the box's MOST RECENT stamp, not the first or an arbitrary one"
    )
    assert report["owner"] == {"degenerate": 0, "total": 1}, "absent means ABSENT, never a blank"
    assert fleet_health.stale_hook_projects(report) == [], "the digest's reader ignores the key"


# ============================================================================
# H15 (1) — compose journald
# ============================================================================


def test_every_service_on_the_public_path_logs_to_journald():
    """json-file logs die with the container (the 08-22 burst vanished from its own
    7-day window); every service a public request or the tunnel touches persists."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    for svc in ("kb-mcp", "kb-mcp-public", "kb-cloudflared", "kb-rest", "kb-web"):
        logging = compose["services"][svc].get("logging") or {}
        assert logging.get("driver") == "journald", f"{svc} would lose its logs on recreate"
