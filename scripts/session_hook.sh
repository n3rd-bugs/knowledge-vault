#!/usr/bin/env bash
# ============================================================================
# DEPRECATED (2026-07) — superseded by contrib/claude/kb-session-flush/.
#
# This installs a per-project `Stop` hook running `kb ingest-session`. The
# kb-session-flush skill now covers the same job better (PreCompact + SessionEnd
# flush with checkpointing + scrubbing, plus SessionStart/UserPromptSubmit
# pre-brief). Prefer:  bash contrib/claude/kb-session-flush/install.sh
#
# Do NOT run both on one machine: the two assembly paths produce differently
# assembled text, defeating the content-hash dedupe in ingest_queue.enqueue, so
# each session gets extracted by the 14B twice. Kept only for reference.
# ============================================================================
# session_hook.sh — opt-in Claude Code Stop hook installer (PLAN.md §7.2, Verdict V5).
#
# Run this INSIDE a target project's working tree. It merges a `Stop` hook into
# THAT project's ./.claude/settings.json only — it never touches ~/.claude and
# never enables global transcript surveillance. On Stop, the hook runs
# `kb ingest-session` in the background, stamping the current repo/branch/commit.
#
# Usage:  scripts/session_hook.sh <project-name>
set -euo pipefail

PROJECT="${1:-}"
if [[ -z "${PROJECT}" ]]; then
  echo "usage: $(basename "$0") <project-name>   (run inside the target project dir)" >&2
  exit 2
fi

# Per-project settings only — explicitly local, never ${HOME}/.claude (V5/SEC).
SETTINGS_DIR="$(pwd)/.claude"
SETTINGS="${SETTINGS_DIR}/settings.json"
mkdir -p "${SETTINGS_DIR}"

# Refuse to touch an existing settings.json that is not a JSON object: merging
# would either fail silently (jq) or clobber the user's file (python coerce).
# Validate first and bail loudly, leaving the file untouched (review finding).
if [[ -f "${SETTINGS}" ]]; then
  if ! python3 -c 'import json,sys; sys.exit(0 if isinstance(json.load(open(sys.argv[1])), dict) else 1)' \
      "${SETTINGS}" 2>/dev/null; then
    echo "error: ${SETTINGS} is not a valid JSON object; refusing to modify it" >&2
    exit 1
  fi
fi

# Resolve `kb` at install time so the hook does not depend on the session PATH.
KB_BIN="$(command -v kb || echo kb)"

# Claude Code delivers the transcript path as `transcript_path` in the hook's
# STDIN JSON (verified 2.1.x; PLAN §7.2's $CLAUDE_TRANSCRIPT_PATH env var does
# not exist — kept only as a fallback for other versions). The command string
# is stored verbatim; $tp and the git sub-shells are evaluated when the hook
# FIRES. Runs backgrounded so Stop is never blocked by ingestion.
CMD="tp=\"\$(python3 -c 'import sys,json;print(json.load(sys.stdin).get(\"transcript_path\",\"\"))' 2>/dev/null)\";"
CMD+=" tp=\"\${tp:-\$CLAUDE_TRANSCRIPT_PATH}\";"
CMD+=" [ -n \"\$tp\" ] && ( \"${KB_BIN}\" ingest-session \"\$tp\" --project \"${PROJECT}\""
CMD+=" --repo \"\$(git rev-parse --show-toplevel 2>/dev/null)\""
CMD+=" --branch \"\$(git branch --show-current 2>/dev/null)\""
CMD+=" --commit \"\$(git rev-parse --short HEAD 2>/dev/null)\" >/dev/null 2>&1 & ); exit 0"

if command -v jq >/dev/null 2>&1; then
  [[ -f "${SETTINGS}" ]] || echo '{}' > "${SETTINGS}"
  tmp="$(mktemp)"
  jq --arg cmd "${CMD}" '
    .hooks = (.hooks // {})
    | .hooks.Stop = (.hooks.Stop // [])
    | if any(.hooks.Stop[]?; (.hooks // [])[]?.command == $cmd)
      then .
      else .hooks.Stop += [{"hooks": [{"type": "command", "command": $cmd}]}]
      end
  ' "${SETTINGS}" > "${tmp}" && mv "${tmp}" "${SETTINGS}"
else
  KB_HOOK_CMD="${CMD}" KB_SETTINGS="${SETTINGS}" python3 - <<'PY'
import json
import os

cmd = os.environ["KB_HOOK_CMD"]
path = os.environ["KB_SETTINGS"]

data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError:
            data = {}
if not isinstance(data, dict):
    data = {}

hooks = data.setdefault("hooks", {})
stop = hooks.setdefault("Stop", [])
exists = any(
    isinstance(entry, dict)
    and any(isinstance(h, dict) and h.get("command") == cmd for h in entry.get("hooks", []))
    for entry in stop
)
if not exists:
    stop.append({"hooks": [{"type": "command", "command": cmd}]})

with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
fi

echo "installed Stop hook for project '${PROJECT}' into ${SETTINGS}"
echo "it runs: ${KB_BIN} ingest-session \$CLAUDE_TRANSCRIPT_PATH --project ${PROJECT} (backgrounded)"
