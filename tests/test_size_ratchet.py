"""File-size ratchet.

CLAUDE.md asks for files under ~300 lines. That rule was the most-violated one in
the repo, so it is now enforced mechanically rather than by discipline:

* a file NOT in ``tests/size_ratchet.json`` must be <= 300 lines;
* a file IN the baseline may not grow past its recorded size + SLACK;
* a baseline entry that is more than SLACK above the file's real size fails too,
  so the baseline only ever ratchets DOWN toward 300.

Pure filesystem — no git, no DB, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
BASELINE_PATH = Path(__file__).resolve().parent / "size_ratchet.json"

MAX_NEW_FILE_LINES = 300
# Slack so a genuine bugfix in an oversized file does not have to fight the
# ratchet (or edit the baseline) for a couple of lines.
SLACK = 25

# Generated / vendored trees never count against the ratchet.
SKIP_PARTS = {"vendor", "node_modules", "__pycache__", ".venv", "build", "dist"}


def _load_baseline() -> dict[str, int]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _source_files() -> list[Path]:
    return sorted(
        p for p in SRC_ROOT.rglob("*.py") if not SKIP_PARTS & set(p.relative_to(REPO_ROOT).parts)
    )


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_new_files_stay_under_the_limit() -> None:
    baseline = _load_baseline()
    offenders = {
        _rel(p): _line_count(p)
        for p in _source_files()
        if _rel(p) not in baseline and _line_count(p) > MAX_NEW_FILE_LINES
    }
    assert not offenders, (
        "These files exceed the "
        f"{MAX_NEW_FILE_LINES}-line limit and are not in the ratchet baseline:\n"
        + "\n".join(f"  {k}: {v} lines" for k, v in sorted(offenders.items()))
        + "\n\nSplit the module (preferred). Only add an entry to "
        f"{_rel(BASELINE_PATH)} for a pre-existing file being grandfathered in, "
        "with a justification in the commit message."
    )


def test_baselined_files_do_not_grow() -> None:
    baseline = _load_baseline()
    grown: list[str] = []
    for rel, recorded in sorted(baseline.items()):
        path = REPO_ROOT / rel
        if not path.exists():
            continue  # covered by test_baseline_entries_point_at_real_files
        actual = _line_count(path)
        if actual > recorded + SLACK:
            grown.append(f"  {rel}: {actual} lines, baseline {recorded} (+{SLACK} slack)")
    assert not grown, (
        "These oversized files GREW past their recorded size:\n"
        + "\n".join(grown)
        + "\n\nEither split the file (preferred — then update its entry in "
        f"{_rel(BASELINE_PATH)} DOWN to the new size), or consciously raise its "
        "entry and justify the increase in the commit message."
    )


def test_baseline_ratchets_down() -> None:
    baseline = _load_baseline()
    stale: list[str] = []
    for rel, recorded in sorted(baseline.items()):
        path = REPO_ROOT / rel
        if not path.exists():
            continue
        actual = _line_count(path)
        if recorded - actual > SLACK:
            stale.append(f"  {rel}: baseline {recorded}, actual {actual}")
    assert not stale, (
        "The file shrank well below its baseline — ratchet the baseline DOWN so "
        "the gain is locked in:\n" + "\n".join(stale)
    )


def test_baseline_entries_point_at_real_files() -> None:
    baseline = _load_baseline()
    missing = [rel for rel in sorted(baseline) if not (REPO_ROOT / rel).exists()]
    assert not missing, (
        "Baseline entries for files that no longer exist — remove them from "
        f"{_rel(BASELINE_PATH)}:\n" + "\n".join(f"  {m}" for m in missing)
    )


def test_baseline_only_lists_oversized_files() -> None:
    baseline = _load_baseline()
    small = {rel: n for rel, n in baseline.items() if n <= MAX_NEW_FILE_LINES}
    assert not small, (
        "These baseline entries are already within the "
        f"{MAX_NEW_FILE_LINES}-line limit — drop them so the plain limit applies:\n"
        + "\n".join(f"  {k}: {v}" for k, v in sorted(small.items()))
    )
