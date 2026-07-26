#!/usr/bin/env bash
# Hermetic test rig for the Claude Code <-> Knowledge Vault session hooks.
#
#   bash tests/hookrig/run.sh                 # build + run every case
#   bash tests/hookrig/run.sh 05 07           # run only matching cases
#   KB_SKILL_DIR=contrib/claude/kb-session-flush bash tests/hookrig/run.sh
#   bash tests/hookrig/run.sh --clean         # also drop the built image
#
# GUARANTEES (see README.md):
#   * every case runs in a FRESH container with its own HOME -- the host's
#     ~/.claude is never read or written (only the skill dir is mounted, :ro)
#   * containers sit on an --internal docker network: no route to the live KB,
#     the live Postgres, the host, or the internet. The only reachable service
#     is the stub this script starts.
#   * containers and the network are removed on exit, success or failure.
set -uo pipefail

RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${KB_SKILL_DIR:-$HOME/.claude/skills/kb-session-flush}"
IMAGE="${KB_HOOKRIG_IMAGE:-kb-hookrig:1}"
NET="kb-hookrig-net"
STUB_CT="kb-hookrig-stub"
# Fake, shaped like a real bearer -- the rig must never carry a live token.
RIG_TOKEN="kbrig_0000000000000000000000000000"
CLEAN_IMAGE=0

FILTERS=()
for a in "$@"; do
  case "$a" in
    --clean) CLEAN_IMAGE=1 ;;
    -h | --help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) FILTERS+=("$a") ;;
  esac
done

c_red() { printf '\033[31m%s\033[0m' "$1"; }
c_grn() { printf '\033[32m%s\033[0m' "$1"; }
c_yel() { printf '\033[33m%s\033[0m' "$1"; }
die() {
  printf '%s\n' "$(c_red "ABORT: $*")" >&2
  exit 2
}

command -v docker >/dev/null || die "docker is required"
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon"
SKILL_DIR="$(cd "$SKILL_DIR" 2>/dev/null && pwd)" || die "skill dir not found: ${KB_SKILL_DIR:-$HOME/.claude/skills/kb-session-flush}"
for f in install.sh flush-session-to-kb.sh kb-session-brief.sh kb-mcp-call.sh; do
  [ -f "$SKILL_DIR/$f" ] || die "skill dir is missing $f: $SKILL_DIR"
done

# --------------------------------------------------------------- host tripwire
# The rig must not touch the host's Claude Code install. Fingerprint the files an
# accidental `install.sh` on the host would rewrite, and re-check at the end.
host_fingerprint() {
  for f in "$HOME/.claude/settings.json" "$HOME/.claude/kb-flush.env" \
    "$HOME/.claude/hooks/flush-session-to-kb.sh" "$HOME/.claude/hooks/kb-session-brief.sh"; do
    if [ -e "$f" ]; then sha256sum "$f" 2>/dev/null; else echo "absent $f"; fi
  done
}
HOST_BEFORE="$(host_fingerprint)"

cleanup() {
  docker rm -f "$STUB_CT" >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  [ "$CLEAN_IMAGE" = 1 ] && docker rmi -f "$IMAGE" >/dev/null 2>&1
  return 0
}
trap cleanup EXIT INT TERM
cleanup # a previous interrupted run leaves nothing behind

printf '\n== kb hook rig ==\n'
printf 'skill under test : %s\n' "$SKILL_DIR"
printf 'image            : %s\n' "$IMAGE"

printf '\n-- building the test image\n'
docker build -t "$IMAGE" "$RIG_DIR" >"$RIG_DIR/.build.log" 2>&1 || {
  tail -25 "$RIG_DIR/.build.log"
  die "image build failed (full log: $RIG_DIR/.build.log)"
}
rm -f "$RIG_DIR/.build.log"
printf '   ok\n'

printf -- '-- creating the isolated network (--internal: no egress)\n'
docker network create --internal "$NET" >/dev/null || die "could not create $NET"

printf -- '-- starting the stub KB server\n'
docker run -d --name "$STUB_CT" --network "$NET" --network-alias hookrig-stub \
  --read-only --tmpfs /tmp:rw,size=64m \
  -v "$RIG_DIR:/rig:ro" -e "RIG_TOKEN=$RIG_TOKEN" \
  "$IMAGE" python3 /rig/stub_server.py >/dev/null || die "could not start the stub"

ready=0
for _ in $(seq 1 40); do
  if docker exec "$STUB_CT" curl -sf --max-time 2 http://127.0.0.1:8000/__health >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.25
done
[ "$ready" = 1 ] || {
  docker logs "$STUB_CT" 2>&1 | tail -20
  die "the stub never became healthy"
}
printf '   ok\n'

# ------------------------------------------------------------------- case loop
mapfile -t CASES < <(find "$RIG_DIR/cases" -name '*.sh' | sort)
[ "${#CASES[@]}" -gt 0 ] || die "no cases found"

RESULTS=()
FAILED=0
for case_path in "${CASES[@]}"; do
  name="$(basename "$case_path" .sh)"
  if [ "${#FILTERS[@]}" -gt 0 ]; then
    keep=0
    for f in "${FILTERS[@]}"; do case "$name" in *"$f"*) keep=1 ;; esac; done
    [ "$keep" = 1 ] || continue
  fi
  xfail=0
  grep -q '^# EXPECT: xfail' "$case_path" && xfail=1

  printf '\n== %s ==\n' "$name"
  t0=$(date +%s)
  docker run --rm --network "$NET" \
    --add-host kb.n3rd-bugs.dev:127.0.0.1 \
    --add-host api.n3rd-bugs.dev:127.0.0.1 \
    --add-host web.n3rd-bugs.dev:127.0.0.1 \
    -v "$SKILL_DIR:/skill:ro" -v "$RIG_DIR:/rig:ro" \
    -e "STUB=http://hookrig-stub:8000" -e "RIG_TOKEN=$RIG_TOKEN" -e "SKILL=/skill" \
    "$IMAGE" bash "/rig/cases/$name.sh"
  rc=$?
  t1=$(date +%s)

  if [ "$xfail" = 1 ]; then
    if [ "$rc" -eq 0 ]; then
      status="XPASS"
      FAILED=$((FAILED + 1))
    else
      status="XFAIL"
    fi
  elif [ "$rc" -eq 0 ]; then
    status="PASS"
  else
    status="FAIL"
    FAILED=$((FAILED + 1))
  fi
  RESULTS+=("$status|$name|$((t1 - t0))s")
done

# ---------------------------------------------------------------------- report
printf '\n\n%s\n' "=========================== RESULTS ==========================="
printf '%-7s %-34s %6s   %s\n' STATUS CASE TIME PINS
printf '%s\n' "---------------------------------------------------------------"
for r in "${RESULTS[@]}"; do
  st="${r%%|*}"
  rest="${r#*|}"
  nm="${rest%%|*}"
  tm="${rest##*|}"
  pin=$(sed -n 's/^# PINS: *//p' "$RIG_DIR/cases/$nm.sh" | head -1 | cut -c1-60)
  case "$st" in
    PASS) col=$(c_grn PASS) ;;
    XFAIL) col=$(c_yel XFAIL) ;;
    XPASS) col=$(c_red XPASS) ;;
    *) col=$(c_red FAIL) ;;
  esac
  printf '%-16s %-34s %6s   %s\n' "$col" "$nm" "$tm" "$pin"
done
printf '%s\n' "---------------------------------------------------------------"

# The host must be exactly as we found it.
if [ "$HOST_BEFORE" != "$(host_fingerprint)" ]; then
  printf '%s\n' "$(c_red 'HOST TRIPWIRE: ~/.claude changed during this run -- investigate immediately')"
  FAILED=$((FAILED + 1))
else
  printf 'host ~/.claude unchanged (tripwire ok)\n'
fi

if [ "$FAILED" -eq 0 ]; then
  printf '%s\n\n' "$(c_grn "all cases green (XFAIL = a known hook defect, see the case header)")"
  exit 0
fi
printf '%s\n\n' "$(c_red "$FAILED case(s) failed")"
exit 1
