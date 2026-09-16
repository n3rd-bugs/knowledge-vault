"""kbworker.eval_epoch_lines — the eval EPOCH on the digest and the weekly (E1).

Pure renderers over a run's counts / the week's report metas, in their own
small module because kbworker.digest_lines and kbworker.weekly_render both sit
at the 300-line ratchet; each hooks in with one line.

WHY (KB 31857 review #8 "E1 contents", KB 32942 review #9a): golden.yaml
changes land only at a review boundary, and that commit "re-anchors the floor
and resets the WoW baseline". Every eval report carries the golden set's
content hash (``eval_golden_hash``, kbcore.eval_epoch). Two consequences
render here, both positive-only — a report without the key contributes
nothing new:

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
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, tzinfo
from typing import Any

from kbworker.digest_lines import night_of
from kbworker.weekly_instruments import clean_reports, run_start

Report = tuple[datetime, dict[str, Any]]


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
