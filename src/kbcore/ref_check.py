"""kbcore.ref_check — flag identifiers an extracted item invented (audit 2026-07-26).

Four of the confirmed errors in the code-grounded audit of 30 machine-extracted
items were INVENTED IDENTIFIERS inside otherwise-correct knowledge: a file that
does not exist (``ingester.py``), a directory that does not exist (``~/bin``), an
env var that is not in the config (``OAUTH_TOKEN``). The surrounding claim was
right, so rejecting the item would have destroyed real knowledge — hence this
check NEVER strips and NEVER rejects. It stamps ``meta.unresolved_refs`` and
rides the review-queue detail, so the human reviewer and the triage LLM see
exactly which token failed to resolve.

Resolution targets (cheap, no network, cached per process): the repo tree, the
installed Claude Code hooks dir, and the kbcore settings field names / the shell
config surface that defines the remaining env vars.

DELIBERATE SCOPE-DOWN: env-var candidates must contain an UNDERSCORE. The audit
brief's ``\\b[A-Z][A-Z0-9_]{3,}\\b`` also matches every acronym and part number in
the corpus (JSON, HTTP, DRV8706, README), and that noise would be actively
harmful — kbcore.consolidation excludes any item carrying ``unresolved_refs``
from summary sources, so a flood of acronym false-positives would starve the
summaries. ``OAUTH_TOKEN`` (the audited miss) is still caught.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

# Repo root is two levels up from src/kbcore/ref_check.py (same derivation as
# kbcore.config._REPO_ROOT — a code-relative path, never a configured one).
_REPO_ROOT = Path(__file__).resolve().parents[2]
# Where the kb-session-flush skill installs its hooks. Overridable per call so
# tests never depend on the developer's home directory.
_HOOKS_DIR = Path.home() / ".claude" / "hooks"

MAX_REFS = 12  # cap the stamped list: meta/detail must stay small

_SKIP_DIRS = frozenset(
    {".git", ".venv", "venv", "__pycache__", "node_modules", ".ruff_cache",
     ".pytest_cache", "backups", "logs", ".mypy_cache", "dist", "build"}
)  # fmt: skip

_SOURCE_FILE = re.compile(
    r"(?<![\w./~-])[\w./~-]+\.(?:py|sh|ts|js|c|h|sql|yml|yaml)\b", re.IGNORECASE
)
_REPO_PATH = re.compile(r"(?<![\w./-])src/[\w./-]+")
_HOME_PATH = re.compile(r"~/[\w./-]+")
# Env-var shape, underscore REQUIRED (see the module docstring's scope-down note).
_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
# Files whose ALL-CAPS tokens define the env vars the hooks/scripts consume.
_ENV_SOURCES = ("scripts", "contrib", "docker-compose.yml", "Makefile", ".env.example")

_TRAILING = ".,;:!?)]}\"'`>"


@lru_cache(maxsize=4)
def _repo_index(root: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(relative posix paths, basenames)`` of every file under ``root``.

    Cached per process: the nightly drain resolves thousands of tokens against
    the same tree, and a fresh walk per item would dominate extraction cost.
    """
    rel: set[str] = set()
    names: set[str] = set()
    base = Path(root)
    if not base.is_dir():
        return frozenset(), frozenset()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        here = Path(dirpath)
        for fn in filenames:
            names.add(fn)
            rel.add((here / fn).relative_to(base).as_posix())
    return frozenset(rel), frozenset(names)


@lru_cache(maxsize=4)
def _hooks_index(hooks_dir: str) -> frozenset[str]:
    """Basenames installed in the Claude Code hooks dir (empty when absent)."""
    d = Path(hooks_dir)
    if not d.is_dir():
        return frozenset()
    return frozenset(p.name for p in d.iterdir() if p.is_file())


@lru_cache(maxsize=4)
def _known_env_names(root: str) -> frozenset[str]:
    """Env-var names this system legitimately defines.

    kbcore.config field names (``kb_token`` → ``KB_TOKEN``) plus the ALL-CAPS
    tokens in the shell/compose surface that the hooks read (``KB_DIR``,
    ``KB_MCP_URL``, …), plus the process environment.
    """
    from kbcore.config import Settings

    known = {name.upper() for name in Settings.model_fields}
    known |= set(os.environ)
    base = Path(root)
    for entry in _ENV_SOURCES:
        target = base / entry
        files = (
            [p for p in target.rglob("*") if p.is_file()]
            if target.is_dir()
            else ([target] if target.is_file() else [])
        )
        for p in files:
            try:
                known |= set(_ENV_VAR.findall(p.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
    return frozenset(known)


def _candidates(text: str) -> list[str]:
    """Ordered, de-duplicated reference-shaped tokens found in ``text``."""
    out: list[str] = []
    for pattern in (_SOURCE_FILE, _REPO_PATH, _HOME_PATH, _ENV_VAR):
        for raw in pattern.findall(text):
            tok = raw.rstrip(_TRAILING)
            if tok and tok not in out:
                out.append(tok)
    return out


def _resolves(
    tok: str, root: str, rel: frozenset[str], names: frozenset[str], hooks: frozenset[str]
) -> bool:
    """True when a path-shaped token points at something that actually exists."""
    if tok.startswith("~/") and Path(tok).expanduser().exists():
        return True
    cleaned = tok.lstrip("~").lstrip("/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned in rel or (Path(root) / cleaned).exists():
        return True
    if "/" in cleaned and any(r == cleaned or r.endswith("/" + cleaned) for r in rel):
        return True
    base = cleaned.rsplit("/", 1)[-1]
    return base in names or base in hooks


def _root_for_project(project: str | None) -> Path | None:
    """The tree a PROJECT's references should be resolved against, or None.

    References are only checkable against the repo the knowledge is about. This
    module resolved everything against the data-server tree, so a session about
    any OTHER project had every path-shaped token flagged: measured 2026-07-30,
    703 of 722 open `unresolved_refs` flags (97.4%) came from aquamonix, i-redis,
    repeater and friends — files that could not possibly exist here. That noise
    buried the 19 real data-server flags and made the signal worthless.

    Returns None when no tree is known for the project. "I cannot see it" is not
    evidence that a reference is broken, so the caller must SKIP the check rather
    than flag — a false accusation is worse than a missed one for a signal whose
    only job is to draw an owner's attention.
    """
    if not project:
        return None
    from kbcore.config import settings

    for repo in settings.git_repos:
        if repo.name == project and repo.is_dir():
            return repo
    if project == _REPO_ROOT.name:
        return _REPO_ROOT
    return None


def unresolved_refs(
    text: str,
    *,
    project: str | None = None,
    repo_root: Path | str | None = None,
    hooks_dir: Path | str | None = None,
    limit: int = MAX_REFS,
) -> list[str]:
    """Reference-shaped tokens in ``text`` that resolve to NOTHING (flag only).

    Never mutates the text and never rejects the item — the caller stamps the
    result on ``meta.unresolved_refs`` and copies it into the review detail.
    Returns at most ``limit`` tokens, in first-appearance order.
    """
    # PATH tokens are only checkable against the tree the knowledge is about; ENV
    # tokens are checked against this process's environment and config files, which
    # is meaningful whatever the project. So a missing tree suppresses paths ONLY —
    # suppressing both also disabled the env check and dropped the OAUTH_TOKEN
    # detection that an audit had specifically added.
    paths_checkable = True
    if repo_root is None and project is not None:
        resolved = _root_for_project(project)
        if resolved is None:
            paths_checkable = False
        else:
            repo_root = resolved
    root = str(repo_root or _REPO_ROOT)
    hooks = str(hooks_dir or _HOOKS_DIR)
    rel, names = _repo_index(root)
    hook_names = _hooks_index(hooks)
    env_names = _known_env_names(root)

    out: list[str] = []
    for tok in _candidates(text):
        if _ENV_VAR.fullmatch(tok):
            if tok in env_names:
                continue
        elif not paths_checkable:
            continue  # no tree for this project: cannot verify, so never accuse
        elif _resolves(tok, root, rel, names, hook_names):
            continue
        out.append(tok)
        if len(out) >= limit:
            break
    return out
