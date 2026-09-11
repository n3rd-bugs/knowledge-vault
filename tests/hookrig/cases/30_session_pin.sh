#!/usr/bin/env bash
# CASE: SESSION PROJECT PIN (C4, KB 33784) — a session's project is decided ONCE
#       from the transcript's FIRST git cwd record and cached, so every firing of
#       one session files under one project no matter where the event cwd wanders.
# PINS (flush-session-to-kb.sh kb_pin_project / kb_record_cwds / kb_first_record_field):
#   * a nested launch (alpha with a beta/ subdir) pins to the git ROOT alpha.
#   * the first cwd RECORD wins over the event cwd (how=gitfirst).
#   * a non-git launch whose LATER records are inside alpha pins to alpha
#     (gitfirst): the scan takes the first record cwd that IS a git dir.
#   * a $HOME / non-git launch with NO git cwd in the scan -> how=firing, from
#     the event cwd, and is NEVER auto-minted (PROJECT_MINTABLE=0) — pinned
#     against a reachable mint path (fake repo + fake uv), with a positive
#     control that DOES mint; same for a /tmp launch.
#   * a .kbproject at the git root overrides the dir name (how=kbproject).
#   * the pin drives token selection: a beta-cwd firing of an alpha-pinned session
#     uses KB_TOKEN_ALPHA, and the server records project=alpha.
#   * STATE/<okey>.project caches "<slug>\t<how>"; a tab-less cache line is a
#     miss (re-derived), never "how=<slug>".
#   * KB_FLUSH_PROJECT_PIN=0 restores today's per-firing derivation (no cache).
# NOTE: the transcript's records carry a `cwd` (Claude Code writes one); the
#       fixtures build real git repos so `git rev-parse --show-toplevel` resolves.
#       install.sh in this tree does not create $HOOKS/state, hence the mkdirs.
. /rig/lib.sh

new_home pin
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"

mkrepo() { mkdir -p "$1"; ( cd "$1" && git init -q && git config user.email a@b.c && git config user.name t ) >/dev/null 2>&1; }
# a transcript whose FIRST record has this cwd (+ a newer-than-stamp timestamp);
# an optional SECOND cwd rides on the 5th record (a session that `cd`-ed later).
mktx() { "$RIG_PY" - "$1" "$2" "${3:-}" <<'EOF'
import json, sys
path, cwd, later = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "cwd": cwd, "timestamp": "2099-01-01T00:00:00Z", "text": "hi"}) + "\n")
    for i in range(15):
        rec = {"type": "user", "i": i, "text": "line %d filler" % i}
        if later and i == 4:
            rec["cwd"] = later
        fh.write(json.dumps(rec) + "\n")
EOF
}

WORK="$HOME/work"
mkrepo "$WORK/alpha"; mkdir -p "$WORK/alpha/beta"
mkrepo "$WORK/other"

# ---------------------------------------------- 1. nested alpha/beta -> alpha
step "first cwd record beta/ pins to the git root alpha (how=gitfirst)"
T="$WORK/alpha/nested.jsonl"
mktx "$T" "$WORK/alpha/beta"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK/other" sess-nested PreCompact)"
wait_firing 0 60 || fail "nested firing never completed"
assert_eq "alpha" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "server recorded project=alpha (not the event cwd 'other')"
assert_eq "alpha	gitfirst" "$(cat "$HOOKS/state/sess-nested.project")" "cache is '<slug>\\tgitfirst'"
# the cache is authoritative on the next firing even from another repo
stub_reset
mk_transcript "$HOME/more.jsonl" 2 more; cat "$HOME/more.jsonl" >>"$T"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK/other" sess-nested PreCompact)"
wait_firing "$(log_count ' (ok|partial) bytes=')" 60 || true
assert_eq "alpha" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "a later firing from 'other' still files as alpha (cache)"

# ------------------------------------------------------- 2. .kbproject umbrella
step "a .kbproject at the git root overrides the dir name (how=kbproject)"
printf 'umbrella\n' >"$WORK/alpha/.kbproject"
stub_reset
T2="$WORK/alpha/umb.jsonl"
mktx "$T2" "$WORK/alpha"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$WORK/alpha" sess-umb PreCompact)"
wait_firing "$(log_count ' (ok|partial) bytes=')" 60 || true
assert_eq "umbrella" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "project=umbrella from .kbproject"
assert_eq "umbrella	kbproject" "$(cat "$HOOKS/state/sess-umb.project")" "cache is 'umbrella\\tkbproject'"
rm -f "$WORK/alpha/.kbproject"

# ------------------------- 3. non-git launch, first GIT cwd later -> alpha gitfirst
step "a non-git launch whose later record cwd is alpha pins to alpha (gitfirst)"
stub_reset
mkdir -p "$WORK/plain"                   # plain is NOT a git repo
T3="$WORK/plain/p.jsonl"
mktx "$T3" "$WORK/plain" "$WORK/alpha/beta"   # first cwd non-git, 5th record inside alpha
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3" "$WORK/other" sess-late PreCompact)"
wait_firing "$(log_count ' (ok|partial) bytes=')" 60 || true
assert_eq "alpha" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "project=alpha from the first GIT cwd in the scan (event cwd 'other' ignored)"
assert_eq "alpha	gitfirst" "$(cat "$HOOKS/state/sess-late.project")" "cache is 'alpha\\tgitfirst'"

# ---------------------------------- 3b. no git cwd in the scan at all -> firing
step "a non-git launch with NO git cwd in the scan pins how=firing from the event cwd"
stub_reset
T3b="$WORK/plain/q.jsonl"
mktx "$T3b" "$WORK/plain"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3b" "$WORK/plain" sess-plain PreCompact)"
wait_firing "$(log_count ' (ok|partial) bytes=')" 60 || true
assert_eq "plain	firing" "$(cat "$HOOKS/state/sess-plain.project")" "cache is 'plain\\tfiring' (slug from the event cwd)"

# ------------------------------------ 3c. a tab-less cache line is a MISS
step "a cache line without a tab is re-derived, never read as how=<slug>"
printf 'alpha\n' >"$HOOKS/state/sess-plain.project"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T3b" "$WORK/plain" sess-plain PreCompact)"
settle
assert_eq "plain	firing" "$(cat "$HOOKS/state/sess-plain.project")" "the corrupt cache was re-derived"

# ------------------------------------- 4. NEVER auto-mint for $HOME / /tmp / non-git
# The mint path is only reachable when a KB repo dir exists and `uv` is on PATH
# (the owner's box); fake both so the assertion can FAIL on regression, and give
# the hook NO token at all so it reaches the mint block.
new_home pinmint
stub_reset
fake_kb_repo
fake_uv
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg      # no KB_TOKEN, no kb-tokens.env
mkrepo "$HOME/work/alpha"
step "a \$HOME launch with no git cwd is how=firing and is NOT auto-minted"
TH="$HOME/h.jsonl"
mktx "$TH" "$HOME"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TH" "$HOME" sess-home PreCompact)"
settle; settle
assert_eq "firing" "$(cut -s -f2 "$HOOKS/state/sess-home.project")" "how=firing for a \$HOME launch"
assert_file_missing "$HOME/uv-ran" "no mint attempted for the \$HOME slug"
assert_not_contains "$(cat "$LOGF" 2>/dev/null)" "auto-minted" "nothing was auto-minted"
step "a /tmp launch is how=firing and is NOT auto-minted"
TT="$HOME/t.jsonl"
mktx "$TT" "/tmp"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TT" "/tmp" sess-tmp PreCompact)"
settle; settle
assert_eq "firing" "$(cut -s -f2 "$HOOKS/state/sess-tmp.project")" "how=firing for a /tmp launch"
assert_file_missing "$HOME/uv-ran" "no mint attempted for the /tmp slug"
step "positive control: a git-root RECORD cwd (event cwd \$HOME) pins gitfirst and DOES reach the mint"
TA="$HOME/work/alpha/a.jsonl"
mktx "$TA" "$HOME/work/alpha"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TA" "$HOME" sess-alpha PreCompact)"
settle; settle
assert_eq "alpha	gitfirst" "$(cat "$HOOKS/state/sess-alpha.project")" "positive control pinned alpha (gitfirst)"
if [ -f "$HOME/uv-ran" ] && grep -q "run python" "$HOME/uv-ran"; then
  pass "positive control: a real git-root project DID reach the mint (uv run python)"
else
  fail "positive control: the mint path was never reached, so the no-mint assertions above prove nothing"
fi
step "a firing-derived slug from a NON-git event cwd never mints even with a git root elsewhere"
rm -f "$HOME/uv-ran"
mkdir -p "$HOME/work/plain2"
TP="$HOME/work/plain2/p.jsonl"
mktx "$TP" "$HOME/work/plain2"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$TP" "$HOME/work/plain2" sess-plain2 PreCompact)"
settle; settle
assert_eq "plain2	firing" "$(cat "$HOOKS/state/sess-plain2.project")" "how=firing for a non-git dir"
assert_file_missing "$HOME/uv-ran" "no mint for a non-git firing slug"

# --------------------------------------- 5. pin drives token + attribution (alpha)
new_home pintok
stub_reset
mkrepo "$HOME/work/alpha"; mkdir -p "$HOME/work/alpha/beta"; mkrepo "$HOME/work/beta"
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
# REST config with NO shared KB_TOKEN, only a per-project token for alpha.
printf '# REMOTE\nKB_REST_URL=%s\n' "$STUB" | write_cfg
printf 'KB_TOKEN_ALPHA=%s\n' "$RIG_TOKEN" >"$HOME/.claude/kb-tokens.env"
chmod 600 "$HOME/.claude/kb-tokens.env"
T4="$HOME/work/alpha/tok.jsonl"
mktx "$T4" "$HOME/work/alpha"
step "an alpha-pinned session firing in beta uses KB_TOKEN_ALPHA and files as alpha"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T4" "$HOME/work/beta" sess-tok PreCompact)"
wait_firing 0 60 || fail "token firing never completed (was KB_TOKEN_ALPHA selected?)"
assert_eq "alpha" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "server recorded project=alpha"
# No shared KB_TOKEN and only KB_TOKEN_ALPHA exist: a landed, accepted request
# proves the alpha token was selected (a 'beta' derivation would find no token
# and send nothing).
assert_ge "$(stub_count '.transport=="rest"')" "1" "the alpha project token was selected and accepted"
assert_eq "0" "$(stub_count '.transport=="rest" and .project=="beta"')" "zero rows filed under beta"

# --------------------------------------------------- 6. PIN=0 -> per firing, no cache
new_home pinoff
stub_reset
mkrepo "$HOME/work/alpha"; mkdir -p "$HOME/work/alpha/beta"; mkrepo "$HOME/work/beta"
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"
cfg_rest
T5="$HOME/work/alpha/off.jsonl"
mktx "$T5" "$HOME/work/alpha"
step "KB_FLUSH_PROJECT_PIN=0 derives from the EVENT cwd each firing and caches nothing"
KB_FLUSH_PROJECT_PIN=0 fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T5" "$HOME/work/beta" sess-off PreCompact)"
wait_firing 0 60 || fail "PIN=0 firing never completed"
assert_eq "beta" "$(stub_field '.transport=="rest"' '.project' | tail -1)" "PIN=0 files under the EVENT cwd (beta), not the record cwd (alpha)"
assert_file_missing "$HOOKS/state/sess-off.project" "PIN=0 writes no pin cache"

finish
