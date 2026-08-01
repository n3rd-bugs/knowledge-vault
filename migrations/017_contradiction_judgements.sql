-- 017_contradiction_judgements.sql — remember which item PAIRS the contradiction
-- judge has already looked at, per embedding model.
-- ADDITIVE only (Invariant 5): one new table, nothing altered, nothing dropped,
-- no existing row touched. Automation only ever INSERTs here.
--
-- WHY. The nightly contradiction step (kbworker.consolidate._contradictions)
-- re-judged EVERY near-but-not-dup pair from scratch, every night: the pair
-- query is derived purely from lead-chunk cosine, so a pair the 14B already
-- ruled "not a contradiction" is offered again the next night, and the next.
-- Measured: the step grew 4,972s -> 8,194s in two days with ~90% of the calls
-- re-paying for negatives it had already paid for, and a power cut mid-step
-- discarded the whole night's judging because nothing was written until the
-- (rare) positive verdict. This table is that missing memory: a verdict is
-- recorded as soon as the judge answers, so an interrupted run loses at most the
-- pair in flight.
--
-- EMBED_MODEL IS PART OF THE KEY, deliberately. The pair set is a function of
-- the embeddings that produced it, so re-embedding the corpus with a new model
-- (kbcore.reembed) legitimately re-opens every pair for judging: the exclusion
-- in kbcore.consolidation._lead_pairs matches on the CURRENT
-- settings.kb_embed_model, and rows written under the old model simply stop
-- matching. That is the intended way to invalidate this memory — nothing is ever
-- updated or deleted to do it.
--
-- The FKs are ON DELETE CASCADE so this non-knowledge memory can never outlive
-- the items it refers to; nothing in the system deletes items, so in production
-- this is a consistency guarantee rather than a delete path (Invariant 5).

CREATE TABLE IF NOT EXISTS contradiction_judgements (
  a_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  b_id BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  embed_model TEXT NOT NULL,
  verdict BOOLEAN NOT NULL,                    -- what llm.items_contradict answered
  checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (a_id, b_id, embed_model)        -- the exclusion probe rides this
);
