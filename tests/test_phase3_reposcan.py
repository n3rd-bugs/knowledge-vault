"""Phase 3 — repo docs scanner: Invariant 9 (zero source files), dedupe, mapping.

Creates a real throwaway git repo in tmp; the required test asserts README/docs
are ingested and .c/.py source files are NOT (PLAN.md §11 Phase 3 / Invariant 9).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kbcore import db
from kbcore.config import settings
from kbcore.errors import KBError
from kbcore.items import set_project
from kbworker import reposcan
from kbworker.reposcan import is_doc_path, scan_repo

pytestmark = pytest.mark.usefixtures("kb")


@pytest.fixture(autouse=True)
def _tmp_raw(tmp_path, monkeypatch):
    """Keep raw archives off the real ~/kb during tests."""
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "kbraw")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_bare_git(repo: Path) -> None:
    """git init + identity in an existing dir (for tests that supply their own files)."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "KB Test")


def _init_fixture_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "KB Test")
    (repo / "README.md").write_text("# Fixture\n\nThis repo documents the widget frobnicator.")
    (repo / "docs").mkdir()
    (repo / "docs" / "design.md").write_text("# Design\n\nThe design uses an SPI bus at 8 MHz.")
    (repo / "src").mkdir()
    (repo / "src" / "main.c").write_text("int main(void){ return 0; }\n")
    (repo / "app.py").write_text("print('hello world')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")


def _git_uris() -> list[str]:
    with db.connect() as conn:
        return [r[0] for r in conn.execute("SELECT uri FROM sources WHERE kind = 'git'").fetchall()]


def _item_count() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) FROM items").fetchone()[0]


def test_non_utf8_filename_does_not_abort_scan(tmp_path):
    # Fix-review regression: with core.quotePath=false git emits raw name bytes;
    # a strict UTF-8 decode in _git would crash _tracked_files uncaught and let
    # scan_all abort the whole daily pass. errors="replace" keeps the scan alive
    # (the odd-named file is skipped, the good README still ingests).
    repo = tmp_path / "latin1repo"
    _init_bare_git(repo)
    (repo / "README.md").write_text("# Good\n\nDocuments the frobnicator widget.")
    (repo / "docs").mkdir()
    # a tracked file whose name is legacy latin-1 (byte 0xe9) — invalid UTF-8
    bad_path = bytes(repo / "docs") + b"/r\xe9sum\xe9.md"
    fd = os.open(bad_path, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, b"# Non-utf8 named doc\n")
    os.close(fd)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    summary = scan_repo(repo)  # must not raise UnicodeDecodeError
    assert any("README.md" in u for u in summary.ingested), "good README still ingested"


def test_is_doc_path_never_matches_source_even_under_adr():
    # Invariant 9 hard guard: an allowed directory cannot smuggle in source.
    assert is_doc_path("README.md")
    assert is_doc_path("README")
    assert is_doc_path("docs/design.md")
    assert is_doc_path("adr/0001-choice.md")
    assert is_doc_path("CHANGELOG")
    assert not is_doc_path("adr/gen.py")  # source under adr/ stays out
    assert not is_doc_path("src/main.c")
    assert not is_doc_path("app.py")
    assert not is_doc_path("docs/schema.sql")
    assert not is_doc_path("scripts/build.sh")


def test_reposcan_ingests_docs_and_zero_source(tmp_path):
    repo = tmp_path / "motor-ctl"
    _init_fixture_repo(repo)

    summary = scan_repo(repo)

    assert summary.project == "motor-ctl", "fallback to repo dir name when unmapped"
    assert summary.commit is not None
    assert set(summary.ingested) == {"motor-ctl:README.md", "motor-ctl:docs/design.md"}

    uris = _git_uris()
    assert any(u.endswith("README.md") for u in uris)
    assert any(u.endswith("docs/design.md") for u in uris)
    assert not any(u.endswith(".c") or u.endswith(".py") for u in uris), "zero source files"
    assert _item_count() == 2


def test_reposcan_rerun_adds_nothing(tmp_path):
    repo = tmp_path / "aq-repo"
    _init_fixture_repo(repo)

    first = scan_repo(repo)
    assert first.saved == 2
    before = _item_count()

    second = scan_repo(repo)
    assert second.saved == 0
    assert second.skipped_dupes == 2
    assert _item_count() == before, "content-hash dedupe makes re-runs no-ops"


def test_reposcan_honors_project_mapping(tmp_path):
    repo = tmp_path / "fw-repo"
    _init_fixture_repo(repo)
    set_project("aquamonix", domain="embedded", repos=[str(repo.resolve())])

    summary = scan_repo(repo)

    assert summary.project == "aquamonix", "projects.repos mapping wins over dir name"
    with db.connect() as conn:
        projects = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT project FROM sources WHERE kind = 'git'"
            ).fetchall()
        }
    assert projects == {"aquamonix"}


def test_scan_all_iterates_configured_repos(tmp_path, monkeypatch):
    repo = tmp_path / "active-repo"
    _init_fixture_repo(repo)
    monkeypatch.setattr(reposcan.settings, "kb_git_repos", str(repo), raising=False)
    summaries = reposcan.scan_all()
    assert len(summaries) == 1
    assert summaries[0].saved == 2


def test_is_doc_path_readme_requires_doc_extension():
    # Defect #3/#4: README/CHANGELOG branch must gate on a doc extension.
    assert not is_doc_path("README.pdf")
    assert not is_doc_path("CHANGELOG.gz")
    assert not is_doc_path("docs/README.png")
    assert not is_doc_path("README.ex")  # uncovered source ext no longer slips through
    assert is_doc_path("README.md")
    assert is_doc_path("README")  # extensionless plain-text README still allowed
    assert is_doc_path("CHANGELOG")
    assert is_doc_path("README.rst")


def test_reposcan_skips_symlinked_docs(tmp_path):
    # Defect #1: a doc-named symlink must never be dereferenced and ingested.
    repo = tmp_path / "repo"
    _init_bare_git(repo)
    (repo / "secret.py").write_text(f"SECRET_KEY = 'ghp_{'0' * 36}'\n")
    (repo / "README.md").write_text("# Real\n\nThis documents the widget.")
    (repo / "docs").mkdir()
    (repo / "docs" / "design.md").write_text("# Design\n\nA real design doc.")
    os.symlink("../secret.py", repo / "docs" / "evil.md")  # -> tracked source file
    os.symlink("/etc/hostname", repo / "docs" / "host.md")  # -> arbitrary host file
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    summary = scan_repo(repo)

    assert set(summary.ingested) == {"repo:README.md", "repo:docs/design.md"}
    assert _item_count() == 2
    with db.connect() as conn:
        bodies = [r[0] for r in conn.execute("SELECT body FROM items").fetchall()]
    assert not any("SECRET_KEY" in b for b in bodies), "symlinked source never ingested"


def test_reposcan_survives_a_bad_doc(tmp_path):
    # Defect #2: one undecodable doc (NUL bytes → DB DataError) must not abort
    # the whole scan; it is logged, counted, and skipped.
    repo = tmp_path / "repo2"
    _init_bare_git(repo)
    (repo / "README.md").write_text("# Good\n\nA good readme about the frobnicator.")
    (repo / "docs").mkdir()
    (repo / "docs" / "bad.md").write_bytes(b"# Bad\n\x00\x00 embedded nul bytes here")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    summary = scan_repo(repo)

    assert "repo2:README.md" in summary.ingested
    assert summary.errors == 1
    assert _item_count() == 1, "good doc ingested, bad one skipped, scan completed"


def test_git_absent_raises_kberror_not_traceback(tmp_path, monkeypatch):
    # Defect #5: FileNotFoundError from a missing git binary is wrapped as a
    # clean KBError, not a bare traceback out of the CLI.
    repo = tmp_path / "repo3"
    _init_fixture_repo(repo)  # real .git so scan_repo gets past the .git check

    def raise_fnf(*a, **k):
        raise FileNotFoundError("git: command not found")

    monkeypatch.setattr(reposcan.subprocess, "run", raise_fnf)
    with pytest.raises(KBError):
        scan_repo(repo)


def test_reposcan_ingests_non_ascii_doc_name(tmp_path):
    # Defect #6: core.quotePath=false so non-ASCII doc filenames are matched.
    repo = tmp_path / "repo4"
    _init_bare_git(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "résumé.md").write_text("# Résumé\n\nProject résumé notes.")
    (repo / "README.md").write_text("# R\n\nreadme body")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    summary = scan_repo(repo)

    assert any(u.endswith("résumé.md") for u in summary.ingested), "non-ASCII doc ingested"


# --- doc-refresh auto-supersede (auto-resolution tier 1a) ----------------------------


def _statuses_for_uri(uri: str) -> list[tuple[int, str, int | None]]:
    with db.connect() as conn:
        return conn.execute(
            "SELECT i.id, i.status, i.superseded_by FROM items i "
            "JOIN sources s ON s.id = i.source_id "
            "WHERE s.kind='git' AND s.uri = %s ORDER BY i.id",
            (uri,),
        ).fetchall()


def test_changed_doc_supersedes_previous_revision(kb, tmp_path):
    repo = tmp_path / "refresh-repo"
    _init_fixture_repo(repo)
    first = scan_repo(repo)
    assert first.saved == 2 and first.superseded == 0

    (repo / "README.md").write_text("# Fixture\n\nThe frobnicator now uses I2C, not SPI.")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "update readme")
    second = scan_repo(repo)
    assert second.saved == 1, "only the changed doc re-ingests"
    assert second.superseded == 1, "the stale revision was auto-superseded"

    rows = _statuses_for_uri("refresh-repo:README.md")
    assert len(rows) == 2
    old, new = rows
    assert old[1] == "superseded" and old[2] == new[0], "old -> superseded_by new"
    assert new[1] == "active"
    with db.connect() as conn:
        reason = conn.execute(
            "SELECT supersede_reason FROM items WHERE id = %s", (old[0],)
        ).fetchone()[0]
    assert "newer revision" in reason


def test_doc_refresh_flag_off_leaves_both_active(kb, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_auto_supersede_doc_refresh", False)
    repo = tmp_path / "noflag-repo"
    _init_fixture_repo(repo)
    scan_repo(repo)
    (repo / "README.md").write_text("# Fixture\n\nChanged content, flag off.")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "update")
    second = scan_repo(repo)
    assert second.superseded == 0
    statuses = [r[1] for r in _statuses_for_uri("noflag-repo:README.md")]
    assert statuses == ["active", "active"], "flag off preserves old behavior"


def test_multiple_stale_revisions_all_superseded(kb, tmp_path):
    repo = tmp_path / "multi-repo"
    _init_fixture_repo(repo)
    scan_repo(repo)
    # two more revisions, the middle one scanned with the flag OFF to build backlog
    from kbcore.config import settings as cfg

    (repo / "README.md").write_text("rev 2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "r2")
    cfg_orig = cfg.kb_auto_supersede_doc_refresh
    cfg.kb_auto_supersede_doc_refresh = False
    try:
        scan_repo(repo)  # rev2 ingests, rev1 stays active (backlog)
    finally:
        cfg.kb_auto_supersede_doc_refresh = cfg_orig
    (repo / "README.md").write_text("rev 3")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "r3")
    third = scan_repo(repo)
    assert third.superseded == 2, "both stale revisions retire at once"
    rows = _statuses_for_uri("multi-repo:README.md")
    assert [r[1] for r in rows] == ["superseded", "superseded", "active"]
    newest = rows[-1][0]
    assert all(r[2] == newest for r in rows[:-1]), "all point at the newest revision"
