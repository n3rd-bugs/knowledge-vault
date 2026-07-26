#!/usr/bin/env bash
# CASE: the SHARED portable block is one block, and its two JSON backends agree
# PINS: platform detection, native-path conversion, the timeout guard, the detach
#       choice and the JSON backend chain are defined ONCE and used by all four
#       scripts. Shell has no import, so they are duplicated verbatim -- which is
#       only safe if a test proves the copies are byte-identical. Before this,
#       Windows knowledge was scattered (install.sh had a `uname -s` case,
#       flush-session-to-kb.sh had two cygpath cases, kb-session-brief.sh had its
#       own timeout probe, kb-mcp-call.sh had none of it) and each copy drifted.
# ALSO PINS: the jq and python3 backends are INTERCHANGEABLE, asserted per
#       primitive (field read, object build incl. a file-sourced value, JSON
#       validation, URL encoding) rather than only end to end -- a difference
#       here would surface as a corrupted submission, not as an error.
. /rig/lib.sh

new_home blockid
SCRIPTS="install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh"

step "every script carries exactly one copy of the block"
for f in $SCRIPTS; do
  B=$(grep -c '^# >>> KB PORTABLE BLOCK' "$SKILL/$f")
  E=$(grep -c '^# <<< END KB PORTABLE BLOCK' "$SKILL/$f")
  assert_eq "1|1" "$B|$E" "$f has exactly one begin+end marker"
done

step "and the four copies are byte-identical"
REF=""
for f in $SCRIPTS; do
  extract_block "$SKILL/$f" "$HOME/block.$f"
  LINES=$(wc -l <"$HOME/block.$f")
  assert_ge "$LINES" "50" "$f block is substantial ($LINES lines)"
  if [ -z "$REF" ]; then
    REF="$HOME/block.$f"
    continue
  fi
  if diff -u "$REF" "$HOME/block.$f" >"$HOME/d.txt"; then
    pass "$f block == install.sh block"
  else
    fail "$f block DRIFTED: $(head -c 300 "$HOME/d.txt")"
  fi
done
assert_eq "1" "$(for f in $SCRIPTS; do sha256sum <"$HOME/block.$f" | cut -d' ' -f1; done | sort -u | wc -l | tr -d ' ')" "one sha256 across all four"

step "the block is self-contained (sourceable on its own)"
# shellcheck disable=SC1090
if . "$REF"; then pass "sourced without error"; else fail "the block cannot be sourced standalone"; fi
for fn in kb_is_windows kb_winpath kb_nativepath kb_detach_cmd kb_timeout kb_json_probe \
  kb_json_get kb_json_valid kb_json_build kb_urlencode kb_nojson_hint kb_nojson_warn; do
  if command -v "$fn" >/dev/null 2>&1; then pass "defines $fn"; else fail "missing $fn"; fi
done

step "the probe finds jq FIRST when jq is on PATH (chain link 1)"
KB_JSON_PROBED=0
KB_JQ_BIN=""
KB_PY_BIN=""
kb_json_probe
assert_eq "$(command -v jq)" "$KB_JQ_BIN" "picked the PATH jq"
assert_eq "" "$KB_PY_BIN" "python3 was not consulted"

step "an unusable jq is skipped, not trusted blindly"
printf '#!/bin/sh\nexit 3\n' >"$HOME/bin/jq"
chmod +x "$HOME/bin/jq"
KB_JSON_PROBED=0
KB_JQ_BIN=""
KB_PY_BIN=""
KB_JQ="$HOME/bin/jq" kb_json_probe
assert_ne "$HOME/bin/jq" "$KB_JQ_BIN" "a jq that cannot parse {} is rejected"
rm -f "$HOME/bin/jq"

# ------------------------------------------------- the two backends, primitive
# by primitive. Each is computed twice from the SAME input, once per backend.
EVENT='{"transcript_path":"/tmp/t with space.jsonl","cwd":"/w/My_Repo","session_id":"s-1","hook_event_name":"PreCompact","n":7,"nested":{"a":1}}'
with_jq() {
  KB_JQ_BIN="$(command -v jq)"
  KB_PY_BIN=""
}
with_py() {
  KB_JQ_BIN=""
  KB_PY_BIN=python3
}

step "kb_json_get agrees"
for k in transcript_path cwd session_id hook_event_name missing_key; do
  with_jq
  A=$(printf '%s' "$EVENT" | kb_json_get "$k")
  with_py
  B=$(printf '%s' "$EVENT" | kb_json_get "$k")
  assert_eq "$A" "$B" "$k: [$A]"
done
with_jq
assert_eq "/tmp/t with space.jsonl" "$(printf '%s' "$EVENT" | kb_json_get transcript_path)" "spaces survive the field read"
with_py
assert_eq "/tmp/t with space.jsonl" "$(printf '%s' "$EVENT" | kb_json_get transcript_path)" "same on the python backend"
assert_eq "" "$(printf 'not json at all' | kb_json_get cwd)" "garbage input yields nothing, not an error"

step "kb_json_build agrees (including a value read from a FILE)"
printf 'line one\nline two — ünicode\n"quoted" \\ backslash\n' >"$HOME/payload.txt"
with_jq
kb_json_build "$HOME/b.jq.json" project s "my-repo" k n 5 transcript f "$HOME/payload.txt" kind s session
with_py
kb_json_build "$HOME/b.py.json" project s "my-repo" k n 5 transcript f "$HOME/payload.txt" kind s session
for f in b.jq.json b.py.json; do
  if "$RIG_JQ" -e . "$HOME/$f" >/dev/null 2>&1; then pass "$f is valid JSON"; else fail "$f is not valid JSON"; fi
done
A=$("$RIG_JQ" -S -c . "$HOME/b.jq.json")
B=$("$RIG_JQ" -S -c . "$HOME/b.py.json")
assert_eq "$A" "$B" "both backends built the same object"
assert_eq "$(cat "$HOME/payload.txt")" "$("$RIG_JQ" -r '.transcript' "$HOME/b.py.json")" "the file value round-trips byte for byte"
assert_eq "5" "$("$RIG_JQ" -r '.k' "$HOME/b.py.json")" "a number stays a number (not a string)"
assert_eq "number" "$("$RIG_JQ" -r '.k | type' "$HOME/b.py.json")" "typed as number in the python build too"

step "kb_json_valid agrees"
printf '{"a":1}' >"$HOME/good.json"
printf '{"a":' >"$HOME/bad.json"
for b in jq py; do
  with_$b
  if kb_json_valid "$HOME/good.json"; then pass "$b: good json accepted"; else fail "$b: good json rejected"; fi
  if kb_json_valid "$HOME/bad.json"; then fail "$b: truncated json accepted"; else pass "$b: truncated json rejected"; fi
done

step "kb_urlencode agrees"
Q='chunk offset & resume? a/b "quoted" — ünicode'
with_jq
A=$(printf '%s' "$Q" | kb_urlencode)
with_py
B=$(printf '%s' "$Q" | kb_urlencode)
assert_eq "$A" "$B" "same percent-encoding: $A"
assert_not_contains "$A" " " "no raw spaces left in the encoded query"

finish
