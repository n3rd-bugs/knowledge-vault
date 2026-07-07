#!/usr/bin/env bash
# Minimal MCP streamable-HTTP client for the KB hooks: initialize, then call one
# tool. Reads KB_MCP_URL + KB_TOKEN from the environment. Prints the tool's text
# result on success (exit 0); prints an error to stderr on failure (exit 1).
# Usage: kb-mcp-call.sh <tool_name> <arguments_json | @args_file>
# Pass @/path/to/file for large arguments — argv is capped at ~128 KB per arg
# by the kernel, so chunk-sized payloads MUST go via the file form.
set -uo pipefail
TOOL="${1:?tool name}"; ARGS="${2:-}"; [ -n "$ARGS" ] || ARGS="{}"
ARGSFILE=""
case "$ARGS" in @*) ARGSFILE="${ARGS#@}";; esac
[ -n "${KB_MCP_URL:-}" ] && [ -n "${KB_TOKEN:-}" ] || { echo "KB_MCP_URL/KB_TOKEN unset" >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { echo "need jq" >&2; exit 1; }
AUTH="Authorization: Bearer $KB_TOKEN"; CT="Content-Type: application/json"
AC="Accept: application/json, text/event-stream"
SID=$(curl -s -D - -o /dev/null --max-time 20 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"kb-hook","version":"1"}}}' \
  | awk 'tolower($1)=="mcp-session-id:"{print $2}' | tr -d '\r')
[ -n "$SID" ] || { echo "no mcp session (auth/url?)" >&2; exit 1; }
curl -s -o /dev/null --max-time 20 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -H "Mcp-Session-Id: $SID" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
# Build the request into a FILE (never argv) so chunk-sized payloads work.
REQF=$(mktemp)
if [ -n "$ARGSFILE" ]; then
  jq -cn --arg n "$TOOL" --slurpfile a "$ARGSFILE" \
    '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$n,arguments:$a[0]}}' >"$REQF"
else
  jq -cn --arg n "$TOOL" --argjson a "$ARGS" \
    '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$n,arguments:$a}}' >"$REQF"
fi
RESP=$(curl -s --max-time 60 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -H "Mcp-Session-Id: $SID" --data @"$REQF")
rm -f "$REQF"
if printf '%s' "$RESP" | jq -e '.result and (.result.isError | not)' >/dev/null 2>&1; then
  printf '%s' "$RESP" | jq -r '.result.content[]? | select(.type=="text") | .text'
  exit 0
fi
# isError:true is a tool rejection (queue full, bad project, oversize) surfaced
# as an HTTP-200 JSON-RPC result — must fail so the caller does NOT advance the
# offset and lose the chunk.
if printf '%s' "$RESP" | jq -e '.result.isError == true' >/dev/null 2>&1; then
  echo "mcp tool rejected: $(printf '%s' "$RESP" | jq -r '.result.content[]? | .text' 2>/dev/null | head -c 200)" >&2
  exit 1
fi
echo "mcp error: $(printf '%s' "$RESP" | jq -r '.error.message // "unknown"' 2>/dev/null | head -c 200)" >&2
exit 1
