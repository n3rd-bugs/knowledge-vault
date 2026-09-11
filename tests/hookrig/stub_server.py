#!/usr/bin/env python3
"""Hermetic stub KB server for the session-hook rig (stdlib only, no deps).

HTTP plumbing + request recording; the submission contract it enforces lives in
stub_contract.py. Routes:

  POST /mcp                  MCP streamable-HTTP, enough for kb-mcp-call.sh:
                             `initialize` (replies with the Mcp-Session-Id
                             RESPONSE HEADER), `notifications/initialized`,
                             `tools/call` (kb_ingest_session, kb_search).
  POST /api/ingest-session   REST: 401 / 422 / 400 / 202, see stub_contract.py.
                             Dedupe keyed on (session_uri, content_hash) with the
                             legacy (kind, project, hash) fallback; the eight
                             flush_* stamp fields are recorded VERBATIM.
  GET  /__requests           the JSONL record (harness only, never recorded)
  POST /__reset              truncate the record, forget sessions + dedupe, clear
                             the __fail queue, restore the RIG_DELAY_MS default
  POST /__fail               {"times": N, "code": C}: the next N ingest-session
                             answers (REST and MCP tool call alike) are HTTP C;
                             C = 0 closes the connection with NO response (curl
                             reports 000). Each such hit IS recorded (status C,
                             "injected": C) so a case can count attempts.
  POST /__delay              {"ms": N}: delay every ingest answer by N ms from
                             now (the env RIG_DELAY_MS sets the default for a
                             stub started with it, e.g. the Windows lane)
  GET  /__health             readiness probe

Every request is appended to the record as one JSON object, with `transcript_gz`
DECODED, so a case can assert on the exact payload bytes the hook put on the
wire. Bound to 0.0.0.0 inside an --internal docker network: reachable only by the
case containers, with no route to anything else. It never touches a database.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from stub_contract import (
    FLUSH_FIELDS,
    Invalid,
    Queue,
    enqueue_result,
    flush_consistent,
    validate_flush,
    validate_submission,
)

TOKEN = os.environ.get("RIG_TOKEN", "kbrig_0000000000000000000000000000")
# Delay every ingest answer (REST and MCP) by this many ms -- the way wincases/08
# proves a >= 1 MB POST survives a 12 s server think-time on Schannel (C6).
DELAY_MS_DEFAULT = int(os.environ.get("RIG_DELAY_MS", "0") or 0)
# What a PROJECT-SCOPED enrolment redeems to. Distinct from TOKEN so a case can
# tell "the machine used the credential it just redeemed" from "it fell back to
# something else it happened to have".
PROJECT_TOKEN = os.environ.get("RIG_PROJECT_TOKEN", "kbg_" + "a" * 43)
RECORD_PATH = os.environ.get("RIG_RECORD", "/tmp/requests.jsonl")
PORT = int(os.environ.get("RIG_PORT", "8000"))
# 0.0.0.0 is right inside the --internal network; a desk-check outside docker
# (python3 stub_server.py against curl on one host) sets RIG_BIND=127.0.0.1.
BIND = os.environ.get("RIG_BIND", "0.0.0.0")
# Where the rig mounted the skill, so the stub can serve the REAL scripts.
SKILL_DIR = Path(os.environ.get("SKILL", "/skill"))
SETUP_SCRIPTS = frozenset(
    {
        "install.sh",
        "flush-session-to-kb.sh",
        "kb-session-brief.sh",
        "kb-mcp-call.sh",
        "notify-complete.sh",
        "turn-start.sh",
        "kb-hook-dispatch.sh",
        "kb-flush-backfill.sh",
        "update.sh",
    }
)

SEARCH_HITS = "\n".join(
    [
        "- [decision · kb-system · verified] Hook transport priority: LOCAL then MCP",
        "- [gotcha · kb-system · unverified] kb-mcp-call.sh needs Mcp-Session-Id",
        "- [decision · kb-system · verified] Flush chunks at 900000 bytes on line boundaries",
    ]
)


class State:
    """The request record and the live MCP sessions."""

    lock = threading.Lock()
    seq = 0
    sessions: set[str] = set()
    # Enrolment codes already redeemed. Single-use is the property under test,
    # so the stub must enforce it rather than accept a code repeatedly.
    spent_codes: set[str] = set()
    # The project the NEXT redemption hands back, set by the harness through
    # POST /__enrol_project. Default enrolment is project-scoped — the credential
    # goes to kb-tokens.env and NO shared KB_TOKEN is ever written — and that is
    # precisely the shape in which a bare $KB_TOKEN reference is unbound.
    enrol_project: str = ""
    # POST /__fail: how many of the NEXT ingest answers fail, and with what code
    # (0 = close without a response). Transport faults are the one thing a
    # hermetic stub cannot otherwise produce, and C6's retry/marker contract is
    # defined entirely by them.
    fail_times: int = 0
    fail_code: int = 0
    delay_ms: int = DELAY_MS_DEFAULT

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.seq = 0
            cls.sessions = set()
            cls.spent_codes = set()
            cls.enrol_project = ""
            cls.fail_times = 0
            cls.fail_code = 0
            cls.delay_ms = DELAY_MS_DEFAULT
            with open(RECORD_PATH, "w", encoding="utf-8"):
                pass
        Queue.reset()

    @classmethod
    def take_failure(cls) -> int | None:
        """Consume one queued failure: its HTTP code, or None when none is queued."""
        with cls.lock:
            if cls.fail_times <= 0:
                return None
            cls.fail_times -= 1
            return cls.fail_code

    @classmethod
    def record(cls, rec: dict[str, Any]) -> None:
        with cls.lock:
            cls.seq += 1
            rec["seq"] = cls.seq
            rec["ts"] = round(time.time(), 3)
            with open(RECORD_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rpc_text(rid: Any, text: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


def _record_meta(rec: dict[str, Any], args: dict[str, Any]) -> None:
    """The identity + git context AS PRESENTED, plus any blank-valued key.

    Recorded verbatim for the same reason ``session_uri`` is: nothing used to
    look at these, which is how ``session:`` — a wildcard grouping key that
    coalesced six projects into one extraction — travelled on every flush from
    every machine. ``empty_keys`` makes the other half assertable: a field with
    nothing to say must be OMITTED, never sent blank for the server to normalise
    away.
    """
    rec["session_uri"] = args.get("session_uri")
    for k in ("repo", "branch", "commit"):
        rec[k] = args.get(k)
    rec["empty_keys"] = sorted(k for k, v in args.items() if isinstance(v, str) and not v.strip())
    # The flush_* telemetry stamps (src/kbrest/schemas.py:80-87), VERBATIM and
    # only when present: a number the hook sent as a string shows up as a string
    # here, which is exactly the drift a case must be able to see.
    for k in FLUSH_FIELDS:
        if k in args:
            rec[k] = args[k]


def _accepted(
    rec: dict[str, Any], project: str, text: str, res: dict[str, Any], facts: dict[str, Any]
) -> None:
    rec.update(
        project=project,
        transcript_bytes=len(text.encode("utf-8")),
        transcript_lines=text.count("\n"),
        ends_with_newline=text.endswith("\n"),
        transcript=text,
        result=res,
        **facts,
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "kb-hookrig-stub/1"

    # ---------------------------------------------------------------- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # keep the rig output clean
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code: int, body: bytes = b"", ctype: str = "application/json", **hdrs: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _done(self, rec: dict[str, Any], code: int, obj: Any, **hdrs: str) -> None:
        """Record the request as answered, then answer it."""
        rec.setdefault("status", code)
        rec.setdefault("bearer", self._bearer())
        State.record(rec)
        self._send(code, json.dumps(obj).encode("utf-8"), **hdrs)

    def _auth(self) -> str:
        h = self.headers.get("Authorization", "")
        if not h.strip():
            return "missing"
        return "ok" if h == f"Bearer {TOKEN}" else "bad"

    def _bearer(self) -> str:
        """The bearer AS PRESENTED, recorded whether or not it was accepted.

        Which credential a hook chose is a separate question from whether the
        server liked it, and per-project token selection can only be asserted by
        seeing the actual value on the wire. Safe here and nowhere else: this is
        a throwaway stub on an --internal network.
        """
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h[:7].lower() == "bearer " else ""

    # ------------------------------------------------------------------ routes
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__health":
            self._send(200, b"ok\n", "text/plain")
        elif path == "/__requests":
            try:
                with open(RECORD_PATH, "rb") as fh:
                    data = fh.read()
            except FileNotFoundError:
                data = b""
            self._send(200, data, "application/x-ndjson")
        elif path == "/__reset":
            State.reset()
            self._send(200, b"reset\n", "text/plain")
        elif path == "/api/search":
            self._rest_search()
        elif path == "/api/setup/manifest.json":
            self._serve_manifest()
        elif path.startswith("/api/setup/"):
            self._serve_script(path.rsplit("/", 1)[-1])
        else:
            self._done({"transport": "http", "method": "GET", "path": path}, 404, {"error": "?"})

    def _rest_search(self) -> None:
        """GET /api/search -- the REMOTE-mode recall endpoint.

        Mirrors src/kbrest/server.py:93 (a list of SearchHit, src/kbcore/models.py
        :104) closely enough for the ONE thing the hook does with it: format each
        row as a bullet. Only the fields kb-session-brief.sh reads are asserted on
        (type/project/trust/title/snippet); the rest are present so the payload is
        shaped like the real one. Bearer-gated exactly like the ingest route.
        """
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        rec: dict[str, Any] = {
            "transport": "rest",
            "method": "GET",
            "path": "/api/search",
            "auth": self._auth(),
            "query": (qs.get("q") or [""])[0],
            "k": (qs.get("k") or [""])[0],
        }
        if rec["auth"] != "ok":
            self._done(rec, 401, {"error": "unauthorized"})
            return
        k = int(rec["k"] or 3)
        hits = [
            {
                "item_id": 1000 + i,
                "title": f"REST hit {i}",
                "type": "decision" if i % 2 == 0 else "gotcha",
                "project": "kb-system",
                "status": "active",
                "trust": "verified" if i % 2 == 0 else "unverified",
                "tags": ["lang:python"],
                "created_at": "2026-07-26T00:00:00+00:00",
                "snippet": f"snippet {i} \u2014 unicode, spaces and a / slash",
                "score": 1.0 - (i / 100),
            }
            for i in range(max(1, min(k, 5)))
        ]
        self._done(rec, 200, hits)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__reset":
            self._body()
            State.reset()
            self._send(200, b"reset\n", "text/plain")
            return
        raw = self._body()
        if path == "/__enrol_project":
            # Harness-only: make the NEXT redemption yield a project-scoped
            # credential (never recorded — it is rig plumbing, not a request
            # under test).
            State.enrol_project = raw.decode("utf-8").strip()
            self._send(200, b"ok\n", "text/plain")
            return
        if path in ("/__fail", "/__delay"):
            # Harness-only fault/latency injection (never recorded). /__fail takes
            # {"times": N, "code": C} ("count" is accepted for "times"); /__delay
            # takes {"ms": N}. A malformed body is a 400 so a typo in a case
            # cannot silently arm nothing.
            try:
                knobs = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(knobs, dict):
                    raise ValueError("body must be a JSON object")
                with State.lock:
                    if path == "/__fail":
                        State.fail_times = int(knobs.get("times", knobs.get("count", 1)))
                        State.fail_code = int(knobs.get("code", 502))
                        armed = {"times": State.fail_times, "code": State.fail_code}
                    else:
                        State.delay_ms = int(knobs.get("ms", DELAY_MS_DEFAULT))
                        armed = {"ms": State.delay_ms}
            except (ValueError, TypeError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode("utf-8"))
                return
            self._send(200, json.dumps(armed).encode("utf-8"))
            return
        if path == "/mcp":
            self._mcp(raw)
        elif path == "/api/ingest-session":
            self._rest_ingest(raw)
        elif path == "/api/enrol":
            self._enrol(raw)
        else:
            rec = {"transport": "http", "method": "POST", "path": path, "bytes": len(raw)}
            self._done(rec, 404, {"error": "not found"})

    # ------------------------------------------------------------- enrolment
    # Mirrors the real GET /api/setup/<name> and POST /api/enrol closely enough
    # to exercise the BOOTSTRAP a new machine performs: fetch the four scripts
    # with the enrolment code as bearer, then redeem it for a real credential.
    # The code must survive the fetches (only the POST spends it) or the
    # bootstrap would consume its own credential before using it.
    def _serve_manifest(self) -> None:
        """Mirror GET /api/setup/manifest.json: a content-derived version plus a
        digest per shipped script, so a box can tell it is stale without pulling
        every file."""
        import hashlib as _h

        rec = {"transport": "http", "method": "GET", "path": self.path, "script": "manifest.json"}
        if self._auth() == "missing":
            self._done(rec, 401, {"error": "unauthorized"})
            return
        files = {
            n: _h.sha256((SKILL_DIR / n).read_bytes()).hexdigest()
            for n in sorted(SETUP_SCRIPTS)
            if (SKILL_DIR / n).exists()
        }
        version = _h.sha256(
            "".join(f"{k}:{v}" for k, v in sorted(files.items())).encode()
        ).hexdigest()[:12]
        State.record({**rec, "status": 200})
        self._send(
            200,
            json.dumps({"version": version, "files": files, "count": len(files)}).encode(),
            "application/json",
        )

    def _serve_script(self, name: str) -> None:
        rec = {"transport": "http", "method": "GET", "path": self.path, "script": name}
        if self._auth() == "missing":
            self._done(rec, 401, {"error": "unauthorized"})
            return
        if name not in SETUP_SCRIPTS:
            self._done(rec, 404, {"error": "not found"})
            return
        body = (SKILL_DIR / name).read_bytes()
        rec["bytes"] = len(body)
        State.record({**rec, "status": 200})
        self._send(200, body, "text/plain")

    def _enrol(self, raw: bytes) -> None:
        header = self.headers.get("Authorization", "")
        code = header[7:].strip() if header[:7].lower() == "bearer " else ""
        rec = {"transport": "http", "method": "POST", "path": "/api/enrol", "code": bool(code)}
        if not code.startswith("kbe_"):
            self._done(rec, 401, {"error": "unauthorized"})
            return
        if code in State.spent_codes:
            # Single use, exactly like the real endpoint.
            self._done(rec, 400, {"error": "enrolment code is not valid"})
            return
        State.spent_codes.add(code)
        body: dict[str, Any] = {"token": TOKEN, "is_owner": True, "principal": None}
        if State.enrol_project:
            # A project-scoped grantee, exactly like the real endpoint returns for
            # a per-project enrolment code.
            body = {
                "token": PROJECT_TOKEN,
                "is_owner": False,
                "principal": "rig-machine",
                "project": State.enrol_project,
            }
        self._done(rec, 200, body)

    # --------------------------------------------------------------------- MCP
    def _mcp(self, raw: bytes) -> None:
        auth = self._auth()
        rec: dict[str, Any] = {
            "transport": "mcp",
            "method": "POST",
            "path": "/mcp",
            "auth": auth,
            "mcp_session_id": self.headers.get("Mcp-Session-Id"),
            "bytes": len(raw),
        }
        if auth != "ok":
            rec["rpc"] = None
            self._done(rec, 401, {"error": "unauthorized"})
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            rec["rpc"] = "<unparseable>"
            self._done(rec, 400, {"error": "invalid json"})
            return

        method = msg.get("method")
        rec["rpc"] = method
        rid = msg.get("id")

        if method == "initialize":
            sid = uuid.uuid4().hex
            with State.lock:
                State.sessions.add(sid)
            rec["issued_session_id"] = sid
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "kb-hookrig-stub", "version": "1"},
            }
            # The session id travels as a RESPONSE HEADER -- kb-mcp-call.sh reads
            # it out of `curl -D -` and fails hard if it is absent.
            body = {"jsonrpc": "2.0", "id": rid, "result": result}
            self._done(rec, 200, body, Mcp_Session_Id=sid)
            return

        rec["session_known"] = rec["mcp_session_id"] in State.sessions
        if method == "notifications/initialized":
            rec["status"] = 202
            State.record(rec)
            self._send(202, b"")
            return

        if method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            rec["tool"] = name
            rec["arg_keys"] = sorted(args) if isinstance(args, dict) else None
            if name == "kb_ingest_session":
                self._mcp_ingest(rec, args, rid)
            elif name == "kb_search":
                rec.update(is_error=False, query=args.get("query"), k=args.get("k"))
                self._done(rec, 200, _rpc_text(rid, SEARCH_HITS))
            else:
                rec["is_error"] = True
                self._done(rec, 200, _rpc_text(rid, f"unknown tool {name!r}", is_error=True))
            return

        err = {"code": -32601, "message": "method not found"}
        self._done(rec, 200, {"jsonrpc": "2.0", "id": rid, "error": err})

    def _mcp_ingest(self, rec: dict[str, Any], args: Any, rid: Any) -> None:
        """A tool rejection is an HTTP-200 result with isError:true, never a 4xx."""
        if not isinstance(args, dict):
            rec.update(is_error=True, reason="arguments must be an object")
            self._done(rec, 200, _rpc_text(rid, "arguments must be an object", is_error=True))
            return
        rec["project"] = args.get("project")
        rec["kind"] = args.get("kind")
        rec["encoding"] = "gz" if args.get("transcript_gz") else "plain"
        _record_meta(rec, args)
        if self._ingest_gate(rec):
            return
        try:
            project, text = validate_submission(args)
        except (ValueError, Invalid) as e:
            rec.update(is_error=True, reason=str(e))
            self._done(rec, 200, _rpc_text(rid, str(e), is_error=True))
            return
        res, facts = enqueue_result(
            text, project=project, kind=args.get("kind"), session_uri=args.get("session_uri")
        )
        rec["is_error"] = False
        _accepted(rec, project, text, res, facts)
        self._done(rec, 200, _rpc_text(rid, json.dumps(res)))

    # ------------------------------------------------------ harness injection
    def _ingest_gate(self, rec: dict[str, Any]) -> bool:
        """Apply the harness knobs to ONE ingest answer: the delay, then a queued
        failure. True when the request was answered (or dropped) here.

        Runs BEFORE validation -- and before the bearer check as well -- so an
        injected 401/5xx/000 is seen even by a request the stub would otherwise
        reject, and the hit is recorded either way -- `stub_count '.injected'`
        is how a case counts retry attempts. Consequently an unauthenticated
        request consumes an armed slot too: count `.injected` hits, not `.auth`.
        """
        delay = State.delay_ms
        if delay > 0:
            time.sleep(delay / 1000.0)
        code = State.take_failure()
        if code is None:
            return False
        rec["injected"] = code
        if code == 0:
            # Simulated network death: record the hit, then close the socket
            # without writing a status line. curl reports http_code 000
            # ("Empty reply from server"), exactly what a dropped connection
            # looks like to the hook.
            rec["status"] = 0
            rec.setdefault("bearer", self._bearer())
            State.record(rec)
            self.close_connection = True
            return True
        self._done(rec, code, {"error": f"injected failure {code}"})
        return True

    # -------------------------------------------------------------------- REST
    def _rest_ingest(self, raw: bytes) -> None:
        auth = self._auth()
        rec: dict[str, Any] = {
            "transport": "rest",
            "method": "POST",
            "path": "/api/ingest-session",
            "auth": auth,
            "bytes": len(raw),
        }
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict):
            rec["project"] = payload.get("project")
            rec["kind"] = payload.get("kind")
            rec["encoding"] = "gz" if payload.get("transcript_gz") else "plain"
            _record_meta(rec, payload)
        if self._ingest_gate(rec):
            return
        if auth != "ok":
            self._done(rec, 401, {"error": "unauthorized"})
            return
        if not isinstance(payload, dict):
            rec["reason"] = "body is not a JSON object"
            self._done(rec, 422, {"detail": [{"msg": rec["reason"]}]})
            return
        try:
            project, text = validate_submission(payload)
            stamp = validate_flush(payload)
        except ValueError as e:  # shape -> pydantic would have refused the model
            rec["reason"] = str(e)
            self._done(rec, 422, {"detail": [{"msg": str(e)}]})
            return
        except Invalid as e:  # domain -> kbcore.InvalidOperation
            rec["reason"] = str(e)
            self._done(rec, 400, {"error": str(e)})
            return
        if stamp:
            # src/kbcore/models.py:268-274 -- recorded, never a refusal.
            rec["flush_consistent"] = flush_consistent(stamp, len(text.encode("utf-8")))
        res, facts = enqueue_result(
            text, project=project, kind=payload.get("kind"), session_uri=payload.get("session_uri")
        )
        _accepted(rec, project, text, res, facts)
        self._done(rec, 202, res)


def main() -> None:
    State.reset()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    srv.daemon_threads = True
    print(f"kb-hookrig-stub listening on {BIND}:{PORT} recording to {RECORD_PATH}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
