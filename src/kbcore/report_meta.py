"""kbcore.report_meta — what a NON-OWNER reads of a nightly run report's meta.

THE GAP (E2 fix-round review H1): the nightly consolidation report (project
kb-system, type 'report', ``meta.consolidation``; ``kbworker.run_report``)
stores the run's WHOLE counts dict as meta — per-case eval telemetry keyed by
golden query text, the ids / types / trust of items in projects the reader may
not see, fleet maps keyed by project and box names. ``GET /api/items/{id}`` and
``GET /api/timeline`` returned it unprojected to any principal with a
kb-system read grant (a box's ``kbg_`` token): a cross-project id oracle past
the walls :func:`kbcore.items.get_item` builds (relations, the supersede
pointer), and the payload master's c73d0b9 made owner-only on ``GET
/api/consolidate``.

THE RULE: the OWNER reads everything. Anyone else reads such a report's meta
reduced to :func:`body_scalars` — the ``- key: value`` lines its BODY carries
(kbworker.run_report writes the body from the same function) plus the
``consolidation`` flag, so a grantee learns nothing the body does not say.
Robust to new keys: a nested value is withheld whatever it is named. No other
item changes.

THE BODY IS SEARCH VOCABULARY (E2 final reviews FB-1, F1): every body line is
chunked and indexed, and one report lands every night, so a key-name word or a
value a report carries becomes a keyword candidate for any query that shares
it — one RARE lexeme is enough for the OR fallback to admit an item
(kbcore.search_legs). E2's embed identity (model, configured and serving
hosts, runtime, prefix sha, device) and its floors shared lexemes with nine
golden cases (index-only census); its other scalars (run start, echo skips,
epoch close, error statuses) shared none today but would at the next golden
edit. So E2 adds NO body line: every scalar it introduced matches
:data:`BODY_EXCLUDED_PREFIXES` and lives in meta only — read it as the owner
(``GET /api/items/{id}``, SQL) or in the journald "consolidation done" line.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from kbcore import scope

REPORT_PROJECT = "kb-system"  # where kbworker.run_report files the nightly report
# Scalar keys kept out of the indexed body (module docstring) — every scalar E2
# added: the embed identity (eval_embed_*), the floors (eval_floor_hit_at_5 /
# eval_floor_mrr / eval_floors_at_defaults), the echo-skip count, the epoch-close
# keys, the two error statuses and the run's start stamp. No earlier key matches.
BODY_EXCLUDED_PREFIXES = (
    "eval_embed_",
    "eval_floor",
    "eval_echo_",
    "eval_regression_epoch_",
    "eval_persistence_",
    "eval_replay_",
    "run_started_at",
)


def body_scalars(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The entries a nightly report's indexed BODY carries: the scalars, minus
    :data:`BODY_EXCLUDED_PREFIXES`. Nested values (per-case telemetry keyed by
    golden query text, id lists, fleet maps) live in meta only."""
    return {
        k: v
        for k, v in meta.items()
        if not isinstance(v, dict | list | tuple) and not k.startswith(BODY_EXCLUDED_PREFIXES)
    }


def visible_meta(meta: Any, project: str, item_type: str) -> dict[str, Any]:
    """``meta`` (a dict, else ``{}``) as the CURRENT principal may read it."""
    meta = meta if isinstance(meta, dict) else {}
    if (
        scope.current().is_owner
        or project != REPORT_PROJECT
        or item_type != "report"
        or meta.get("consolidation") is not True
    ):
        return meta
    return body_scalars(meta)
