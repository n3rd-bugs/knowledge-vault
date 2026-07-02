"""kbcore.migrate — apply migrations/*.sql in order, tracked in schema_migrations.

Migrations are applied in filename order; each is recorded in the
``schema_migrations`` registry so re-runs are no-ops. Migrations after 001 must
be additive only (PLAN.md §4).

Run:  ``python -m kbcore.migrate``  (or ``make migrate``).
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from kbcore.db import connect

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_ENSURE_REGISTRY = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {r[0] for r in rows}


def migrate() -> list[str]:
    """Apply any pending migrations. Returns the list of versions applied this run."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied_now: list[str] = []
    conn = connect()
    # Autocommit keeps plain executes (registry DDL, SELECT) out of psycopg's
    # implicit transaction; otherwise the transaction() blocks below would be
    # savepoints inside it and conn.close() would roll everything back.
    conn.autocommit = True
    try:
        conn.execute(_ENSURE_REGISTRY)
        done = _applied_versions(conn)
        for path in files:
            version = path.stem
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)  # multi-statement script, no parameters
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            applied_now.append(version)
            print(f"applied {version}")
    finally:
        conn.close()
    if not applied_now:
        print("no pending migrations")
    return applied_now


if __name__ == "__main__":
    migrate()
