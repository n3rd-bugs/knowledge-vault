import pytest


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


def test_the_embedder_can_live_on_another_machine(monkeypatch):
    """Embedding runs on every chunk of every row, so it is the model worth
    moving off the extractor's card. A 11.4GB extractor and a 2.3GB embedder do
    not both fit in 16GB alongside a 3GB reranker, and the resulting swap is
    what dominated drain time. Defaults to the main host, so a single-box setup
    is unchanged."""
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_embed_url", "")
    assert settings.embed_url == settings.ollama_url.rstrip("/")

    monkeypatch.setattr(settings, "kb_embed_url", "http://192.168.1.100:11434/")
    assert settings.embed_url == "http://192.168.1.100:11434", "trailing slash not normalised"


def test_the_embed_call_actually_goes_to_the_embed_host(monkeypatch):
    """The setting is worthless if embed_texts still posts to the main host."""
    import httpx

    from kbcore import embed
    from kbcore.config import settings

    monkeypatch.setattr(settings, "kb_embed_url", "http://remote-gpu:11434")
    seen: list[str] = []

    def fake_post(url, **kw):
        seen.append(url)
        raise httpx.ConnectError("no server")

    monkeypatch.setattr(httpx, "post", fake_post)
    # The autouse fake_embeddings fixture replaces embed_texts; reload to get
    # the real one back for this test only.
    import importlib

    importlib.reload(embed)
    from kbcore.errors import EmbedderUnavailable

    with pytest.raises(EmbedderUnavailable):
        embed.embed_texts(["probe"])
    assert seen and seen[0].startswith("http://remote-gpu:11434"), seen


def test_a_gaming_desktop_that_vanishes_does_not_break_embedding(monkeypatch):
    """The remote embed host is a PREFERENCE, never a dependency.

    KB_EMBED_URL may point at a machine that is a gaming desktop by day. If a
    nightly drain hard-depended on it, every daytime run would fail — so an
    unreachable embed host falls back to the main one: slower under VRAM
    contention, but always there.
    """
    import importlib

    import httpx

    from kbcore import embed
    from kbcore.config import settings
    from kbcore.errors import EmbedderUnavailable

    importlib.reload(embed)
    monkeypatch.setattr(settings, "kb_embed_url", "http://gaming-box:11434")
    monkeypatch.setattr(settings, "ollama_url", "http://localhost:11434")

    tried: list[str] = []

    def post(url, **kw):
        tried.append(url)
        if "gaming-box" in url:
            raise httpx.ConnectError("host is busy playing something")
        return httpx.Response(
            200,
            json={"embeddings": [[0.1] * settings.kb_embed_dim]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    out = embed.embed_texts(["a chunk"])
    assert len(out) == 1
    assert "gaming-box" in tried[0] and "localhost" in tried[1], tried

    # ...but a host that ANSWERS badly must not be retried elsewhere: that is a
    # content/server problem, and failing over would just repeat it at double cost.
    tried.clear()

    def bad(url, **kw):
        tried.append(url)
        raise (
            httpx.ConnectError("down")
            if False
            else httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", url),
                response=httpx.Response(500, request=httpx.Request("POST", url)),
            )
        )

    monkeypatch.setattr(httpx, "post", bad)
    with pytest.raises(EmbedderUnavailable):
        embed.embed_texts(["a chunk"])
    assert len(tried) == 1, f"a 500 was retried on the fallback host: {tried}"
