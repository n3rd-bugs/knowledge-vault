"""kbworker.eval_epoch_lines — the eval EPOCH on the digest and the weekly (E1).

Pure renderers over a run's counts / the week's report metas, in their own
small module because kbworker.digest_lines and kbworker.weekly_render both sit
at the 300-line ratchet; each hooks in with one line.

WHY (KB 31857 review #8 "E1 contents", KB 32942 review #9a): golden.yaml
changes land only at a review boundary, and that commit "re-anchors the floor
and resets the WoW baseline". Every eval report carries the eval EPOCH KEY
(``eval_golden_hash``, kbcore.eval_epoch: the golden bytes + the embed model
+ the configured embed hosts — no longer the golden file's content alone).
Two consequences render here, both positive-only — a report without the key
contributes nothing new:

* DIGEST: the first night of a new epoch (``eval_epoch_reset``, set by
  kbworker.eval_regression) says so, because that night's eval has no prior
  and the owner should read its numbers against the floors alone.
* WEEKLY: the ``eval mrr`` / ``eval hit@5`` trends must not draw one line
  through two golden sets (52 -> 59 positive-criterion cases — 61 cases in
  all — moves the MRR quantum from 0.0096 to 0.0085 and every case's rank),
  so the series is confined to the LATEST epoch in the window. Epochs are
  defined over the reports that actually CARRY an eval — ``eval_status ==
  'ok'``, the same eligibility rule as ``kbcore.eval_baseline.read_baseline``.
  An error / skipped / disabled night writes neither the hash nor the metrics
  (kbworker.eval_sentinel stamps both on the ok path only), so it is not a
  group of its own and never the "newest" report: a trailing error night must
  not select the key-less group and trend the PRE-epoch reports, and an error
  night mid-window must not read as a reset. Ok reports without the key form
  one implicit pre-epoch group. A window spanning more than one group says
  ``(epoch reset <night>)`` — the LOCAL night the new epoch's first report
  belongs to, keyed on that run's START exactly as the weekly's table rows
  are (``weekly_instruments.run_start`` + ``digest_lines.night_of``), so the
  reader can find the row where the series restarted.

E2 (review #10) renders the detector's new facts here too — digest.py and
digest_lines.py sit at the ratchet: the EVAL RED alert (hit@5 AND MRR against
the floors the detector compared, plus the trip cause; ``digest_lines.
_eval_red_line`` delegates to :func:`eval_red_line`), ONE alert line per
persistent golden case — at most :data:`CASE_LINES_MAX` in golden order plus
one ``EVAL CASE +N more``, so a mass failure cannot push every other alert
class past the digest's 20-line cap — and a notice per first pass after a red
streak (kbworker.eval_persistence), capped the same way. Golden INDEX and
satisfier id only — never a query text (a digest is a phone push, and a pushed
golden query is an echo).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, tzinfo
from typing import Any

from kbworker.digest_lines import night_of
from kbworker.eval_persistence import PERSIST_NIGHTS
from kbworker.weekly_instruments import clean_reports, run_start

Report = tuple[datetime, dict[str, Any]]
CASE_LINES_MAX = 5  # EVAL CASE alert lines before one '+N more' (digest.DIGEST_MAX_LINES = 20)


def eval_epoch_line(counts: Mapping[str, Any]) -> str | None:
    """The digest's first-night-of-an-epoch fact, or None when the run has no reset.

    Pure ASCII (ntfy to a phone, like every digest line). The line names the
    epoch and tonight's n so the number beside it in the same digest is read
    against the floors, not against a week that measured a different set.
    """
    reset = counts.get("eval_epoch_reset")
    if not isinstance(reset, Mapping):
        return None
    return (
        f"eval epoch {reset.get('golden_hash')} (n={reset.get('queries')}): "
        "first night - WoW baseline reset, floors only"
    )


def _epoch(meta: Mapping[str, Any]) -> str | None:
    """A report's epoch id; None for a report that predates epoch tracking."""
    v = meta.get("eval_golden_hash")
    return v if isinstance(v, str) and v else None


def _evaluated(reports: list[Report]) -> list[Report]:
    """The reports that carry an eval: ``eval_status == 'ok'`` (read_baseline's rule).

    Order is preserved. A night whose eval errored, was skipped or disabled
    has no hash and no metrics (module docstring) and takes no part in the
    epoch grouping at all.
    """
    return [(at, m) for at, m in reports if m.get("eval_status") == "ok"]


def epoch_groups(reports: list[Report]) -> list[str | None]:
    """The distinct epochs among the window's EVALUATED reports, oldest-first.

    None is the implicit pre-epoch group (an ok report without the key);
    reports are OLDEST first, as ``weekly._nightly_reports`` returns them.
    """
    seen: list[str | None] = []
    for _at, meta in _evaluated(reports):
        e = _epoch(meta)
        if e not in seen:
            seen.append(e)
    return seen


def epoch_reports(reports: list[Report]) -> list[Report]:
    """The window's reports measured on the LATEST epoch — the newest EVALUATED report's.

    The trend series read only these; an untracked window (no report carries
    the key) is one group, so the series is what it always was. A window with
    no evaluated report at all is empty (the series renders n/a, as before).
    """
    evaluated = _evaluated(reports)
    if not evaluated:
        return []
    latest = _epoch(evaluated[-1][1])
    return [(at, m) for at, m in evaluated if _epoch(m) == latest]


def epoch_clean(reports: list[Report]) -> list[Report]:
    """The primary-replay series' input: the latest epoch's FALLBACK-FREE reports.

    Both filters the weekly's ``eval hit@5`` / ``eval mrr`` lines need, composed
    here so the hook in kbworker.weekly_render stays one line per series
    (review #9a EV-1 for the fallback half, E1 for the epoch half).
    """
    return clean_reports(epoch_reports(reports))


def epoch_suffix(reports: list[Report], tz: tzinfo | None = None) -> str:
    """`` (epoch reset <night>)`` when the window spans more than one epoch, else ``""``.

    The night is the LOCAL night of the latest epoch's first report, keyed on
    that run's START (``run_start``: finish minus ``run_secs``) — the same
    rule ``weekly_instruments.nights`` files the table rows under, so the
    reader can find the row where the series restarted. Keying on the finish
    instant filed a 6 h run that finished after the noon rollover one row
    later than its own table row.
    """
    if len(epoch_groups(reports)) < 2:
        return ""
    first_at, first_meta = epoch_reports(reports)[0]
    return f" (epoch reset {night_of(run_start(first_at, first_meta), tz).isoformat()})"


# --- E2: the EVAL RED alert, persistent cases, first passes ---------------------


def _shown(v: Any) -> str:
    return "?" if v is None else str(v)


def _floor(counts: Mapping[str, Any], key: str, setting: str) -> str:
    """The floor the detector compared tonight (``eval_floor_*``); an older run
    without the key reads the configured floor at render time, as ever."""
    v = counts.get(key)
    if isinstance(v, int | float) and not isinstance(v, bool):
        return f"{v:g}"
    try:
        from kbcore.config import settings

        return f"{getattr(settings, setting):g}"
    except Exception:  # noqa: BLE001 — a digest line may never cost the digest
        return "?"


def eval_red_line(counts: Mapping[str, Any]) -> str:
    """``EVAL RED: hit@5 H (floor F), MRR M (floor G) - <cause>``.

    The MRR and cause parts render only when the run carries them
    (positive-only, like every digest line); a pre-E2 run reads exactly the
    pre-E2 ``EVAL RED: hit@5 H (floor F)``.
    """
    line = (
        f"EVAL RED: hit@5 {_shown(counts.get('eval_hit_at_5'))} "
        f"(floor {_floor(counts, 'eval_floor_hit_at_5', 'kb_eval_min_hit5')})"
    )
    if counts.get("eval_mrr") is not None:
        line += (
            f", MRR {counts['eval_mrr']} "
            f"(floor {_floor(counts, 'eval_floor_mrr', 'kb_eval_min_mrr')})"
        )
    cause = counts.get("eval_regression_cause")
    if isinstance(cause, list) and cause:
        line += f" - {'+'.join(str(c) for c in cause)}"
    return line


def _entries(counts: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    v = counts.get(key)
    return [e for e in v if isinstance(e, Mapping)] if isinstance(v, list) else []


def eval_case_lines(counts: Mapping[str, Any]) -> list[str]:
    """ALERT lines per persistent case: ``EVAL CASE #14 (263): red 3 nights since <date>``.

    The first :data:`CASE_LINES_MAX` in golden order (the producer's order),
    then ONE ``EVAL CASE +N more`` line for the rest — the EVAL RED line above
    already names the 'persistent' cause, and the full list stays in meta.
    """
    entries = _entries(counts, "eval_case_persistent")
    lines = [
        f"EVAL CASE #{_shown(p.get('case'))} ({_shown(p.get('satisfier'))}): "
        f"red {_shown(p.get('nights'))} nights since {_shown(p.get('since'))}"
        for p in entries[:CASE_LINES_MAX]
    ]
    if len(entries) > CASE_LINES_MAX:
        lines.append(f"EVAL CASE +{len(entries) - CASE_LINES_MAX} more")
    return lines


def first_pass_lines(counts: Mapping[str, Any]) -> list[str]:
    """A NOTICE per case green tonight after >= PERSIST_NIGHTS red nights (fact tier).

    Capped like :func:`eval_case_lines` (fix-round review D2): a mass-recovery
    night shows the first :data:`CASE_LINES_MAX` then ONE ``eval case +N more
    first passes``, so the facts after them survive the digest's 20-line cap.
    """
    entries = _entries(counts, "eval_case_first_pass")
    lines = [
        f"eval case #{_shown(p.get('case'))} ({_shown(p.get('satisfier'))}): "
        f"first pass after {PERSIST_NIGHTS}+ red nights"
        for p in entries[:CASE_LINES_MAX]
    ]
    if len(entries) > CASE_LINES_MAX:
        lines.append(f"eval case +{len(entries) - CASE_LINES_MAX} more first passes")
    return lines
