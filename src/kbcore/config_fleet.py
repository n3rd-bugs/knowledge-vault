"""kbcore.config_fleet — resend-guard, fleet-flush telemetry and resend-repair knobs.

Mixin split (2026-09-11, flush proposal v2 / KB 33784, root cause KB 33783):
config.py sits at its size-ratchet ceiling (433), so the wave-S knobs live here,
mixed into :class:`kbcore.config.Settings` exactly as ``LLMSettings`` and
``SettingsViews`` are (see config_llm's docstring for the verified
pydantic-settings inheritance semantics). Never instantiate this class on its
own: config flows through ``kbcore.config.settings`` only (CLAUDE.md hard rule).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class FleetSettings(BaseSettings):
    """Wave-S knobs, mixed into ``kbcore.config.Settings``."""

    # Server-side resend guard: a transcript chunk (kind='session', uri 'session:<sid>')
    # whose (uri, content_hash) already exists under ANOTHER project is refused as a
    # duplicate — the resend of a resumed session under a drifted project name can no
    # longer become rows. Kill switch: false restores the pre-020 behaviour, no deploy.
    kb_ingest_resend_guard: bool = True
    # Same-principal probe: the same chunk hash arriving under a DIFFERENT session uri
    # from the same submitter (a /branch or --fork-session copy of the transcript) is
    # refused too. Same-principal only — project-agnostic would refuse fixture reuse.
    kb_ingest_xsession_guard: bool = True
    # A box with rows in the last 30 days but none in this many hours is named in the
    # digest as FLEET SILENT (evidence-of-work, no heartbeat endpoint).
    kb_fleet_silent_hours: int = 72
    # A stamped session whose latest chunk ends below its transcript size for longer
    # than this is an OPEN TAIL (the flush stopped short and nothing resumed it).
    kb_fleet_tail_hours: int = 24
    # When true, rows without flush stamps count as a stale-hook signal. Stays false
    # until every box runs a stamping hook (rollout, proposal §5 step 5).
    kb_fleet_require_stamps: bool = False
    # Digest alert threshold: refused resend rows per night above which the digest shouts
    # (a box on the old hook resending from byte 0 costs wire, never rows — but say so).
    kb_resend_alert_per_night: int = 50
    # Median MB the nightly extracts per night (measured 2026-09: 6-86, median ~28); the
    # repair prints "GPU-nights reclaimed" with it. Reporting only.
    kb_repair_mb_per_gpu_night: int = 28
