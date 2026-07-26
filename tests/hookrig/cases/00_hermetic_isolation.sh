#!/usr/bin/env bash
# CASE: hermetic isolation (the rig's own guarantee, made executable)
# PINS: the rig NEVER reaches the live KB or the host ~/.claude.
#       Not a hook fact -- a RIG fact. Prior incidents: a sub-agent wrote to the
#       live schema (memory: subagent-live-db-write-risk) and hook folklore was
#       gathered by poking production. If this case fails, every other result in
#       this run is suspect, so it runs first.
. /rig/lib.sh

new_home hermetic

step "no egress: the case container is on an --internal docker network"
if grep -qE '^\S+\s+00000000\s' /proc/net/route; then
  fail "container HAS a default route (network is not --internal)"
else
  pass "no default route in /proc/net/route"
fi

step "the live KB endpoints are unreachable"
for host in kb.n3rd-bugs.dev api.n3rd-bugs.dev web.n3rd-bugs.dev; do
  if curl -sS --max-time 5 "https://$host/" >/dev/null 2>&1; then
    fail "reached https://$host -- NOT hermetic"
  else
    pass "https://$host unreachable"
  fi
done
for hostport in 127.0.0.1:8321 127.0.0.1:8322 127.0.0.1:8323; do
  if curl -sS --max-time 3 "http://$hostport/" >/dev/null 2>&1; then
    fail "reached http://$hostport -- NOT hermetic"
  else
    pass "http://$hostport unreachable"
  fi
done
if curl -sS --max-time 5 https://1.1.1.1/ >/dev/null 2>&1; then
  fail "reached the public internet -- NOT hermetic"
else
  pass "public internet unreachable"
fi

step "the stub IS reachable (otherwise the suite proves nothing)"
assert_eq "ok" "$(curl -sS --max-time 5 "$STUB/__health" | tr -d '\n')" "stub /__health"

step "the host filesystem is not exposed"
assert_file_missing /home/usama "host home not mounted"
if [ -e "$HOME/../usama/.claude" ]; then fail "host ~/.claude visible"; else pass "no host ~/.claude"; fi
if : 2>/dev/null >/skill/.rig-write-probe; then
  rm -f /skill/.rig-write-probe
  fail "the skill mount is WRITABLE -- must be :ro"
else
  pass "skill mount is read-only"
fi
if : 2>/dev/null >/rig/.rig-write-probe; then
  rm -f /rig/.rig-write-probe
  fail "the rig mount is WRITABLE -- must be :ro"
else
  pass "rig mount is read-only"
fi

step "the image ships no real transport (each mode's precondition is a fake)"
for bin in claude uv kb; do
  if command -v "$bin" >/dev/null 2>&1; then
    fail "image provides a real '$bin' -- mode-priority cases would be meaningless"
  else
    pass "no '$bin' in the image"
  fi
done

step "the skill under test is complete"
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  if [ -f "$SKILL/$f" ]; then pass "skill has $f"; else fail "skill missing $f"; fi
done

finish
