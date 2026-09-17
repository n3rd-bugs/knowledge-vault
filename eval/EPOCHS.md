# eval/EPOCHS.md — eval-epoch ledger

One entry per **eval epoch**: a version of `eval/golden.yaml` together with the floors the nightly
sentinel (`kbworker.eval_sentinel` → `kbworker.eval_regression`) holds it to.

**The rule.** `golden.yaml` and ranking changes land only at a **review boundary**, in **one
batched commit**, with a new entry here — never dribbled between reviews. Every report carries the
golden set's hash (`eval_golden_hash`); when the hash changes, the week-over-week baseline
(`kbcore.eval_baseline.previous_eval_metrics`) **resets** — tonight is compared only with reports of
the same epoch, never with a prior set whose case count or quantum differs. Floors are re-anchored
per the epoch's entry below, and `.env` (`KB_EVAL_MIN_HIT5`, `KB_EVAL_MIN_MRR`) is the live
anchor; the `kbcore.config` defaults (0.75 / 0.5) are vestigial and stay untouched.

Reading an MRR delta: one case slipping rank 1 → 2 moves MRR by exactly `0.5 / n_positive` (the
**quantum**). A per-case diff (`eval_case_ranks`, keyed by query text) names the slipped case; a
scalar never does.

| Epoch | Since | Cases (positive) | Quantum | Clean baseline hit@5 / MRR | Floors hit@5 / MRR | Golden hash |
|-------|-------|------------------|---------|----------------------------|--------------------|-------------|
| E0 | wave F (`9c7ab4e`) | 52 (52) | 0.5/52 = 0.0096 | 0.9808 / 0.9269 (review #9a, KB 32942) | 0.97 / 0.90 | n/a (key lands with E1) |
| E1 | 2026-09-16 | 61 (59) | 0.5/59 = 0.0085 | 0.9836 / 0.8975 (daytime replay 2026-09-16, fallback-free; expected first clean night 0.9836 / 0.889 — see below) | 0.97 / 0.86 (MRR re-anchored 2026-09-17, see night 1) | `f687077853bd` |

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
