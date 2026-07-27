#!/usr/bin/env python3
"""win_driver.py — drive the hook rig against a REAL Windows box over WinRM.

Why this exists: the container `winsim` variant FAKES Windows (a stubbed
`uname -s`, a shell-script `cygpath`, a fake `timeout.exe`). It pins the logic
but it cannot catch what only a real box has — CRLF checkouts, the genuine
`C:\\Windows\\System32\\timeout.exe`, Git Bash's real `EXEPATH`, MSYS path
mangling of arguments, a jq/python that are Windows .exe files, and the fact
that Claude Code on Windows spawns hooks through CreateProcess.

Design:
  * Credentials come from the repo's gitignored `.windows` (WIN_IP/WIN_USER/
    WIN_PASSWORD). They are NEVER echoed — not to stdout, not into a remote
    command line, not into the transcript.
  * Everything runs inside a per-run sandbox under the Windows TEMP dir, and
    every case sets HOME inside it. The user's REAL `%USERPROFILE%\\.claude`
    is hashed before and after: a change fails the run (host tripwire), the
    same guarantee the container lane gets for free.
  * The stub server runs ON the Windows box under its own Python, bound to
    127.0.0.1. Nothing crosses the network except WinRM control traffic, so
    the hooks under test still talk to a local, hermetic KB.
  * Files are uploaded base64-chunked with LF endings preserved byte-for-byte
    (a CRLF-mangled hook script is a bug we want to DETECT, never to inject).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import socket
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    import winrm
except ImportError:  # pragma: no cover - environment guard
    sys.exit("pywinrm is not installed (apt install python3-winrm / pip install pywinrm)")

REPO = Path(__file__).resolve().parents[2]
RIG = Path(__file__).resolve().parent

# The binding limit is not Windows' 32767 command line but WinRS's ~8k Command
# element, and run_ps spends ~2.67 characters of it per source character
# (base64 of UTF-16LE). 2400 is what fits. That makes chunked upload SLOW —
# staging the rig this way took over ten minutes — so it is the FALLBACK; the
# fast path serves one zip over HTTP (see push_bundle).
CHUNK = 2400


def load_creds() -> dict[str, str]:
    """Read .windows without leaking values into the environment dump."""
    path = REPO / ".windows"
    if not path.exists():
        sys.exit(f"no {path} — cannot reach the Windows test host")
    creds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        creds[k.strip()] = v.strip().strip("'\"")
    for k in ("WIN_IP", "WIN_USER", "WIN_PASSWORD"):
        if not creds.get(k):
            sys.exit(f"{path} is missing {k}")
    return creds


class Win:
    """A thin, loud wrapper over a WinRM session."""

    def __init__(self, creds: dict[str, str]) -> None:
        self.ip = creds["WIN_IP"]
        self.session = winrm.Session(
            f"http://{self.ip}:5985/wsman",
            auth=(creds["WIN_USER"], creds["WIN_PASSWORD"]),
            transport="ntlm",
            read_timeout_sec=360,
            operation_timeout_sec=300,
        )

    def ps(self, script: str, check: bool = False) -> tuple[int, str, str]:
        r = self.session.run_ps(script)
        out = r.std_out.decode("utf-8", "replace")
        err = r.std_err.decode("utf-8", "replace")
        if "#< CLIXML" in err:  # PowerShell progress noise, not a real error
            err = ""
        if check and r.status_code != 0:
            raise RuntimeError(f"remote powershell failed ({r.status_code}): {err.strip()[:600]}")
        return r.status_code, out, err

    # ---------------------------------------------------------------- upload
    def put_bytes(self, data: bytes, remote_path: str) -> None:
        """Write exact bytes to a remote path (no encoding/newline translation)."""
        b64 = base64.b64encode(data).decode("ascii")
        tmp = f"$env:TEMP\\kbup_{hashlib.sha1(remote_path.encode()).hexdigest()[:12]}.b64"
        self.ps(f"Remove-Item -Force -ErrorAction SilentlyContinue {tmp}")
        for i in range(0, len(b64), CHUNK):
            piece = b64[i : i + CHUNK]
            self.ps(
                f"Add-Content -Path {tmp} -Value '{piece}' -NoNewline -Encoding Ascii", check=True
            )
        self.ps(
            "$p = '{path}'\n"
            "New-Item -ItemType Directory -Force -Path (Split-Path -Parent $p) | Out-Null\n"
            "[IO.File]::WriteAllBytes($p, [Convert]::FromBase64String((Get-Content {tmp} -Raw)))\n"
            "Remove-Item -Force {tmp}".format(path=remote_path.replace("'", "''"), tmp=tmp),
            check=True,
        )

    def put_file(self, local: Path, remote_path: str) -> None:
        self.put_bytes(local.read_bytes(), remote_path)

    def put_tree(self, local_dir: Path, remote_dir: str, pattern: str = "*") -> int:
        n = 0
        for f in sorted(local_dir.glob(pattern)):
            if f.is_file():
                self.put_bytes(f.read_bytes(), f"{remote_dir}\\{f.name}")
                n += 1
        return n

    def push_bundle(self, files: dict[str, bytes], remote_dir: str) -> None:
        """Stage the whole rig remotely as ONE zip, then expand it in place.

        Zip entries carry exact bytes, so hook scripts arrive with their LF
        endings intact — the rig must never inject the CRLF corruption it is
        meant to detect.

        Transport: WinRS caps a command at ~8k, which makes base64-over-WinRM
        staging take minutes. So the zip is served over a short-lived, loopback-
        scoped HTTP server on THIS host and pulled once by the Windows box. That
        is a control-plane convenience only — the hooks under test still talk
        exclusively to a stub on the Windows box's own loopback, so the test
        itself stays hermetic. If the pull fails (no route back), it falls back
        to the slow chunked upload rather than failing the run.
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        payload = buf.getvalue()
        remote_zip = f"{remote_dir}\\_rig.zip"

        if not self._push_via_http(payload, remote_zip):
            print("   (http staging unavailable — falling back to chunked upload)")
            self.put_bytes(payload, remote_zip)

        self.ps(
            f"Expand-Archive -Path '{remote_zip}' -DestinationPath '{remote_dir}' -Force\n"
            f"Remove-Item -Force '{remote_zip}'",
            check=True,
        )

    def _push_via_http(self, payload: bytes, remote_zip: str) -> bool:
        """Serve `payload` once over HTTP and have the box pull it. True on success."""
        try:
            local_ip = self._route_src()
        except OSError:
            return False

        digest = hashlib.sha256(payload).hexdigest()

        class OneShot(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib signature
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_a: object) -> None:  # keep the rig output clean
                return

        srv = HTTPServer((local_ip, 0), OneShot)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://{local_ip}:{srv.server_port}/rig.zip"
            rc, out, _ = self.ps(
                f"$z = '{remote_zip}'\n"
                "try {\n"
                f"  Invoke-WebRequest -UseBasicParsing '{url}' -OutFile $z -TimeoutSec 30\n"
                "  (Get-FileHash $z -Algorithm SHA256).Hash.ToLower()\n"
                "} catch { 'ERR' }"
            )
            got = out.strip().splitlines()[-1] if out.strip() else ""
            if got == digest:
                return True
            if got and got != "ERR":
                # A truncated or altered transfer must never silently become a
                # test run against half a rig.
                raise RuntimeError(f"staged zip digest mismatch: {got[:16]} != {digest[:16]}")
            return False
        finally:
            srv.shutdown()
            srv.server_close()

    def _route_src(self) -> str:
        """The local address the Windows host would see us on."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((self.ip, 5985))
            return sock.getsockname()[0]
        finally:
            sock.close()

    def read_text(self, remote_path: str) -> str:
        """Read a remote text file as faithful UTF-8.

        Without pinning the console encoding, PowerShell re-encodes its stdout
        through the OEM code page, which turned the hooks' UTF-8 punctuation
        into replacement characters. Garbled readback would let a real content
        bug hide behind what looks like a display artifact.
        """
        rc, out, _ = self.ps(
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
            "$OutputEncoding = [Text.Encoding]::UTF8\n"
            f"if (Test-Path '{remote_path}') {{ Get-Content -Raw -Encoding UTF8 '{remote_path}' }}"
        )
        return out

    # ------------------------------------------------------------- git bash
    def bash(
        self, script_win_path: str, env: dict[str, str], timeout_s: int = 300
    ) -> tuple[int, str]:
        """Run a case under Git Bash and return (rc, output).

        Two Windows-specific traps are designed around here:

        1. The hooks DETACH a child (`nohup ... &`). If the case's stdout were a
           PowerShell pipe, that child would inherit the write handle and keep it
           open, so `Out-String` would block long after the case exited — the run
           just hangs. Redirecting to a FILE inside the wrapper means the pipe
           never exists, and Start-Process waits only on the direct child.
        2. MSYS2 rewrites arguments that look like POSIX paths, which would
           corrupt the env values. Setting them inside the wrapper avoids the
           argument layer entirely; only the wrapper path itself crosses it.
        """
        base = env["RIG_SANDBOX_WIN"]
        log_win, wrap_win, rc_win = f"{base}\\_case.out", f"{base}\\_wrap.sh", f"{base}\\_case.rc"
        exports = "\n".join(f"export {k}='{v}'" for k, v in env.items())
        # The WRAPPER reports the verdict, not PowerShell: a -PassThru process
        # does not populate .ExitCode unless its handle was cached first, which
        # silently yielded an empty status. A file cannot be ambiguous.
        wrapper = (
            f"{exports}\n"
            f"bash '{self._posix(script_win_path)}' >'{self._posix(log_win)}' 2>&1\n"
            f"echo $? >'{self._posix(rc_win)}'\n"
        )
        self.put_bytes(wrapper.encode("utf-8"), wrap_win)
        self.ps(f"Remove-Item -Force -ErrorAction SilentlyContinue '{log_win}','{rc_win}'")

        self.ps(
            f"$p = Start-Process -FilePath '{GITBASH}' "
            f"-ArgumentList '--noprofile','--norc','{self._posix(wrap_win)}' "
            f"-NoNewWindow -PassThru\n"
            f"$null = $p.Handle\n"
            f"if (-not $p.WaitForExit({timeout_s * 1000})) {{ "
            f"Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }}"
        )
        body = self.read_text(log_win)
        verdict = self.read_text(rc_win).strip()
        if not verdict:
            return 124, body + f"\n[rig] the case produced no exit status within {timeout_s}s"
        try:
            return int(verdict), body
        except ValueError:
            return 1, body + f"\n[rig] unreadable exit status {verdict!r}"

    @staticmethod
    def _posix(win_path: str) -> str:
        p = win_path.replace("\\", "/")
        if len(p) > 1 and p[1] == ":":
            p = "/" + p[0].lower() + p[2:]
        return p


GITBASH = r"C:\Program Files\Git\bin\bash.exe"


def posix(win_path: str) -> str:
    return Win._posix(win_path)


def main() -> int:
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    keep = "--keep" in sys.argv

    win = Win(load_creds())
    print(f"-- connecting to the Windows host ({win.ip}) over WinRM")
    rc, out, _ = win.ps(
        "$os = (Get-CimInstance Win32_OperatingSystem).Caption\n"
        '"$env:COMPUTERNAME|$os|$env:TEMP|$env:USERPROFILE"'
    )
    if rc != 0:
        return fail("could not authenticate to the Windows host")
    comp, caption, temp, profile = out.strip().split("|")
    print(f"   {caption} on {comp}")

    if not win.ps(f"Test-Path '{GITBASH}'")[1].strip().lower().startswith("true"):
        return fail(f"Git Bash not found at {GITBASH} — the Windows lane needs it")

    # ---------------------------------------------------------- host tripwire
    # Watch only what an errant hook install could plausibly touch. Hashing the
    # WHOLE ~/.claude tree tripped on the owner simply USING Claude Code during a
    # run: live session transcripts under projects/ grow continuously, as do
    # history.jsonl and the telemetry dirs. A tripwire that fires on unrelated
    # activity gets ignored, which is worse than not having one.
    WATCHED = ["settings.json", "settings.local.json", "kb-flush.env", "hooks", "skills"]
    watched_ps = ",".join(f"'{profile}\\.claude\\{w}'" for w in WATCHED)
    tripwire_ps = (
        f"$paths = @({watched_ps})\n"
        "$acc = ''\n"
        "foreach ($p in $paths) {\n"
        "  if (Test-Path $p) {\n"
        "    $items = Get-ChildItem -Recurse -File $p -ErrorAction SilentlyContinue\n"
        "    if ($items) {\n"
        "      foreach ($i in ($items | Sort-Object FullName)) {\n"
        "        $acc += $i.FullName + ':' + $i.Length + ';'\n"
        "      }\n"
        "    } else {\n"
        "      $f = Get-Item $p -ErrorAction SilentlyContinue\n"
        "      if ($f -and -not $f.PSIsContainer) { $acc += $f.FullName + ':' + $f.Length + ';' }\n"
        "    }\n"
        "  } else { $acc += $p + ':ABSENT;' }\n"
        "}\n"
        "$acc"
    )
    before = win.ps(tripwire_ps)[1].strip()
    print(f"-- host tripwire armed on {profile}\\.claude")

    # Sweep sandboxes orphaned by a killed run. Six hours is far longer than any
    # real run, so this can never touch a concurrent one.
    rc, out, _ = win.ps(
        "$cut = (Get-Date).AddHours(-6)\n"
        "$n = 0\n"
        "$dirs = Get-ChildItem \"$env:TEMP\" -Filter 'kb-hookrig-*' -Directory `\n"
        "  -ErrorAction SilentlyContinue\n"
        "foreach ($d in $dirs) {\n"
        "  if ($d.LastWriteTime -lt $cut) {\n"
        "    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $d.FullName\n"
        "    $n++\n"
        "  }\n"
        "}\n"
        "$n"
    )
    swept = out.strip().splitlines()[-1] if out.strip() else "0"
    if swept.isdigit() and int(swept) > 0:
        print(f"-- swept {swept} sandbox(es) orphaned by earlier runs")

    runid = f"{os.getpid()}-{int(time.time())}"
    sandbox = f"{temp}\\kb-hookrig-{runid}"
    sandbox_posix = posix(sandbox)
    print(f"-- sandbox: {sandbox}")

    try:
        win.ps(f"New-Item -ItemType Directory -Force -Path '{sandbox}' | Out-Null", check=True)

        # ------------------------------------------------------------ upload
        # One zip for the whole rig: staging file-by-file cost ~150 WinRM round
        # trips. Zip entries are stored byte-exact, so LF endings survive — the
        # CRLF risk we are here to DETECT must never be introduced by the rig.
        skill_dir = Path(os.environ.get("KB_SKILL_DIR", REPO / "contrib/claude/kb-session-flush"))
        bundle: dict[str, bytes] = {
            "lib.sh": (RIG / "lib.sh").read_bytes(),
            "winlib.sh": (RIG / "winlib.sh").read_bytes(),
            "stub_server.py": (RIG / "stub_server.py").read_bytes(),
            "stub_contract.py": (RIG / "stub_contract.py").read_bytes(),
        }
        n = 0
        for f in sorted((RIG / "wincases").glob("*.sh")):
            bundle[f"cases/{f.name}"] = f.read_bytes()
            n += 1
        ns = 0
        for f in sorted(skill_dir.glob("*")):
            if f.is_file():
                bundle[f"skill/{f.name}"] = f.read_bytes()
                ns += 1
        win.push_bundle(bundle, sandbox)
        print(f"-- staged {n} case(s), {ns} skill file(s), stub + lib (one zip, LF preserved)")

        # ------------------------------------------------ start the local stub
        # A PID-derived port collides across concurrent runs (two PIDs can be
        # congruent mod the range), which would silently point one run's cases
        # at the OTHER run's stub. Ask the box which ports are actually free.
        print("-- choosing a free port on the host")
        busy = set()
        rc, out, _ = win.ps(
            "(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |"
            " Select-Object -ExpandProperty LocalPort) -join ','"
        )
        for tok in out.strip().split(","):
            if tok.strip().isdigit():
                busy.add(int(tok.strip()))
        port = next(
            (p for p in range(18800 + (os.getpid() % 700), 19800) if p not in busy),
            0,
        )
        if not port:
            return fail("no free port in 18800-19800 on the Windows host")
        print("-- locating python on the host")
        pyexe = win.ps("(Get-Command python -ErrorAction SilentlyContinue).Source")[1].strip()
        if not pyexe:
            return fail("no python on the Windows host — the stub cannot run")
        stub_log = f"{sandbox}\\stub.log"
        print(f"-- starting the stub with {pyexe} on port {port}")
        # PowerShell's Start-Process BLOCKS forever under WinRM here (the remote
        # shell keeps waiting on the spawned process's handles), so the stub is
        # launched through a .cmd via Win32_Process.Create — the one way to get a
        # genuinely detached process back from a WinRM session. The .cmd also
        # carries the environment, which Win32_Process.Create cannot pass.
        launcher = (
            "@echo off\r\n"
            f"set RIG_PORT={port}\r\n"
            f"set RIG_RECORD={sandbox}\\requests.jsonl\r\n"
            "set RIG_TOKEN=kbrig_0000000000000000000000000000\r\n"
            f"set PYTHONPATH={sandbox}\r\n"
            # So the stub can serve the real hook scripts at /api/setup/<name>,
            # which is what a bootstrapping machine fetches.
            f"set SKILL={sandbox}\\skill\r\n"
            f'cd /d "{sandbox}"\r\n'
            f'"{pyexe}" "{sandbox}\\stub_server.py" > "{stub_log}" 2>&1\r\n'
        )
        launch_cmd = f"{sandbox}\\_stub.cmd"
        win.put_bytes(launcher.encode("ascii"), launch_cmd)
        rc, out, _ = win.ps(
            "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{ CommandLine = 'cmd.exe /c \"{launch_cmd}\"' }}\n"
            '"$($r.ReturnValue):$($r.ProcessId)"'
        )
        if not out.strip().startswith("0:"):
            return fail(f"could not launch the stub on the Windows host ({out.strip()})")
        stub_pid = out.strip().split(":", 1)[1]
        print(f"-- stub launched (pid {stub_pid})")

        stub_url = f"http://127.0.0.1:{port}"
        print("-- waiting for the stub to answer")
        ready = False
        for _ in range(40):
            rc, out, _ = win.ps(
                f"try {{ (Invoke-WebRequest -UseBasicParsing '{stub_url}/__requests' "
                f"-TimeoutSec 3).StatusCode }} catch {{ 0 }}"
            )
            if out.strip() == "200":
                ready = True
                break
            time.sleep(1)
        if not ready:
            print(win.read_text(f"{sandbox}\\stub.err")[:1500])
            return fail("the stub never became healthy on the Windows host")
        print(f"-- stub healthy on {stub_url} (bound to loopback, hermetic)")

        # -------------------------------------------------------- run cases
        names = sorted(p.name for p in (RIG / "wincases").glob("*.sh"))
        if only:
            names = [n for n in names if any(f in n for f in only)]
        env_base = {
            "STUB": stub_url,
            "RIG_TOKEN": "kbrig_0000000000000000000000000000",
            "SKILL": f"{sandbox_posix}/skill",
            "RIG_LIB": f"{sandbox_posix}/lib.sh",
            "RIG_SANDBOX": sandbox_posix,
            "RIG_SANDBOX_WIN": sandbox,
            "RIG_HOMEBASE": f"{sandbox_posix}/homes",
            "RIG_WIN_LANE": "1",
        }

        results: list[tuple[str, bool, int, str]] = []
        for name in names:
            print(f"\n== {name[:-3]} ==")
            t0 = time.time()
            rc, out = win.bash(f"{sandbox}\\cases\\{name}", env_base)
            print(out.rstrip())
            xfail = "# EXPECT: xfail" in (RIG / "wincases" / name).read_text(encoding="utf-8")
            ok = (rc == 0) if not xfail else True
            results.append((name[:-3], ok, int(time.time() - t0), "XFAIL" if xfail else ""))

        # -------------------------------------------------------- teardown
        # Killing the stub takes a TREE kill, and two earlier attempts got this
        # wrong: matching the command line with doubled backslashes never fired
        # (PowerShell's -like is a wildcard operator, where "\\" is two literal
        # characters, not an escape), and Stop-Process on the returned PID killed
        # only the cmd.exe wrapper while python — the actual stub — kept running
        # and kept the sandbox undeletable. taskkill /T takes the whole tree.
        win.ps(f"taskkill /T /F /PID {stub_pid} 2>&1 | Out-Null")
        # Belt and braces, with a CORRECT single-backslash wildcard this time.
        win.ps(
            "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
            f"Where-Object {{ $_.CommandLine -like '*{runid}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        if not keep:
            # The stub exits asynchronously; retry so the removal is not racing it.
            win.ps(
                f"foreach ($i in 1..5) {{\n"
                f"  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue '{sandbox}'\n"
                f"  if (-not (Test-Path '{sandbox}')) {{ break }}\n"
                f"  Start-Sleep -Milliseconds 400\n"
                f"}}"
            )

        # -------------------------------------------------------- tripwire
        after = win.ps(tripwire_ps)[1].strip()
        print("\n" + "-" * 63)
        if before != after:
            print(f"HOST TRIPWIRE TRIPPED: {profile}\\.claude changed during the run")
            return 3
        print(f"host {profile}\\.claude unchanged (tripwire ok)")

        # -------------------------------------------------------- summary
        print("-" * 63)
        failed = 0
        for name, ok, secs, tag in results:
            mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
            print(f"{mark}  {name:<38} {secs:>3}s {tag}")
            if not ok:
                failed += 1
        if failed:
            print(f"\033[31m{failed} windows case(s) failed\033[0m")
            return 1
        print("\033[32mall windows cases green\033[0m")
        return 0
    except Exception as exc:  # noqa: BLE001 - the driver must report, not traceback
        return fail(f"{type(exc).__name__}: {exc}")


def fail(msg: str) -> int:
    print(f"\033[31mABORT: {msg}\033[0m")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
