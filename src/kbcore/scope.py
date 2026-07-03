"""kbcore.scope — the public/cloud allow-list gate (Phase 6 safety).

A server INSTANCE is either full-access or public-only, chosen once by
``KB_PUBLIC_ONLY``. When public-only:
- READS see only items whose project is flagged ``public`` (filters), and a
  targeted read of a withheld item/project raises **NotFound** — never
  "forbidden" — so there is NO existence leak.
- WRITES to non-public projects are rejected with **InvalidOperation**.

Every kbcore read/write consults this module; no per-call flag is threaded.
The predicate below is a constant SQL literal (no user input) safe to embed.
"""

from __future__ import annotations

import psycopg

from kbcore.config import settings
from kbcore.db import connect
from kbcore.errors import InvalidOperation, NotFound

# A leading-AND-free predicate: `<alias>.project` limited to public projects.
_PUBLIC_SUBQUERY = "SELECT name FROM projects WHERE public IS TRUE"


def public_only() -> bool:
    """True if this instance may only see/write public projects."""
    return settings.kb_public_only


def project_predicate(project_column: str) -> str:
    """SQL predicate restricting a project column to public projects, or '' when
    full-access. The argument is a constant column reference chosen by kbcore."""
    if not public_only():
        return ""
    return f"{project_column} IN ({_PUBLIC_SUBQUERY})"


def project_is_public(conn: psycopg.Connection, project: str) -> bool:
    row = conn.execute("SELECT public FROM projects WHERE name = %s", (project,)).fetchone()
    return bool(row and row[0])


def assert_readable(project: str, not_found_msg: str) -> None:
    """Under public-only, a non-public project must look like it does not exist.

    Raises NotFound with the SAME message a truly-missing target would, so the
    caller cannot distinguish "withheld" from "absent" (no existence leak).
    """
    if not public_only():
        return
    with connect() as conn:
        if not project_is_public(conn, project):
            raise NotFound(not_found_msg)


def assert_writable(project: str) -> None:
    """Under public-only, reject a write to a non-public project."""
    if not public_only():
        return
    with connect() as conn:
        if not project_is_public(conn, project):
            raise InvalidOperation(
                f"project {project!r} is not public / not writable on this instance"
            )
