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
| E3 | 2026-11-09 | 61 (59) | 0.5/59 = 0.0085 | 1.0000 / 0.9017 (gate 2026-11-09: two back-to-back replays, fallback-free) | 0.950 / 0.860 | `291ffdd667b2` |

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
- hit@5 0.9672 / MRR 0.8734, failing exactly {7, 48}; rerank active 60 / off 1 / fallback 0. Ranks: 47 at
  1, 8 at 2, 1 at 3, 1 at 5, and 4 without a rank (7, 48 and the two forbid-only cases). Case 7 skips echo 13061, case 14 reads rank 5
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

**Night 2 (2026-10-31, 01:30–07:14 PKT, report 57745): clean, on production's prefix.**
- `eval_embed_query_prefix_sha` `df4b2898bf22`; `eval_embed_fingerprint` `e7676e44450d`, the gate's value
  on the same host (night 1 read `ece945c52a65`). Key `092b054e8e9e`, floors 0.95 / 0.845 in meta.
- hit@5 0.9672 / MRR 0.8734, failing exactly {7, 48}, with night 1's ranks (47 at 1, 8 at 2, 1 at 3, 1 at
  5, and 4 without a rank (7, 48 and the two forbid-only cases)), as the A/B predicted. Case 7 skips echo 13061, case 14 reads rank 5 on 263, case
  54 rank 1 on 28469. Rerank active 60 / off 1 / fallback 0.
- `eval_regression` False, no open regression row, no KB ALERT; `eval_case_persistent` and
  `eval_case_first_pass` empty; the report body carries no E2 line.
- The run: success, 0 errors, 57 routine warnings, 178 rows drained to 311 items (queue empty), eval step
  94 s. It ran 5 h 44 m against night 1's 3 h 54 m, the extra time in review triage (plus an 83-minute
  top-up pass), evidence refresh and contradiction queueing, not in the eval.

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

## E3 — 2026-11-09 (owner go 2026-11-06 on a measured recommendation; pack ~/kb/packs/e3-2026-11-05/, pre-registration PREREG.md there)

**Why E2 ended.** E2 carried two known reds: 7, the canary, kept red on purpose, and 48, whose fix it deferred to E3
as a candidate-generation change. The 2026-11-02 screening A/B read that as vector-leg depth plus a wider rerank
window. The E3 measurements found the cause in another leg, and it was 7's cause too.

**The cause.** The OR-coverage query is the keyword leg whenever AND finds fewer than 5 chunk rows: 57 of the 61
golden cases. It sorted chunk rows by an ITEM-level coverage key, so all of a long document's chunks sort together,
and its LIMIT 40 counted rows. On 150 sampled real queries those 40 rows held a median of 4 distinct items, and fewer
than 40 on 149 of them. On case 48 they held 1 (2 in a 200-row window). In place since `c602753` (2026-07-17). E2's
known-reds note blamed the vector window, though its deferred list had also named OR-leg chunk caps: item 23, case 48's
rank-1 answer on 2026-08-08, was crowded out of the OR leg. 9832 at vector rank 44 was only the nearest other satisfier.

**What changed — one commit (the one that adds this entry).**
1. `kbcore.search_legs._or_coverage_rows` returns one row per item, ordered by coverage, then that row's rank, then
   newest item. `LIMIT 40` now counts items. Three rules travel with it:
   - An item's row is its best chunk over the KEPT lexemes, not all of them. Ranked over all of them it was the chunk
     with the most stopwords, and that chunk is what the snippet and the reranker read for an item only this leg found.
   - `sum(w ORDER BY w)`: float addition depends on order, and under a hash plan items with equal weights differed by an
     ulp, which outranked every tie rule below it.
   - Chunk text is joined only for the final 40.
2. `kbcore.rerank.rerank_fused` assigns strictly decreasing scores, so the reranker's order survives `_to_hits`. Without
   this, tied RRF scores fell to trust and recency: the final top-5 left the reranker's top-5 on 6 of 150 real queries.
3. `kbworker.eval_epoch_lines.escalates`: a repeat night of a persistence trip already reported (row open, nobody new
   joined, no floor or drop) goes out as an ordinary digest. 7 and 48 are now armed and sit near an edge (below); without
   this a relapse would push a high-priority KB ALERT every night until the next boundary, which is E1's failure. The one
   alert is best-effort: a failed push on the trip night is not retried, and the EVAL lines still lead later digests. A second
   case that first lists past night 3 (only after an unreadable history night) joins without its own alert.
   `eval_persistence_repeat` is the new meta key, out of the report body by its prefix.
4. `kbcore.search._to_hits` reads `left(body, 300)`, not the whole body: the same snippet, for every fused candidate.
5. `golden.yaml`, comments only: case 7's and case 48's annotations, and the canary mention in case 6's. Every case
   parses identical. Still 61 cases, 59 positive, quantum 0.0085. Any byte change opens the epoch.
6. This ledger: the E2 night-1 and night-2 rank census read 49 at 1 and 2 without a rank; reports 57358 and 57745 say 47
   at 1 and 4 without a rank (7, 48 and the two forbid-only cases). Corrected above. **Restart rule, extended:** a landing
   that changes ranking code merges while kb-consolidate is inactive, restarts kb-worker right after the merge (it holds
   `search_legs` in memory and would key the new epoch on old code), then rebuilds the image and recreates the four
   HTTP services (the nightly reads the host tree, the services read the image).
Not changed: the AND query, the vector leg, the identifier legs, the rerank window (20), its timeout, the warm-up.

**Candidates measured** (read-only replays on one corpus, 2026-11-05, `tool=None`, warmed, fallback-free. Golden cells
are two identical runs, except those marked one run. Sample 1 = 150 real queries, no boost. Sample 2 = 200 fresh real
queries with `boost_project` set to the logged principal's project, the production call shape. nDCG@5 is against E2.
Raw tables: `final/tables.txt` in the pack.)

| Variant | hit@5 | MRR | Fails | Sample 1 nDCG@5 diff | Sample 2 nDCG@5 diff |
|---------|-------|-----|-------|----------------------|----------------------|
| E2 (0.7760 / 0.7588) | 0.9672 | 0.8805 | 7, 48 | | |
| Row windows 200 → 40 items, R=40 (the screening winner) | 1.0000 | 0.9093 | | +0.0184 | |
| The same at R=30 | 0.9836 | 0.8932 | 48 | | |
| W=60, R=30 / 40 / 50 | 0.9836 | 0.8862-0.9082 | 48 or 7 | | |
| OR by item as first written, R=20 | 1.0000 | 0.8932 | | +0.0106 | |
| **OR by item with the kept-lexeme rule and strict order, R=20 (E3)** | **1.0000** | **0.8932** | | **+0.0097 (-0.0180, +0.0392)** | **+0.0217 (-0.0026, +0.0471)** |
| + no project boost for OR-only items (one run) | 1.0000 | 0.8932 | | | +0.0200 (-0.0037, +0.0454) |
| + the 12 longest lexemes instead of the alphabetically first 12 (one run with strict order) | 1.0000 | 0.8932 | | +0.0055 | |
| E3 at R=30 (golden: one run, first-written query) | 1.0000 | 0.9017 | | | +0.0263 (+0.0047, +0.0505) |
| E3 at R=40 (golden: one run; golden and sample 1: first-written query) | 1.0000 | 0.9093 | | +0.0250 | +0.0194 (-0.0047, +0.0453) |

Per-item windows on the keyword-AND query changed no result on 150 of 150 queries; on the vector leg, 6 of 150 top-5
sets, a 2-2 panel. Neither landed.

**Gates.** Written before each run; PREREG.md carries the order they were written in, addenda included. The judged
readings of real queries disagree in size and in sign, and none shows a large effect. Two written lines were missed:
- G1 golden: eligible. No case lost; case 45 slips 1→2 and passes.
- G2 H1 on E3's code, mined search→get pairs (these favour the old ranking): top 50, 0 exits, 1 entry; top 200, 2 exits, 4
  entries; MRR on confirmed items 0.0918 → 0.0968. The screening winner read 4 exits, 0 entries, 0.0827 on the same pairs.
- G2b, blind list-vs-list panel on sample 1, as first written: 99 of 150 top-5 sets differ; E3 30, E2 38, tie 31, so 44.1% of
  decided queries (p = 0.40). **The written line was 45%. This missed it by one query.**
- G2c, pooled graded relevance (added after the miss, rule written before it ran): two blind judges per batch, exact
  agreement 91.9% and 93.2%. Sample 1 +0.0097, up 38 down 37: passes.
- Boost, first read: 78 queries of sample 1 replayed under boost, judges not told the author's project: E3 -0.0253 (-0.0735,
  +0.0189). **That FAILED Addendum 7's line (>= -0.01, CI lower >= -0.03), whose rule was fix or do not land.** No fix landed. The
  one tried (no boost for OR-only items) did nothing, and the window's local share was not up (10.5 → 9.3 of 20).
- Boost, second read (Addendum 8, written after the failure, replacing it): sample 2, judges told the author's project: +0.0217, up 57
  down 39: passes. So: no harm at the written margin on the larger sample with the better-informed judges, one earlier failed read on
  a smaller one, no proven gain. Selection took E3 over Addendum 8's first-listed variant (no boost for OR-only items): E3 passes without that patch and is
  strictly smaller, 0.0017 apart.
- The owner decided on that reading: `Please contine as recomended` (2026-11-06, answering "Should I land E3? I recommend yes on 11-09 after the fifth morning").
- G4 H2, five mornings, base and candidate twice each: five PASS lines, 2026-11-05 through 2026-11-09, all on the final code (the first-written code's 11-05 run is kept apart in `h2/first-written-*`). Base 0.9672 / 0.8805 on 11-05 to 11-08 and 0.9672 / 0.8720 on 11-09; candidate 1.0000 / 0.8932, then
  1.0000 / 0.9017 on 11-09. Both 11-09 moves are case 45, one quantum each: E2 1 → 2 and E3 2 → 1, as the corpus changed under a
  near-tie between its satisfier 56693 and summary 33761. Every morning: no case lost, no candidate failure, 0 fallbacks in four
  replays, case 48 rank 1 on 23, case 7 rank 4, case 14 rank 5; rerank p95 0.43-0.82 s (0.82 the morning that ran beside the
  nightly, PREREG Addendum 9).
- Retro cuts (corpus at `created_at <= D`, today's statuses): 10-01 and 10-15 fail {34} where E2 fails {7, 34}; 10-29 none where
  E2 fails {7}; 11-02 none where E2 fails {7, 48}. No cut loses a case.
- Rerank off (the fail-open path): E2 0.9344 / 0.8023, E3 0.9344 / 0.8734, both failing {7, 48, 51, 54}.
- Rerank timing. R is unchanged, but its input grew: 12,971 → 16,375 characters per call, from longer chunks entering the window.
  Alternating pair at load ~10: p50 0.398 → 0.482 s, p95 0.530 → 0.614 s, max 0.587 → 0.640 s. So R=20 keeps its margin at the 2 s timeout.
- Cost, alternating pairs at load ~10: the OR query p50 226/234 → 212/214 ms, p95 639/652 → 584/596 ms, max 814/865 → 750/762 ms over sample 1,
  fewer chunks ranked with the kept lexemes. Filtered searches (project small, mid, big; type; tag) unchanged within noise. Fused candidates
  median 44 → 69 on golden, 43 → 79 on sample 2; `search()` on golden, alternating pair: p50 0.858, 0.834 → 0.969, 0.972 s; p95 1.786, 1.717 → 1.831, 1.722 s.
- For E4's reading, R=40 on the screening winner's lighter input (26,782 characters per call; E3's family reads 31,302): serial p50 0.64 s, p95
  0.80 s at load 6, p95 1.15 s at load ~27; 2 clients p95 1.28 s, 0 fallbacks; 3 clients p95 1.88 s, 3 of 180 fall back. A FIFO model over 30 days
  of real search times, excluding one 469-search burst on 10-08, puts 2-8% of searches over the 2 s timeout at that R, against 0% at R=20.
- Gate 2026-11-09 (09:40-09:42 PKT): two back-to-back in-tree replays: hit@5 61/61 = 1.0000, MRR 0.9017 (0.90169), no case fails. Ranks: 49 at 1, 7 at 2, 2 at 4, 1 at 5, and 2 without a rank (the forbid-only cases); 11-05 read 48 at 1, 8 at 2,
  the difference being case 45. Ranks and top-5 lists are identical to morning 5's candidate replays. The gate ran on the tree rebased
  onto `e4d7c5e` (the KB 61594/61595 fixes, which touch no ranking file); H2 measured the same code on `08f9890`. This landing's
  id-only KB note (62790) was saved before the gate and enters no top-5. Rerank active 60 / off 1 / fallback 0. Embed: served by the preferred host only (`192.168.1.100:11434`); runtime `ollama 0.32.5
  qwen3-embedding:0.6b@ac6da0dfba84`, fingerprint `e7676e44450d`, prefix sha `df4b2898bf22` (the 11-09 nightly's `eval_embed_*`). pgvector `0.8.4`. The gated set keys **`291ffdd667b2`**.

**Collateral.** 32 of 61 top-5 lists change, 25 of them as sets; top-1 changes on 45, 47, 48, 52. Echo slots 1 → 1; eval-day ids in top-5
slots 14 → 16. 56680, 56701 and 57360, flagged by the screening critic, are repo docs from the nightly scan, not eval notes. Case 34's reranker
input: unchanged on the 13 window items E2 and E3 share. Leg fill: 150 of 150 OR reads reach 40 items (E2: 1 of 150).
`eval_top5_unverified_share` 0.623 → 0.584, `eval_top5_summary_share` 0.200 → 0.216, `eval_top5_verified_lift` 18.43 → 20.36, so the trust
watch should stay quiet (its week-on-week resets on query count, not epoch, and compares E3 with E2 for up to six nights). On real queries the
change is broad: 116 of 200 top-5 sets differ on sample 2.

**Known reds: none. Four passes sit near an edge (ranks as of 11-05; re-read at the gate on 11-09: unchanged, except case 48's OR
rank, 6 → 7).**
- **Case 7** is no longer red. Rank 4 on 963 (fused 8; the case's best fused rank is 7, on 961), behind echo 13061 at rank 1. It stays the canary:
  a relapse means vague cross-project recall regressed.
- **Case 14** stays at rank 5 on 263; fused 2 → 5.
- **Case 59** reads rank 4, from a new summary (58880) on 11-03, unchanged by E3. Its satisfier sits behind items the reranker scores near 1.
- **Case 48** reads rank 1 on 23 with a reranker score of 0.94 against 0.66, so inside the window it is safe. Getting into the window is the edge: 23
  is OR-only, fused 14 of 20, at OR rank 6, and it leaves R=20 near OR rank 9-10. Corpus cuts by `created_at` over today's statuses read OR rank 1 at
  10-29, 3 at 10-30, 4 at 10-31, 6 at 11-03; that is not the real history (a refreshed document is missing from earlier cuts), so the slope comes from
  the forward series in `h2/verdicts.txt`: OR rank 6 on 11-05 to 11-08 and 7 on 11-09, fused 14 every morning.
  One rank in four days would leave roughly 8-12 days before 23 exits R=20 near OR rank 9-10, but five mornings are too few for a
  rate, so the margin is unknown beyond about a week. If it relapses it costs 1/59 of MRR and one case
  of hit@5, the persistence trip alerts once, and the digest keeps saying so.
- A case red on night 1 never arms (the rule needs an earlier pass in the epoch). So night 1's read checks by hand that 7 and 48 passed.

**Floors** (`.env`, applied with the commit and before the first E3 run; kb-worker restarted). This keeps E2's arithmetic with known_reds = 2:
7 and 48 pass today, but they are boundary passes kept inside the slack, so one night's relapse is the persistence trip's job, not a floor's.
- **hit@5** = (61 − 2 − 1)/61 = 0.9508 → **0.950** (unchanged). Trips at the 4th simultaneous failure.
- **MRR** = lower gate replay − the reciprocal mass of 7 and 48 in that replay ((1/4 + 1/1)/59 = 0.02119 at the gate's ranks 4 and 1) − 2 quanta (0.01695),
  rounded down to 0.005: **0.860** (0.90169 − 0.02119 − 0.01695 = 0.86356; 11-05's 0.8932 would have given 0.855). Trips past about 2.25 of reciprocal lost, about a third rank-1 failure.

**Comparability.** Fused-rank and fused-count telemetry does not compare across the boundary. Thin-miss counts in fallback mode will fall, because more top
hits have a keyword contribution; that is not a quality gain. No `eval_case_first_pass` fires for 7 or 48: their red history belongs to E2.

**Night-1 predictions.** `eval_golden_hash` the new key; `eval_epoch_reset` present; `eval_case_first_pass` and `eval_case_persistent` empty; floors in meta;
no `eval_regression`; rerank tally active 60 / off 1 / fallback 0; one digest line for the new epoch. By hand, by index, printing no key: case 7's and case
48's verdicts pass, their satisfiers read 963 and 23, and no echo carrier beyond 13061.

**Deferred to E4.**
- The OR key itself: item-level coverage favours long documents, which is what is sliding case 48. A best-chunk coverage key should hold its margin better. Unmeasured.
- Rerank window. R=30 reads best under boost and clears zero, at +0.46 s p50 and +1.13 s p95 on sample 2 (a loaded hour; +0.25 s p50 on golden) and the queueing
  above. It needs the timing gate, and 156 of 1,208 real searches are hook repeats of the same query within 2 s: cutting those first would pay for part of it.
- The sidecar under host CPU load: R=40 p50 0.64 s at load 6, 0.93 s at load ~27. No priority or thread setting measured.
- `lexemes[:MAX_OR_LEXEMES]` runs on alphabetical order: 57 of 150 real queries are over 12 lexemes, 207 rare lexemes cut. Longest-12 did not help (above); a real
  rarity cut has not been tried.
- A lexeme with df = 0 passes the df filter and disables the all-common fallback. `websearch_to_tsquery` re-parses lexemes, turning signed numbers into NOT.
- Deterministic tie-breakers on the AND query: E2's "53/61 fused orders differ" did not reproduce (0/61 between replays on 2026-11-05).
- `exact_id_match_counts` scales with the candidates' total chunks; not a problem measured end to end, worth a phase timing.
- With `expand=true`, more seeds are two-leg, so query-blind neighbours can take window slots. Re-arm the expansion A/B before raising `KB_GRAPH_EXPAND_HOPS`.
- Carried over: reranker trust handling, the summaries GO rule, multilingual cases, the D7/D8 embedder pin, `eval_satisfier_unsatisfied_cases`, the query prefix in
  the epoch key, recording a partial CPU offload.
