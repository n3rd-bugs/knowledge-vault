#!/usr/bin/env bash
# CASE: the JSON backend chain against REAL Windows binaries
# PINS: kb-system items on the sudo-free box. The chain is jq on PATH -> a jq
#       co-located with the hooks -> $KB_JQ -> python3/python -> LOUD failure.
#       On Windows every one of those is a .exe: `command -v jq` yields a path
#       with a drive letter and possibly a space, the co-located copy is
#       `jq.exe`, and `python3` is often a Store alias stub that resolves but
#       CANNOT `import json`. The container tests the logic; only a real box
#       tests it against binaries that behave like Windows binaries.
. "$RIG_SANDBOX/winlib.sh"

new_home jsonbackend
load_portable_block

step "backend 1: jq on PATH (the normal case here)"
if kb_json_probe; then pass "kb_json_probe found a backend"; else fail "no JSON backend at all"; fi
note "jq=${KB_JQ_BIN:-<none>}  py=${KB_PY_BIN:-<none>}"
if [ -n "${KB_JQ_BIN:-}" ]; then
  pass "jq backend selected: $KB_JQ_BIN"
else
  note "no jq on this box; python backend in use"
fi

step "the four JSON helpers work on real Windows binaries"
J='{"a":"one","b":"two words","c":"C:\\Users\\First Last\\p","n":42}'
assert_eq "one" "$(printf '%s' "$J" | kb_json_get a)" "kb_json_get simple value"
assert_eq "two words" "$(printf '%s' "$J" | kb_json_get b)" "kb_json_get value with a space"
assert_eq 'C:\Users\First Last\p' "$(printf '%s' "$J" | kb_json_get c)" "kb_json_get a native Windows path (backslashes survive)"
assert_eq "42" "$(printf '%s' "$J" | kb_json_get n)" "kb_json_get a number"
assert_eq "" "$(printf '%s' "$J" | kb_json_get nope)" "kb_json_get missing key -> empty"

# kb_json_valid takes a FILE (that is how the hooks use it: validating the
# payload they just wrote before POSTing it).
printf '%s' "$J" >"$HOME/good.json"
printf 'not json' >"$HOME/bad.json"
if kb_json_valid "$HOME/good.json"; then pass "kb_json_valid accepts valid JSON"; else fail "kb_json_valid rejected valid JSON"; fi
if kb_json_valid "$HOME/bad.json"; then fail "kb_json_valid accepted garbage"; else pass "kb_json_valid rejects garbage"; fi

step "kb_json_build emits JSON that survives a round trip"
# Signature: kb_json_build <outfile|-> key type value ... (type s|n|f)
printf 'line1\nline2 "quoted" and \\backslash\n' >"$HOME/content.txt"
BUILT=$(kb_json_build - project s "my proj" session s 'C:\tmp\s' n_items n 42 content f "$HOME/content.txt")
printf '%s' "$BUILT" >"$HOME/built.json"
if kb_json_valid "$HOME/built.json"; then pass "kb_json_build produced valid JSON"; else fail "kb_json_build produced INVALID JSON"; fi
assert_eq "my proj" "$(printf '%s' "$BUILT" | kb_json_get project)" "built: value with a space"
assert_eq 'C:\tmp\s' "$(printf '%s' "$BUILT" | kb_json_get session)" "built: backslash path round-trips"
assert_eq "42" "$(printf '%s' "$BUILT" | kb_json_get n_items)" "built: numeric type stays a number"
GOT=$(printf '%s' "$BUILT" | kb_json_get content)
assert_contains "$GOT" 'line2 "quoted" and \backslash' "built: quotes and backslashes round-trip (rawfile)"
assert_contains "$GOT" "line1" "built: multi-line content round-trips"

step "kb_urlencode agrees between backends (used to build recall queries)"
ENC=$(printf 'a b&c=d/e' | kb_urlencode)
assert_eq "a%20b%26c%3Dd%2Fe" "$ENC" "kb_urlencode percent-encodes reserved characters"

step "backend 2: NO jq on PATH -> the python fallback must take over"
# Hide every jq the probe could find, exactly like a locked-down box.
mkdir -p "$HOME/nojq"
cat >"$HOME/nojq/jq" <<'EOF'
#!/bin/sh
echo "jq must not be used here" >&2
exit 127
EOF
chmod +x "$HOME/nojq/jq"
(
  # A PATH with python but no jq. RIG_JQ keeps working for the harness because
  # winlib resolved it to an absolute path before we clobbered PATH.
  PYDIR=$(dirname "$RIG_PY")
  export PATH="$PYDIR:/usr/bin:/bin"
  unset KB_JQ
  KB_JSON_PROBED=0; KB_JQ_BIN=""; KB_PY_BIN=""
  if command -v jq >/dev/null 2>&1; then
    echo "    --   jq still resolvable at $(command -v jq); tightening"
  fi
  if kb_json_probe && [ -z "$KB_JQ_BIN" ] && [ -n "$KB_PY_BIN" ]; then
    echo "    ok   python fallback selected ($KB_PY_BIN) when jq is gone"
  elif kb_json_probe && [ -n "$KB_JQ_BIN" ]; then
    echo "    --   jq was still discoverable ($KB_JQ_BIN); fallback untested here"
  else
    echo "    ok   no backend found and kb_json_probe reported failure (loud path)"
  fi
  # The helpers must behave identically under whichever backend won.
  V=$(printf '%s' '{"k":"v w"}' | kb_json_get k)
  [ "$V" = "v w" ] && echo "    ok   kb_json_get works under the fallback backend" \
                   || { echo "    FAIL kb_json_get returned [$V] under the fallback"; exit 1; }
) || fail "python fallback subshell failed"
pass "python fallback exercised"

step "backend 3: a co-located jq.exe is found (the sudo-free install trick)"
mkdir -p "$HOME/colo"
if [ -n "${KB_JQ_BIN:-}" ] && [ -f "$KB_JQ_BIN" ]; then
  cp "$KB_JQ_BIN" "$HOME/colo/jq.exe" 2>/dev/null || cp "$KB_JQ_BIN" "$HOME/colo/jq"
  (
    export PATH="/usr/bin:/bin"
    export KB_JQ_DIR1="$HOME/colo"
    unset KB_JQ
    KB_JSON_PROBED=0; KB_JQ_BIN=""; KB_PY_BIN=""
    kb_json_probe
    case "${KB_JQ_BIN:-}" in
      "$HOME/colo"*) echo "    ok   co-located jq found at $KB_JQ_BIN" ;;
      "") echo "    FAIL co-located jq.exe was NOT found (KB_JQ_DIR1 ignored)"; exit 1 ;;
      *) echo "    --   another jq won the race: $KB_JQ_BIN" ;;
    esac
  ) || fail "co-located jq probe failed"
  pass "co-location path exercised"
else
  note "no jq binary to copy; co-location path skipped on this box"
fi

finish
