"""kbworker.reposcan — git-tracked docs scanner; never source files (§7.5).

Invariant 9: the KB is not a code index. This scanner ingests only git-tracked
documentation — README/CHANGELOG, ``docs/**/*.md``, ``adr/**``,
``design/**/*.md``, root ``*.md`` — and a hard denylist of source extensions
means a stray ``adr/foo.py`` can never slip in. Ingest is content-hash deduped,
so daily re-runs are no-ops (DATA-1/OPS-5). All SQL is in kbcore.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path, PurePosixPath

from kbcore.config import settings
from kbcore.errors import KBError
from kbcore.ingest import ingest
from kbcore.items import project_for_repo
from kbcore.models import RepoScanSummary
from kbcore.supersede import supersede_doc_refresh

logger = logging.getLogger("kbworker.reposcan")

# Prose/markup extensions eligible as documentation.
DOC_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt"})
# Invariant 9 hard guard: anything with a source/build/data suffix is NEVER a
# doc, whatever directory it sits in (a `.py` under adr/ stays out).
SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".hh",
        ".ino",
        ".s",
        ".asm",
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".m",
        ".mm",
        ".rb",
        ".php",
        ".pl",
        ".lua",
        ".r",
        ".jl",
        ".scala",
        ".dart",
        ".vue",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".bat",
        ".sql",
        ".css",
        ".scss",
        ".less",
        ".html",
        ".htm",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".xml",
        ".v",
        ".sv",
        ".vhd",
        ".vhdl",
    }
)


def is_doc_path(rel: str) -> bool:
    """True if a git-tracked relative path is documentation we may ingest (§7.5)."""
    p = PurePosixPath(rel)
    suffix = p.suffix.lower()
    if suffix in SOURCE_SUFFIXES:
        return False  # Invariant 9: never a source file
    name = p.name.upper()
    parts = p.parts
    if name.startswith("README") or name.startswith("CHANGELOG"):
        # Allowlist the extension too: README.pdf / CHANGELOG.gz / README.png
        # are binary, not docs (review finding — this branch had no ext gate,
        # which also let uncovered source exts like README.ex through).
        return suffix in DOC_SUFFIXES or suffix == ""
    if len(parts) == 1 and suffix == ".md":  # root *.md
        return True
    top = parts[0].lower() if parts else ""
    if top == "docs" and suffix == ".md":
        return True
    if top == "design" and suffix == ".md":
        return True
    if top == "adr" and suffix in DOC_SUFFIXES:
        return True
    return False


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        # errors="replace" so a git-tracked doc with a non-UTF-8 (legacy-encoded)
        # filename can't raise UnicodeDecodeError here. With core.quotePath=false
        # git emits raw name bytes; a strict decode would crash _tracked_files
        # (uncaught, since it wraps only CalledProcessError/FileNotFoundError),
        # and scan_all's `except KBError` would let it abort the whole daily
        # docs pass (review finding). The mangled name then fails to resolve to a
        # real file and is skipped; valid UTF-8 names (résumé.md) are unaffected.
        errors="replace",
        check=True,
    )
    return result.stdout


def _tracked_files(repo: Path) -> list[str]:
    try:
        # core.quotePath=false: git otherwise C-quotes non-ASCII paths
        # (docs/résumé.md → "docs/r\303\251...") so they never match is_doc_path
        # and are silently skipped (review finding).
        out = _git(repo, "-c", "core.quotePath=false", "ls-files")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise KBError(f"git ls-files failed for {repo}: {e}") from e
    return [line for line in out.splitlines() if line.strip()]


def _short_head(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "--short", "HEAD").strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        # No commits yet, or git binary absent — return None; _tracked_files
        # raises the clean KBError if git is truly missing (review finding: a
        # bare FileNotFoundError used to escape from here).
        return None


def _doc_type(rel: str) -> str:
    name = PurePosixPath(rel).name.upper()
    return "note" if name.startswith("CHANGELOG") else "spec"


def _safe_repo_file(repo: Path, rel: str) -> Path | None:
    """Return the real file for a tracked doc, or None if it must be skipped.

    Invariant 9 defense-in-depth: git tracks symlinks, so a doc-named symlink
    (``docs/api.md -> ../src/driver.py`` or ``-> /etc/passwd``) would otherwise
    be dereferenced by read_bytes() and ingested as trusted source/host content.
    We refuse symlinks outright and require the resolved path to be a regular
    file living inside the repo tree.
    """
    file_path = repo / rel
    if file_path.is_symlink():
        logger.warning("skipping tracked symlink %s (Invariant 9: no dereferencing)", rel)
        return None
    try:
        resolved = file_path.resolve()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    try:
        resolved.relative_to(repo)
    except ValueError:
        logger.warning("skipping %s: resolves outside the repo tree", rel)
        return None
    return resolved


def scan_repo(path: str | Path) -> RepoScanSummary:
    """Ingest git-tracked docs from one repo. Returns which paths were ingested."""
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise KBError(f"{repo} is not a git repository (no .git)")
    repo_name = repo.name
    project = project_for_repo(str(repo)) or repo_name
    commit = _short_head(repo)

    ingested: list[str] = []
    skipped = 0
    errors = 0
    superseded = 0
    for rel in _tracked_files(repo):
        if not is_doc_path(rel):
            continue
        file_path = _safe_repo_file(repo, rel)
        if file_path is None:
            continue
        try:
            raw = file_path.read_bytes()
            item_id = ingest(
                project,
                _doc_type(rel),
                title=rel,
                body=raw.decode("utf-8", errors="replace"),
                kind="git",
                uri=f"{repo_name}:{rel}",
                trust="verified",
                code_ref={"repo": repo_name, "path": rel, "commit": commit},
                raw_content=raw,
            )
            # A changed doc = a NEWER REVISION of the same source uri: retire the
            # stale predecessor(s) instead of letting old-vs-new coexist and get
            # flagged as a contradiction later (owner decision — git history IS
            # the human decision). Inside the per-doc guard: hook failures count
            # as errors without aborting the scan.
            if item_id is not None and settings.kb_auto_supersede_doc_refresh:
                superseded += len(supersede_doc_refresh(item_id))
        except Exception:  # noqa: BLE001 — one bad doc must not abort the scan
            logger.exception("failed to ingest %s:%s", repo_name, rel)
            errors += 1
            continue
        if item_id is None:
            skipped += 1
        else:
            ingested.append(f"{repo_name}:{rel}")
    return RepoScanSummary(
        repo=str(repo),
        project=project,
        commit=commit,
        ingested=ingested,
        skipped_dupes=skipped,
        errors=errors,
        superseded=superseded,
    )


def scan_all() -> list[RepoScanSummary]:
    """Scan every repo in ``KB_GIT_REPOS`` (the daily worker pass, §7.5)."""
    summaries: list[RepoScanSummary] = []
    for repo in settings.git_repos:
        if not (repo / ".git").exists():
            logger.warning("skipping %s: not a git repository", repo)
            continue
        try:
            summaries.append(scan_repo(repo))
        except KBError:
            logger.exception("repo docs scan failed for %s", repo)
    return summaries
