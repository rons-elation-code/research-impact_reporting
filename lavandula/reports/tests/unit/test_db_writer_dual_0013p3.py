"""Tests that each db_writer function enqueues a parallel RDS closure
when `rds_writer` is provided (Spec 0013 Phase 3).

These tests mock the RDS writer as a recording stub and then run each
submitted closure against a fake psycopg2-like connection to assert
that the generated SQL uses %s placeholders, schema-qualifies tables
with `lava_impact.<table>`, omits `id` for auto-id tables, and uses
the expected ON CONFLICT clauses.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from lavandula.reports import db_writer, budget


# -------------------------------------------------------------------- helpers


class _RecordedCursor:
    def __init__(self, fetchone_result=None):
        self.executed: list[tuple[str, tuple]] = []
        self._fetchone_result = fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params) if params else ()))

    def fetchone(self):
        return self._fetchone_result


class _RecordedPgConn:
    def __init__(self, fetchone_result=None):
        self.cursors: list[_RecordedCursor] = []
        self._fetchone_result = fetchone_result

    def cursor(self):
        c = _RecordedCursor(self._fetchone_result)
        self.cursors.append(c)
        return c

    def all_sql(self) -> list[str]:
        return [sql for c in self.cursors for sql, _ in c.executed]

    def all_params(self) -> list[tuple]:
        return [p for c in self.cursors for _, p in c.executed]


class _RecordingRdsWriter:
    def __init__(self):
        self.ops = []

    def put(self, op):
        self.ops.append(op)


def _run_ops(rds, pg_conn):
    for op in rds.ops:
        op(pg_conn)


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def sqlite_conn(tmp_path):
    # Use the real schema module to bootstrap an empty reports.db; for
    # tests where we only care about the RDS side, we route SQLite to a
    # minimal local schema.
    from lavandula.reports import schema
    path = tmp_path / "reports.db"
    conn = schema.ensure_db(path)
    yield conn
    conn.close()


# -------------------------------------------------------------------- tests


def test_record_fetch_enqueues_postgres_closure(sqlite_conn):
    rds = _RecordingRdsWriter()
    db_writer.record_fetch(
        sqlite_conn,
        ein="12-3456789",
        url_redacted="example.com/robots.txt",
        kind="robots",
        fetch_status="ok",
        status_code=200,
        elapsed_ms=42,
        notes="n",
        rds_writer=rds,
    )
    assert len(rds.ops) == 1
    pg = _RecordedPgConn()
    _run_ops(rds, pg)
    sql = pg.all_sql()[0]
    assert "INSERT INTO lava_impact.fetch_log" in sql
    # Uses %s placeholders, not ?
    assert "%s" in sql
    assert "?" not in sql
    # Auto-id: no `id` column in the INSERT.
    assert re.search(r"\bid\b", sql.split("VALUES")[0]) is None
    params = pg.all_params()[0]
    assert params[0] == "12-3456789"
    assert params[2] == "robots"
    assert params[4] == 200


def test_upsert_crawled_org_enqueues_postgres_closure(sqlite_conn):
    rds = _RecordingRdsWriter()
    db_writer.upsert_crawled_org(
        sqlite_conn,
        ein="99-0000001",
        candidate_count=2,
        fetched_count=1,
        confirmed_report_count=0,
        rds_writer=rds,
    )
    assert len(rds.ops) == 1
    pg = _RecordedPgConn()
    _run_ops(rds, pg)
    sql = pg.all_sql()[0]
    assert "INSERT INTO lava_impact.crawled_orgs" in sql
    assert "ON CONFLICT (ein) DO UPDATE" in sql
    # SQLite-parity: crawler passes 0 on re-crawl; GREATEST prevents the
    # classify_null-backfilled count from being overwritten by 0. Raw
    # EXCLUDED assignment would cause drift.
    assert "GREATEST" in sql
    assert "confirmed_report_count = GREATEST" in sql


def test_record_deletion_omits_id_and_uses_schema(sqlite_conn):
    rds = _RecordingRdsWriter()
    db_writer.record_deletion(
        sqlite_conn,
        content_sha256="a" * 64,
        reason="test",
        operator="builder",
        pdf_unlinked=1,
        rds_writer=rds,
    )
    assert len(rds.ops) == 1
    pg = _RecordedPgConn()
    _run_ops(rds, pg)
    sql = pg.all_sql()[0]
    assert "INSERT INTO lava_impact.deletion_log" in sql
    # No `id` in col list (auto-id table)
    assert re.search(r"\bid\b", sql.split("VALUES")[0]) is None


def _call_upsert_report(sqlite_conn, rds_writer):
    db_writer.upsert_report(
        sqlite_conn,
        content_sha256="b" * 64,
        source_url_redacted="https://ex.org/doc.pdf",
        referring_page_url_redacted="https://ex.org/",
        redirect_chain_redacted=None,
        source_org_ein="12-3456789",
        discovered_via="homepage-link",
        hosting_platform="own-domain",
        attribution_confidence="own_domain",
        file_size_bytes=100,
        page_count=5,
        first_page_text="hello",
        pdf_creator=None,
        pdf_producer=None,
        pdf_creation_date=None,
        pdf_has_javascript=0,
        pdf_has_launch=0,
        pdf_has_embedded=0,
        pdf_has_uri_actions=0,
        classification=None,
        classification_confidence=None,
        classifier_model="m",
        classifier_version=1,
        report_year=2024,
        report_year_source="filename",
        extractor_version=1,
        rds_writer=rds_writer,
    )


def test_upsert_report_insert_path_targets_schema(sqlite_conn):
    rds = _RecordingRdsWriter()
    _call_upsert_report(sqlite_conn, rds)
    assert len(rds.ops) == 1
    # Simulate an empty RDS table: fetchone returns None → INSERT path.
    pg = _RecordedPgConn(fetchone_result=None)
    _run_ops(rds, pg)
    sql_joined = " \n ".join(pg.all_sql())
    assert "FROM lava_impact.reports" in sql_joined  # the SELECT
    assert "INSERT INTO lava_impact.reports" in sql_joined
    # Uses %s placeholders
    assert "%s" in sql_joined
    assert "?" not in sql_joined


def test_upsert_report_update_path_merges(sqlite_conn):
    rds = _RecordingRdsWriter()
    _call_upsert_report(sqlite_conn, rds)
    # Fake an existing row (25-column SELECT shape matches the module's
    # SELECT list). All fields null-ish except attribution_confidence
    # set to lower rank so the merge prefers the new source.
    existing = (
        "old_url", None, None, "00-0000000", "old",
        None, "platform_unverified", 50, None, None, None, None, None,
        0, 0, 0, 0, None, None, "m0", 0, None, None, None, 0,
    )
    pg = _RecordedPgConn(fetchone_result=existing)
    _run_ops(rds, pg)
    sql_joined = " \n ".join(pg.all_sql())
    assert "UPDATE lava_impact.reports" in sql_joined
    # The UPDATE is the second statement on the single cursor.
    # Its params must include 100 = max(existing 50, new 100).
    update_stmt = pg.cursors[0].executed[1]
    assert 100 in update_stmt[1]


def test_budget_check_and_reserve_enqueues_preflight(sqlite_conn):
    rds = _RecordingRdsWriter()
    from lavandula.reports import config
    reservation_id = budget.check_and_reserve(
        sqlite_conn,
        estimated_cents=10,
        classifier_model="m",
        rds_writer=rds,
    )
    assert isinstance(reservation_id, int)
    assert len(rds.ops) == 1
    pg = _RecordedPgConn()
    _run_ops(rds, pg)
    sql = pg.all_sql()[0]
    assert "INSERT INTO lava_impact.budget_ledger" in sql
    assert "'preflight'" in sql
    # Correlation: the notes field encodes the SQLite reservation_id.
    params = pg.all_params()[0]
    assert any(f"reserved:{reservation_id}" == p for p in params)


def test_budget_settle_enqueues_update(sqlite_conn):
    rds = _RecordingRdsWriter()
    reservation_id = budget.check_and_reserve(
        sqlite_conn, estimated_cents=5, classifier_model="m",
        rds_writer=rds,
    )
    # Drop the reserve op; we only care about settle's op here.
    rds.ops.clear()

    budget.settle(
        sqlite_conn,
        reservation_id=reservation_id,
        actual_input_tokens=100,
        actual_output_tokens=50,
        sha256_classified="c" * 64,
        rds_writer=rds,
    )
    assert len(rds.ops) == 1
    pg = _RecordedPgConn()
    _run_ops(rds, pg)
    sql = pg.all_sql()[0]
    assert "UPDATE lava_impact.budget_ledger" in sql
    assert "notes = 'settled'" in sql
    # WHERE clause matches the correlation key we stored on reserve.
    params = pg.all_params()[0]
    assert f"reserved:{reservation_id}" in params


def test_budget_release_enqueues_delete(sqlite_conn):
    rds = _RecordingRdsWriter()
    reservation_id = budget.check_and_reserve(
        sqlite_conn, estimated_cents=5, classifier_model="m",
        rds_writer=rds,
    )
    rds.ops.clear()

    budget.release(sqlite_conn, reservation_id=reservation_id, rds_writer=rds)
    assert len(rds.ops) == 1
    pg = _RecordedPgConn()
    _run_ops(rds, pg)
    sql = pg.all_sql()[0]
    assert "DELETE FROM lava_impact.budget_ledger" in sql


# -------------------------------------------------------------- byte-identity


def test_no_rds_writer_means_no_rds_work(sqlite_conn):
    """Without rds_writer kwarg, behavior must be byte-identical
    to pre-0013: no RDS closures enqueued (obviously) and no import
    of lavandula.common.db triggered."""
    # A simple smoke: each call should succeed with rds_writer=None and
    # not touch the RDS world.
    db_writer.record_fetch(
        sqlite_conn,
        ein="12-3456789",
        url_redacted="u",
        kind="robots",
        fetch_status="ok",
    )
    db_writer.record_deletion(
        sqlite_conn,
        content_sha256="a" * 64,
        reason=None, operator=None, pdf_unlinked=0,
    )
    db_writer.upsert_crawled_org(
        sqlite_conn, ein="99-0000001",
        candidate_count=1, fetched_count=0, confirmed_report_count=0,
    )
    # If we reach here without import errors on boto3/psycopg2, pass.
