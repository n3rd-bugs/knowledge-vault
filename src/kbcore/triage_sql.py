"""kbcore.triage_sql — the ONE spelling of the triage JSON-path SQL (KB 21332).

Review #4's fresh-maintainer catch (Sonnet): the SQL predicates over
``review_queue.detail``'s triage object were copy-pasted across five modules
(store, apply, expire, auto-apply, count), and a drifted copy would silently
split "has a triage verdict" between the surfaces that write and read it. They
now live HERE and only here; :mod:`kbcore.review_triage` re-exports them as the
established public face (the digest/tail_steps seam pattern — this file exists
only because the 300-line ratchet left no room in review_triage itself).

CONSTANT SQL TEXT ONLY: callers pass a constant column expression
(``'rq.detail'`` / ``'detail'``) and interpolate the returned fragment into
constant SQL — every VALUE still travels as a bound parameter (CLAUDE.md:
parameterized SQL only). Byte-identity with the historic literals is pinned in
tests/test_wave_e_hygiene.py, which also fails any module that spells the
literal again.
"""

from __future__ import annotations

TRIAGE_KEY = "triage"
TRIAGE_KEY_SQL = f"'{TRIAGE_KEY}'"
#: Where auto-apply's dead-target re-queue parks the displaced verdict
#: (kbcore.review_apply_rows.invalidate_dead_dup).
TRIAGE_PRIOR_KEY = "triage_prior"
TRIAGE_PRIOR_KEY_SQL = f"'{TRIAGE_PRIOR_KEY}'"


def sql_has_triage(detail: str = "rq.detail") -> str:
    """Null-safe "row carries a stored triage" existence predicate on ``detail``."""
    return f"COALESCE({detail}, '{{}}'::jsonb) ? {TRIAGE_KEY_SQL}"


def sql_not_triaged(detail: str = "rq.detail") -> str:
    """Negation of :func:`sql_has_triage`, spelled once (selection + count sites)."""
    return f"NOT ({sql_has_triage(detail)})"


def sql_triage_obj(detail: str = "rq.detail") -> str:
    """The stored triage OBJECT (jsonb ``->``), e.g. for moving it aside whole."""
    return f"{detail}->{TRIAGE_KEY_SQL}"


def sql_triage_field(field: str, detail: str = "rq.detail") -> str:
    """One triage scalar as text (jsonb ``->>``); ``field`` is a CONSTANT name."""
    return f"{sql_triage_obj(detail)}->>'{field}'"


def sql_triage_prior_field(field: str, detail: str = "rq.detail") -> str:
    """One displaced-verdict scalar as text; ``field`` is a CONSTANT name."""
    return f"{detail}->{TRIAGE_PRIOR_KEY_SQL}->>'{field}'"
