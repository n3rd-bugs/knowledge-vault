#!/usr/bin/env bash
# Minimal MCP streamable-HTTP client for the KB hooks: initialize, then call one
# tool. Reads KB_MCP_URL + KB_TOKEN from the environment. Prints the tool's text
# result on success (exit 0); prints an error to stderr on failure (exit 1).
# Usage: kb-mcp-call.sh <tool_name> <arguments_json>
set -uo pipefail
TOOL="${1:?tool name}"; ARGS="${2:-}"; [ -n "$ARGS" ] || ARGS="{}"
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
REQ=$(jq -cn --arg n "$TOOL" --argjson a "$ARGS" \
  '{jsonrpc:"2.0",id:2,method:"tools/call",params:{name:$n,arguments:$a}}')
RESP=$(printf '%s' "$REQ" | curl -s --max-time 40 -X POST "$KB_MCP_URL" -H "$AUTH" -H "$CT" -H "$AC" \
  -H "Mcp-Session-Id: $SID" --data @-)
if printf '%s' "$RESP" | jq -e '.result' >/dev/null 2>&1; then
  printf '%s' "$RESP" | jq -r '.result.content[]? | select(.type=="text") | .text'
  exit 0
fi
echo "mcp error: $(printf '%s' "$RESP" | jq -r '.error.message // "unknown"' 2>/dev/null | head -c 200)" >&2
exit 1
