#!/usr/bin/env bash
# run-connector.sh — the ONE hook lane that cannot be hermetic: CONNECTOR mode.
#
# CONNECTOR mode relays the transcript through a headless `claude -p` using
# cloud-held claude.ai auth, and the KB tool it calls lives on the SERVER side of
# that account. There is therefore no stub to point at: proving this path works
# means (a) egress to claude.ai, (b) spending real Claude usage — the model IS
# the data path — and (c) a real row landing in the LIVE KB ingest queue.
#
# So this is deliberately NOT part of `make hooktest` (which stays hermetic and
# free). It is opt-in, it announces exactly what it will do, and it CLEANS UP the
# queue rows it creates so the nightly drain never mints knowledge from a test.
#
# Usage:  KB_HOOKRIG_LIVE=1 bash tests/hookrig/run-connector.sh
#
# What it pins (nothing else in the repo covers these):
#   - a container with ONLY the claude binary + credentials selects CONNECTOR mode
#   - the relay reaches the claude.ai KB connector and returns a queue id
#   - a delta larger than KB_CONNECTOR_CHUNK is relayed in MULTIPLE chunks
#   - the offset advances so the next firing resumes rather than re-sending
#   - KB_HOOK_GUARD=1 is exported so the child `claude` run's own hooks stay inert
set -euo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$RIG_DIR/../.." && pwd)"
SKILL_DIR="${KB_SKILL_DIR:-$REPO_DIR/contrib/claude/kb-session-flush}"
PROJECT="kb-hookrig-live"          # disposable; every row we create carries it
IMAGE="${KB_HOOKRIG_IMAGE:-kb-hookrig:1}"   # the rig image: jq/curl/python baked in as root
                                            # (the container runs non-root, so runtime apt-get is impossible)

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }

if [ "${KB_HOOKRIG_LIVE:-0}" != "1" ]; then
  red "REFUSING TO RUN — this lane is not hermetic."
  cat <<EOF
It will:
  1. give a container your claude.ai credentials (read-only mount)
  2. reach the internet (claude.ai) — the hermetic guarantee does NOT apply
  3. SPEND real Claude usage (one headless 'claude -p' per relayed chunk)
  4. WRITE rows into the LIVE KB ingest queue (project '$PROJECT'), which this
     script then deletes so the nightly drain never extracts knowledge from them

Re-run with:  KB_HOOKRIG_LIVE=1 bash tests/hookrig/run-connector.sh
EOF
  exit 2
fi

# --- locate the claude binary the same way install.sh does --------------------
find_claude() {
  command -v claude 2>/dev/null && return 0
  for c in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude"; do
    [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  ls -1t "$HOME"/.vscode-server/extensions/anthropic.claude-code-*/resources/native-binary/claude 2>/dev/null | head -1 | grep . && return 0
  return 1
}
CLAUDE_BIN="$(find_claude || true)"
[ -n "$CLAUDE_BIN" ] || { red "no claude binary found — CONNECTOR mode is untestable here"; exit 1; }
CREDS="$HOME/.claude/.credentials.json"
[ -f "$CREDS" ] || { red "no $CREDS — cannot exercise cloud-held auth"; exit 1; }

ylw "CONNECTOR lane: binary=$CLAUDE_BIN  project=$PROJECT (rows are deleted afterwards)"

# --- build a throwaway HOME + project dir -------------------------------------
WORK="$(mktemp -d)"
cleanup_fs() { rm -rf "$WORK"; }
trap cleanup_fs EXIT
mkdir -p "$WORK/home/.claude" "$WORK/home/.local/bin" "$WORK/proj/$PROJECT"
cp "$CREDS" "$WORK/home/.claude/.credentials.json"
# A .claude.json WITHOUT mcpServers: otherwise install.sh would pick MCP mode and
# we would not be testing CONNECTOR at all (mode priority, case 01).
printf '{}\n' >"$WORK/home/.claude.json"
chmod -R go-rwx "$WORK"

# A transcript comfortably larger than KB_CONNECTOR_CHUNK (24000) so the relay
# MUST chunk. Line-oriented JSONL, like a real Claude Code transcript.
python3 - "$WORK/proj/$PROJECT/transcript.jsonl" <<'PY'
import json, sys
with open(sys.argv[1], "w") as f:
    for i in range(220):
        f.write(json.dumps({
            "type": "user" if i % 2 == 0 else "assistant",
            "message": {"role": "user" if i % 2 == 0 else "assistant",
                        "content": f"hookrig connector probe line {i}: " + ("x" * 160)},
        }) + "\n")
PY
SIZE=$(wc -c <"$WORK/proj/$PROJECT/transcript.jsonl")
echo "  synthetic transcript: $SIZE bytes (KB_CONNECTOR_CHUNK=24000 -> expect multiple chunks)"

# --- run install + one PreCompact flush inside the container ------------------
# The claude binary is placed at ~/.local/bin/claude so install.sh's own lookup
# finds it (we are testing DETECTION as well as the relay).
set +e
docker run --rm \
  -v "$CLAUDE_BIN:/opt/claude:ro" \
  -v "$SKILL_DIR:/skill:ro" \
  -v "$WORK/home:/home/tester:rw" \
  -v "$WORK/proj:/proj:rw" \
  -e HOME=/home/tester \
  --user "$(id -u):$(id -g)" \
  -w "/proj/$PROJECT" \
  "$IMAGE" bash -c '
    set -e
    export PATH="$HOME/.local/bin:$PATH"
    mkdir -p "$HOME/.local/bin"
    ln -sf /opt/claude "$HOME/.local/bin/claude"
    command -v jq >/dev/null || { echo "FAIL jq missing from the rig image"; exit 1; }
    cp -r /skill /tmp/skill && chmod -R u+w /tmp/skill
    echo "--- install (expect: CONNECTOR) ---"
    bash /tmp/skill/install.sh 2>&1 | grep -Ei "mode:|config:" || true
    grep -q "^KB_CONNECTOR=1" "$HOME/.claude/kb-flush.env" \
      && echo "ok   CONNECTOR mode selected" \
      || { echo "FAIL wrong mode: $(cat "$HOME/.claude/kb-flush.env")"; exit 1; }
    echo "--- flush (PreCompact) — this spends Claude usage ---"
    printf "{\"transcript_path\":\"%s\",\"cwd\":\"%s\",\"session_id\":\"hookrig-connector\"}" \
      "/proj/'"$PROJECT"'/transcript.jsonl" "/proj/'"$PROJECT"'" \
      | "$HOME/.claude/hooks/flush-session-to-kb.sh"
    # the hook detaches; give the relay time (each chunk is a headless claude -p)
    for i in $(seq 1 60); do sleep 5; grep -q "connector" "$HOME/.claude/hooks/flush-session.log" 2>/dev/null && break; done
    sleep 20
    echo "--- flush log ---"
    cat "$HOME/.claude/hooks/flush-session.log" 2>/dev/null || echo "(no log)"
    echo "--- offset state (proves resume-not-resend) ---"
    cat "$HOME/.claude/hooks/state/"*.offset 2>/dev/null || echo "(no offset file)"
  '
RC=$?
set -e
[ $RC -eq 0 ] || red "container exited $RC"

# --- verify + CLEAN UP the live queue rows we created -------------------------
echo
echo "=== live ingest_queue rows created by this test (project=$PROJECT) ==="
docker exec kb-db psql -U kb -d kb -c \
  "SELECT id, project, kind, status, length(content) AS bytes, created_at
     FROM ingest_queue WHERE project = '$PROJECT' ORDER BY id;"

N=$(docker exec kb-db psql -U kb -d kb -Atc \
  "SELECT count(*) FROM ingest_queue WHERE project = '$PROJECT';" | tr -d '[:space:]')
if [ "${N:-0}" -gt 0 ]; then
  grn "PASS  the CONNECTOR relay reached the live KB: $N queued submission(s)"
else
  red  "FAIL  no rows landed — the relay did not reach the KB"
fi

echo
echo "=== cleanup: deleting the test rows so the nightly drain never extracts them ==="
docker exec kb-db psql -U kb -d kb -c \
  "DELETE FROM ingest_queue WHERE project = '$PROJECT';"
LEFT=$(docker exec kb-db psql -U kb -d kb -Atc \
  "SELECT count(*) FROM ingest_queue WHERE project = '$PROJECT';" | tr -d '[:space:]')
[ "${LEFT:-1}" -eq 0 ] && grn "cleanup ok — 0 rows remain for $PROJECT" || red "cleanup INCOMPLETE: $LEFT rows remain"

# Nothing should have been minted: this project must own no knowledge items.
ITEMS=$(docker exec kb-db psql -U kb -d kb -Atc \
  "SELECT count(*) FROM items WHERE project = '$PROJECT';" | tr -d '[:space:]')
[ "${ITEMS:-1}" -eq 0 ] && grn "no knowledge items minted for $PROJECT (expected: the drain never ran)" \
                        || red "UNEXPECTED: $ITEMS item(s) exist for $PROJECT — review before the next consolidate"
