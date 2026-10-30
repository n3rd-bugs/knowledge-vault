# eval/EPOCHS.md — eval-epoch ledger

One entry per **eval epoch**: a version of `eval/golden.yaml` together with the floors the nightly
sentinel (`kbworker.eval_sentinel` → `kbworker.eval_regression`) holds it to.

**The rule.** `golden.yaml` and ranking changes land only at a **review boundary**, in **one
batched commit**, with a new entry here — never dribbled between reviews. Every report carries the
**epoch key** (`eval_golden_hash`): the golden set's hash through E1; from E2 on, the first 12 hex of
sha256 over the golden bytes, `kb_embed_model` and the configured embed hosts, NUL-separated
(`kbcore.eval_epoch.golden_hash`; details under E2). When the key changes, the week-over-week
baseline (`kbcore.eval_baseline.previous_eval_metrics`) **resets** — tonight is compared only with
reports of the same epoch, never with a prior set whose case count or quantum differs — and an open
regression row from another epoch closes as `epoch changed`. Floors are re-anchored per the epoch's
entry below, and `.env` (`KB_EVAL_MIN_HIT5`, `KB_EVAL_MIN_MRR`) is the live anchor; the
`kbcore.config` defaults (0.75 / 0.5) are vestigial and stay untouched.

Reading an MRR delta: one case slipping rank 1 → 2 moves MRR by exactly `0.5 / n_positive` (the
**quantum**). A per-case diff (`eval_case_ranks`, keyed by query text) names the slipped case; a
scalar never does.

| Epoch | Since | Cases (positive) | Quantum | Clean baseline hit@5 / MRR | Floors hit@5 / MRR | Epoch key |
|-------|-------|------------------|---------|----------------------------|--------------------|-------------|
| E0 | wave F (`9c7ab4e`) | 52 (52) | 0.5/52 = 0.0096 | 0.9808 / 0.9269 (review #9a, KB 32942) | 0.97 / 0.90 | n/a (key lands with E1) |
| E1 | 2026-09-16 | 61 (59) | 0.5/59 = 0.0085 | 0.9836 / 0.8975 (daytime replay 2026-09-16, fallback-free; expected first clean night 0.9836 / 0.889 — see below) | 0.97 / 0.86 (MRR re-anchored 2026-09-17, see night 1) | `f687077853bd` |
| E2 | 2026-10-29 | 61 (59) | 0.5/59 = 0.0085 | 0.9672 / 0.8650 (gate 2026-10-29: two back-to-back replays, fallback-free) | 0.950 / 0.845 | `092b054e8e9e` |

## E0 — the 52-case set (wave F, `9c7ab4e`, through 2026-09-16)

The set that ran unchanged from wave F to E1: every case positive-bearing except the forbid-only
guards; harness K=5. The review #9a **clean baseline** (fallback-free replay, KB 32942) read
hit@5 0.9808 / MRR 0.9269 (48.2/52). The last E0 night, 2026-09-16 (report 38163), read
0.9808 / 0.9077 — two quanta under the clean baseline, both slips named by the per-case diff and
neither a case change:

- **`qpm_dnp_index_for_reg quantum_dnp_pointmap.c`** rank 1 → 2 in nightlies since 2026-09-10
  (report 33788): a near-tie at the top between 22247 (verified rm3 `docs/DNP3/README.md`, carries
  the symbol) and 33761 ("Summary — quantum", rm3-tester consolidation summary created 09-09, no
  symbol). A daytime replay on 09-16 read rank 1 again. Bound (≤3) holds.
- **`UART RX ring buffer ISR overrun fix`** 985 rank 1 → 2 since 2026-09-12 (report 37343): 35627
  (repeater note "UART RX overflow fix", unverified, created 2026-09-12; ord-0 cosine 0.68 to 985 —
  not a twin, no fold applies; 0.865 to its own-project sibling 2998) outranks the verified
  motor-ctl fix. Corpus composition from the fleet pushes; bound (≤3) holds.
- The 09-06..09-09 dip (0.9038 / 0.8814 / 0.8542 / 0.8638, fallback-free) fully recovered by
  09-09 (report 33421) — transient, coincided with the resend-flood extraction nights, not analysed
  further at E1.

Floors for E0 lived in the owner's `.env`: `KB_EVAL_MIN_HIT5=0.97`, `KB_EVAL_MIN_MRR=0.90`.

## E1 — 2026-09-16 (review #8 "E1 contents" KB 31857; 297/294 KB 31855; #9a rules KB 32942)

**What changed, in one commit:**

- **golden.yaml +9 cases, 7 positive** (n_positive 52 → 59; quantum 0.0096 → 0.0085), appended as
  the "E1 EVAL EPOCH — 2026-09-16" block: the bitemporal transaction-time pair on
  `mTLS fleet agent deferred config-sync` (forbid 101 by default; `include_superseded` finds 101
  itself at rank ≤3), the bitemporal valid-time pair on the 22480 bug (found as-of 2026-08-10,
  hidden as-of 2026-09-01), the 297 ↔ 294 must-not-fold guard (one `type=decision` query carrying
  both verified ADRs, 297 ≤3 and 294 ≤5), identifier-leg 2 (`ingest_resend_events chunk_key` →
  34161 ≤4; `offgrid_match OFFGRID_CONSECUTIVE_ROWS` → 32942 ≤3) and one `kb eval mine` case
  (`KB_RERANK_USE_CHUNK …` → 6495 ≤4). Every rank observed by the owner on 2026-09-16 with the local
  harness; bound = observed + 2 capped at 5.
- **Harness: `expect_item_id_raw`** — a positive criterion that asserts the id ITSELF (no
  `_chain_head` resolution), so a superseded revision can be asserted PRESENT under
  `include_superseded`; counts toward n_positive and MRR like `expect_item_id`.
- **Ranking: the collapse twin discriminator** — `search_rank.collapse_alternatives` folds only
  alternative_to pairs that satisfy the cross-project twin linker's own definition (both ord-0
  chunks embedded on the current embed model, cosine strictly above `kb_xtwin_threshold`), computed
  at rank time. The 4 manual ADR-alternatives edges (297→294 cosine 0.374, 302→294 0.667,
  374→373 0.677, 37→38 0.829) no longer fold; the 32 linker twins (0.935–0.999) still do. The
  297/294 pair in golden.yaml is the regression guard — never unlink, never delete the row, never
  flip `KB_COLLAPSE_ALTERNATIVES`.
- **Epoch key: `eval_golden_hash`** on every report; the WoW baseline compares only same-hash
  reports, so the first post-E1 night is not judged against a 52-case prior.

**Per-case diff and identifier-mass verdict (carried in the golden block header):** the two E0
slips above are the whole difference between the #9a clean baseline and the last E0 night.
Identifier-mass go/no-go (satisfier types, 18 nights 08-29..09-16): 33 verified / 17 unverified /
1 summary / 1 unsatisfied every night, summary-only cases 0 × 18, while summaries' top-5 slot share
rose 0.1577 → 0.2115 (55/260) — summaries fill slots without displacing satisfiers. **NO-GO on
identifier-mass demotion at E1**; the qpm near-tie is the one watch item for the review #9
summaries verdict; the SPI canary (`have we hit SPI problems anywhere`) stays untouched and
known-red.

**Floor rule, applied by the owner after the first live post-E1 run:**

- hit@5 floor **stays 0.97**: one structural miss passes (60/61 = 0.984), a second trips
  (59/61 = 0.967). hit@5 divides by ALL 61 cases (a forbid-only case counts as a pass); the MRR
  quantum divides by the 59 positive-criterion cases.
- MRR floor = **clean post-E1 baseline − 2 quanta (2 × 0.0085 = 0.017), rounded DOWN to 0.005**.
  Anchored on the EXPECTED first clean night, not the daytime replay: the 09-16 daytime replay read
  0.8975 (52.95/59) with the qpm case at rank 1, but every nightly since 09-10 has read that case
  at rank 2 (the near-tie above), so the expected nightly value is 0.8975 − 0.0085 = 0.889;
  0.889 − 0.017 = 0.872 → **0.870**. The first clean night is recorded below when it lands; the
  floor is NOT re-tuned on it unless it reads more than two quanta away from 0.889. **Applied
  2026-09-17:** night 1 read 3.3 quanta away (below), 1.3 of them structural, so the expected clean
  baseline is now 0.8777 and the floor moved to **0.860** (0.8777 − 0.017 = 0.8607, rounded down).

| Post-E1 field | Value |
|---------------|-------|
| Post-E1 daytime replay 2026-09-16 (fallback-free: rerank active 60 / off 1) | hit@5 0.9836 (60/61) / MRR 0.8975 (52.95/59); new positives: 101-raw 1, 22480 1, 297 1, 294 4, 34161 2, 6495 2, 32942 1 |
| Expected first clean night | 0.9836 / 0.889 (qpm at rank 2) |
| First clean night, measured | 2026-09-17 01:00 run (report 38551; the 23:16 boot catch-up, report 38206, read the same): hit@5 0.9672 (59/61) / MRR 0.8607, fallback-free — BOTH floors tripped. Per case: raw-101 at rank 3 (structural: the rm3 doc refresh minted fleet-agent README revision 38195 at 11:55 on 09-16, so 101 now trails 473 and 38195; bound ≤3 holds), `service dies at boot…` MISS (window-edge flapper, below); every other case at its E1 rank |
| Golden hash (`eval_golden_hash`) | `f687077853bd` |
| Applied `KB_EVAL_MIN_MRR` | 0.87 with the commit → **0.86 on 2026-09-17** per the rule above (`.env`, owner box); `KB_EVAL_MIN_HIT5` stays 0.97 |

**Night 1 read (2026-09-17).** The miss is a *window-edge flapper*: with reranking off,
`service dies at boot but starts fine manually afterwards` sits at fused rank 19 of the 20-item
rerank window (`kb_rerank_top_n`), and only the reranker lifts it to rank 1 — a query-vector
difference of order 1e-4 puts it at fused rank 21, outside the window, and it misses outright.
Measured on 09-17: the preferred embed host (`KB_EMBED_URL`, the owner's desktop GPU, Ollama
0.32.5) returns bit-identical vectors while its model stays loaded (cosine 1.000000 over repeated
calls) but differs across model reloads (0.999837 between two loads), and differs from the local
GPU by 0.999717; the case hit at rank 1 in six replays at 10:35 and missed in every replay from
10:47, on the same host. Its E0 history (09-06..09-09 ranks 1 → 2 → 3 → miss → 4 → 1) is the same
behaviour. hit@5 trips on any flap night by design (a second miss). Nothing in the golden set or
the ranking moves before review #9; E2 decides among widening the case's expectation, the rerank
window, the floor semantics, and pinning the embedder to one always-warm host (the Orin plan).

## E2 — 2026-10-29 (review #10; amends review #9's pre-registered E2, KB 40402 → superseded by KB 56704)

**Why E1 ended.** E1 read red on 60 of its 69 reports: its night-1 pair (09-16/17, hit@5 0.9672 under the
0.97 floor) and every report from 2026-09-24 on, except the two rerank-fallback measurement nights (09-30,
10-01). There was no ranking regression: no ranking or eval
code changed after `61737cb`, and every per-case move traces to data — new items, supersedes, summary
refreshes and curation.

The honest E1 read never rose above 59/61 = 0.9672. The seven 0.9836 reports (09-18..09-23) were
echo-satisfied (review #9). So two values were unreachable:
- the 0.97 floor, which allows one miss, and the canary always takes that miss;
- the 0.9836 recovery anchor on row 35091, frozen from an echo night.

The sentinel pushed a KB ALERT on 34 of 36 days. Every genuine per-case move after 09-24 surfaced only as a
different number inside that same alert.

**E1 restated** (case 48 counted as a miss wherever echo carrier 38552 satisfied it):

| Reports | Dates | Honest hit@5 / MRR | As read | Failing cases |
|---------|-------|--------------------|---------|---------------|
| 38206–38551 | 09-16/17 | 0.9672 / 0.8607 | same | 7, 48 |
| 39050–39744 | 09-18..23 | 0.9672 / 0.8523 | 0.9836 / 0.8607 | 7, 48 |
| 40202 | 09-24 | 0.9344 / 0.8475 | 0.9508 / 0.8559 | 7, 14, 48, 54 |
| 40399 | 09-25 03:15 | 0.9508 / 0.8489 | 0.9672 / 0.8573 | 7, 14, 48 |
| 40410–49676 | 09-25..10-13 | 0.9344 / 0.8593–0.8771 | same | 7, 14, 48, 54 (42198/42635 = fallback measurement nights, excluded) |
| 50226–56662 | 10-14..10-29 | 0.9508 / 0.8508–0.8763 | same | 7, 48, 54 |

**What changed — one commit (the one that adds this entry).**

1. **golden.yaml.**
   - Case 54 (the positive raw pin on 101 with `include_superseded`) is **retired** and replaced in place, so
     every other case keeps its index.
   - The new case 54 is a closed-chain raw pin on **28469**, with a new query. The chain is 21851/28469 →
     31854, a manual verified note with no source URI, so it can never be doc-refreshed. Its bound is the
     E2-day rank + 2, capped at 5: rank 1 at the gate, so **≤ 3**.
   - Case 53 (the forbid-101 guard) is unchanged.
   - Case 48 gets a comment-only KNOWN RED #2 annotation.
   - Every other case is byte-identical. Still 61 cases, 59 positive, quantum 0.0085.
2. **Echo rule** (`kbcore.eval_echo`, re-exported by `eval_criteria`; used by `eval_composition` too).
   - A hit whose title, snippet or body contains the case's normalized query, as whole words, keeps its slot
     but cannot satisfy an `expect_substr` case.
   - It applies only when the query has ≥ 4 words and is not identifier-dominant, using `search_legs`' own
     test. Id criteria are exempt.
   - Every carrier in the top-K is recorded, including those ranked below the satisfier
     (`eval_echo_skips` / `eval_echo_skipped`); the rank is still the first real satisfier.
   - A miss whose substring only echo carriers hold reads `CONTENT-ABSENT … (only echo carriers hold it)`.
     The content checks match substrings literally (LIKE metacharacters escaped).
3. **Detector** (`eval_regression`, `eval_detector_io`, `eval_persistence`).
   - Open `search_regression` rows from another epoch are closed as `epoch changed`, every one of them
     (at most 20 a night), and their anchors are never inherited (`eval_regression_epoch_closed`, plus
     `_count` past one). An other-epoch row newer than an open same-epoch row closes only once that row
     resolves. New rows carry `golden_hash`.
   - Floors are written to meta every night, with a warning when they sit at the code defaults.
     `eval_regression_cause` names the trip, and the WARNING prints the anchor the resolve test reads.
   - Drops are compared at the metrics' stored 4-decimal precision: a drop of exactly epsilon is no trip.
   - **Persistence trip** `eval_case_persistent`. A case trips when it passed on at least one same-epoch,
     fallback-free night and then failed on the 3 most recent such nights. Fallback nights are skipped,
     not reset, and the night's last clean run counts. The trip alerts, opens the single row, and blocks
     auto-resolve. It is reported by index and satisfier id.
     - The history window (60 reports) counts eligible reports only. A streak longer than the window
       stays armed: each case's newest listing in `eval_case_persistent` carries it, and a report that
       merely omits a red case cannot drop it.
     - A night whose persistence cannot be read is UNKNOWN (`eval_persistence_status='error'`): no
       persistence trip and no auto-resolve that night; the floors and drops still gate.
     - Tonight's night is the local, noon-rollover night (`digest_lines.night_of`) of `run_started_at`, a
       UTC stamp taken when the run starts.
   - The `eval_case_first_pass` notice fires when a long-red case turns green.
   - The digest caps the case lines and the first-pass notices at 5 each, plus one `+N more` line, so the
     other alerts survive its 20-line cap.
4. **Telemetry.** E2 adds **no line to the indexed report body** (`kbcore.report_meta.body_scalars`,
   final reviews FB-1 and F1): every key below lives in report meta only. Read it as the owner (`GET
   /api/items/{id}`, SQL) or in the journald `consolidation done` line.
   - Dicts and lists: `eval_case_passed`, `eval_case_top5`, `eval_case_satisfier`,
     `eval_case_fused_rank`, `eval_case_n_fused`, `eval_case_above_satisfier`, `eval_echo_skips`,
     `eval_case_fused_error`, `eval_case_persistent`, `eval_case_first_pass`.
   - Scalars: the embed identity — `eval_embed_model`, `eval_embed_hosts_key`, `eval_embed_host` (the
     serving hosts as runs, e.g. `A,B,A`), `eval_embed_fingerprint`, `eval_embed_fingerprint_host`,
     `eval_embed_runtime` (server build + model digest), `eval_embed_query_prefix_sha`, `eval_embed_device`
     — the floors (`eval_floor_hit_at_5`, `eval_floor_mrr`, `eval_floors_at_defaults`), `run_started_at`,
     `eval_echo_skipped`, the epoch-close keys and the two error statuses.
   - Why: a body line is indexed and one report lands every night, so its key-name words and values
     become keyword candidates, and one rare lexeme is enough for the OR fallback to admit an item. An
     index-only census found 9 golden cases sharing a lexeme with the identity and floor lines, 2 of them
     with two; the other E2 scalars share none today, but would be exposed at the next golden edit.
   - Reading the fingerprint: it is an exact-equality flag. A new value beside an unchanged
     `eval_embed_runtime` is a model reload (the preferred host is not load-deterministic), a query-prefix
     or device change (see the sha and device beside it) or a partial CPU offload, which nothing records.
     An unchanged value does not prove bit-identical vectors.
   - Sinks: every query-keyed dict stays out of log lines and stored job results
     (`kbworker.eval_cases.loggable`). A non-owner reading a run report (`GET /api/items/{id}`,
     `GET /api/timeline`) sees only its body scalars plus the `consolidation` flag (`kbcore.report_meta`);
     the owner sees everything.
     `GET /api/consolidate` shows a run's result to the owner only (`c73d0b9`, landed separately).
5. **Epoch key** = the first 12 hex of sha256(golden bytes ␀ `kb_embed_model` ␀ `embed_hosts_key()`),
   where `embed_hosts_key()` is the configured embed hosts in preference order, comma-joined and
   credential-free (`kbcore.eval_epoch`). Falling back to another serving host does not change it, nor
   does a credential, query or trailing-slash edit to a host URL. **Restart rule:** a change to
   `KB_EMBED_MODEL` or to the configured host list opens a new epoch, and ANY edit to `KB_EMBED_MODEL`,
   `KB_EMBED_URL`, `OLLAMA_URL` or the floors in `.env` needs a kb-worker restart (it runs on-demand
   consolidations in-process with the settings it started with).

**Known reds.**

- **Case 7** is the canary, kept red since 2026-08-04. The best satisfier chunk sits at exact vector rank 691.
- **Case 48** (satisfier 9832) is a **candidate-generation miss**.
  - Its best chunk sits at exact vector rank 44, but the vector leg window holds 40 rows. 25 of the 43
    closer chunks belong to items created after 2026-09-17.
  - It has 0 AND-keyword matches, and none of its 12 satisfiers is among its 34 fused candidates.
  - It was an honest miss on 69/69 E1 reports; its only passes were echo-satisfied (38552).
  - Review #9's "window-edge sentinel, keep unchanged" verdict is superseded. The criteria stay
    untouched. The fix is a candidate-generation change, which belongs to E3.

**Case 54 retirement record.**
- The raw-101 doc-refresh chain grew from 4 revisions to 7: 101 → 473 → 38195 → 39780 → 43713 → 50442 →
  53424.
- Each new revision cost 101 one rank: 3 → 4 at 40202, → 5 at 43847, → miss at 50532 (10-16). The README
  head in `eval_satisfier_ids` swapped on exactly those reports, plus 53573.
- Today 101 sits at fused 22, outside the 20-item rerank window. A 40-item window lifts it only to rank 4,
  still above its bound of 3.
- Superseded echo 38164 is re-admitted at fused 5 by `include_superseded`.

**Case 14 trace; review #9 D6 = NO** (stays a sentinel under the persistence trip).
- Satisfier 263 is vector #1 and fused #2. The reranker lifts 3856 (fused 18), 954 (11) and 40201 (14)
  above it, so it reads rank 5 (the gate's top-5: 3856, 954, 40201, 257, 263); with rerank off it reads
  rank 2.
- The miss from 09-24 to 10-13 began when summary 40201 appeared (created 09-23). It ended when the 10-13
  nightly refreshed 949 → 50177 and 37195 → 50153.
- Backtest of the persistence trip over E1 in the **E2 frame** (honest echo-rule reads, E2 floors, the new
  case 54 assumed passing): exactly this episode fires (night 09-25 → clear at 50226), nothing else, at
  every history window from 5 reports to unlimited.

**Floors** (`.env`, applied with the commit and before the first E2 run; kb-worker restarted).
- **hit@5** = (n − known_reds − 1)/n = (61 − 2 − 1)/61 = 0.9508 → **0.950**. That leaves one failure of
  slack beyond the two known reds. Single-case losses are the persistence trip's job.
- **MRR** = the lower of two back-to-back gate replays minus 2 quanta (0.0169), rounded down to 0.005:
  min(0.86497, 0.86497) − 0.01695 = 0.84802 → **0.845**.
- The backtest of E1 in the same E2 frame gives 0 floor trips. KB ALERT reports drop from 60 to the 32 of the
  case-14 episode.

**E1 environment changes recorded late.**
- **2026-10-11:** the reranker sidecar's host-RAM prompt cache was disabled with an `LLAMA_ARG_CACHE_RAM=0`
  drop-in on the live unit, and the nightly moved 01:00 → 01:30 (`47c288e`).
- **2026-10-25:** Ollama 0.31.1 → 0.40.11 on the KB host (`5686ac4`). Local embeddings sit at median
  cosine 0.99973 against 0.31.1 and are now bit-deterministic. The rerank sidecar was pinned to a 0.31.1
  runtime copy.
- **New rule:** an embed or rerank runtime upgrade is a ranking-input change and gets a ledger line here.
  `eval_embed_runtime`, `eval_embed_host` and `eval_embed_fingerprint` now record it.

**Gate (2026-10-29 15:22 UTC, read-only replays, `tool=None`, fallback-free).** Two back-to-back replays of
the E2 set on the live corpus, after 40402 → 56704, with the E2 code; rerank warmed (2 calls, ok).
- Both replays: hit@5 59/61 = **0.9672**, MRR **0.8650** (0.86497), failing exactly **{7, 48}**. Ranks:
  46 at 1, 9 at 2, 1 at 3, 1 at 5, and 4 without a rank (7, 48 and the two forbid-only cases).
- Case 7: one echo carrier skipped (13061). Case 14: rank 5 on 263 (fused 2). Case 54: rank 1 on 28469
  (fused 7) in both replays, so its bound is 3. No item holds case 54's query words in order.
- Embed: served by the preferred host only (`192.168.1.100:11434`); runtime `ollama 0.32.5
  qwen3-embedding:0.6b@ac6da0dfba84`, fingerprint `e7676e44450d`, device `auto`, prefix sha
  `df4b2898bf22`.
- The gated set keyed `d7abe903a1c7` (case 54's bound still the placeholder 5); with the bound written it
  keys **`092b054e8e9e`**, the key night 1 records.

**Night 1 (2026-10-30, 01:30–05:24 PKT, report 57358): clean, as expected.**
- `eval_golden_hash` `092b054e8e9e`; `eval_epoch_reset` {61 queries, 28 earlier reports skipped as
  another epoch}; floors 0.95 / 0.845 in meta, no `eval_floors_at_defaults`.
- Row 35091 resolved `epoch changed` at 00:23:47 UTC (`eval_regression_epoch_closed` 35091; the WARNING
  names `untracked`). `eval_regression` False with no cause, no open regression row, no KB ALERT.
- hit@5 0.9672 / MRR 0.8734, failing exactly {7, 48}; rerank active 60 / off 1 / fallback 0. Ranks: 49 at
  1, 8 at 2, 1 at 3, 1 at 5, and 2 without a rank (7, 48). Case 7 skips echo 13061, case 14 reads rank 5
  on 263, case 54 rank 1 on 28469. `eval_case_persistent` and `eval_case_first_pass` are empty. The drain
  had saved 522 new items since the gate.
- The report body carries no E2 line (103 lines).
- **Finding: the nightly's query prefix is not production's.** `eval_embed_query_prefix_sha` reads
  `945a97534ce6`; the gate's was `df4b2898bf22`. The systemd units load `.env` through `EnvironmentFile`,
  which keeps the `\n` inside the double-quoted `KB_EMBED_QUERY_PREFIX` as a literal backslash-n, and the
  process environment overrides pydantic-settings' own `.env` read, which would turn it into a newline.
  The search containers (kb-mcp, kb-rest) and shell runs such as the gate get the newline. It is the
  only one of the 38 `.env` keys that systemd parses differently, and `eval_embed_fingerprint` differs
  (`ece945c52a65`) for the same reason. So the nightly eval (and kb-worker's on-demand runs) has been
  embedding queries with a prefix production never uses, probably since the prefix setting landed
  (`01c9d4f`, 2026-07-18) — likely part of E1's day/night replay gap. The epoch key leaves the prefix out,
  so it did not move. The fix waits for an owner decision: it changes what the nightly measures, so it
  gets its own ledger line.

**Prefix fix (2026-10-30, KB 57359; the commit that adds this paragraph).** Owner-approved. `kbcore.config`
now decodes every literal backslash-n in `KB_EMBED_QUERY_PREFIX` into a newline (a field validator), so a
unit that loads `.env` through `EnvironmentFile` resolves the same prefix as the containers and shell
runs: `df4b2898bf22` (92 characters) everywhere, where the units had `945a97534ce6` (93). Neither `.env`
nor the units changed. On kb-worker's own environment (`/proc/<pid>/environ`) the previous code resolves
`945a97534ce6` and the fix resolves `df4b2898bf22`.
- Same-corpus A/B (2026-10-30 18:50 PKT): read-only replays on the live corpus, `tool=None`, two runs per
  prefix, alternating, rerank warmed, fallback-free. Both prefixes read hit@5 **0.9672** / MRR
  **0.8734**, failing exactly {7, 48}, and both runs of each were identical. No case's rank moved; 15 of
  the 61 top-5 id lists differ in other positions. So the fix moves no golden rank on this corpus. Night
  1's guess that the split explains part of E1's day/night gap is not supported here, and night 1's MRR
  above the gate's (0.8734 against 0.8650) comes from the corpus the drain changed (522 new items), not
  from the prefix.
- What night 2 records: `eval_embed_query_prefix_sha` `df4b2898bf22`, and `eval_embed_fingerprint` moves
  once, because the prefix is part of what it hashes. No code compares either across nights, so the
  change raises no alert. The epoch key leaves the prefix out, so E2 continues. Earlier nights were
  measured under the systemd prefix, and the readers that look back (the persistence trip, the trust
  week-on-week, the weekly series) read across that mix. On this corpus the two prefixes rank every case
  the same.
- Only `\n` is decoded, the one escape the prefix uses. Any other escaped `.env` value (`\t`, text after
  the closing quote) would still read differently under the units than in the containers. Whether the
  prefix joins the epoch key stays on the E3 list below.

**Deferred to E3.**
- Per-item leg windows and capped chunks per item in the OR leg (case 48).
- Rerank window 20/30/40.
- Reranker trust handling. In 7 cases (11, 22, 31, 32, 35, 59, 60) a verified fused-#1 satisfier is
  demoted to #2 behind an unverified extraction (5 cases) or a summary (2 cases): −0.059 MRR every night.
  The reranker is still net +0.054 MRR against rerank-off.
- Deterministic SQL tie-breakers. 53/61 fused orders differ between back-to-back calls.
- The summaries GO rule. Decide its reading first: 3 cases qualify on today's replay under the narrow
  reading, 6 under the literal one.
- Multilingual cases.
- The D7/D8 embedder pin.
- `eval_satisfier_unsatisfied_cases`.
- Whether `kb_embed_query_prefix` joins the epoch key (today a prefix edit is recorded, not keyed), and
  recording a partial CPU offload (Ollama `/api/ps` `size_vram` against `size`).
