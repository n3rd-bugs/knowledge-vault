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
# Windows/Git Bash (KB item 496): a co-located jq (installer drops one next to
# the hooks) must resolve even when Claude Code inherited a PATH without it.
HOOKDIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"; PATH="$HOOKDIR:$PATH"
command -v jq >/dev/null 2>&1 || exit 0
# setsid is absent on Git Bash — nohup is the detach fallback (item 496).
DETACH="setsid"; command -v setsid >/dev/null 2>&1 || DETACH="nohup"

INPUT=$(cat)
T=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty')
CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // empty')
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty')
EV=$(printf '%s' "$INPUT" | jq -r '.hook_event_name // empty')
# Native Windows event paths (C:\... or C:/...) must be cygpath'ed — basename
# on a backslash path yields the whole drive path (item 496; two-alternative
# case pattern required, the single bracket-class form does not match).
case "$T" in
  [A-Za-z]:\\*|[A-Za-z]:/*) T=$(cygpath "$T" 2>/dev/null || printf '%s' "$T") ;;
esac
case "$CWD" in
  [A-Za-z]:\\*|[A-Za-z]:/*) CWD=$(cygpath "$CWD" 2>/dev/null || printf '%s' "$CWD") ;;
esac
[ -n "$T" ] && [ -f "$T" ] || exit 0

# Project = the working repo's dir name, slugified (falls back to "sessions").
PROJECT=$(basename "${CWD:-sessions}" | tr '[:upper:]' '[:lower:]' \
  | tr -c 'a-z0-9-' '-' | sed 's/-\{2,\}/-/g;s/^-//;s/-$//')
[ -z "$PROJECT" ] && PROJECT=sessions
# Git context for code_ref anchoring: kb_ingest_session accepts repo/branch/
# commit and the drain stamps a code_ref from them. Local plumbing reads only
# (ms-cheap, no network); a non-git CWD degrades silently to empty. GITCTX is
# the ready-made JSON fragment the network arg builders merge in — empty
# fields are OMITTED entirely (never sent as "").
REPO=$(git -C "${CWD:-.}" remote get-url origin 2>/dev/null)
[ -n "$REPO" ] || REPO=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null)
REPO=${REPO%/}; REPO=${REPO%.git}; REPO=${REPO##*/}; REPO=${REPO##*:}
BRANCH=$(git -C "${CWD:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$BRANCH" = "HEAD" ] && BRANCH=""   # detached HEAD names no branch
COMMIT=$(git -C "${CWD:-.}" rev-parse --short HEAD 2>/dev/null)
GITCTX=$(jq -n --arg r "$REPO" --arg b "$BRANCH" --arg c "$COMMIT" \
  '(if $r != "" then {repo:$r} else {} end)
   + (if $b != "" then {branch:$b} else {} end)
   + (if $c != "" then {commit:$c} else {} end)')
[ -n "$GITCTX" ] || GITCTX='{}'
LOG="$HOME/.claude/hooks/flush-session.log"
export T CWD SID PROJECT LOG REPO BRANCH COMMIT GITCTX

# Opportunistic housekeeping — bound our OWN state/log growth so a daily-driver
# machine doesn't accumulate thousands of dead .offset files and an unrotated
# multi-MB log. Cheap (runs before the detach) and scoped strictly to the hooks'
# own dirs; never touches anything else.
STATE="$HOME/.claude/hooks/state"; mkdir -p "$STATE" 2>/dev/null || true
find "$STATE" -name '*.offset' -mtime +30 -delete 2>/dev/null || true
if [ -f "$LOG" ]; then
  LSIZE=$(stat -c%s "$LOG" 2>/dev/null || stat -f%z "$LOG" 2>/dev/null || wc -c <"$LOG" | tr -d " ")
  case "$LSIZE" in ""|*[!0-9]*) LSIZE=0 ;; esac
  if [ "$LSIZE" -gt 1048576 ]; then
    tail -n 500 "$LOG" >"$LOG.t" 2>/dev/null && mv "$LOG.t" "$LOG" 2>/dev/null || rm -f "$LOG.t" 2>/dev/null
  fi
fi

if [ -n "${KB_DIR:-}" ]; then
  # LOCAL: trusted CLI enqueue — checkpointed, GPU-free (nightly drain extracts).
  UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
  export KB_DIR UV
  "$DETACH" bash -c '
    echo "$(date -Is) local $PROJECT $T"
    cd "$KB_DIR" 2>/dev/null || { echo "$(date -Is) local $PROJECT BUG: KB_DIR unreachable ($KB_DIR)"; exit 0; }
    set --   # git-context flags for code_ref — empty values omitted entirely
    [ -n "$REPO" ] && set -- "$@" --repo "$REPO"
    [ -n "$BRANCH" ] && set -- "$@" --branch "$BRANCH"
    [ -n "$COMMIT" ] && set -- "$@" --commit "$COMMIT"
    if "$UV" run kb ingest-session "$T" --project "$PROJECT" --queue "$@"; then
      # Log the same fully-drained success marker the network paths emit, so a
      # prior stale FAILED self-clears in the pre-brief warn logic instead of
      # resurfacing as noise for ~40 firings (LOCAL never chunks — a successful
      # enqueue is always a full drain, hence offset SZ/SZ).
      SZ=$(stat -c%s "$T" 2>/dev/null || stat -f%z "$T" 2>/dev/null || wc -c <"$T" | tr -d " ")
      case "$SZ" in ""|*[!0-9]*) SZ=0 ;; esac
      echo "$(date -Is) local $PROJECT ok bytes=$SZ (offset $SZ/$SZ)"
    else
      echo "$(date -Is) local $PROJECT FAILED: kb ingest-session errored"
    fi
  ' </dev/null >>"$LOG" 2>&1 &
elif { [ -n "${KB_MCP_URL:-}" ] || [ -n "${KB_REST_URL:-}" ]; } && [ -n "${KB_TOKEN:-}" ]; then
  # MCP or REMOTE: submit the delta over the network, CHUNKED. Large sessions
  # (up to 1 GB) are sent as sequential ≤900 KB windows cut at line boundaries;
  # the per-session byte offset advances after EACH accepted chunk, so a failure
  # resumes mid-delta next firing. Chunks go via a temp file + jq --rawfile —
  # passing the text as an argv (--arg) hits the kernel's ~128 KB per-argument
  # limit long before 1 MB.
  STATE="$HOME/.claude/hooks/state"; mkdir -p "$STATE"
  OKEY=$(printf '%s' "${SID:-$T}" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120)
  OFFF="$STATE/${OKEY:-default}.offset"
  HELPER="$(dirname "$0")/kb-mcp-call.sh"
  export KB_MCP_URL="${KB_MCP_URL:-}" KB_REST_URL="${KB_REST_URL:-}" KB_TOKEN OFFF HELPER EV
  "$DETACH" bash -c '
    CHUNK=900000; MAX_PER_FIRING=209715200   # 200 MB/firing; rest resumes next firing
    [ "$EV" = "SessionEnd" ] && MAX_PER_FIRING=1099511627776   # terminal firing: drain all
    _fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" | tr -d " "; }
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    case "$OFFSET" in ""|*[!0-9]*) OFFSET=0 ;; esac   # empty/corrupt offset -> restart
    SIZE=$(_fsize "$T"); case "$SIZE" in ""|*[!0-9]*) SIZE=0 ;; esac
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    SENT=0
    while [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -lt "$MAX_PER_FIRING" ]; do
      TMPC=$(mktemp) || { echo "$(date -Is) flush $PROJECT BUG: mktemp failed"; break; }
      tail -c +$((OFFSET+1)) "$T" | head -c "$CHUNK" >"$TMPC"
      BYTES=$(_fsize "$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      # ALWAYS cut on a line boundary: trim to the last newline unless the chunk
      # already ends in one. Robust to the transcript growing after SIZE was read
      # (an EOF-crossing window is still trimmed), so the offset never advances
      # into the middle of a JSONL line and no straddled message is lost. Only a
      # single line longer than CHUNK is sent unterminated (server tolerates it).
      if [ "$(tail -c1 "$TMPC")" != "" ]; then
        PART=$(tail -n1 "$TMPC" | wc -c)
        KEEP=$((BYTES - PART))
        if [ "$KEEP" -gt 0 ]; then
          head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"
          BYTES=$KEEP
        elif [ "$BYTES" -lt "$CHUNK" ]; then
          # No newline anywhere AND smaller than a full chunk => a single line still
          # being written. HOLD it (break, no advance) so the COMPLETED line is picked
          # up next firing, instead of shipping a half-line the drain drops as malformed
          # JSON. EXCEPTION: the terminal SessionEnd firing at EOF has no next firing —
          # send the final line as-is rather than lose it silently (the drain still
          # drops it only if truly malformed, so sending never loses data).
          if [ "$EV" = "SessionEnd" ] && [ $((OFFSET + BYTES)) -ge "$SIZE" ]; then
            :
          else
            rm -f "$TMPC"; break
          fi
        fi
      fi
      if [ -n "$KB_MCP_URL" ]; then
        # args go via FILE (@) — a ~900KB argv would hit the kernel arg limit.
        # Chunks are gzip+base64 encoded when the tools exist (10x less wire);
        # plaintext fallback keeps minimal systems working.
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1 \
           && gzip -c "$TMPC" | base64 | tr -d "\n" >"$TMPC.gz64" && [ -s "$TMPC.gz64" ]; then
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC.gz64" --argjson g "$GITCTX" \
            "{project:\$p,transcript_gz:\$t,kind:\"session\"} + \$g" >"$TMPC.args"
          rm -f "$TMPC.gz64"
        else
          rm -f "$TMPC.gz64"
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC" --argjson g "$GITCTX" \
            "{project:\$p,transcript:\$t,kind:\"session\"} + \$g" >"$TMPC.args"
        fi
        if [ ! -s "$TMPC.args" ] || ! jq -e . "$TMPC.args" >/dev/null 2>&1; then
          echo "$(date -Is) flush $PROJECT BUG: request body build failed (empty/invalid) at offset $OFFSET — NOT submitted, will retry"
          rm -f "$TMPC.args" "$TMPC"; break
        fi
        bash "$HELPER" kb_ingest_session "@$TMPC.args" >/dev/null 2>>"$LOG" && CODE=202 || CODE=err
        rm -f "$TMPC.args"
      else
        BODYF="$TMPC.body"
        if command -v gzip >/dev/null 2>&1 && command -v base64 >/dev/null 2>&1 \
           && gzip -c "$TMPC" | base64 | tr -d "\n" >"$TMPC.gz64" && [ -s "$TMPC.gz64" ]; then
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC.gz64" --argjson g "$GITCTX" \
            "{project:\$p,kind:\"session\",transcript_gz:\$t} + \$g" >"$BODYF"
          rm -f "$TMPC.gz64"
        else
          rm -f "$TMPC.gz64"
          BODYF="$TMPC.body"
          jq -n --arg p "$PROJECT" --rawfile t "$TMPC" --argjson g "$GITCTX" \
            "{project:\$p,kind:\"session\",transcript:\$t} + \$g" >"$BODYF"
        fi
        if [ ! -s "$BODYF" ] || ! jq -e . "$BODYF" >/dev/null 2>&1; then
          echo "$(date -Is) flush $PROJECT BUG: request body build failed (empty/invalid) at offset $OFFSET — NOT submitted, will retry"
          rm -f "$BODYF" "$TMPC"; break
        fi
        CODE=$(curl -sS -o "$TMPC.resp" -w "%{http_code}" --max-time 30 \
             -X POST "$KB_REST_URL/api/ingest-session" \
             -H "Authorization: Bearer $KB_TOKEN" -H "Content-Type: application/json" \
             --data @"$BODYF")
        # A flaked response read (000) whose body still carries a queue id was
        # ACCEPTED by the server (KB item 497) — count it as success.
        case "$CODE" in 2*) : ;; *) grep -q "queue_id" "$TMPC.resp" 2>/dev/null && CODE=202 ;; esac
        rm -f "$BODYF" "$TMPC.resp"
      fi
      rm -f "$TMPC"
      case "$CODE" in
        2*|202) OFFSET=$((OFFSET + BYTES)); printf '%s' "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)) ;;
        *) echo "$(date -Is) flush $PROJECT chunk FAILED ($CODE) at offset $OFFSET — will resume"; break ;;
      esac
    done
    # Truthful summary: only a FULLY drained firing (OFFSET>=SIZE) logs the
    # " ok bytes=" success line the pre-brief matches. A firing that broke early
    # (chunk FAILED, held partial line, or per-firing cap) logs a "partial" line
    # that does NOT match that pattern — so a failed chunk followed by earlier
    # successes can no longer masquerade as success and strand the tail forever
    # on the terminal SessionEnd firing (HIGH-1).
    if [ "$SENT" -gt 0 ]; then
      if [ "$OFFSET" -ge "$SIZE" ]; then
        echo "$(date -Is) flush $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)"
      else
        echo "$(date -Is) flush $PROJECT partial bytes=$SENT (offset $OFFSET/$SIZE) — will resume"
      fi
    fi
    # Terminal event, fully drained: drop this session'"'"'s offset file (else it is
    # dead weight forever). The 30-day sweep at hook start catches the rest.
    [ "$EV" = "SessionEnd" ] && [ "$OFFSET" -ge "$SIZE" ] && rm -f "$OFFF" 2>/dev/null
  ' </dev/null >>"$LOG" 2>&1 &
elif [ "${KB_CONNECTOR:-0}" = "1" ] && [ -n "${CLAUDE_BIN:-}" ]; then
  # CONNECTOR: zero-config — uses the claude.ai KB connector bridged into
  # Claude Code (cloud-held OAuth; nothing local). A headless `claude -p` call
  # relays each chunk into kb_ingest_session. The MODEL is the data path, so
  # chunks are small (~24 KB echo fidelity) and each flush costs Claude usage —
  # bearer modes (MCP/REST) are the efficient path; this is the no-setup one.
  STATE="$HOME/.claude/hooks/state"; mkdir -p "$STATE"
  OKEY=$(printf '%s' "${SID:-$T}" | tr -c 'A-Za-z0-9._-' '_' | tail -c 120)
  OFFF="$STATE/${OKEY:-default}.offset"
  export CLAUDE_BIN OFFF EV
  export KB_CONNECTOR_CHUNK="${KB_CONNECTOR_CHUNK:-24000}"
  export KB_CONNECTOR_MAX="${KB_CONNECTOR_MAX:-240000}"
  "$DETACH" bash -c '
    export KB_HOOK_GUARD=1   # children (claude -p) must not re-trigger KB hooks
    _fsize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1" 2>/dev/null || wc -c <"$1" | tr -d " "; }
    OFFSET=$(cat "$OFFF" 2>/dev/null || echo 0)
    case "$OFFSET" in ""|*[!0-9]*) OFFSET=0 ;; esac
    SIZE=$(_fsize "$T"); case "$SIZE" in ""|*[!0-9]*) SIZE=0 ;; esac
    [ "$SIZE" -le "$OFFSET" ] && exit 0
    SENT=0
    CAP="$KB_CONNECTOR_MAX"; [ "$EV" = "SessionEnd" ] && CAP=1099511627776   # drain all at end
    while [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -lt "$CAP" ]; do
      TMPC=$(mktemp)
      tail -c +$((OFFSET+1)) "$T" | head -c "$KB_CONNECTOR_CHUNK" >"$TMPC"
      BYTES=$(stat -c%s "$TMPC" 2>/dev/null || wc -c <"$TMPC")
      [ "$BYTES" -gt 0 ] || { rm -f "$TMPC"; break; }
      if [ "$(tail -c1 "$TMPC")" != "" ]; then
        PART=$(tail -n1 "$TMPC" | wc -c); KEEP=$((BYTES - PART))
        if [ "$KEEP" -gt 0 ]; then
          head -c "$KEEP" "$TMPC" >"$TMPC.k" && mv "$TMPC.k" "$TMPC"; BYTES=$KEEP
        elif [ "$BYTES" -lt "$KB_CONNECTOR_CHUNK" ]; then
          # single still-being-written partial line: hold for the next firing rather
          # than ship a half-line — UNLESS this is the terminal SessionEnd firing at
          # EOF (no next firing), where we send the final line as-is to avoid losing it.
          if [ "$EV" = "SessionEnd" ] && [ $((OFFSET + BYTES)) -ge "$SIZE" ]; then
            :
          else
            rm -f "$TMPC"; break
          fi
        fi
      fi
      OUT=$("$CLAUDE_BIN" -p "The stdin content is a session transcript delta. Call the kb_ingest_session tool (mcp__claude_ai_Knowledge_Base__kb_ingest_session, or any tool named kb_ingest_session) with project='"'"'$PROJECT'"'"', kind='"'"'session'"'"', transcript = the stdin content VERBATIM. Reply ONLY with the queue id number, or FAILED."             --allowedTools "mcp__claude_ai_Knowledge_Base__kb_ingest_session,mcp__kb__kb_ingest_session"             <"$TMPC" 2>>"$LOG" | tail -1)
      rm -f "$TMPC"
      # OUT is untrusted model output: a headless claude -p run whose stdin is the
      # raw transcript delta, so an adversarial tool-result/webpage seen during the
      # session can steer it. Classify on the raw $OUT, but LOG a sanitized copy —
      # the pre-brief later lifts FAILED lines verbatim into the next session
      # context, so an un-truncated line is a prompt-injection + context-bloat
      # vector (LOW-2). Strip control chars + backticks, cap at 200 chars.
      OUTLOG=$(printf "%s" "$OUT" | tr -d "[:cntrl:]\140" | cut -c1-200)
      case "$OUT" in
        *[Ff][Aa][Ii][Ll]*|*[Ee]rror*|*NOTOOL*|*unavailable*)
          echo "$(date -Is) connector $PROJECT chunk FAILED ($OUTLOG) at offset $OFFSET — will resume"; break ;;
        *[0-9]*) OFFSET=$((OFFSET + BYTES)); printf '%s' "$OFFSET" >"$OFFF.t" && mv "$OFFF.t" "$OFFF"; SENT=$((SENT + BYTES)) ;;
        *) echo "$(date -Is) connector $PROJECT chunk FAILED (no id: $OUTLOG) at offset $OFFSET — will resume"; break ;;
      esac
    done
    # Truthful summary (HIGH-1): only a fully-drained firing logs " ok bytes=";
    # an early break (chunk FAILED / held partial line / per-firing cap) logs a
    # "partial" line the pre-brief does not read as success.
    if [ "$SENT" -gt 0 ]; then
      if [ "$OFFSET" -ge "$SIZE" ]; then
        echo "$(date -Is) connector $PROJECT ok bytes=$SENT (offset $OFFSET/$SIZE)"
      else
        echo "$(date -Is) connector $PROJECT partial bytes=$SENT (offset $OFFSET/$SIZE) — will resume"
      fi
    fi
    [ "$OFFSET" -lt "$SIZE" ] && [ "$SENT" -ge "$KB_CONNECTOR_MAX" ] &&       echo "$(date -Is) connector $PROJECT per-firing cap reached; $((SIZE-OFFSET)) bytes resume next firing"
    # Terminal event, fully drained: drop this session'"'"'s offset file.
    [ "$EV" = "SessionEnd" ] && [ "$OFFSET" -ge "$SIZE" ] && rm -f "$OFFF" 2>/dev/null
  ' </dev/null >>"$LOG" 2>&1 &
fi
exit 0
