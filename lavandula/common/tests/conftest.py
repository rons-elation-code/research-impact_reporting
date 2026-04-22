"""Shared fixtures for Category A tests (real Postgres via testing.postgresql).

Tests that need to exercise Postgres-specific features — `ON CONFLICT`,
`pg_advisory_xact_lock`, `RETURNING`, `BIGSERIAL`, the
`attribution_rank()` helper, or concurrent write semantics — request
the `postgres_engine` fixture. In environments without
`testing.postgresql` installed the fixture auto-skips those tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest

try:
    import testing.postgresql as _testing_postgresql  # noqa: F401
    _HAVE_TESTING_PG = True
except Exception:  # noqa: BLE001
    _HAVE_TESTING_PG = False


_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2] / "migrations" / "rds"
)


def _apply_migrations(engine) -> None:
    """Apply lavandula/migrations/rds/*.sql against `engine`.

    The production migrations assume the `lava_impact` schema and the
    `app_user1` / `ro_user1` roles already exist (created outside of
    migrations on the real RDS instance). For a fresh testing.postgresql
    cluster we stand those up first.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS lava_impact"))
        for role in ("app_user1", "ro_user1"):
            conn.execute(text(
                f"DO $$BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') "
                f"THEN CREATE ROLE {role}; "
                f"END IF; END$$"
            ))
    migrations = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    for m in migrations:
        sql = m.read_text()
        with engine.begin() as conn:
            conn.execute(text(sql))


@pytest.fixture
def postgres_engine():
    """Spawn a throwaway Postgres, apply migrations, yield an Engine."""
    if not _HAVE_TESTING_PG:
        pytest.skip("testing.postgresql not installed")
    import testing.postgresql
    from sqlalchemy import create_engine
    with testing.postgresql.Postgresql() as pg:
        engine = create_engine(pg.url(), future=True)
        try:
            _apply_migrations(engine)
            yield engine
        finally:
            engine.dispose()
