# Knowledge Vault model-upgrade research report — 2026-07-18

## 1. TL;DR

- **Extractor: keep qwen3:14b as default.** The only challenger worth benchmarking now is the already-pulled **Qwen3.6-35B-A3B** (`qwen3.6:latest`, Apache 2.0) — live-verified on our 4080S at **52.9 tok/s** with CPU offload under plain Ollama 0.31.1. Its "beats dense 27B on coding" marketing claim was **refuted** (73.4 SWE-bench Verified is *below* Qwen3.5-27B's 75.0), so it must earn the slot purely via our SEC-3 grounding eval. All dense 27B-class candidates are **ruled out** on this box (17GB Q4 > 16GB VRAM; dense offload measured at ~6.5 tok/s).
- **Cheapest real win: embedder swap** bge-m3 → **qwen3-embedding:0.6b** — identical 1024 dims (reembed-only, no migration), +13.4 MTEB Code (75.41 vs 62.04, https://arxiv.org/html/2506.05176v1), Apache 2.0, official Ollama tag. Gate on an eval-v2 A/B with the instruction prefix, and add a post-upgrade non-zero-embedding smoke test (known Ollama pooling regressions).
- **Add an interactive rerank leg**: **Qwen3-Reranker-0.6B** Q4 (0.37GB) via a llama-server sidecar (Ollama cannot serve rerankers — PR #7219 closed unmerged). Anthropic's measurement shows reranking roughly halves residual retrieval failures on an already-hybrid pipeline (5.7%→2.9%→1.9%, https://www.anthropic.com/engineering/contextual-retrieval).
- **Add a nightly verify stage** (extract → cross-check): NuExtract3-4B's native `verbatim-string` template (verified) and/or an Apache-licensed MiniCheck-class judge, with **factored** verification (verifier sees only claim+transcript) and verdicts that **demote to review, never delete**. Note Bespoke-MiniCheck-7B is CC-BY-NC → license-blocked; use the 770M Flan-T5 variant.
- **Do not**: re-benchmark gemma4:12b yet (the Jul-15 weight refresh has **not** reached Ollama's registry — re-pulling today re-tests the failed weights), switch to vLLM, add spec-decode to any MoE, or adopt any CC-BY-NC model (jina v5/v3, zerank-2, ctxl-rerank, Bespoke-7B).

---

## 2. Model upgrade candidates by role

### 2.1 Extractor (nightly batch, SEC-3 verbatim grounding is the gate)

| Candidate | Verdict | Key evidence |
|---|---|---|
| **qwen3:14b (incumbent)** | **Keep as default** | Won our 2026-07-03 bench 9.2/10 with 100% grounding; 61.85 tok/s fully resident on this exact card (https://www.glukhov.org/llm-performance/benchmarks/choosing-best-llm-for-ollama-on-16gb-vram-gpu/). |
| **Qwen3.6-35B-A3B** (`qwen3.6:latest`, 23GB Q4, already pulled) | **Benchmark-first** | VERIFIED: Apache 2.0, 35B/3B-active, 262K ctx (https://huggingface.co/Qwen/Qwen3.6-35B-A3B). Live-tested on our box: **52.93 tok/s eval / 36.18 tok/s prompt, 30.4s load** with partial CPU offload — ~20s per 1000-token item, inside the 30-60s budget. CAVEATS: (a) coding-superiority claim **REFUTED** — 73.4 SWE-bench Verified loses to Qwen3.5-27B (75.0) and Qwen3.6-27B (77.2); (b) the sourced "--cpu-moe 30-50 tok/s on 16GB" path was **REFUTED** (llama.cpp-only flag; Ollama lacks expert offload, https://github.com/ollama/ollama/issues/11772) — our own Ollama whole-layer measurement supersedes it; (c) cannot be VRAM-resident — transient nightly load only (keep_alive=0). **Must confirm**: SEC-3 strict verbatim-substring rate ≥ qwen3:14b's 100% on the item-313-style set; typed-item quality; tok/s at real 8-16K transcript contexts; ~10GB RAM spillover acceptable. |
| **NuExtract3-4B** (specialist, second-pass) | **Benchmark-first** | VERIFIED: Apache 2.0, fine-tuned on Qwen3.5-4B, official GGUF Q4_K_M **2.71GB**, runnable via `ollama run hf.co/numind/NuExtract3-GGUF:Q4_K_M`; template has a native **`verbatim-string`** type ("extract text exactly as it appears in the document") — the only model found with an SEC-3-shaped primitive (https://huggingface.co/numind/NuExtract3, https://huggingface.co/numind/NuExtract3-GGUF). Release date corrected to **May 19, 2026** (https://about.nuextract.ai/blog/nuextract-3-release). Its 0.651-vs-0.479 win over Qwen3.5-9B is vendor-run, on *vision documents*, scored with indel distance that tolerates near-verbatim — it does **not** prove strict-substring compliance. **Must confirm**: verbatim-string output survives our exact-substring validator on text transcripts; template-paradigm fit as a quote/field re-extraction stage (it is not a drop-in free-form extractor). |
| **Granite 4.1 8B** (`granite4.1:8b`, 5.3GB) | **Benchmark-first** (fast-fallback slot) | VERIFIED: Apache 2.0, Ollama library, 128K ctx, dense (walked back 4.0's MoE; 8B "matches or outperforms" the old 32B-A9B, vendor-reported) (https://ollama.com/library/granite4.1, https://research.ibm.com/blog/granite-4-1-ai-foundation-models). Training QC flagged non-grounded RAG responses as hallucinations (https://huggingface.co/blog/ibm-granite/granite-4-1) — a grounding *culture* signal, training-time only, not verbatim-quote evidence. Candidate to replace gemma3:12b as fast fallback. **Must confirm**: SEC-3 rate; JSON reliability on our item schema. 30b tag (17GB) does not fit — 8b/3b only. |
| **gemma4:12b re-benchmark** | **Defer** | The Jul-15 stealth refresh is real on HF (tool-calling/template fixes, https://www.techtimes.com/articles/320775/20260716/gemma-4-gets-stealth-update-h100-speed-gains-tool-calling-fixes-no-version-bump.htm) but **REFUTED as actionable**: Ollama's `gemma4` tags predate the refresh, so `ollama pull` today re-tests the same weights that failed grounding at ~50%. No source claims the refresh touches grounding. Action: watch https://ollama.com/library/gemma4 for a post-Jul-15 update, then re-pull (Ollama ≥0.32.1) and re-run the grounding eval — low priority. |
| **qwen3.5:9b** (6.6GB) | **Skip as upgrade** (speed fallback only) | VERIFIED: 6.6GB, 256K ctx, family has no 14B (https://ollama.com/library/qwen3.5). Scored 0.479 with 170/600 hard failures on NuMind's bench (verified, though partly an inference-config artifact) — no accuracy case. |
| **Qwen3.6-27B dense** | **Skip** | Facts verified but feasibility **REFUTED**: smallest quant is 17GB > 16GB VRAM (https://ollama.com/library/qwen3.6/tags), and dense partial offload measured **6.48 tok/s** on this exact card — ~2.5min/item, outside budget (https://www.glukhov.org/llm-performance/benchmarks/choosing-best-llm-for-ollama-on-16gb-vram-gpu/). MoE is the only way up on this box. |
| Mistral Small 4 (119B), Llama 4 (≥109B), GLM-5/5.2 (744B), DeepSeek R1 distills (Jan-2025 stale), Aion 1.0 (weights not shipped), Phi-5 (unreleased) | **Skip / watch** | Negative results: nothing fits or nothing exists to pull (https://mistral.ai/news/mistral-small-4/, https://www.techtarget.com/whatis/feature/Meta-Llama-4-explained-Everything-you-need-to-know, https://epoch.ai/models/deepseek-r1-distill-qwen-14b, https://azure.microsoft.com/en-us/products/phi/). Re-check Aion Instruct on HF in August. |

### 2.2 Triage judge (~50 rows/night)

- **Granite Guardian 4.1 8B** (`granite4.1-guardian:8b`, 6.9GB Q6_K) — **benchmark-first**. VERIFIED in the Ollama library with pre-baked groundedness/context-relevance/answer-relevance criteria (https://ollama.com/library/granite4.1-guardian); Guardian line reports AUC 0.854 on RAG-hallucination (https://arxiv.org/pdf/2412.07724). Constraint: 6.9 + 9.3GB > 16GB — it **cannot co-reside** with qwen3:14b; swap it in during the nightly window only (50 rows is minutes). Confirm the exact license variant before adoption. It is a yes/no judge — never an extractor.
- Fallback: keep qwen3:14b as judge, or use gemma3:12b for cross-family decorrelation (see §3).

### 2.3 Embedder (interactive, <2s)

- **qwen3-embedding:0.6b** — **benchmark-first, adopt on win** (strongest single recommendation). Same 1024 dims as bge-m3 → reembed-only (~1h, 5k chunks), **no schema migration** (https://huggingface.co/Qwen/Qwen3-Embedding-0.6B). MMTEB 64.33 vs 59.56, and the headline for an engineering-notes corpus: **MTEB Code 75.41 vs bge-m3's 62.04** (https://github.com/QwenLM/Qwen3-Embedding, https://arxiv.org/html/2506.05176v1). Caveats: multilingual retrieval subscore is a near-tie (80.83 vs 80.76) — the win is English+code; scores assume the **instruction prefix** on queries; Ollama has a recurring pooling-regression history for this family (https://github.com/ollama/ollama/issues/12368, /12633, /12611) → pin the Ollama version, use only the official library tag, and gate the reembed behind a non-zero/non-degenerate embedding smoke test. ~100ms/query is fine.
- **snowflake-arctic-embed2:568m** — designated **fallback** (1024-dim, Apache, official tag, very fast) if Qwen pooling quirks bite (https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0).
- **Skip**: qwen3-embedding:4b/8b (2560/4096-dim → migration; 8B has an all-zero-vector bug report, https://github.com/ollama/ollama/issues/12611, and doesn't fit residency); jina-v5-small (best small model but **CC BY-NC** — hard blocker, https://jina.ai/models/jina-embeddings-v5-text-small/); jina-v4 (Qwen research license); nomic-v2-moe (512-token ctx regression, https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe); granite-embedding-r2, EmbeddingGemma (768-dim migration + weaker).
- Note: **no bge-m4 exists** (https://github.com/flagopen/flagembedding) — bge-m3 is beaten, not obsolete; keeping it is defensible until the A/B says otherwise.

### 2.4 Reranker (new leg, interactive)

- **Qwen3-Reranker-0.6B** — **benchmark-first, leading pick**. Apache 2.0, 32K ctx (no truncation of ~500-token candidates), 65.80 MTEB-R (https://huggingface.co/Qwen/Qwen3-Reranker-0.6B). Serving reality: **Ollama cannot host it** (rerank PR #7219 closed unmerged, https://github.com/ollama/ollama/pull/7219; TEI unsupported, https://github.com/huggingface/text-embeddings-inference/issues/691) → run a **llama-server sidecar** (`--reranking --pooling rank`), Q4_K_M **0.37GB**, resident beside 9.3+1.2GB with headroom. Known failure mode: broken community GGUFs return ~4.5e-23 garbage scores (https://gist.github.com/VooDisss/42bce4eb5c76d3c325633886c5e348ee); verified-working conversions exist (https://huggingface.co/Qwen/Qwen3-Reranker-0.6B/discussions/22) and llama.cpp #16407 flags build-specific regressions — **AC must include a golden relevant/irrelevant pair ordering correctly**.
- **Ettin 150M** (May 19, 2026, Apache) — benchmark-first alternative: 0.5994 nDCG@10 MTEB-eng-v2, edging Qwen3-Reranker-0.6B, 8K ctx, ~0.3GB, but needs a sentence-transformers/TEI sidecar (https://huggingface.co/blog/ettin-reranker). English-only is acceptable for our KB.
- **mxbai-rerank-base-v2** via infinity — Apache fallback (BEIR 55.57, https://www.mixedbread.com/blog/mxbai-rerank-v2).
- **Skip**: bge-reranker-v2-m3 (512-token pair window truncates our chunks); jina-reranker-v3 (BEIR 61.94 but CC-BY-NC), zerank-2, ctxl-rerank-v2 (both NC).
- **Never** point qwen3:14b at interactive reranking (2-5s class, https://thegeocommunity.com/blogs/generative-engine-optimization/reranking-cross-encoder-llm-reranker/); LLM rerank belongs only in kb_deep_search.
- Expected gain grounding: reranking extended hybrid retrieval's failure reduction from 49% to 67% (5.7%→1.9%) in Anthropic's study (https://www.anthropic.com/engineering/contextual-retrieval); final number must come from eval-v2.

### 2.5 Verifier stage (nightly, new)

- **MiniCheck-Flan-T5-Large (770M)** — **adopt-candidate** (Apache, GPT-4-level fact-checking at ~400x lower cost, https://github.com/Liyan06/MiniCheck). Sidecar, ~0.8GB.
- **Bespoke-MiniCheck-7B** — **license-blocked** for our NDA/work use: CC BY-NC, commercial only via bespokelabs (https://huggingface.co/bespokelabs/Bespoke-MiniCheck-7B), despite topping LLM-AggreFact (~77.7 BAcc, https://llm-aggrefact.github.io/blog) and being on Ollama. Mention-only unless a license is bought.
- **Paladin-mini (3.8B)** — benchmark-first: beats MiniCheck on combined real-world grounding (79.31 vs 77.86) and crushes it on numeric/price grounding (96.0 vs 46.0 BAcc) — directly relevant to our version/port/config items; weaker on dates (82 vs 90); no Ollama entry → transformers sidecar or self-made GGUF (https://arxiv.org/html/2506.20384v1).
- **HHEM-2.1-Open** — CPU-only pre-gate (Apache, <600MB, ~1.5s/2K tokens; 67.1% acc — triage band-pass only, https://www.vectara.com/blog/hhem-2-1-a-better-hallucination-detection-model).
- **LettuceDetect** (150-395M, MIT) — token-span highlighting for the review queue (79.22 F1 RAGTruth, https://arxiv.org/abs/2502.17125).
- Wiring rules from the verified literature: **factored** verification — the verifier sees only (claim, transcript), never the extractor's draft (CoVe: hallucinated entities -77%, https://arxiv.org/pdf/2309.11495); **cross-family** critic — never qwen3:14b judging itself; **max 2 passes** (SCIR, https://arxiv.org/pdf/2512.12337); verifier verdicts **demote to review, never delete** — factuality metrics are biased against paraphrase and distant-context claims (https://arxiv.org/abs/2501.14883), matching Invariant 5.

---

## 3. Staged multi-model pipeline designs (16GB VRAM plans)

**Enabler for all designs (do first):** set `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` — halves KV memory, frees ~0.5-1GB at our contexts, negligible quality risk (https://docs.ollama.com/faq). Ollama's exact-measurement scheduler makes multi-model co-residency reliable; load the big model first (https://ollama.com/blog/new-model-scheduling).

### Design A — "Verify-behind" (lowest risk, fully resident, ship first)
`qwen3:14b extract → SEC-3 substring validator (unchanged) → HHEM-2.1 CPU score → uncertain band → MiniCheck-Flan-T5-770M (factored: claim+transcript only) → fail ⇒ status flip to review + LettuceDetect span highlight`
- **Accuracy gain (evidence-grounded):** catches *semantic* unsupport that a verbatim quote cannot — extract-then-verify measured +0.1-0.3 F1 on clinical IE (https://arxiv.org/pdf/2306.00024) and -77% hallucinated entities in CoVe (https://arxiv.org/pdf/2309.11495); directional, different tasks.
- **Nightly GPU cost:** one 770M forward per uncertain item (~30-50% of items) — <5% added wall-clock; HHEM is CPU-free.
- **VRAM:** 9.3 (qwen3:14b) + 1.2 (bge-m3) + ~0.8 (verifier) + q8 KV ≈ **11-13GB resident**, all keep_alive=-1.

### Design B — "Specialist quote canonicalizer" (NuExtract3 second pass)
`qwen3:14b free-form extract → NuExtract3-4B re-extracts quote + typed fields via verbatim-string template (factored, from transcript) → strict substring validator → agreement ⇒ keep; disagreement ⇒ review`
- **Accuracy gain:** hypothesis is converting SEC-3 from post-hoc rejection into schema-constrained generation, cutting the rejection rate; NuMind's 0.651-vs-0.479 supports extraction competence but is vendor/vision/indel-scored — **local benchmark decides**. Caveat: NuExtract3 is a Qwen3.5-4B fine-tune, so vendor-lineage decorrelation vs qwen3:14b is weaker than a gemma3 critic — pair it with Design A's checker rather than replacing it.
- **Nightly GPU cost:** +1 4B call/item, ~2-5s — trivial in the 30-60s budget.
- **VRAM:** 9.3 + 1.2 + 2.71 (Q4_K_M) + q8 KV ≈ **13.5-14.5GB** — fits resident; or keep_alive=0 for NuExtract if tight.

### Design C — "Big-extractor night shift" (benchmark-gated, highest ceiling)
`nightly: demote qwen3:14b → load qwen3.6:latest transient (30s load; measured 52.9 tok/s, ~20s/1000-token item) → extract batch → unload → load granite4.1-guardian:8b (6.9GB) → groundedness-judge the ~50 triage rows → unload → restore qwen3:14b keep_alive=-1 for daytime`
- **Accuracy gain:** unproven — SEC-3 eval is the gate; the case is model scale + 262K ctx for long sessions, **not** coding-benchmark superiority (that claim was refuted). Guardian judge evidence: AUC 0.854 RAG-hallucination (https://arxiv.org/pdf/2412.07724).
- **Nightly GPU cost:** two model swaps (~1-2 min total) + ~10GB system-RAM pinning during MoE offload (60GB host absorbs it); per-item ~20-48s at measured 20.7-52.9 tok/s (https://www.glukhov.org/llm-performance/benchmarks/choosing-best-llm-for-ollama-on-16gb-vram-gpu/).
- **Optional optimization:** serve the 35B via raw llama-server `--n-cpu-moe` (same GGUF; Ollama post-#16031 literally wraps llama-server, https://github.com/ollama/ollama/pull/16031; expert-offload guide: https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide). Optimization, not a prerequisite — plain Ollama already hits ~53 tok/s. **Do not** add a spec-decode draft to this MoE: all 19 llama.cpp speculative configs were net-negative on A3B (https://github.com/thc1006/qwen3.6-speculative-decoding-rtx3090).

Recommended sequencing: A now → B once NuExtract3 passes the strict-substring bench → C only if the 35B wins the SEC-3 + quality eval.

---

## 4. What NOT to change

- **Append-only / immutable body / supersede design** — independently validated: mem0 deleted its UPDATE/DELETE pass because it destroyed information (LoCoMo 71.4→92.5 after going ADD-only, https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm); no major memory product even has our verbatim-quote grounding. A verify stage is a differentiator, not table stakes.
- **qwen3:14b as extractor default** — 100% grounding is the incumbent's moat; the gemma4:12b episode proves newest ≠ grounded.
- **The 1024-dim schema** — every migration-forcing embedder is dominated by qwen3-embedding:0.6b at zero migration cost.
- **No vLLM switch** — batch=1 parity with Ollama (~71 vs 62 tok/s); wins require rewriting the worker for 4-8 concurrent requests, and Qwen3-14B-AWQ had a severe concurrency throughput collapse (https://github.com/vllm-project/vllm/issues/20469). Revisit only if the nightly window overflows.
- **No speculative decoding today** — Ollama exposes none on CUDA (open #5800, MLX-only shipping, https://github.com/ollama/ollama/issues/5800); MoE spec-decode is net-negative; a 14B+0.6b draft pair on raw llama-server is a plausible 1.5-2x for JSON output but is an experiment, not a change.
- **No naive same-model self-correction loop** — harmed >half of tested setups at ~40x token cost; spend that effort mining judge rejections into offline prompt refinement instead (+3.9% F1 evidence, https://arxiv.org/html/2503.02718v1).
- **No cascade/gating (GLiNER2 etc.) yet** — measured cascade wins are cost, not accuracy, and our nightly window is not a bottleneck; also GLiNER2 relation extraction is weak (17.1 micro-F1, https://arxiv.org/pdf/2605.10108) — relations stay with the 14B.
- **No gemma4:12b re-pull/re-bench until Ollama repackages** the Jul-15 refresh (verified stale registry).
- **No CC-BY-NC models anywhere in the stack**: jina-embeddings-v5, jina-reranker-v3, zerank-2, ctxl-rerank-v2, Bespoke-MiniCheck-7B.

---

## 5. Open questions only a local benchmark can settle

1. **SEC-3 strict-substring grounding rates** (item-313-style harness) for: Qwen3.6-35B-A3B, NuExtract3-4B (verbatim-string mode), granite4.1:8b, qwen3.5:9b — and refreshed gemma4:12b once Ollama updates. One-shot verbatim probes (35B passed one) are not the benchmark.
2. **Does NuExtract3's verbatim-string survive our exact validator** (whitespace/unicode/casing) given its training metric tolerates indel-level deviation?
3. **qwen3-embedding:0.6b vs bge-m3 on eval-v2** (our corpus, instruction prefix on queries): recall/nDCG deltas, especially on code_ref-anchored items; plus the post-upgrade non-degenerate-embedding smoke test.
4. **Reranker end-to-end**: golden-pair sanity on our llama-server build (broken-GGUF guard), then measured eval-v2 gain over RRF-only, and latency for 30x~500-token candidates within the 2s budget. Qwen3-Reranker-0.6B vs Ettin-150M head-to-head.
5. **Verifier threshold calibration** on ~50 hand-labeled (transcript, item) pairs — specifically the over-rejection rate on paraphrased-but-valid summaries; and Paladin-mini vs MiniCheck-770M on our numeric-heavy items.
6. **35B-A3B throughput at production contexts** (8-16K prompts, thinking on/off) under plain Ollama vs llama-server `--n-cpu-moe`, and total nightly wall-clock including the two model swaps of Design C.
7. **q8_0 KV-cache effect on extraction grounding** (expected nil — confirm before making it the fleet default).
8. Only if 0.6b loses the A/B: does **MRL-truncated qwen3-embedding:4b at 1024 dims** beat 0.6b without forcing the schema migration (truncated-dim scores are unpublished)?

---

## Appendix: claims refuted during adversarial verification

- With only 3B active parameters it outperforms the dense Qwen3.5-27B on several key coding benchmarks (73.4 SWE-bench Verified)
- On 16GB cards the model runs via UD-Q3_K_M (16.6GB) with KV offload, or via --cpu-moe expert offload at roughly 30-50 tok/s; full-GPU Q4 needs a 24GB floor (120 tok/s on a 4090)
- Qwen3.6-27B released April 2026: dense, Apache 2.0, 256K context, ~17GB VRAM at Q4_K_M via 'ollama run qwen3.6:27b'
- Ollama tags page confirms qwen3.6:27b-q4_K_M = 17GB, q8_0 = 30GB, 256K context
- 27B dense scores higher than the 35B-A3B MoE on SWE-bench (77.2% vs 73.4%) and is the recommended pick for the 16GB tier (at reduced quant)
- NuExtract 3 released May 25, 2026: 4B parameters, fine-tuned on Qwen3.5-4B, Apache 2.0, scored 0.651 vs Qwen3.5-9B's 0.479 on NuMind's 600-document benchmark
- July 15, 2026 update refreshed weights/configs for all Gemma 4 sizes including 12B, with tool-calling and JSON-reliability patches, template fixes, and instruction to re-pull via Ollama under unchanged names
- google/gemma-4 HF checkpoints show ~July 15, 2026 update timestamps; developers advised to treat it like a patch — re-pull and re-run eval harnesses
- Community reports still describe significant factual hallucination in Gemma 4 models (e.g., invented product details when summarizing), so grounding failure may persist post-refresh

*Produced 2026-07-18 by a 46-agent research workflow (5-angle web sweep, 20 load-bearing claims 2-lens adversarially verified). Baseline: KB item 313 (2026-07-03 extractor benchmark).*

---

## Addendum 2026-07-18: local benchmark results (the §5 questions, answered)

### Extractor SEC-3 grounding bench (item-313 harness rerun, 3 real transcripts, production quote check)

Round 1 exposed a **production bug**: no call in `kbcore.llm` passed `options.num_ctx`, so every
extraction ran at Ollama's runtime default context — input silently truncated, output cut mid-JSON
(`done_reason=length`). The incumbent measured 0% grounding on >4K-token transcripts until the fix.
Production's real grounding survival was **36.2%** (567 rejected / 322 saved) — item 313's "100%"
held only on its curated transcripts. Fixed: `kb_llm_num_ctx` (default 16384) now rides every call.

Round 2 (num_ctx=16384, all `done_reason=stop`):

| model | grounded | items | speed/call | verdict |
|---|---|---|---|---|
| qwen3:14b (incumbent) | 6/13 (46%) | 13 | 17-27s | **keep** — best balance |
| granite4.1:8b | 4/9 (44%) | 9 | **7-9s** | fast-fallback candidate (had 2 JSON failures at default ctx; clean at 16K) |
| gemma3:12b | 4/16 (25%) | 16 | 13-15s | paraphrases code-heavy quotes; fallback status doubtful vs granite |
| qwen3.6:latest (35B-A3B) | 3/11 (27%) | 11 | 92-104s | **rejected** — slower AND worse grounding; also emitted empty content (thinking ate the budget) until think=False |

No model exceeds 50% verbatim grounding on real code-heavy transcripts → the recovery lever is the
verify/quote-repair stage (report §3), not a model swap.

### Embedder A/B (eval-v2, kb_test corpus clone; baseline bit-identical to live sentinel 0.690/0.569)

| config | hit@5 | MRR | cases |
|---|---|---|---|
| bge-m3 (incumbent) | 0.690 | 0.569 | 20/29 |
| qwen3-embedding:0.6b, plain queries | 0.655 | 0.595 | 19/29 |
| **qwen3-embedding:0.6b + retrieval instruction prefix** | **0.724** | **0.613** | **21/29** |

Sanity gates all passed (unit-norm 1024-dim drop-in; no pooling degeneracy); 5242-chunk reembed =
182s. The prefix is REQUIRED (plain queries regress). One regression: the DRV8706 SPI-polarity
cross-project case drops rank 3→5. Six cases fail under every embedder (Urdu, drv_spi_write-gotcha,
UART trio, ollama-keep-alive) — corpus/leg gaps, not embedder-sensitive. Adoption pending owner
decision + query-prefix support in kbcore.embed.

---

## Addendum 2 (2026-07-18): multi-LLM converging pipeline — prototype measurements

Owner asked for an unboxed exploration of "multiple LLMs processing data and converging."
All three mechanisms prototyped on the round-2 bench data (3 real transcripts, production code paths).

### 1. Quote repair (pure code, no LLM)
62% of ALL ungrounded quotes fuzzy-locate to a true verbatim span at difflib ratio >= 0.95 —
models quote *nearly* right; a ~50-line locator recovers them while keeping SEC-3's verbatim
invariant (the stored quote becomes the located true span; model_quote + repair_ratio kept for audit).
Usable-item rates after repair: qwen3:14b 12/13 (92%), gemma3:12b 13/16 (81%), granite4.1:8b 6/9 (67%).

### 2. Ensemble + convergence + factored verification
- Corroboration is LOW (29% of clusters multi-model): models find DIFFERENT knowledge → the right
  shape is UNION + per-item verification, NOT consensus voting (consensus would discard most knowledge).
- Ensemble union + repair yields **21 quote-anchored knowledge units vs 5 under today's policy (4x)**;
  12/24 clusters had no qwen3:14b member at all.
- Judge quality is measured and asymmetric: qwen3:14b as factored judge = perfect on this sample
  (approved reals, caught 6/6 fabricated probes); gemma3:12b as judge FAILED both directions
  (rejected 4 real, approved 5/6 fakes) → gemma is disqualified from the judge slot.
- Judge cost ~6.3 s/item; keep_alive=0 reloads are ~free (page cache).

### 3. Retrieval convergence (dual-embedder RRF fusion, kb_test, n=29 golden)
| config | hit@5 | MRR | cases |
|---|---|---|---|
| bge-m3 (live, validated bit-identical) | 0.724 | 0.603 | 21/29 |
| qwen3-embedding:0.6b + prefix (swap) | **0.759** | 0.641 | **22/29** |
| fusion K=60 | 0.724 | 0.661 | 21/29 |
| fusion K=100 | 0.724 | **0.701** | 21/29 |

Fusion rescues DRV8706 SPI-polarity (3 -> **1**) but pushes both systemd wins out of top-5; swap wins
hit@5 but keeps the DRV8706 regression (3 -> 5). No config dominates. Fusion costs +117 ms/query and
needs an additive second-vector migration + double embedding on ingest. Six golden cases miss under
EVERY config (Urdu, UART trio, ollama-keep-alive, cross-project SPI phrasing) — corpus/leg gaps that
cap all embedder work.

### Proposed pipeline v2 (evidence-backed, pending owner approval)
extract[qwen3:14b + granite4.1:8b + gemma3:12b, independent] -> quote-repair(>=0.95) ->
converge[cluster @0.80, UNION, corroboration recorded in meta] -> factored verify
[qwen3:14b judges gemma/granite items; qwen items pass via corroboration now, MiniCheck-770M sidecar
later] -> ingest as unverified + review rows (existing triage keeps working). Fails route to review,
never deleted (Inv. 5); repaired quotes stay verbatim (Inv. 4); all inside consolidate.py (Inv. 8).
Measured yield: ~4x knowledge units per session at ~3-4x nightly GPU (window is empty; loads ~free).
