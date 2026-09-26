#!/usr/bin/env bash
# IMAGE: winsim
# CASE: the RAW Windows spawn PATH -- SessionEnd and SessionStart driven through
#       kb-hook-dispatch.sh exactly as Claude Code spawns them on Windows when the
#       registered command is the raw `usr\bin\bash.exe`: a NON-login MSYS bash
#       that inherited the WINDOWS PATH (wave C.1, KB 40411 fix set item 1).
# PINS: the usama-home root cause (KB 40411). On that PATH there is no Git
#       /usr/bin and no /mingw64/bin -- grep, awk, perl and cygpath are "command
#       not found" -- and a NATIVE tr answers first. MSYS rewrites the lone `/`
#       argument of the old kb_winpath fallback (`tr '\\' '/'`) into
#       `C:/Program Files/Git/`, every backslash of the transcript path becomes a
#       `C`, and flush-session-to-kb.sh's `[ -f "$T" ] || exit 0` left in silence:
#       no log line, no state, no pin, no sweep trigger -- for two weeks. The
#       pre-brief died the same way (a garbled project slug finds no per-project
#       token, so no search ran and only the `[KB sweep]` line printed).
#       The fix, as pinned here:
#         * the dispatcher (first statement after EV=) and the portable block
#           (before KB_UNAME=) prepend /usr/bin:/mingw64/bin under
#           `case $OSTYPE in msys*|cygwin*` -- bash's OWN variable, because uname
#           is itself one of the missing /usr/bin binaries;
#         * kb_winpath's no-cygpath fallback is pure bash (no cut, no tr).
# MODEL: OSTYPE=msys, and uname stays the REAL Linux one (no fake_uname_windows):
#       only the $OSTYPE gate may select Windows here. $HOME/rawbin holds
#       exit-127 stand-ins for grep/awk/perl/cygpath and the MSYS-converted tr
#       (lib.sh fake_raw_windows_path). It is prepended ONLY to the firings
#       (fire_raw), never to this script -- lib.sh's own assertions use grep.
#       /usr/bin stays on PATH (usrmerged image: bash itself lives there); the
#       stand-ins sit ahead of it, which is the raw PATH's effective order.
#       HOME is /c/Users/rig (the winsim image's writable /c), so the Windows
#       form C:\Users\rig\... of every path really is the sandbox HOME.
#       Per-project tokens only (kb-tokens.env, no shared KB_TOKEN): the default
#       enrolment shape, and the one where a garbled slug is fatal.
# RED on today's hooks (HEAD b2b37a3) -- expected FAIL lines:
#       "the detached drain finished" (nothing logged in 30 s);
#       "exactly ONE verdict line for sid=7e57ab1e -- expected [1], got [0]";
#       "...a fully drained SessionEnd ok -- expected [1], got [0]";
#       "a pin exists", "the pin names winproj -- expected [winproj], got []";
#       "the stub accepted the chunk(s) -- 0 < 1", "filed under winproj",
#       "session_uri", "byte for byte", "offset kept at the transcript size";
#       "[KB pre-brief · project winproj] not found in [[KB sweep] 1 transcript(s)
#       STALLED ...]", "the stub's hits", "the search ran ... -- 0 < 1";
#       "no grep/awk/perl stand-in was reached -- expected [0], got [N>=1]" (the
#       dispatcher's timeout probe pipes into the grep stand-in today);
#       "the MSYS-converted native tr was never reached -- expected [0], got [N]".
#       GREEN after the fix: the prepend restores the real tools and the fallback
#       never calls tr. The `[KB sweep]` count line and "no BUG line" pass both.
. /rig/lib.sh

RIG_HOMEBASE=/c/Users
new_home rig
RAWBIN="$HOME/rawbin"
fake_raw_windows_path "$RAWBIN"
export KB_SWEEP_INTERVAL=0 KB_VERSION_CHECK=0 KB_RECALL_BREAKER_SECS=0

step "the box: real Linux uname, winsim image, HOME under /c"
assert_eq "Linux" "$(uname -s)" "uname -s is the REAL one: only \$OSTYPE can select Windows in this case"
if command -v setsid >/dev/null 2>&1; then fail "setsid is on PATH -- winsim image is wrong"; else pass "no setsid (Git Bash)"; fi
assert_eq "/c/Users/rig" "$HOME" "HOME is the POSIX form of C:\\Users\\rig"

install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
# The default enrolment shape: a REST transport, NO shared KB_TOKEN, one
# per-project credential. A slug the hook garbles has no token at all.
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg
printf 'KB_TOKEN_WINPROJ=%s\n' "$RIG_TOKEN" >"$HOME/.claude/kb-tokens.env"
chmod 600 "$HOME/.claude/kb-tokens.env"
# From here on this shell -- and every hook it spawns -- says MSYS.
export OSTYPE=msys

PROJ_POSIX="$HOME/work/winproj"
PROJ_WIN='C:\Users\rig\work\winproj'
mkdir -p "$PROJ_POSIX"
(cd "$PROJ_POSIX" && git init -q) >/dev/null 2>&1
SID="7e57ab1e-0000-4000-8000-000000000040"
SID8="${SID:0:8}"
TDIR="$HOME/.claude/projects/C--Users-rig-work-winproj"
T_POSIX="$TDIR/$SID.jsonl"
T_WIN="C:\\Users\\rig\\.claude\\projects\\C--Users-rig-work-winproj\\$SID.jsonl"
mkdir -p "$TDIR"
# A real Windows transcript: every record carries the NATIVE cwd, which the
# session pin converts before asking git for the root.
"$RIG_PY" - "$T_POSIX" "$PROJ_WIN" "$SID" <<'EOF'
import json, sys
path, cwd, sid = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "w", encoding="utf-8") as fh:
    for i in range(12):
        fh.write(json.dumps({
            "type": "user" if i % 2 == 0 else "assistant",
            "timestamp": "2026-09-25T10:00:%02d.000Z" % i,
            "cwd": cwd, "sessionId": sid, "text": "raw-path line %d" % i,
        }) + "\n")
EOF
SIZE=$(wc -c <"$T_POSIX" | tr -d ' ')

# The dispatcher as Claude Code spawns it on Windows: `bash.exe <dispatcher>
# <event>`, the event JSON on stdin, the raw PATH (stand-ins first), OSTYPE=msys.
fire_raw() { # <event> <payload> -> RC, OUT
  printf '%s' "$2" | env PATH="$RAWBIN:$PATH" OSTYPE=msys bash "$HOOKS/kb-hook-dispatch.sh" "$1" \
    >"$HOME/.raw.out" 2>"$HOME/.raw.err"
  RC=$?
  OUT=$(cat "$HOME/.raw.out")
}
end_event() {
  "$RIG_JQ" -nc --arg s "$SID" --arg t "$T_WIN" --arg c "$PROJ_WIN" \
    '{session_id:$s,transcript_path:$t,cwd:$c,hook_event_name:"SessionEnd",reason:"exit"}'
}

# ------------------------------------------------------------- 1. SessionEnd
step "SessionEnd through the dispatcher on the raw PATH: logged, pinned, submitted"
stub_reset
: >"$HOME/rawpath.calls"
: >"$HOME/tr.calls"
fire_raw SessionEnd "$(end_event)"
assert_eq "0" "$RC" "the dispatcher exits 0"
assert_eq "" "$OUT" "...and prints nothing (the flush has no stdout contract)"
if wait_firing 0 30; then
  pass "the detached drain finished (a summary line landed)"
else
  fail "the detached drain finished -- no ' ok bytes=' / ' partial bytes=' line within 30 s (the flush left before logging anything)"
fi
assert_eq "1" "$(log_count " (ok|partial) bytes=.* sid=$SID8\$")" "exactly ONE verdict line for sid=$SID8"
assert_eq "1" "$(log_count " ok bytes=.* ev=SessionEnd sid=$SID8\$")" "...a fully drained SessionEnd ok"
assert_eq "0" "$(log_count 'BUG: transcript not found')" "no BUG line: the transcript path resolved"
PIN="$HOOKS/state/$SID.project"
if [ -s "$PIN" ]; then pass "a pin exists: state/<sid>.project = [$(tr '\t' ' ' <"$PIN")]"; else fail "a pin exists -- no state/$SID.project"; fi
assert_eq "winproj" "$(cut -f1 "$PIN" 2>/dev/null)" "the pin names winproj (the CONVERTED record cwd's git root)"
assert_ge "$(stub_count '.path=="/api/ingest-session" and .status==202')" "1" "the stub accepted the chunk(s)"
assert_eq "winproj" "$(stub_field '.path=="/api/ingest-session" and .status==202' '.project' | sort -u)" "filed under winproj"
assert_eq "session:$SID" "$(stub_field '.path=="/api/ingest-session" and .status==202' '.session_uri' | sort -u)" "session_uri is the session's own"
stub_concat '.path=="/api/ingest-session" and .status==202' '.transcript' >"$HOME/got"
if cmp -s "$HOME/got" "$T_POSIX"; then
  pass "the accepted chunks reassemble to the transcript byte for byte"
else
  fail "byte for byte -- got $(wc -c <"$HOME/got" | tr -d ' ') of $SIZE bytes"
fi
assert_eq "$SIZE" "$(cat "$HOOKS/state/$SID.offset" 2>/dev/null)" "offset kept at the transcript size (drained SessionEnd)"

# ------------------------------------------------------------ 2. SessionStart
# usama-home's sweep had stalled, so its SessionStart printed the [KB sweep]
# count line and NOTHING else. Same state here; the brief must print more.
step "SessionStart through the dispatcher, same raw PATH: the brief body is printed"
NOW=$(date +%s)
printf 'ts=%s rc=0 mode=auto ready=0 pushed=0 failed=0 deferred=0 stalled=1 unknown=0 live=0 unflushed=0 busy=0 partial=0\n' \
  "$((NOW - 600))" >"$HOOKS/state/.sweep-last"
stub_reset
fire_raw SessionStart "$(start_event startup "$PROJ_WIN")"
assert_eq "0" "$RC" "the dispatcher exits 0"
assert_eq "1" "$(printf '%s\n' "$OUT" | grep -c '^\[KB sweep\]')" "the [KB sweep] line is there (it was on usama-home too)"
assert_contains "$OUT" "[KB pre-brief · project winproj]" "the pre-brief header names the CONVERTED project"
assert_contains "$OUT" "REST hit 0" "...and carries the stub's hits"
assert_ge "$(stub_count '.path=="/api/search" and .auth=="ok"')" "1" "the search ran with the winproj token"

# -------------------------------------------- 3. which tools the hooks reached
step "the prepend put the real tools back ahead of the raw-PATH stand-ins"
note "stand-in calls: $(cut -d' ' -f1 "$HOME/rawpath.calls" 2>/dev/null | sort | uniq -c | tr -s ' \n' ' ')"
_bad=$(grep -cE '^(grep|awk|perl) ' "$HOME/rawpath.calls" 2>/dev/null) || true
assert_eq "0" "${_bad:-0}" "no grep/awk/perl stand-in was reached"
_trn=$(grep -c . "$HOME/tr.calls" 2>/dev/null) || true
assert_eq "0" "${_trn:-0}" "the MSYS-converted native tr was never reached"

finish
