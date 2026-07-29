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
