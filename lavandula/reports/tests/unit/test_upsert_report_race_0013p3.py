"""Races two `_upsert_report_pg_inner` calls against the same
content_sha256 using an in-memory fake Postgres (Spec 0013 P3).

Codex round-3 finding: the crawler and classify_null each run
independent `RDSDBWriter`s, so both can SELECT (miss), INSERT, and
hit a UniqueViolation on `content_sha256`. The retry loop must
recover by re-running the read-merge path and landing in UPDATE.
"""
from __future__ import annotations

import threading

import pytest

from lavandula.reports import db_writer


class _FakeUniqueViolation(Exception):
    """Duck-typed psycopg2.errors.UniqueViolation (SQLSTATE 23505)."""
    pgcode = "23505"


class _FakePgState:
    """Shared Postgres-side state guarded by a lock. Simulates:
      - SELECT ... WHERE content_sha256 = %s  → fetchone / None
      - INSERT INTO ... (content_sha256, …)   → unique violation if exists
      - UPDATE ... WHERE content_sha256 = %s  → update in place
      - SAVEPOINT / RELEASE / ROLLBACK        → no-ops (single-txn fake)
    """
    def __init__(self):
        self.rows: dict[str, list] = {}  # sha -> 25-tuple (existing shape)
        self.lock = threading.Lock()
        self.insert_attempts = 0
        self.update_attempts = 0


class _FakeCursor:
    def __init__(self, state: _FakePgState, *, stall_before_insert: threading.Event | None = None):
        self._state = state
        self._fetched: list | None = None
        self._stall = stall_before_insert

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SAVEPOINT") or s.startswith("RELEASE") or s.startswith("ROLLBACK"):
            return
        if s.startswith("SELECT source_url_redacted"):
            sha = params[0]
            with self._state.lock:
                self._fetched = list(self._state.rows.get(sha, []))
                if not self._fetched:
                    self._fetched = None
            return
        if s.startswith("INSERT INTO lava_impact.reports"):
            if self._stall is not None:
                # Let the racing thread overtake before we commit.
                self._stall.wait(timeout=2.0)
            sha = params[0]
            with self._state.lock:
                self._state.insert_attempts += 1
                if sha in self._state.rows:
                    raise _FakeUniqueViolation("dup key: content_sha256")
                # Store the 25-col projection the SELECT path reads back.
                # Columns we mirror: source_url_redacted, referring,
                # chain_json, ein, discovered, platform, attr, size,
                # page_count, first_page_text, creator, producer,
                # creation_date, js, launch, embedded, uri,
                # classification, confidence, model, version,
                # classified_at, report_year, report_year_source,
                # extractor_version.
                self._state.rows[sha] = [
                    params[1], params[2], params[3], params[4], params[5],
                    params[6], params[7], params[10], params[11], params[12],
                    params[13], params[14], params[15], params[16], params[17],
                    params[18], params[19], params[20], params[21], params[22],
                    params[23], params[24], params[25], params[26], params[27],
                ]
            return
        if s.startswith("UPDATE lava_impact.reports"):
            with self._state.lock:
                self._state.update_attempts += 1
                sha = params[-1]
                # Just record the update; we don't need to fully
                # reapply all columns for the race assertion.
                if sha in self._state.rows:
                    pass
            return
        raise AssertionError(f"unexpected SQL: {s[:80]}")

    def fetchone(self):
        if self._fetched is None:
            return None
        return tuple(self._fetched)


class _FakePgConn:
    def __init__(self, state, *, stall_before_insert=None):
        self._state = state
        self._stall = stall_before_insert

    def cursor(self):
        return _FakeCursor(self._state, stall_before_insert=self._stall)


def _call_upsert(pg_conn, sha: str, *, attribution="own_domain", size=100):
    db_writer._upsert_report_pg_inner(
        pg_conn,
        content_sha256=sha,
        source_url_redacted="https://ex.org/doc.pdf",
        referring_page_url_redacted=None,
        chain_json=None,
        source_org_ein="12-3456789",
        discovered_via="homepage-link",
        hosting_platform="own-domain",
        attribution_confidence=attribution,
        archived_at="2026-04-22T00:00:00+00:00",
        content_type="application/pdf",
        file_size_bytes=size,
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
        classified_at=None,
        report_year=2024,
        report_year_source="filename",
        extractor_version=1,
    )


def test_concurrent_insert_race_recovers_via_update():
    """Two threads upsert the same sha concurrently. The loser of the
    INSERT race gets UniqueViolation, the retry loop re-SELECTs, and
    the second attempt lands in UPDATE — no exception propagates out."""
    state = _FakePgState()
    gate = threading.Event()

    # First writer stalls inside its INSERT so the second writer can
    # slip in its own SELECT-miss → INSERT-success.
    conn_a = _FakePgConn(state, stall_before_insert=gate)
    conn_b = _FakePgConn(state)

    errs: list[BaseException] = []

    def run_a():
        try:
            _call_upsert(conn_a, "a" * 64)
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)

    def run_b():
        # Let the second caller race past the first.
        try:
            _call_upsert(conn_b, "a" * 64)
        except BaseException as exc:  # noqa: BLE001
            errs.append(exc)
        finally:
            # Release A so its stalled INSERT proceeds (and hits the
            # unique violation, triggering the retry loop).
            gate.set()

    t_a = threading.Thread(target=run_a)
    t_b = threading.Thread(target=run_b)
    t_a.start()
    # Ensure A is parked inside the stalling INSERT before B starts.
    import time; time.sleep(0.05)
    t_b.start()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)

    assert errs == [], f"unexpected exceptions: {errs}"
    # Exactly one row survived.
    assert len(state.rows) == 1
    # B inserted; A retried and landed in UPDATE (or vice versa).
    assert state.insert_attempts >= 2
    assert state.update_attempts >= 1


def test_retry_loop_bounded_by_max_attempts():
    """If the racing writer keeps re-inserting between our SELECT and
    INSERT, the retry loop must give up instead of looping forever."""
    state = _FakePgState()

    class _AlwaysMissingSelectCursor(_FakeCursor):
        """A cursor whose SELECT always reports 'no row' — this pushes
        _upsert_report_pg_inner down the INSERT branch every attempt,
        and the INSERT then always raises UniqueViolation because we
        pre-seed the row."""
        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            if s.startswith("SELECT source_url_redacted"):
                self._fetched = None  # always miss
                return
            super().execute(sql, params)

    class _PathologicalConn:
        def __init__(self, state_):
            self._state = state_

        def cursor(self):
            return _AlwaysMissingSelectCursor(self._state)

    state.rows["a" * 64] = [None] * 25  # pre-existing row to force dup
    conn = _PathologicalConn(state)

    with pytest.raises(_FakeUniqueViolation):
        _call_upsert(conn, "a" * 64)

    # Retry bound: attempted 2x insert, gave up on the second dup.
    assert state.insert_attempts == db_writer._UPSERT_REPORT_MAX_ATTEMPTS
