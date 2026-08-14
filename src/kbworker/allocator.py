"""kbworker.allocator — who gets the nightly window: the drain, or the LLM tail.

THE SUBSCRIPTION MATH. The window is 01:00->12:00 (11 h). Median arrival costs
the drain 5.9-7.1 h (73-89 MB/day at ~12.5 MB/h measured); the LLM tail at
current caps costs 5.5-6.5 h (triage 2.9 h, contradictions 1.3 h, summaries
0.8-1.6 h, evidence 0.5-0.7 h). A median night DEMANDS 11.4-13.6 h of an 11 h
window — subscribed at 104-124%, so SOMEBODY is the residual claimant: the step
that runs last gets what the others left, on a saturated night nothing. The
drain is Step 0 with a refill-until-empty loop, so today that is the tail —
report 9684: queued_drained 33,284 s (99.5% of the window), triage 0 rows,
contradictions/summaries/evidence stood down, judged-pair memory (migration 017)
frozen at 0 rows because the contradictions step has not run since it landed.

WHY THE DRAIN MUST BE THE RESIDUAL CLAIMANT INSTEAD (owner-approved: keep the
drain first, cap its appetite).

* **Drain debt is durable and self-announcing.** An unreached group stays
  'pending', is counted by ``ingest_queue.queue_depth()``, rides every run
  report, and ingest is idempotent (Invariant 3) — falling behind is VISIBLE and
  RECOVERABLE: a deeper queue tomorrow, drained by a lighter night.
* **Tail debt compounds invisibly.** Nothing records "triage did not run":
  verdicts sit unapplied so the queue grows; unjudged contradictions never write
  the per-pair memory that makes the NEXT night cheaper; evidence auto-resolution
  needs ``AUTO_RESOLVE_STREAK`` CONSECUTIVE nightly visits, so a skipped night
  RESETS a resolution. The cost is the work that never converges.

So the drain stops admitting NEW refill cohorts once the next cohort's estimated
cost would eat into ``kb_tail_reserve_minutes`` (180; 0 restores the old
behaviour). The reserve is an INNER bound only: the deadline and battery guards
still preempt everything including the tail, and it can never extend a run past
``KB_DRAIN_DEADLINE``. The arithmetic is naive-local wall clock, like
:func:`kbworker.consolidate._past_deadline` — scheduling, not storage, so
Invariant 7 (TIMESTAMPTZ/UTC for stored data) is untouched.

INTERNAL SHARES WITHIN THE RESERVE (WAVE-2, 2026-08-09; anchor KB 19663). The
pooled reserve protects the tail FROM the drain, not the tail's later steps from
its earlier ones: triage runs FIRST and can claim the whole reserve, leaving
contradictions/evidence ~0. ``TailReserve`` therefore hands a step an INNER
share: admit work while ``now + est_cost < tail_start + share*reserve``, anchored
to when the tail ACTUALLY entered its first step (review #3: on reserve_start, an
overrunning drain silently spent triage's slice; reserve_start stands in until
the tail is marked started). ``kb_tail_triage_share`` gives triage its slice
(default 0 = OLD pooled behaviour, byte-identical inert); weekends carry no
deadline, so the share is inert there and the top-up runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kbcore.config import settings

logger = logging.getLogger("kbworker.allocator")

# ``'deadline'`` / ``'battery'`` / None — the consolidate stand-down guard.
StopCheck = Callable[[], "str | None"]

# The stop reason recorded in counts["stopped"], same shape as the deadline and
# battery stops — distinct so a report can tell "the window ran out" (deadline)
# apart from "the window was DEALT" (tail-reserve).
TAIL_RESERVE_REASON = "tail-reserve"

# The reason a tail step stops because it spent its INTERNAL share of the reserve —
# distinct from the run's deadline/battery stop and the drain's pooled tail-reserve.
TRIAGE_SHARE_REASON = "triage-share"

# Measured wall-clock cost of ONE triage row (the 14B judge) — the one-row lookahead
# a share-bounded triage uses to stop before its slice closes (10.4 s/row measured).
TRIAGE_SECONDS_PER_ROW = 10.4

# Cost of the FIRST cohort of a run, before it has observed anything. Measured
# across nightly reports: ~111 s/job wall clock, dominated by model loading
# rather than input size. Deliberately a constant, not a cross-run average — a
# learned estimate inherits the pathological night, and all this has to do is
# stop the first refill of a spent window being admitted blind.
FIRST_COHORT_SECONDS_PER_JOB = 111.0


def parse_deadline(raw: str, now: datetime) -> datetime | None:
    """Resolve an ``HH:MM`` knob against ``now``'s date; None when unset/invalid.

    Same parse as the deadline guard, here so the arithmetic is testable without
    patching wall clocks. Unparseable disables the reserve (the guard warns about
    it already) rather than guessing a cutoff.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None


def remaining_window(now: datetime, deadline: datetime | None) -> float | None:
    """Seconds left before the wall-clock cutoff; None when there is no cutoff.

    Negative is legitimate (the cutoff has passed): the deadline guard stops the
    run, this only decides who may START more work.
    """
    if deadline is None:
        return None
    return (deadline - now).total_seconds()


def should_admit_cohort(
    remaining: float | None, reserve_seconds: float, est_cost_seconds: float
) -> bool:
    """May the drain start one more cohort costing ``est_cost_seconds``?

    True unless the cohort would run into the tail's reserve, i.e. unless
    ``est_cost > remaining - reserve``. Two disabling cases, both restoring the
    pre-reserve behaviour exactly: ``reserve_seconds <= 0`` (the kill switch
    ``KB_TAIL_RESERVE_MINUTES=0``) and ``remaining is None`` (no deadline, so no
    window to apportion and nothing to reserve FROM).
    """
    if reserve_seconds <= 0 or remaining is None:
        return True
    return est_cost_seconds <= remaining - reserve_seconds


@dataclass
class CostTracker:
    """Observed drain cost for ONE run: jobs completed and seconds spent.

    Per RUN on purpose — tonight's GPU is the only honest predictor of tonight's
    next cohort (power cap, ensemble mode, one-row notes vs 350-row sessions).
    Before the first cohort lands, :data:`FIRST_COHORT_SECONDS_PER_JOB` stands in.
    """

    jobs: int = 0
    seconds: float = 0.0

    def record(self, jobs: int, seconds: float) -> None:
        """Fold one completed cohort into the average."""
        if jobs > 0 and seconds >= 0:
            self.jobs += jobs
            self.seconds += seconds

    @property
    def observed(self) -> bool:
        """True once this run has finished at least one cohort."""
        return self.jobs > 0 and self.seconds > 0

    @property
    def seconds_per_job(self) -> float:
        """This run's measured s/job, or the measured fallback constant."""
        return self.seconds / self.jobs if self.observed else FIRST_COHORT_SECONDS_PER_JOB

    def estimate(self, jobs: int) -> float:
        """Predicted wall-clock cost of a cohort of ``jobs`` jobs."""
        return max(0, jobs) * self.seconds_per_job


def deferred_groups(jobs: Sequence[Mapping[str, Any]]) -> int:
    """How many coalesced GROUPS the given jobs belong to.

    A group's window jobs share one mutable state dict (``job["group"]``), so
    identity counts them without the queue ids. Groups are the unit an owner
    reasons about: "12 submissions deferred" means something, "37 windows" does not.
    """
    return len({id(job.get("group")) for job in jobs}) if jobs else 0


def _effective_deadline() -> str:
    from kbcore.drainsched import effective_deadline  # day-aware (weekends: none)

    return effective_deadline()


@dataclass
class TailReserve:
    """Run-scoped admission control for drain cohorts (the reserve arithmetic).

    Built once per drain (:meth:`for_run`), fed each cohort's cost
    (:meth:`record`), asked before each cohort (:meth:`deny_reason`).
    """

    reserve_seconds: float = 0.0
    deadline: datetime | None = None
    tracker: CostTracker = field(default_factory=CostTracker)
    tail_start: datetime | None = None  # when the TAIL actually started (share anchor)
    _now: Callable[[], datetime] = datetime.now

    @classmethod
    def for_run(cls, now: datetime | None = None) -> TailReserve:
        """Read the knobs once, at the start of a drain."""
        started = now or datetime.now()
        return cls(
            reserve_seconds=max(0, int(settings.kb_tail_reserve_minutes)) * 60.0,
            deadline=parse_deadline(_effective_deadline(), started),
        )

    @property
    def enabled(self) -> bool:
        """False when the reserve cannot bind (kill switch or no deadline)."""
        return self.reserve_seconds > 0 and self.deadline is not None

    @property
    def reserve_start(self) -> datetime | None:
        """Wall-clock instant the tail's reserved window opens (deadline - reserve).

        None whenever the reserve cannot bind (same condition as :attr:`enabled`).
        """
        if not self.enabled:
            return None
        return self.deadline - timedelta(seconds=self.reserve_seconds)  # deadline set

    def mark_tail_start(self) -> None:
        """Stamp the share anchor at the FIRST tail-step entry (idempotent, KB 19663)."""
        self.tail_start = self.tail_start or self._now()

    def share_cutoff(self, share: float) -> datetime | None:
        """The instant a step holding ``share`` must stop by: ``anchor +
        share*reserve``, the INNER bound keeping an early tail step (triage) from
        spending the later ones' window. Anchor = the tail's ACTUAL start when
        marked, else ``reserve_start``. None (= OLD pooled) when off/``share<=0``."""
        if not self.enabled or share <= 0:
            return None
        start = self.tail_start or self.reserve_start
        return start + timedelta(seconds=share * self.reserve_seconds)

    def admit_step(self, share: float, est_cost_seconds: float = 0.0) -> bool:
        """May a step holding ``share`` admit ``est_cost_seconds`` more work?

        ``now + est_cost < share_cutoff``; always True when the share is inert, so
        a non-opted-in run behaves exactly as it did before this knob existed.
        """
        cutoff = self.share_cutoff(share)
        if cutoff is None:
            return True
        return self._now() + timedelta(seconds=est_cost_seconds) < cutoff

    def share_budget_minutes(self, share: float) -> float:
        """Minutes ``share`` reserves for its step — the report's MEASURED number
        in place of the module docstring's hand-arithmetic; 0.0 when inert."""
        if self.share_cutoff(share) is None:
            return 0.0
        return round(share * self.reserve_seconds / 60.0, 1)

    def share_guard(
        self, share: float, base: StopCheck, est_cost_seconds: float = TRIAGE_SECONDS_PER_ROW
    ) -> StopCheck:
        """Wrap ``base`` so a step also stands down once it has spent its ``share``.

        Returns :data:`TRIAGE_SHARE_REASON` when the share is exhausted, else
        ``base()`` — the run's deadline/battery verdict, which STILL preempts within
        the share (an inner bound, never a way to run longer). When the share is
        inert (kill switch, no deadline, ``share <= 0``) this is ``base`` UNCHANGED,
        so the pooled behaviour is reproduced byte for byte and the knob is opt-in.
        Building a BINDING guard marks the tail started (the share anchor, KB 19663:
        this call site IS the first tail-step entry)."""
        if self.share_cutoff(share) is None:
            return base
        self.mark_tail_start()

        def guard() -> str | None:
            if not self.admit_step(share, est_cost_seconds):
                return TRIAGE_SHARE_REASON
            return base()

        return guard

    def record(self, jobs: int, seconds: float) -> None:
        """Fold a completed cohort into this run's observed cost."""
        self.tracker.record(jobs, seconds)

    def deny_reason(self, remaining_batches: Sequence[Sequence[Mapping[str, Any]]]) -> str | None:
        """:data:`TAIL_RESERVE_REASON` if the next cohort must not be admitted.

        ``remaining_batches`` is the not-yet-processed cohort list: the first is
        the cohort being considered, the rest only say how much is handed to the
        next run. None (admit) when disabled, when idle, or when the cohort fits.
        """
        if not self.enabled or not remaining_batches or not remaining_batches[0]:
            return None
        nxt = remaining_batches[0]
        est = self.tracker.estimate(len(nxt))
        now = self._now()
        if should_admit_cohort(remaining_window(now, self.deadline), self.reserve_seconds, est):
            return None
        deferred = sum(deferred_groups(b) for b in remaining_batches)
        logger.warning(
            "drain pausing for tail reserve: %d groups deferred — the tail runs from %s "
            "(next cohort ~%.0fs at %.0f s/job %s; %.0f min reserved before the %s deadline). "
            "Rows stay 'pending' and the next run resumes here.",
            deferred,
            now.strftime("%H:%M"),
            est,
            self.tracker.seconds_per_job,
            "observed this run" if self.tracker.observed else "assumed (first cohort)",
            self.reserve_seconds / 60.0,
            self.deadline.strftime("%H:%M") if self.deadline else "?",
        )
        return TAIL_RESERVE_REASON
