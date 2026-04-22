"""Verify that `LAVANDULA_DUAL_WRITE` gates the real RDS construction
path in `crawler.run()` (Spec 0013 Phase 3).

AC1: flag off → `make_app_engine` is NOT called and no `RDSDBWriter`
     is constructed.
AC2: flag on  → `make_app_engine` IS called, `RDSDBWriter` is started
     and stopped exactly once.

These tests stub out the heavy parts of `crawler.run()` (archive
probes, flock, encryption check, TLS self-test, DB schema setup,
per-org processing) so we can exercise the actual decision and
construction code path without touching the network or filesystem.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from lavandula.reports import crawler


class _ArchiveStub:
    def startup_probe(self):
        return None


def _run_with_stubs(monkeypatch, tmp_path, *, flag_value=None,
                    engine_raises: Exception | None = None):
    """Call crawler.run() with enough stubs that it reaches the
    dual-write decision block and returns cleanly with zero orgs."""
    if flag_value is None:
        monkeypatch.delenv("LAVANDULA_DUAL_WRITE", raising=False)
    else:
        monkeypatch.setenv("LAVANDULA_DUAL_WRITE", flag_value)

    # Stub heavy operational steps.
    monkeypatch.setattr(
        crawler, "_resolve_archive",
        lambda parser, args: _ArchiveStub(),
    )
    monkeypatch.setattr(
        crawler, "acquire_flock", lambda path: 999,
    )
    monkeypatch.setattr(
        crawler, "check_encryption_at_rest",
        lambda data_dir: crawler.EncryptionCheckResult(ok=True),
    )
    monkeypatch.setattr(crawler, "tls_self_test", lambda: None)

    class _StubConn:
        def close(self):
            pass

        def execute(self, *a, **kw):  # for budget.reconcile_stale_reservations
            class _C:
                rowcount = 0
                def fetchone(self_):
                    return (0,)
            return _C()

    monkeypatch.setattr(
        crawler.schema, "ensure_db",
        lambda db_path: _StubConn(),
    )
    # No seeds → the pool loop short-circuits; writer.start()/stop() still run.
    monkeypatch.setattr(
        crawler, "fetch_seeds_from_0001",
        lambda path: [],
    )
    # Bypass budget reconcile (reports.db not real).
    monkeypatch.setattr(
        crawler.budget, "reconcile_stale_reservations",
        lambda conn: 0,
    )
    # Silence DBWriter's actual SQLite work — it'd fail on our stub conn.
    class _DBWriterStub:
        def __init__(self, *a, **kw):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def is_alive(self):
            return True
    monkeypatch.setattr(crawler, "DBWriter", _DBWriterStub)

    # Engine / RDSDBWriter stubs.
    engine_sentinel = MagicMock(name="engine")
    make_engine_calls = {"n": 0}

    def fake_make_engine():
        make_engine_calls["n"] += 1
        if engine_raises is not None:
            raise engine_raises
        return engine_sentinel

    rds_instances: list[MagicMock] = []

    class _RDSDBWriterStub:
        def __init__(self, engine, **kwargs):
            self.engine = engine
            self.started = 0
            self.stopped = 0
            rds_instances.append(self)

        def start(self):
            self.started += 1

        def stop(self, *args, **kwargs):
            self.stopped += 1

        def put(self, *a, **k):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(crawler, "RDSDBWriter", _RDSDBWriterStub)
    # crawler imports make_app_engine lazily inside run(); patch the
    # module attribute on lavandula.common.db.
    import lavandula.common.db as common_db
    monkeypatch.setattr(common_db, "make_app_engine", fake_make_engine)

    rc = crawler.run([
        "--archive", str(tmp_path / "archive"),
        "--skip-tls-self-test",
        "--skip-encryption-check",
        "--data-dir", str(tmp_path),
    ])
    return rc, make_engine_calls, rds_instances


def test_flag_off_does_not_construct_rds(monkeypatch, tmp_path):
    rc, calls, rds = _run_with_stubs(monkeypatch, tmp_path, flag_value=None)
    assert rc == 0
    assert calls["n"] == 0, "make_app_engine must NOT be called when flag off"
    assert rds == [], "RDSDBWriter must NOT be constructed when flag off"


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_flag_on_constructs_starts_and_stops_rds(monkeypatch, tmp_path, val):
    rc, calls, rds = _run_with_stubs(monkeypatch, tmp_path, flag_value=val)
    assert rc == 0
    assert calls["n"] == 1, f"make_app_engine must be called exactly once for {val!r}"
    assert len(rds) == 1
    assert rds[0].started == 1
    assert rds[0].stopped == 1


def test_flag_on_but_engine_fails_does_not_crash_run(monkeypatch, tmp_path):
    """If make_app_engine raises at startup, the crawler continues
    with SQLite only (no RDS writer) and returns 0."""
    rc, calls, rds = _run_with_stubs(
        monkeypatch, tmp_path, flag_value="1",
        engine_raises=RuntimeError("boto creds unavailable"),
    )
    assert rc == 0
    assert calls["n"] == 1
    # Engine construction raised, so no RDSDBWriter was built.
    assert rds == []
