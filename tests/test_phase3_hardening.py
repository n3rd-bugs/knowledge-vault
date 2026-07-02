"""Phase 3 hardening — regression tests for the adversarial-review fixes that
live in the kbcore layer and the session-hook installer.

Covers: project_for_repo two-sided normalization (#7), the cross-process
session lock (#13), llm.extract_items coercion of non-list output (#17), and
the session_hook.sh JSON-object guard (#19).
"""

from __future__ import annotations

import fcntl
import json
import subprocess
from pathlib import Path

import pytest

from kbcore import llm
from kbcore.config import settings
from kbcore.items import project_for_repo, set_project
from kbcore.locks import session_lock, session_lock_path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "session_hook.sh"


# --- #7 project_for_repo normalizes BOTH sides -------------------------------


def test_project_for_repo_normalizes_trailing_slash(kb, tmp_path):
    repo = tmp_path / "sub" / "x"
    repo.mkdir(parents=True)
    set_project("proj", repos=[str(repo) + "/"])  # stored WITH a trailing slash
    # A non-normalized input (…/x/.) and the resolved absolute path both match.
    assert project_for_repo(str(tmp_path / "sub" / "x" / ".")) == "proj"
    assert project_for_repo(str(repo.resolve())) == "proj"


def test_project_for_repo_normalizes_tilde(kb, tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / "work" / "y").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    set_project("tildep", repos=["~/work/y"])  # stored in ~ form
    assert project_for_repo(str(fake_home / "work" / "y")) == "tildep"


# --- #13 session lock is exclusive per transcript path -----------------------


def test_session_lock_is_exclusive_per_path(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kb_raw", tmp_path / "raw")
    p1, p2 = "/abs/one.jsonl", "/abs/two.jsonl"
    with session_lock(p1):
        # A second, independent open of the SAME lock file cannot acquire it.
        fh = open(session_lock_path(p1))
        with pytest.raises(BlockingIOError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.close()
        # A different transcript path is independently lockable.
        fh2 = open(session_lock_path(p2), "w")
        fcntl.flock(fh2.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh2.fileno(), fcntl.LOCK_UN)
        fh2.close()
    # Once released, the path is lockable again.
    with session_lock(p1):
        pass


# --- #17 extract_items coerces non-list model output -------------------------


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def raise_for_status(self) -> None:  # noqa: D401
        return None

    def json(self) -> dict:
        return self._p


@pytest.mark.parametrize(
    "content", ['{"items": null}', '{"items": "x"}', '{"nope": 1}', '"just a string"']
)
def test_extract_items_coerces_non_list(monkeypatch, content):
    def fake_post(*_a, _c=content, **_k):
        return _FakeResp({"message": {"content": _c}})

    monkeypatch.setattr(llm.httpx, "post", fake_post)
    assert llm.extract_items("a non-empty transcript") == []


# --- #19 session_hook.sh refuses a malformed settings.json -------------------


def _run_hook(project_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SCRIPT), "myproj"],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )


def test_hook_installer_refuses_malformed_settings(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    settings_file = proj / ".claude" / "settings.json"
    settings_file.write_text("{ this is : not valid json ]")
    before = settings_file.read_text()

    result = _run_hook(proj)

    assert result.returncode != 0
    assert "installed" not in result.stdout
    assert "not a valid JSON object" in result.stderr
    assert settings_file.read_text() == before, "malformed file must be left untouched"


def test_hook_installer_merges_into_fresh_project(tmp_path):
    proj = tmp_path / "proj2"
    proj.mkdir()

    result = _run_hook(proj)

    assert result.returncode == 0
    assert "installed" in result.stdout
    data = json.loads((proj / ".claude" / "settings.json").read_text())
    assert isinstance(data, dict)
    stop = data["hooks"]["Stop"]
    assert any("ingest-session" in h["command"] for entry in stop for h in entry["hooks"])
