def test_a_read_timeout_is_transient_not_a_content_defect():
    """An embedder that is starved is not a chunk that is malformed.

    This was classified as ExtractionFailed on the theory that a timeout is
    "shaped by a pathological chunk". A real drain then hit 28 rows across three
    projects with the identical timeout while the GPU sat idle and VRAM was
    full: every one was permanently failed after burning its attempts budget,
    for a condition that would have cleared on its own.

    Deferring is right for both causes — the deferral cap still escalates a
    chunk that genuinely always times out.
    """
    import httpx

    from kbcore.embed import _classify_embed_error
    from kbcore.errors import EmbedderUnavailable

    for exc in (
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
        httpx.PoolTimeout("timed out"),
        httpx.ConnectError("refused"),
    ):
        assert isinstance(_classify_embed_error(exc), EmbedderUnavailable), type(exc).__name__


def test_a_4xx_is_still_a_content_defect():
    """The bounded path must survive: a request the embedder REJECTS is about
    the content, and retrying it forever would be the opposite mistake."""
    import httpx

    from kbcore.embed import _classify_embed_error
    from kbcore.errors import EmbedderUnavailable, ExtractionFailed

    req = httpx.Request("POST", "http://x/api/embed")
    bad = httpx.HTTPStatusError("", request=req, response=httpx.Response(400, request=req))
    assert isinstance(_classify_embed_error(bad), ExtractionFailed)

    # ...but a missing model is the server's state, not the chunk's fault.
    missing = httpx.HTTPStatusError(
        "", request=req, response=httpx.Response(404, text="model not found", request=req)
    )
    assert isinstance(_classify_embed_error(missing), EmbedderUnavailable)


def test_the_read_path_does_not_inherit_the_drain_timeout(monkeypatch):
    """A stalled embedder must not hold a user's prompt open for ten minutes.

    The drain budget went 120s -> 600s to match llm.py: the client used to give
    up while Ollama was still legitimately loading a model, recording a failure
    for something about to be ready. But kb_search runs through embed_query, and
    inheriting 600s there would turn a slow embedder into a hung prompt.
    embed_query is the seam between the two budgets, so the split lives there.
    """
    from kbcore import embed

    assert embed._TIMEOUT.read == 600.0, "the drain lost its longer budget"
    assert embed._QUERY_TIMEOUT.read == 30.0

    seen: list = []
    # Capture at embed_texts, the single point both paths funnel through. The
    # autouse fake_embeddings fixture already replaces it, so patch it again
    # here rather than trying to intercept httpx underneath it.
    monkeypatch.setattr(
        embed, "embed_texts", lambda texts, **kw: (seen.append(kw.get("timeout")), [[0.0]])[1]
    )

    embed.embed_query("a user question")
    embed.embed_one("a drain chunk")

    assert seen[0] is embed._QUERY_TIMEOUT, "the read path used the drain budget"
    assert seen[1] is None, "embed_one should default to the drain budget"
