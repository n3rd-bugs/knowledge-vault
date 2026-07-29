"""The remote embed host is a PREFERENCE, never a dependency — and a dead one
must not be re-probed on every call.

Measured 2026-07-29: with the RTX box asleep, a drain paid the 10s connect
timeout on all 73 embed calls (12 minutes) while the GPU sat at 0%, because the
process was blocked on a socket rather than computing.
"""

from __future__ import annotations

import httpx
import pytest

from kbcore import embed
from kbcore.config import settings

# conftest autouse-mocks embed.embed_texts for every test (mock only Ollama).
# This suite is ABOUT that function's host selection, so it needs the real one —
# captured at import, before the fixture swaps it out.
_REAL_EMBED_TEXTS = embed.embed_texts

REMOTE = "http://10.0.0.254:11434"
LOCAL = "http://127.0.0.1:11434"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    embed.reset_host_health()
    monkeypatch.setattr(embed, "embed_texts", _REAL_EMBED_TEXTS)
    monkeypatch.setattr(settings, "kb_embed_url", REMOTE)
    monkeypatch.setattr(settings, "ollama_url", LOCAL)
    yield
    embed.reset_host_health()


def _install(monkeypatch, dead: set[str]) -> list[str]:
    """Record every host actually dialled; hosts in `dead` time out."""
    tried: list[str] = []

    def fake_post(url, **kw):
        host = url.rsplit("/api/", 1)[0]
        tried.append(host)
        if host in dead:
            raise httpx.ConnectTimeout("box is asleep")
        return httpx.Response(
            200,
            json={"embeddings": [[0.1] * settings.kb_embed_dim]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return tried


def test_a_dead_remote_is_dialled_once_then_skipped(monkeypatch):
    """The whole point: the second call must not pay the timeout again."""
    tried = _install(monkeypatch, dead={REMOTE})

    for _ in range(5):
        _REAL_EMBED_TEXTS(["x"])

    assert tried.count(REMOTE) == 1, f"dead host re-probed {tried.count(REMOTE)}x"
    assert tried.count(LOCAL) == 5, "the fallback did not serve every call"


def test_the_breaker_reopens_so_a_returning_box_is_picked_up(monkeypatch):
    """A cooldown that never expires would strand the remote after it comes back."""
    tried = _install(monkeypatch, dead={REMOTE})
    _REAL_EMBED_TEXTS(["x"])
    assert tried.count(REMOTE) == 1

    monkeypatch.setattr(embed, "_HOST_DOWN_SECONDS", 0.0)
    embed._mark_down(REMOTE)  # re-trip with the zero cooldown
    _REAL_EMBED_TEXTS(["x"])
    assert tried.count(REMOTE) == 2, "the breaker never re-probed"


def test_a_recovered_host_is_used_again_and_the_breaker_clears(monkeypatch):
    dead: set[str] = {REMOTE}
    tried = _install(monkeypatch, dead=dead)
    _REAL_EMBED_TEXTS(["x"])

    dead.clear()  # the box comes back
    embed.reset_host_health()  # cooldown elapses
    _REAL_EMBED_TEXTS(["x"])

    assert tried[-1] == REMOTE, "the recovered host was not preferred again"
    assert not embed._is_down(REMOTE), "the breaker stayed open after a success"


def test_the_last_host_is_never_skipped(monkeypatch):
    """Skipping the fallback of last resort would fail the call outright rather
    than serving it slowly — the breaker must never cause an outage."""
    tried = _install(monkeypatch, dead=set())
    embed._mark_down(LOCAL)
    embed._mark_down(REMOTE)

    _REAL_EMBED_TEXTS(["x"])
    assert LOCAL in tried, "the only remaining host was skipped"
