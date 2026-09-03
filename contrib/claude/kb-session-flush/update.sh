#!/usr/bin/env bash
# update.sh -- refresh this machine's KB hook scripts from the KB host, then hand
# over to install.sh. The ONE command a stale box needs; the pre-brief's
# "[KB setup] ... OUT OF DATE" banner points here.
#
#   ~/.claude/hooks/update.sh              # fetch + verify + install
#   ~/.claude/hooks/update.sh --dry-run    # fetch + verify into ~/.claude/kb-setup, install nothing
#
# WHY THIS EXISTS (review #8 H15). Catching up used to mean pasting a curl loop
# with a hand-maintained file list, and every copy of that list went stale in the
# quiet direction: SHARING.md named 4 of the 8 served assets while install.sh
# installs kb-hook-dispatch.sh UNCONDITIONALLY (it is the only script registered
# with Claude Code), so the documented paste died at the installer. This script
# has no list. It asks the SERVER what it serves -- /api/setup/manifest.json,
# whose version is derived from the file contents -- fetches exactly that set,
# and verifies every download against the manifest's sha256 BEFORE anything
# touches the installed hooks. A list the server owns cannot be short or long.
#
# WHAT IT DELIBERATELY DOES NOT DO. It never writes into ~/.claude/hooks itself:
# install.sh owns that (idempotent copy, per-file verification, the
# .setup-version stamp, settings.json). This fetches into ~/.claude/kb-setup --
# the same staging dir the bootstrap uses -- and runs the freshly fetched
# install.sh. A failed or mismatching download leaves EVERYTHING as it was.
#
# CREDENTIALS. KB_REST_URL and a bearer come from ~/.claude/kb-flush.env (the
# environment overrides it); a default-enrolled box has no shared KB_TOKEN, so
# the first per-project token in ~/.claude/kb-tokens.env is used instead -- any
# live credential may fetch the setup assets. The bearer reaches curl on STDIN
# (`-K -`), never in argv, like every other KB curl on this box, and `ipv4` pins
# the address family for the same reason the hooks do. Nothing here prints a
# secret.
set -uo pipefail

DRY_RUN=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help)
      sed -n '2,7p' "$0"
      exit 0
      ;;
    *)
      echo "update.sh: unknown argument: $a (try --dry-run)" >&2
      exit 2
      ;;
  esac
done

# The environment beats the config file, so one run can be pointed elsewhere.
_ENV_URL="${KB_REST_URL:-}"
_ENV_TOKEN="${KB_TOKEN:-}"
CFG="$HOME/.claude/kb-flush.env"
if [ -f "$CFG" ]; then
  # shellcheck disable=SC1090
  . "$CFG"
fi
[ -n "$_ENV_URL" ] && KB_REST_URL="$_ENV_URL"
[ -n "$_ENV_TOKEN" ] && KB_TOKEN="$_ENV_TOKEN"
KB_REST_URL="${KB_REST_URL:-}"
KB_TOKEN="${KB_TOKEN:-}"

if [ -z "$KB_REST_URL" ]; then
  echo "update.sh: KB_REST_URL is not set (in $CFG or the environment)." >&2
  echo "           Updates fetch from the REST base, e.g. KB_REST_URL=https://api.<host>" >&2
  exit 2
fi
BASE="${KB_REST_URL%/}"

# Per-project token fallback: the default enrolment path writes NO shared
# KB_TOKEN, only KB_TOKEN_<PROJECT>=... lines. Any one of them may fetch assets.
TOKENS="$HOME/.claude/kb-tokens.env"
if [ -z "$KB_TOKEN" ] && [ -f "$TOKENS" ]; then
  KB_TOKEN=$(grep -m1 '^KB_TOKEN_[A-Z0-9_]*=' "$TOKENS" 2>/dev/null | cut -d= -f2- | tr -d ' \r"' || true)
fi
if [ -z "$KB_TOKEN" ]; then
  echo "update.sh: no bearer found (KB_TOKEN in $CFG, or a KB_TOKEN_<PROJECT> line in $TOKENS)." >&2
  exit 2
fi

kb_curl_auth_config() { # -> curl config on stdout; the bearer never touches argv
  printf 'ipv4\n'
  [ -n "${KB_TOKEN:-}" ] || return 0
  printf 'header = "Authorization: Bearer %s"\n' "$KB_TOKEN"
}

kb_sha256() { # <file> -> lowercase hex digest; empty when this box has no backend
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" 2>/dev/null | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1
  else
    for _py in python3 python; do
      if command -v "$_py" >/dev/null 2>&1; then
        "$_py" -c 'import hashlib,sys
print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1" 2>/dev/null
        return 0
      fi
    done
  fi
}

# 1. The manifest: {"version":"<12 hex>","files":{"<name>":"<sha256>",...},"count":N}
MANIFEST=$(kb_curl_auth_config | curl -fsS -K - --connect-timeout 5 --max-time 20 \
  "$BASE/api/setup/manifest.json" 2>/dev/null) || MANIFEST=""
if [ -z "$MANIFEST" ]; then
  echo "update.sh: could not fetch $BASE/api/setup/manifest.json (server unreachable, or this credential was refused). Nothing changed." >&2
  exit 1
fi
VERSION=$(printf '%s' "$MANIFEST" | tr -d ' \n\r\t' | sed -n 's/.*"version":"\([a-f0-9]*\)".*/\1/p')
# One "<name> <digest>" per line. The name pattern IS the allowlist: a plain file
# name (no slash, no leading dot) with a 64-hex digest. A hostile or malformed
# manifest entry is simply not a file to fetch, so nothing can steer a write
# outside the staging dir.
FILES=$(printf '%s' "$MANIFEST" | tr -d ' \n\r\t' | sed -n 's/.*"files":{\([^}]*\)}.*/\1/p' |
  tr ',' '\n' | sed -n 's/^"\([A-Za-z0-9_-][A-Za-z0-9_.-]*\)":"\([a-f0-9]\{64\}\)"$/\1 \2/p')
if [ -z "$FILES" ]; then
  echo "update.sh: the manifest listed no fetchable files. Nothing changed." >&2
  exit 1
fi

# 2. Fetch into a private staging dir beside the setup dir; verify; then move.
SETUP="$HOME/.claude/kb-setup"
mkdir -p "$SETUP" || exit 1
STAGE=$(mktemp -d "$SETUP/.update.XXXXXX") || exit 1
trap 'rm -rf "$STAGE"' EXIT
HAVE_SHA=1
[ -n "$(kb_sha256 "$0" 2>/dev/null || true)" ] || HAVE_SHA=0
[ "$HAVE_SHA" = 1 ] || echo "update.sh: no sha256 backend on this box -- downloads are NOT verified here (install.sh will report the same)" >&2

N=0
while read -r NAME WANT; do
  [ -n "$NAME" ] || continue
  if ! kb_curl_auth_config | curl -fsS -K - --connect-timeout 5 --max-time 60 \
    -o "$STAGE/$NAME" "$BASE/api/setup/$NAME" 2>/dev/null; then
    echo "update.sh: fetch FAILED for $NAME -- aborting, nothing changed." >&2
    exit 1
  fi
  if [ "$HAVE_SHA" = 1 ]; then
    GOT=$(kb_sha256 "$STAGE/$NAME" || true)
    if [ "$GOT" != "$WANT" ]; then
      echo "update.sh: sha256 MISMATCH for $NAME (expected $(printf '%.12s' "$WANT"), got $(printf '%.12s' "$GOT")) -- aborting, nothing changed." >&2
      exit 1
    fi
  fi
  case "$NAME" in *.sh) chmod 0755 "$STAGE/$NAME" ;; esac
  N=$((N + 1))
done <<FILES_EOF
$FILES
FILES_EOF

# Every file arrived and verified: replace the staged set, one rename per file.
while read -r NAME _; do
  [ -n "$NAME" ] || continue
  mv -f "$STAGE/$NAME" "$SETUP/$NAME" || {
    echo "update.sh: could not place $NAME into $SETUP" >&2
    exit 1
  }
done <<FILES_EOF
$FILES
FILES_EOF
rm -rf "$STAGE"
trap - EXIT
echo "update.sh: fetched and verified $N file(s) (server version ${VERSION:-unknown}) into $SETUP"

if [ "$DRY_RUN" = 1 ]; then
  echo "update.sh: --dry-run -- install.sh NOT run; ~/.claude/hooks is unchanged."
  exit 0
fi
if [ ! -f "$SETUP/install.sh" ]; then
  echo "update.sh: the server did not serve install.sh -- cannot install. Files are staged in $SETUP." >&2
  exit 1
fi
# Hand the installer the same base URL and bearer, so its manifest verification
# and .setup-version stamp work on a default-enrolled box too (no shared KB_TOKEN
# anywhere). KB_SETUP_BEARER is the installer's FETCH credential; it never writes
# it into a config file.
export KB_REST_URL="$BASE"
KB_SETUP_BEARER="$KB_TOKEN"
export KB_SETUP_BEARER
exec bash "$SETUP/install.sh"
