def test_a_long_commit_subject_query_stays_fast(kb):
    """The pre-brief hook seeds its query with recent git commit SUBJECTS.

    A few conventional-commit lines tokenise to ~30 lexemes, and each lexeme
    costs a full scan of `chunks` to compute its document frequency. At the old
    MAX_OR_LEXEMES=64 that took 18.8s against a real corpus — past the hook's
    20s budget, so the brief silently returned nothing and the user saw no
    diagnostic at all (sungrow, KB 1111).

    This asserts the CAP rather than a wall-clock time, because timing is a
    property of the machine and the corpus, while the cap is the thing that
    bounds the work.
    """
    from kbcore import search_legs

    assert search_legs.MAX_OR_LEXEMES <= 16, (
        "MAX_OR_LEXEMES governs how many full chunk scans one query can trigger; "
        "raising it re-opens the pre-brief timeout"
    )


def test_the_or_fallback_still_finds_a_rare_term_in_a_noisy_query(kb):
    """The cap must bound COST, not signal: a rare anchor buried in a long,
    punctuation-heavy commit-subject blob must still retrieve its item."""
    from kbcore.ingest import ingest

    ingest(
        "eval-proj",
        "gotcha",
        "Zylophonic tunnelling regression",
        "The zylophonic tunnelling path fails when the coefficient is negative.",
        tags=["domain:testing"],
    )
    noisy = (
        "fix(mcp): recognize the owner email on the OAuth/connector path "
        "feat(kbweb): email-centric one-click backup setup + recovery-key download "
        "chore(deps): bump things zylophonic"
    )
    from kbcore import search as search_mod

    hits = search_mod.search(noisy, k=8)
    assert any("Zylophonic" in h.title for h in hits), (
        "the rare anchor was lost — the lexeme cap is discarding signal, not noise"
    )
