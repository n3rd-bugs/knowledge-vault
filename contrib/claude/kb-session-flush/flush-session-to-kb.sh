#!/usr/bin/env bash
# Claude Code PreCompact/SessionEnd hook: flush this session's NEW transcript
# delta into the Knowledge Vault before compaction. Large sessions supported:
# deltas are sent as sequential <=900KB line-boundary chunks (up to 1 GB total),
# with the offset advancing per accepted chunk (mid-delta resume on failure). Detaches immediately so it
# never blocks/stalls compaction. Config lives in ~/.claude/kb-flush.env:
#   LOCAL  (KB host): KB_DIR=/path/to/kb-repo   [UV=/path/to/uv]
#   MCP    (client) : KB_MCP_URL=https://host/mcp   KB_TOKEN=...  (reuses the
#                     `claude mcp` connector registration — zero extra config)
#   REMOTE (client) : KB_REST_URL=https://host[:port]   KB_TOKEN=...
# Installed by the `kb-session-flush` skill. See that skill's README for setup.
set -uo pipefail

[ -n "${KB_HOOK_GUARD:-}" ] && exit 0   # we were spawned by a KB hook — never recurse
CFG="$HOME/.claude/kb-flush.env"
[ -f "$CFG" ] || exit 0
# shellcheck disable=SC1090
. "$CFG"
command -v jq >/dev/null 2>&1 || exit 0

INPUT=$(cat)
T=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty')
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty')
[ -n "$T" ] && [ -f "$T" ] || exit 0

# Project = the working repo's dir name, slugified (falls back to "sessions").
PROJECT=$(basename "${CWD:-sessions}" | tr '[:upper:]' '[:lower:]' \
  | tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')
[ -z "$PROJECT" ] && PROJECT=sessions
LOG="$HOME/.claude/hooks/flush-session.log"
export T CWD SID PROJECT LOG

if [ -n "${KB_DIR:-}" ]; then
  # LOCAL: trusted CLI enqueue — checkpointed, GPU-free (nightly drain extracts).
  UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
  export KB_DIR UV
  setsid bash -c '
    cd "$KB_DIR" 2>/dev/null || exit 0
    echo "$(date -Is) local $PROJECT $T"
    "$UV" run kb ingest-session "$T" --project "$PROJECT" --queue
  ' </dev/null >>"$LOG" 2>&1 &
elif { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; } && [ -n "${KB_TOKEN:-}" ]; then
  # MCP or REMOTE: submit the delta over the network, CHUNKED. Large sessions
  # (up to 1 GB) are sent as sequential ≤900 KB windows cut at line boundaries;
  # the per-session byte offset advances after EACH accepted chunk, so a failure
  # resumes mid-delta next firing. Chunks go via a temp file + jq --rawfile —
  # passing the text as an argv (--arg) hits the kernel's ~128 KB per-argument
  # limit long before 1 MB.
  STATE="$HOME/.claude/hooks/state"; mkdir -p "$STATE"
  OFFF="$STATE/${SID:-default}.offset"
  HELPER="$(dirname "$0")/kb-mcp-call.sh"
  export KB_MCP_URL="${KB_MCP_URL:-}" KB_REST_URL="${KB_REST_URL:-}" KB_TOKEN OFFF HELPER
  setsid bash -c '
    CHUNK=900000; MAX_TOTAL=1073741824
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    SIZE=$(stat -c%s "$T" 2>/dev/null || echo 0)
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    if [ $((SIZE - OFFSET)) -gt "$MAX_TOTAL" ]; then
      echo "$(date -Is) flush $PROJECT SKIP delta > 1GB ($((SIZE-OFFSET)) bytes)"; exit 0
    fi
    SENT=0
    while [ "$OFFSET" -lt "$SIZE" ]; do
      TMPC=$(mktemp)
      tail -c +$((OFFSET+1)) "$T" | head -c "$CHUNK" >"$TMPC"
      BYTES=$(stat -c%s "$TMPC" 2>/dev/null || wc -c <"$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      # Trim to the last complete line unless this window reaches EOF (a
      # partial trailing line is re-read next firing, never lost).
      if [ $((OFFSET + BYTES)) -lt "$SIZE" ] && [ "$(tail -c1 "$TMPC")" != "" ]; then
        PART=$(tail -n1 "$TMPC" | wc -c)
        KEEP=$((BYTES - PART))
        [ "$KEEP" -gt 0 ] || KEEP=$BYTES   # one monster line: send as-is
        head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"
        BYTES=$KEEP
      fi
      if [ -n "$KB_MCP_URL" ]; then
        # args go via FILE (@) — a ~900KB argv would hit the kernel arg limit.
        # Chunks are gzip+base64 encoded when the tools exist (10x less wire);
        # plaintext fallback keeps minimal systems working.
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1; then
          gzip -c "$TMPC" | base64 -w0 >"$TMPC.gz64"
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC.gz64" \
            "{project:\$p,transcript_gz:\$t,kind:\"session\"}" >"$TMPC.args"
          rm -f "$TMPC.gz64"
        else
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC" \
            "{project:\$p,transcript:\$t,kind:\"session\"}" >"$TMPC.args"
        fi
        bash "$HELPER" kb_ingest_session "@$TMPC.args" >/dev/null 2>>"$LOG" && CODE=202 || CODE=err
        rm -f "$TMPC.args"
      else
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1; then
          gzip -c "$TMPC" | base64 -w0 >"$TMPC.gz64"
          BODYF="$TMPC.body"
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC.gz64" \
            "{project:\$p,kind:\"session\",transcript_gz:\$t}" >"$BODYF"
          rm -f "$TMPC.gz64"
        else
          BODYF="$TMPC.body"
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC" \
            "{project:\$p,kind:\"session\",transcript:\$t}" >"$BODYF"
        fi
        CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 30 \
             -X POST "$KB_REST_URL/api/ingest-session" \
             -H "Authorization: Bearer $KB_TOKEN" -H "Content-Type: application/json" \
             --data @"$BODYF")
        rm -f "$BODYF"
      fi
      rm -f "$TMPC"
      case "$CODE" in
        2*|202) OFFSET=$((OFFSET + BYTES)); echo "$OFFSET" >"$OFFF"; SENT=$((SENT + BYTES)) ;;
        *) echo "$(date -Is) flush $PROJECT chunk FAILED ($CODE) at offset $OFFSET — will resume"; break ;;
      esac
    done
    [ "$SENT" -gt 0 ] && echo "$(date -Is) flush $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)"
  ' </dev/null >>"$LOG" 2>&1 &
elif [ "${KB_CONNECTOR:-0}" = "1" ] && [ -n "${CLAUDE_BIN:-}" ]; then
  # CONNECTOR: zero-config — uses the claude.ai KB connector bridged into
  # Claude Code (cloud-held OAuth; nothing local). A headless `claude -p` call
  # relays each chunk into kb_ingest_session. The MODEL is the data path, so
  # chunks are small (~24 KB echo fidelity) and each flush costs Claude usage —
  # bearer modes (MCP/REST) are the efficient path; this is the no-setup one.
  STATE="$HOME/.claude/hooks/state"; mkdir -p "$STATE"
  OFFF="$STATE/${SID:-default}.offset"
  export CLAUDE_BIN OFFF
  export KB_CONNECTOR_CHUNK="${KB_CONNECTOR_CHUNK:-24000}"
  export KB_CONNECTOR_MAX="${KB_CONNECTOR_MAX:-240000}"
  setsid bash -c '
    export KB_HOOK_GUARD=1   # children (claude -p) must not re-trigger KB hooks
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    SIZE=$(stat -c%s "$T" 2>/dev/null || echo 0)
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    SENT=0
    while [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -lt "$KB_CONNECTOR_MAX" ]; do
      TMPC=$(mktemp)
      tail -c +$((OFFSET+1)) "$T" | head -c "$KB_CONNECTOR_CHUNK" >"$TMPC"
      BYTES=$(stat -c%s "$TMPC" 2>/dev/null || wc -c <"$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      if [ $((OFFSET + BYTES)) -lt "$SIZE" ] && [ "$(tail -c1 "$TMPC")" != "" ]; then
        PART=$(tail -n1 "$TMPC" | wc -c); KEEP=$((BYTES - PART))
        [ "$KEEP" -gt 0 ] || KEEP=$BYTES
        head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"; BYTES=$KEEP
      fi
      OUT=$("$CLAUDE_BIN" -p "The stdin content is a session transcript delta. Call the kb_ingest_session tool (mcp__claude_ai_Knowledge_Base__kb_ingest_session, or any tool named kb_ingest_session) with project='"'"'$PROJECT'"'"', kind='"'"'session'"'"', transcript = the stdin content VERBATIM. Reply ONLY with the queue id number, or FAILED."             --allowedTools "mcp__claude_ai_Knowledge_Base__kb_ingest_session,mcp__kb__kb_ingest_session"             <"$TMPC" 2>>"$LOG" | tail -1)
      rm -f "$TMPC"
      case "$OUT" in
        *[0-9]*) OFFSET=$((OFFSET + BYTES)); echo "$OFFSET" >"$OFFF"; SENT=$((SENT + BYTES)) ;;
        *) echo "$(date -Is) connector $PROJECT chunk FAILED ($OUT) at offset $OFFSET — will resume"; break ;;
      esac
    done
    [ "$SENT" -gt 0 ] && echo "$(date -Is) connector $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)"
    [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -ge "$KB_CONNECTOR_MAX" ] &&       echo "$(date -Is) connector $PROJECT per-firing cap reached; $((SIZE-OFFSET)) bytes resume next firing"
  ' </dev/null >>"$LOG" 2>&1 &
fi
exit 0
