#!/usr/bin/env python3
"""Hermetic stub KB server for the session-hook rig (stdlib only, no deps).

HTTP plumbing + request recording; the submission contract it enforces lives in
stub_contract.py. Routes:

  POST /mcp                  MCP streamable-HTTP, enough for kb-mcp-call.sh:
                             `initialize` (replies with the Mcp-Session-Id
                             RESPONSE HEADER), `notifications/initialized`,
                             `tools/call` (kb_ingest_session, kb_search).
  POST /api/ingest-session   REST: 401 / 422 / 400 / 202, see stub_contract.py.
  GET  /__requests           the JSONL record (harness only, never recorded)
  POST /__reset              truncate the record, forget sessions + dedupe
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
from typing import Any

from stub_contract import Invalid, Queue, enqueue_result, validate_submission

TOKEN = os.environ.get("RIG_TOKEN", "kbrig_0000000000000000000000000000")
RECORD_PATH = os.environ.get("RIG_RECORD", "/tmp/requests.jsonl")
PORT = int(os.environ.get("RIG_PORT", "8000"))

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

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.seq = 0
            cls.sessions = set()
            with open(RECORD_PATH, "w", encoding="utf-8"):
                pass
        Queue.reset()

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


def _accepted(rec: dict[str, Any], project: str, text: str, res: dict[str, Any]) -> None:
    rec.update(
        project=project,
        transcript_bytes=len(text.encode("utf-8")),
        transcript_lines=text.count("\n"),
        ends_with_newline=text.endswith("\n"),
        transcript=text,
        result=res,
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
        State.record(rec)
        self._send(code, json.dumps(obj).encode("utf-8"), **hdrs)

    def _auth(self) -> str:
        h = self.headers.get("Authorization", "")
        if not h.strip():
            return "missing"
        return "ok" if h == f"Bearer {TOKEN}" else "bad"

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
        else:
            self._done({"transport": "http", "method": "GET", "path": path}, 404, {"error": "?"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/__reset":
            self._body()
            State.reset()
            self._send(200, b"reset\n", "text/plain")
            return
        raw = self._body()
        if path == "/mcp":
            self._mcp(raw)
        elif path == "/api/ingest-session":
            self._rest_ingest(raw)
        else:
            rec = {"transport": "http", "method": "POST", "path": path, "bytes": len(raw)}
            self._done(rec, 404, {"error": "not found"})

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
        try:
            project, text = validate_submission(args)
        except (ValueError, Invalid) as e:
            rec.update(is_error=True, reason=str(e))
            self._done(rec, 200, _rpc_text(rid, str(e), is_error=True))
            return
        res = enqueue_result(text)
        rec["is_error"] = False
        _accepted(rec, project, text, res)
        self._done(rec, 200, _rpc_text(rid, json.dumps(res)))

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
        if auth != "ok":
            self._done(rec, 401, {"error": "unauthorized"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if not isinstance(payload, dict):
            rec["reason"] = "body is not a JSON object"
            self._done(rec, 422, {"detail": [{"msg": rec["reason"]}]})
            return
        rec["project"] = payload.get("project")
        rec["kind"] = payload.get("kind")
        rec["encoding"] = "gz" if payload.get("transcript_gz") else "plain"
        try:
            project, text = validate_submission(payload)
        except ValueError as e:  # shape -> pydantic would have refused the model
            rec["reason"] = str(e)
            self._done(rec, 422, {"detail": [{"msg": str(e)}]})
            return
        except Invalid as e:  # domain -> kbcore.InvalidOperation
            rec["reason"] = str(e)
            self._done(rec, 400, {"error": str(e)})
            return
        res = enqueue_result(text)
        _accepted(rec, project, text, res)
        self._done(rec, 202, res)


def main() -> None:
    State.reset()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"kb-hookrig-stub listening on :{PORT} recording to {RECORD_PATH}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
