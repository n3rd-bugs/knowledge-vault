"""kbcore.llm_prompts — system prompts split out of :mod:`kbcore.llm` (size ratchet).

:mod:`kbcore.llm` re-imports each one under its old private name for its readers; since
KB 55589 the pair-judge requests are built from THIS module by :mod:`kbcore.judge_fit`, so
patching ``llm._EVIDENCE_SYSTEM`` / ``llm._CONTRADICT_SYSTEM`` no longer changes a request.

EVIDENCE (2026-10-26, KB 55038). The nightly evidence step (:mod:`kbcore.evidence`)
asks, for each neighbour of a queued contradiction, which side it supports, and a
lopsided tally becomes a suggested resolution. The old prompt let the judge credit a
side to items about a RELATED but different situation: on 82 blind-labelled cases
gemma4:12b gave a wrong side 35 times (qwen3:14b 25), and one such tally suggested
retiring the CURRENT item of a stale-vs-current pair (row 14559, KB 55037). This text
cut that to 14 (qwen3 8) and, on 152 held-out cases, changed 18 answers of which 14
were right; it is shipped verbatim as benched — reword it only with a new bench.

CONTRADICT (moved 2026-10-27, KB 55589) so :mod:`kbcore.judge_fit` can measure the
contradiction request without importing :mod:`kbcore.llm`; the text is unchanged.
"""

from __future__ import annotations

CONTRADICT_SYSTEM = (
    "You compare two knowledge items and decide whether they factually CONTRADICT — "
    "state incompatible facts or decisions about the same thing (not merely different "
    'topics). Answer STRICT JSON only: {"contradict": true} or {"contradict": false}. '
    "If you are unsure, answer false."
)

EVIDENCE_SYSTEM = (
    "Two knowledge items (A and B) were flagged as contradicting each other. You are given "
    'a third item as evidence. Answer "a" only if the evidence states or directly implies '
    "the specific point on which A differs from B in A's favour (including a later state, "
    'fix or measured result that settles it as A says); answer "b" likewise for B. Answer '
    '"neither" if the evidence is about a related but different situation, project, '
    "component or time; if it only repeats what A and B both say; if it is ambiguous; or "
    'if it supports both. When unsure, answer "neither". Answer STRICT JSON only: {"side": '
    '"a"} or {"side": "b"} or {"side": "neither"}.'
)
