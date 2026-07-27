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


def test_a_natural_language_query_is_not_dominated_by_its_stopwords(kb):
    """Latency scaled with STOPWORD COUNT, not query length.

    The 'simple' tsquery config keeps stopwords deliberately (it also keeps Urdu
    and identifiers), so "how does the retry backoff work in this service"
    carried lexemes matching nearly every chunk. Their IDF weight ln(1 + N/df)
    tends to zero — they cannot change the ranking — but the coverage join had to
    visit every row they matched: 22.9s against 0.8s for the same query with
    stopwords removed by hand, while the recall hook's budget is 6s.

    The earlier MAX_OR_LEXEMES cap did NOT fix this: a 14-word question is
    already under the cap, so it never engaged. This asserts the df FILTER, the
    thing that actually removes the work.
    """
    from kbcore import search_legs

    src = search_legs._or_coverage_rows.__doc__ or ""
    import inspect

    body = inspect.getsource(search_legs._or_coverage_rows)
    assert "df <=" in body, (
        "the high-df lexeme filter is gone — a natural-language query will again "
        "join on words that match the whole corpus"
    )
    assert "NOT EXISTS (SELECT 1 FROM sel)" in body, (
        "the all-common fallback is gone — a query made entirely of frequent "
        "words would return nothing at all"
    )
    assert src is not None


def test_a_query_of_only_common_words_still_returns_something(kb):
    """Filtering high-df lexemes must not empty the result set for a question
    that happens to be made entirely of frequent words."""
    from kbcore import search as search_mod
    from kbcore.ingest import ingest

    for i in range(3):
        ingest(
            "common-proj",
            "note",
            f"How does the thing work {i}",
            "It works by doing the thing that the other thing does.",
        )
    hits = search_mod.search("how does the it work", k=5)
    assert hits, "an all-common query returned nothing — the fallback is not firing"
