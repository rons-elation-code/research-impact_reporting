"""Spec 0017 — assert_schema_at_least fails loud on stale / missing schema."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text


def test_missing_schema_version_table_raises_system_exit():
    from lavandula.common.db import assert_schema_at_least

    # A fresh in-memory engine with no tables mimics a DB where
    # migrations were never applied.
    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(SystemExit):
        assert_schema_at_least(engine, min_version=1)


def test_stale_version_raises(postgres_engine):
    """With only 001 applied the check should fail at min_version=3."""
    from lavandula.common.db import assert_schema_at_least

    # Migrations 001 + 002 were applied in the fixture, so v2 is the
    # current max. Demanding v3+ must hard-fail.
    with pytest.raises(SystemExit):
        assert_schema_at_least(postgres_engine, min_version=3)


def test_current_version_passes(postgres_engine):
    from lavandula.common.db import assert_schema_at_least, MIN_SCHEMA_VERSION

    # No raise.
    assert_schema_at_least(postgres_engine, min_version=MIN_SCHEMA_VERSION)


def test_schema_version_row_at_2(postgres_engine):
    with postgres_engine.connect() as conn:
        v = conn.execute(text(
            "SELECT MAX(version) FROM lava_impact.schema_version"
        )).scalar()
    assert int(v) >= 2
