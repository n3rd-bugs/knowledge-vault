#!/usr/bin/env bash
# CASE: ABSOLUTE LATTICE (C2, Appendix A, KB 33784) — a chunk's end is the
#       smallest lattice point B_k > OFFSET, where B_k is the byte after the last
#       newline at or before k*CHUNK measured from byte 0. The cut is a function
#       of the FILE ONLY, so a from-0 pass and a pass made of several deltas that
#       stopped at ARBITRARY EOFs cut IDENTICAL chunks, except in the cells where
#       a delta firing stopped off-lattice — and those are counted exactly.
# PINS (flush-session-to-kb.sh, the child lattice loop + oversize path):
#   * from-0 pass (one sid) vs a multi-delta pass (another sid, file grown in
#     place, >= 3 firings ending mid-cell at ragged sizes, 2 ending inside a
#     giant line): the sets of chunk boundaries and sha256(chunk) differ ONLY at
#     the off-lattice EOF stops E: |full - delta| == C (full cells hit by a stop)
#     and |delta - full| == |E| + C, computed from the stub JSONL with RIG_PY —
#     no dedup semantics involved.
#   * a 300 KB line straddling a cell edge and a 2 MB line (both > SEND_MAX)
#     fragment IDENTICALLY in both passes (fragments are cut at fixed distances
#     from the line start); no chunk ever exceeds SEND_MAX.
#   * a from-0 RESEND of the delta session (offset rewritten to 0) is answered
#     duplicate:true for every chunk except the canonical chunks of the C split
#     cells (never sent whole under that session_uri): dup == N - C.
#   * a session SEEDED at a mid-cell line boundary sends exactly ONE new row (the
#     [seed, next B_k) piece) and then all duplicates.
#   * every REST request carries the 7 always-on flush_* stamps (flush_branch_of
#     rides only on forks).
# DEPENDS ON C10 (RIG): stub_contract keyed on (session_uri, content_hash) —
#   the dup== assertions read N-C under that key (the legacy hash-only stub would
#   read N); the stub records the flush_* fields.
. /rig/lib.sh

new_home lattice
stub_reset
cfg_rest
install_hooks >/dev/null
mkdir -p "$HOOKS/state"
printf '2000-01-01T00:00:00Z\n' >"$HOOKS/state/.checkpoints-trusted-since"

WORK="$HOME/work/latproj"
mkdir -p "$WORK"
BIG="$HOME/big.jsonl"
# ~4.2 MB of ragged lines with a 300 KB line planted near 900 KB (straddles a
# 200 KB cell edge, > SEND_MAX -> the oversize/fragment path) and a 2 MB line
# planted near 1.6 MB, then more ragged lines.
"$RIG_PY" - "$BIG" <<'EOF'
import json, sys
path = sys.argv[1]
w = i = 0
with open(path, "w", encoding="utf-8") as fh:
    def rag(upto, mul):
        global w, i
        while w < upto:
            pad = "x" * (37 + (i * mul) % 91)
            line = json.dumps({"type": "assistant", "i": i, "pad": pad}) + "\n"
            fh.write(line); w += len(line); i += 1
    rag(900000, 13)
    # non-repeating payloads: constant bytes would make SEND_MAX fragments
    # byte-identical, and a hash-keyed stub (or server) rightly calls those duplicates
    line = json.dumps({"type": "assistant", "big": "".join("%07d" % k for k in range(43000))}) + "\n"; fh.write(line); w += len(line)
    rag(1600000, 7)
    line = json.dumps({"type": "assistant", "huge": "".join("%08d" % k for k in range(250000))}) + "\n"; fh.write(line); w += len(line)
    rag(4200000, 11)
EOF
SIZE=$(wc -c <"$BIG")
export KB_FLUSH_CHUNK=200000 KB_FLUSH_SEND_MAX=260000 KB_FLUSH_MAX=209715200
note "final transcript $SIZE bytes, $(wc -l <"$BIG") lines (one ~300 KB line, one 2 MB line)"

dup_of() { # sid8 -> the dup=K value from that session's last ok/partial line
  grep " sid=$1" "$LOGF" | grep -oE 'dup=[0-9]+' | tail -1 | cut -d= -f2
}
# Per-uri chunk analysis straight from the stub JSONL: cumulative ends and
# sha256 of every chunk, in submission order. Usage: analyse <uri> <out-prefix>
analyse() {
  stub_requests | "$RIG_PY" -c '
import sys, json, hashlib
uri, out = sys.argv[1], sys.argv[2]
ends, hashes, pos = [], [], 0
for line in sys.stdin:
    try: r = json.loads(line)
    except Exception: continue
    if r.get("session_uri") != uri or r.get("transcript") is None: continue
    t = r["transcript"].encode("utf-8"); pos += len(t)
    ends.append(pos); hashes.append(hashlib.sha256(t).hexdigest())
open(out + ".ends", "w").write("\n".join(map(str, ends)) + ("\n" if ends else ""))
open(out + ".hashes", "w").write("\n".join(hashes) + ("\n" if hashes else ""))
' "$1" "$2"
}

# --------------------------------------------------- 1. from-0 pass (s-full)
step "a from-0 pass drains byte-for-byte, no chunk over SEND_MAX, zero duplicates"
T="$WORK/full.jsonl"
cp "$BIG" "$T"
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" s-full PreCompact)"
wait_log "flush .* ok bytes=.* sid=s-full" 180 || fail "s-full never drained: $(tail -2 "$LOGF" | tr '\n' '|')"
NFULL=$(stub_count '.session_uri=="session:s-full"')
assert_ge "$NFULL" "20" "the file was cut into many chunks ($NFULL)"
stub_concat '.session_uri=="session:s-full"' '.transcript' >"$HOME/re1"
if cmp -s "$T" "$HOME/re1"; then pass "from-0 reassembly is byte-identical"; else fail "from-0 reassembly differs: $(cmp "$T" "$HOME/re1" 2>&1 | head -1)"; fi
MAX1=$(stub_field '.session_uri=="session:s-full"' '.transcript_bytes' | sort -n | tail -1)
assert_le "$MAX1" "260000" "no chunk exceeds SEND_MAX (largest $MAX1)"
assert_eq "0" "$(dup_of s-full)" "the fresh pass has zero duplicates"
analyse "session:s-full" "$HOME/full"

# ------------------------------- 2. the multi-delta pass (s-delta, grown in place)
step "a multi-delta pass with EOF firings at arbitrary sizes cuts the SAME cells except the split ones"
T2="$WORK/delta.jsonl"
STOPS=""
NSTOPS=0
for S in 430000 1010000 1530000 2900000 3700000 "$SIZE"; do
  head -c "$S" "$BIG" >"$T2"
  SUMS=$(log_count ' (ok|partial) bytes=')
  fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$WORK" s-delta PreCompact)"
  wait_firing "$SUMS" 180 || { fail "delta firing at size $S never completed: $(tail -2 "$LOGF" | tr '\n' '|')"; break; }
  OFF=$(cat "$HOOKS/state/s-delta.offset")
  STOPS="$STOPS $OFF"
  NSTOPS=$((NSTOPS + 1))
  note "firing at size $S stopped at offset $OFF"
done
assert_eq "$SIZE" "$(cat "$HOOKS/state/s-delta.offset")" "the delta pass reached EOF"
stub_concat '.session_uri=="session:s-delta"' '.transcript' >"$HOME/re2"
if cmp -s "$BIG" "$HOME/re2"; then pass "delta reassembly is byte-identical"; else fail "delta reassembly differs: $(cmp "$BIG" "$HOME/re2" 2>&1 | head -1)"; fi
MAX2=$(stub_field '.session_uri=="session:s-delta"' '.transcript_bytes' | sort -n | tail -1)
assert_le "$MAX2" "260000" "no delta chunk exceeds SEND_MAX (largest $MAX2)"
analyse "session:s-delta" "$HOME/delta"
# The arithmetic of Appendix A, checked on the real cuts.
"$RIG_PY" - "$HOME/full" "$HOME/delta" $STOPS >"$HOME/verdict" <<'EOF'
import sys
full, delta = sys.argv[1], sys.argv[2]
stops = [int(x) for x in sys.argv[3:]]
fe = [int(x) for x in open(full + ".ends").read().split()]
de = [int(x) for x in open(delta + ".ends").read().split()]
fh = open(full + ".hashes").read().split()
dh = open(delta + ".hashes").read().split()
F, D = set(fe), set(de)
E = D - F                                    # off-lattice boundaries = EOF stops
offlat = [s for s in stops if s not in F]    # firings that stopped mid-cell
cells = list(zip([0] + fe[:-1], fe))         # full-pass cells [a, b)
C = sum(1 for a, b in cells if any(a < e < b for e in E))
fset, dset = set(fh), set(dh)
print("E", len(E)); print("OFFLAT", len(offlat)); print("C", C)
print("FULL_MINUS_DELTA", len(fset - dset)); print("DELTA_MINUS_FULL", len(dset - fset))
print("NFULL", len(fe)); print("NDELTA", len(de))
print("F_SUBSET_D_ENDS", int(F <= D))
# giant-line fragments: every chunk that does not end in a newline is a fragment;
# the SET of fragment hashes must be identical across the passes.
def frags(ends, hashes, t):
    data = open(t, "rb").read(); out = set(); a = 0
    for b, h in zip(ends, hashes):
        if data[b - 1:b] != b"\n": out.add(h)
        a = b
    return out
big = sys.argv[1].rsplit("/", 1)[0] + "/big.jsonl"
ff, df = frags(fe, fh, big), frags(de, dh, big)
print("NFRAG", len(ff)); print("FRAG_IDENTICAL", int(ff == df and len(ff) > 0))
EOF
v() { sed -n "s/^$1 //p" "$HOME/verdict"; }
note "E=$(v E) off-lattice stops, C=$(v C) cells hit; full-delta=$(v FULL_MINUS_DELTA) delta-full=$(v DELTA_MINUS_FULL); fragments=$(v NFRAG)"
assert_ge "$(v OFFLAT)" "3" "fixture: at least 3 firings stopped mid-cell"
assert_eq "$(v OFFLAT)" "$(v E)" "the off-lattice boundaries are EXACTLY the mid-cell EOF stops (nothing else moved)"
assert_eq "1" "$(v F_SUBSET_D_ENDS)" "every from-0 boundary is also a delta boundary (the lattice is start-independent)"
assert_eq "$(v C)" "$(v FULL_MINUS_DELTA)" "full-pass chunks missing from the delta pass == the C split cells"
assert_eq "$(( $(v E) + $(v C) ))" "$(v DELTA_MINUS_FULL)" "delta-pass chunks missing from the full pass == |E| + C"
assert_ge "$(v NFRAG)" "8" "the two giant lines were fragmented (>= 8 unterminated fragments)"
assert_eq "1" "$(v FRAG_IDENTICAL)" "the 300 KB and 2 MB line fragments are IDENTICAL in both passes"
C=$(v C)

# ------------------------------------------ 3. from-0 resend of the SAME session
step "a from-0 resend of the delta session is duplicate:true except the $C split cells"
# NO stub_reset here: the dedup key set IS the thing under test.
B3=$(stub_count '.session_uri=="session:s-delta"')
printf 0 >"$HOOKS/state/s-delta.offset"
SUMS=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T2" "$WORK" s-delta PreCompact)"
wait_firing "$SUMS" 180 || fail "the from-0 resend never completed"
N3=$(( $(stub_count '.session_uri=="session:s-delta"') - B3 ))
assert_eq "$NFULL" "$N3" "the resend cut exactly the from-0 chunk count ($NFULL)"
assert_eq "$((NFULL - C))" "$(dup_of s-delta)" "dup == N - C: only the split cells' canonical chunks were new under this session_uri (C10 key)"
assert_eq "$SIZE" "$(cat "$HOOKS/state/s-delta.offset")" "the resend reached EOF"

# --------------------------------------- 4. seed at a mid-cell line boundary
step "a session seeded at a mid-cell line boundary sends exactly ONE new row"
B4=$(stub_count '.session_uri=="session:s-full"')
# V = the byte after the last newline at or before 100000 (inside cell 1, not a B_k)
V=$("$RIG_PY" - "$T" <<'EOF'
import sys
data = open(sys.argv[1], "rb").read()
print(data.rfind(b"\n", 0, 100000) + 1)
EOF
)
if grep -qx "$V" "$HOME/full.ends"; then fail "fixture: V=$V is a lattice point"; else pass "fixture: V=$V is off-lattice"; fi
printf '%s' "$V" >"$HOOKS/state/s-full.offset"
SUMS=$(log_count ' (ok|partial) bytes=')
fire "$HOOKS/flush-session-to-kb.sh" "$(flush_event "$T" "$WORK" s-full PreCompact)"
wait_firing "$SUMS" 180 || fail "s-full re-fire from the seed never completed"
N4=$(( $(stub_count '.session_uri=="session:s-full"') - B4 ))
assert_eq "$((N4 - 1))" "$(dup_of s-full)" "exactly ONE new row ([seed, next B_k)), the rest duplicate"
assert_eq "$SIZE" "$(cat "$HOOKS/state/s-full.offset")" "the seeded pass reached EOF"

# --------------------------------------------------- 5. the flush_* stamps
step "every REST request carries the 7 always-on flush_* stamps (C10 stub capture)"
MISS=$(stub_requests | "$RIG_JQ" -c 'select(.session_uri=="session:s-full") | select((.flush_offset==null) or (.flush_size==null) or (.flush_bytes==null) or (.flush_event==null) or (.flush_mtime==null) or (.flush_box==null) or (.flush_project_by==null))' | wc -l | tr -d ' ')
assert_eq "0" "$MISS" "no s-full request is missing a flush_* stamp"
assert_eq "PreCompact" "$(stub_field '.session_uri=="session:s-full"' '.flush_event' | tail -1)" "flush_event stamped"
assert_eq "$V" "$(stub_field '.session_uri=="session:s-full"' '.flush_offset' | tail -n "$N4" | head -1)" "flush_offset of the first seeded chunk == the seed"
assert_eq "$SIZE" "$(stub_field '.session_uri=="session:s-full"' '.flush_size' | tail -1)" "flush_size == the transcript size"
assert_eq "firing" "$(stub_field '.session_uri=="session:s-full"' '.flush_project_by' | tail -1)" "flush_project_by stamped (a non-git launch dir -> firing)"

finish
