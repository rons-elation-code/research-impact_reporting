"""Unit tests for lavandula.common.db (Spec 0013 Phase 1).

Covers AC5-AC9 without touching real RDS. The tests exercise the
IAM token manager directly and inspect the SQLAlchemy engine's event
machinery; they do not actually connect to Postgres.

A live-RDS smoke test lives at the bottom, gated behind
LAVANDULA_LIVE_RDS=1 — run it manually after deployment.
"""
from __future__ import annotations

import os
import threading
from unittest.mock import MagicMock

import pytest

from lavandula.common.db import (
    IAMTokenManager,
    make_app_engine,
    make_engine,
    make_ro_engine,
)


# ------------------------- IAMTokenManager ---------------------------


def _make_mgr(clock, tokens=("tok-1", "tok-2", "tok-3")):
    rds = MagicMock()
    rds.generate_db_auth_token.side_effect = list(tokens)
    mgr = IAMTokenManager(
        region="us-east-1",
        host="db.example",
        port=5432,
        user="app_user1",
        rds_client=rds,
        clock=clock,
    )
    return mgr, rds


def test_token_cached_within_ttl():
    """Successive calls within 8 minutes reuse the same token."""
    now = [1000.0]
    mgr, rds = _make_mgr(clock=lambda: now[0])

    t1 = mgr.token()
    now[0] += 60  # 1 min later
    t2 = mgr.token()
    now[0] += 400  # ~7 min 40 s after the first fetch, still < 8 min
    t3 = mgr.token()

    assert t1 == t2 == t3 == "tok-1"
    assert rds.generate_db_auth_token.call_count == 1


def test_token_refreshes_after_8_minutes():
    """AC6: after 8 min of simulated time, a new token is fetched."""
    now = [1000.0]
    mgr, rds = _make_mgr(clock=lambda: now[0])

    assert mgr.token() == "tok-1"
    now[0] += 8 * 60 + 1  # past the refresh threshold
    assert mgr.token() == "tok-2"
    assert rds.generate_db_auth_token.call_count == 2


def test_token_generate_call_arguments():
    """Generated tokens target the right host/port/user/region."""
    now = [0.0]
    mgr, rds = _make_mgr(clock=lambda: now[0])
    mgr.token()
    rds.generate_db_auth_token.assert_called_once_with(
        DBHostname="db.example",
        Port=5432,
        DBUsername="app_user1",
        Region="us-east-1",
    )


def test_token_manager_is_thread_safe():
    """Concurrent calls serialise through the lock; one token fetched."""
    now = [1000.0]
    mgr, rds = _make_mgr(clock=lambda: now[0])

    # Make the underlying call slow to force thread contention.
    original = rds.generate_db_auth_token.side_effect

    def slow(*a, **kw):
        import time as _t
        _t.sleep(0.01)
        if callable(original):
            return original(*a, **kw)
        # side_effect is a list iterator — take next value
        return next(iter(original))

    rds.generate_db_auth_token.side_effect = lambda **kw: "tok-shared"

    results = []

    def worker():
        results.append(mgr.token())

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r == "tok-shared" for r in results)
    assert rds.generate_db_auth_token.call_count == 1


# --------------------------- make_engine -----------------------------


def test_engine_url_has_no_password_and_requires_ssl():
    """AC8: no password in the URL; TLS is mandatory."""
    mgr = MagicMock(spec=IAMTokenManager)
    mgr.token.return_value = "tok"

    engine = make_engine(
        host="db.example",
        port=5432,
        database="lava_prod1",
        user="app_user1",
        token_manager=mgr,
    )
    url = str(engine.url)
    assert "app_user1@db.example:5432/lava_prod1" in url
    # SQLAlchemy masks passwords; assert the render-with-password is empty
    assert engine.url.password is None
    assert engine.url.query.get("sslmode") == "require"


def test_engine_pool_config():
    """pool_pre_ping enabled, pool_recycle ≥ 8 min (AC7 + trap #3)."""
    mgr = MagicMock(spec=IAMTokenManager)
    engine = make_engine(
        host="h", port=5432, database="d", user="u", token_manager=mgr,
    )
    assert engine.pool._pre_ping is True
    assert engine.pool._recycle >= 8 * 60


def test_do_connect_injects_token_as_password():
    """AC8: the do_connect event populates cparams['password']."""
    mgr = MagicMock(spec=IAMTokenManager)
    mgr.token.return_value = "fresh-token-abc"

    engine = make_engine(
        host="h", port=5432, database="d", user="u", token_manager=mgr,
    )

    # Dispatch do_connect directly with a mutable cparams dict; the
    # registered listener should mutate it in place.
    cparams: dict = {}
    engine.dialect.dispatch.do_connect(engine.dialect, None, (), cparams)
    assert cparams["password"] == "fresh-token-abc"
    mgr.token.assert_called()


@pytest.mark.parametrize(
    "bad",
    [
        'lava"; DROP SCHEMA public; --',
        "lava_corpus; SELECT 1",
        "Lava_Impact",           # uppercase not allowed
        "1lava",                 # leading digit
        "lava-impact",           # hyphen
        "a" * 65,                # too long
        "",                      # empty
        "lava impact",           # space
    ],
)
def test_make_engine_rejects_malformed_schema(bad):
    """Schema is interpolated into SET search_path; reject non-identifiers."""
    mgr = MagicMock(spec=IAMTokenManager)
    with pytest.raises(ValueError):
        make_engine(
            host="h", port=5432, database="d", user="u",
            schema=bad, token_manager=mgr,
        )


def test_make_engine_accepts_valid_schema():
    mgr = MagicMock(spec=IAMTokenManager)
    engine = make_engine(
        host="h", port=5432, database="d", user="u",
        schema="lava_corpus", token_manager=mgr,
    )
    assert engine is not None


def test_ssm_based_factory_wiring(monkeypatch):
    """AC5: make_app_engine wires SSM values into make_engine.

    We monkeypatch get_secret and make_engine to verify the call path
    without actually building an engine (no psycopg2 connection, no
    boto3 rds client).
    """
    from lavandula.common import db as dbmod
    from lavandula.common import secrets as secretsmod

    values = {
        "rds-endpoint": "db.prod.example",
        "rds-port": "5432",
        "rds-database": "lava_prod1",
        "rds-schema": "lava_corpus",
        "rds-app-user": "app_user1",
        "rds-ro-user": "ro_user1",
    }
    secretsmod.clear_cache()
    monkeypatch.setattr(
        secretsmod, "get_secret", lambda name, **kw: values[name],
    )

    captured = {}

    def fake_make_engine(**kw):
        captured.update(kw)
        return "ENGINE"

    monkeypatch.setattr(dbmod, "make_engine", fake_make_engine)

    assert dbmod.make_app_engine() == "ENGINE"
    assert captured["user"] == "app_user1"
    assert captured["host"] == "db.prod.example"
    assert captured["port"] == 5432
    assert captured["database"] == "lava_prod1"
    assert captured["schema"] == "lava_corpus"
    assert captured["region"] == "us-east-1"

    captured.clear()
    assert dbmod.make_ro_engine() == "ENGINE"
    assert captured["user"] == "ro_user1"


def test_ro_engine_uses_ro_user(monkeypatch):
    """AC9 (wiring portion): make_ro_engine reads rds-ro-user from SSM.

    The database-level negative test (INSERT denied) is covered by the
    live-RDS smoke test below; the unit layer only verifies that the
    ro factory targets the ro-user SSM key.
    """
    from lavandula.common import db as dbmod
    from lavandula.common import secrets as secretsmod

    asked = []
    monkeypatch.setattr(
        secretsmod,
        "get_secret",
        lambda name, **kw: (asked.append(name) or {
            "rds-endpoint": "h",
            "rds-port": "5432",
            "rds-database": "d",
            "rds-schema": "s",
            "rds-ro-user": "ro_user1",
            "rds-app-user": "app_user1",
        }[name]),
    )
    monkeypatch.setattr(dbmod, "make_engine", lambda **kw: kw)

    kw = dbmod.make_ro_engine()
    assert kw["user"] == "ro_user1"
    assert "rds-ro-user" in asked
    assert "rds-app-user" not in asked


# ----------------------- live-RDS smoke tests ------------------------


@pytest.mark.skipif(
    os.environ.get("LAVANDULA_LIVE_RDS") != "1",
    reason="set LAVANDULA_LIVE_RDS=1 to run live-RDS integration test",
)
def test_live_rds_app_engine_select_1():
    """AC5: real make_app_engine() connects and runs SELECT 1.

    Requires the EC2 IAM role to have rds-db:connect for app_user1 and
    the SSM parameters to be populated. Run manually after deployment.
    """
    from sqlalchemy import text

    engine = make_app_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar()
    assert result == 1


@pytest.mark.skipif(
    os.environ.get("LAVANDULA_LIVE_RDS") != "1",
    reason="set LAVANDULA_LIVE_RDS=1 to run live-RDS integration test",
)
def test_live_rds_ro_engine_cannot_insert():
    """AC9: make_ro_engine() is denied INSERT (DB-level check)."""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    engine = make_ro_engine()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
        with pytest.raises(DBAPIError):
            conn.execute(
                text(
                    "INSERT INTO lava_corpus.schema_version (version, note) "
                    "VALUES (-1, 'ro-user insert probe')"
                )
            )
