"""Phase 0 smoke tests: the package imports and config resolves as documented.

These are intentionally trivial (the Phase 0 AC just needs a green test run);
real ingest/search/scrub tests land in Phase 1.
"""

from __future__ import annotations

from kbcore.config import Settings


def test_settings_defaults():
    # _env_file=None: assert construct-time defaults, not whatever the
    # developer's real .env happens to contain.
    s = Settings(_env_file=None)
    assert s.kb_embed_dim == 1024
    assert s.kb_port == 8321
    # The embedder default moved to the instruction-aware model that is actually
    # deployed; assert the PAIR, since the model alone without its query prefix is
    # measurably worse than the bge-m3 it replaced (MODEL_RESEARCH_2026-07.md:192).
    assert s.kb_embed_model == "qwen3-embedding:0.6b"
    assert s.kb_embed_query_prefix.strip(), "instruction-aware default ships no prefix"


def test_db_url_expands_password():
    s = Settings(
        kb_db_url="postgresql://kb:${KB_DB_PASSWORD}@127.0.0.1:5433/kb",
        kb_db_password="s3cret",
    )
    assert "${KB_DB_PASSWORD}" not in s.db_url
    assert "s3cret" in s.db_url


def test_db_url_reinjects_interpolated_empty_password():
    # python-dotenv interpolates ${KB_DB_PASSWORD} in file order; the password
    # is defined after the URL in .env, so the URL arrives with an empty
    # password. db_url must re-inject it (this bit us in Phase 0).
    s = Settings(
        kb_db_url="postgresql://kb:@127.0.0.1:5433/kb",
        kb_db_password="s3cret",
    )
    assert "s3cret" in s.db_url
    assert ":@" not in s.db_url


def test_db_url_injects_quoted_special_char_password():
    # Template URL carries no password; injection must URL-quote specials —
    # raw interpolation of e.g. 'p@ss/w:rd' would corrupt the URL (review finding).
    s = Settings(
        _env_file=None,
        kb_db_url="postgresql://kb@127.0.0.1:5433/kb",
        kb_db_password="p@ss/w:rd",
    )
    assert s.db_url == "postgresql://kb:p%40ss%2Fw%3Ard@127.0.0.1:5433/kb"


def test_db_url_respects_inline_password():
    # A user-provided URL with its own inline password wins over kb_db_password.
    s = Settings(
        _env_file=None,
        kb_db_url="postgresql://kb:inline@10.0.0.1:5433/kb",
        kb_db_password="other",
    )
    assert s.db_url == "postgresql://kb:inline@10.0.0.1:5433/kb"


def test_git_repos_strips_whitespace_and_empties():
    s = Settings(_env_file=None, kb_git_repos="~/work/a: ~/work/b ::")
    repos = s.git_repos
    assert len(repos) == 2
    assert all(not str(r).startswith("~") and " " not in str(r) for r in repos)


def test_paths_expanduser():
    s = Settings(kb_inbox="~/kb/inbox")
    assert not str(s.inbox).startswith("~")
