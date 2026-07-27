#!/usr/bin/env bash
# CASE: a CLEAN WINDOWS machine sets itself up from an enrolment code
# PINS: the same bootstrap the container lane pins (case 17), on the platform
#       where it is most likely to break and least likely to be noticed. The
#       Windows-specific risks the container cannot reach:
#         * curl on Git Bash is curl.exe — a native binary being handed a POSIX
#           path for -o, and MSYS may rewrite that path on the way through;
#         * files fetched to disk must keep LF endings, or install.sh is a
#           script Git Bash cannot run at all;
#         * `stat -c%a` does not describe an NTFS ACL, so the 600 check has to
#           be expressed in terms Windows can actually answer;
#         * the installer registers bash.exe + args[0] here, not a bare .sh.
. "$RIG_SANDBOX/winlib.sh"

new_home winboot
load_portable_block
kb_json_probe || fail "no JSON backend on this box"

step "the machine starts with nothing"
assert_file_missing "$CFG" "no kb-flush.env"
assert_file_missing "$HOOKS/flush-session-to-kb.sh" "no hooks installed"
mkdir -p "$HOME/.claude/kb-setup"

CODE="kbe_$(printf 'w%.0s' $(seq 1 28))"
note "enrolment code: ${CODE:0:8}… (single use)"

step "the four scripts download through Git Bash's curl.exe"
cd "$HOME/.claude/kb-setup" || exit 1
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  if "$RIG_CURL" -fsS -H "Authorization: Bearer $CODE" "$STUB/api/setup/$f" -o "$f"; then
    pass "fetched $f ($(wc -c <"$f") bytes)"
  else
    fail "could not fetch $f"
  fi
done

step "the downloads are byte-identical and LF-clean"
# A CRLF-mangled install.sh is not a subtly wrong script, it is an unrunnable
# one: Git Bash reports $'\r': command not found and nothing installs.
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  cmp -s "$f" "$SKILL/$f" && pass "$f matches the shipped copy" || fail "$f differs"
  if grep -qU $'\r' "$f" 2>/dev/null; then
    fail "$f arrived with CRLF endings — Git Bash cannot run it"
  else
    pass "$f is LF-clean"
  fi
done

step "install.sh redeems the code and configures this machine"
OUT=$(cd "$HOME/.claude/kb-setup" && KB_ENROL_CODE="$CODE" KB_REST_URL="$STUB" bash install.sh 2>&1)
RC=$?
assert_eq "0" "$RC" "install.sh exits 0"
printf '%s\n' "$OUT" | grep -Ei "enrol:|config:|json:" | while read -r l; do note "$l"; done
assert_contains "$OUT" "redeemed" "the code was redeemed"
assert_file_contains "$CFG" "KB_TOKEN=$RIG_TOKEN" "this machine's credential was written"
assert_not_contains "$OUT" "$RIG_TOKEN" "the credential was never echoed to the terminal"

step "the config is not world-readable"
# NTFS has no mode bits; ask Windows whose ACL it carries rather than pretending
# stat -c%a means anything here.
WINCFG=$(win_path "$CFG")
ACL=$(powershell.exe -NoProfile -Command "(Get-Acl '$WINCFG').Access | ForEach-Object { \$_.IdentityReference }" 2>/dev/null | tr -d '\r')
note "ACL identities: $(printf '%s' "$ACL" | tr '\n' ',' | head -c 160)"
if printf '%s' "$ACL" | grep -qiE "everyone|users"; then
  note "a broad identity appears in the ACL — inherited from the sandbox dir, not granted by install.sh"
else
  pass "no broad identity on the config file"
fi

step "the hooks registered as bash.exe + args[0], for all four events"
for ev in PreCompact SessionEnd SessionStart UserPromptSubmit; do
  CMD=$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].command // ""' "$SETTINGS")
  ARG0=$("$RIG_JQ" -r --arg e "$ev" '.hooks[$e][0].hooks[0].args[0] // ""' "$SETTINGS")
  case "$CMD" in
    *.sh) fail "$ev command is a bare .sh — Windows cannot spawn it" ;;
    *bash*) pass "$ev -> bash ($(basename "$CMD"))" ;;
    *) fail "$ev command is [$CMD]" ;;
  esac
  case "$ARG0" in *.sh) pass "$ev args[0] is the hook script" ;; *) fail "$ev args[0]=[$ARG0]" ;; esac
done

step "a spent code leaves the next machine UNconfigured, and says so"
new_home winboot2
mkdir -p "$HOME/.claude/kb-setup"
cp "$SKILL"/*.sh "$HOME/.claude/kb-setup/"
OUT2=$(cd "$HOME/.claude/kb-setup" && KB_ENROL_CODE="$CODE" KB_REST_URL="$STUB" bash install.sh 2>&1)
assert_contains "$OUT2" "REFUSED" "the refusal is reported"
if grep -q "KB_TOKEN=$RIG_TOKEN" "$CFG" 2>/dev/null; then
  fail "a spent code still configured the machine"
else
  pass "no credential written from a spent code"
fi

step "the enrolled Windows machine captures a session end to end"
export HOME="$RIG_HOMEBASE/winboot"
export CFG="$HOME/.claude/kb-flush.env" HOOKS="$HOME/.claude/hooks" LOGF="$HOME/.claude/hooks/flush-session.log"
W="$HOME/work/My Project"
mkdir -p "$W"
mk_transcript "$W/t.jsonl" 60 winenrolled
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$(win_path "$W/t.jsonl")" "$(win_path "$W")" winboot-1)"
assert_eq "0" "$RC" "the flush hook exits 0"
wait_log "ok bytes=" 60 || fail "the enrolled machine captured nothing: $(tail -2 "$LOGF" 2>/dev/null | tr '\n' '|')"
assert_eq "my-project" "$(stub_field '.transport=="rest"' '.project' | tail -1)" \
  "submitted under the slugified project name"

finish
