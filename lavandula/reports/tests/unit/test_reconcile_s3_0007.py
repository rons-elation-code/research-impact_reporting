"""Spec 0007 AC16 — orphan reconciliation tool tests (moto-backed)."""
from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from lavandula.reports import schema
from lavandula.reports import s3_archive as s3a
from lavandula.reports.tools import reconcile_s3


BUCKET = "reconcile-test"
REGION = "us-east-1"
SHA = "b" * 64
PDF = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")


@pytest.fixture
def orphan_bucket(env, tmp_path):
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        arch.put(SHA, PDF, {
            "source-url": "https://ex.org/report.pdf",
            "ein": "123456789",
            "crawl-run-id": "runA",
            "fetched-at": "2026-04-22T16:30:05Z",
        })
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        conn.close()
        yield client, str(db)


def test_ac16_dry_run_detects_orphan(orphan_bucket, capsys):
    client, db = orphan_bucket
    rc = reconcile_s3.reconcile(
        db_path=db,
        uri=f"s3://{BUCKET}/pdfs",
        apply=False,
        client=client,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert f"ORPHAN sha={SHA}" in out
    assert "ein=123456789" in out
    assert "source=https://ex.org/report.pdf" in out

    # --dry-run must not mutate the DB.
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        rows = list(conn.execute("SELECT content_sha256 FROM reports"))
    finally:
        conn.close()
    assert rows == []


def test_ac16_apply_inserts_report_row(orphan_bucket):
    client, db = orphan_bucket
    rc = reconcile_s3.reconcile(
        db_path=db,
        uri=f"s3://{BUCKET}/pdfs",
        apply=True,
        client=client,
    )
    assert rc == 0

    import sqlite3
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT content_sha256, source_org_ein, source_url_redacted "
            "FROM reports WHERE content_sha256=?", (SHA,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == SHA
    assert row[1] == "123456789"
    assert row[2] == "https://ex.org/report.pdf"


def test_reconciler_redacts_source_url_matching_crawler(env, tmp_path):
    """Parity check (architect review round 1): reconciler-inserted rows
    must store the same `source_url_redacted` that the crawler would have
    written for the same source URL. Catches regression where reconcile
    persists the raw URL and skips the redaction pipeline."""
    sensitive = (
        "https://user:pw@ex.org/report.pdf?token=SECRET&report=annual"
    )
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        arch.put(SHA, PDF, {
            "source-url": sensitive,
            "ein": "123456789",
            "crawl-run-id": "runA",
            "fetched-at": "2026-04-22T16:30:05Z",
        })
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        conn.close()

        reconcile_s3.reconcile(
            db_path=str(db),
            uri=f"s3://{BUCKET}/pdfs",
            apply=True,
            client=client,
        )

    import sqlite3
    from lavandula.reports.url_redact import redact_url
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT source_url_redacted FROM reports WHERE content_sha256=?",
            (SHA,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    stored = row[0]
    expected = redact_url(sensitive)
    assert stored == expected
    # Belt-and-braces: the raw URL's userinfo and token value must not
    # appear in the stored row.
    assert "user:pw" not in stored
    assert "SECRET" not in stored


def test_fetch_log_attribution_beats_s3_metadata(env, tmp_path, caplog):
    """Round-3 review: content-addressed dedup means two orgs can share
    one S3 key. fetch_log is per-attempt and crawler-written, so it's
    authoritative for attribution. Verify the reconciler prefers the
    fetch_log EIN when S3 metadata disagrees, and warns about the
    mismatch."""
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        # Simulate: orgA's PUT succeeds and fetch_log records it; later
        # orgB publishes identical bytes and the latest PUT's metadata
        # carries orgB's EIN. The reconciler should pick orgA from
        # fetch_log.
        arch.put(SHA, PDF, {
            "source-url": "https://orgB.example/same-bytes.pdf",
            "ein": "222222222",
            "crawl-run-id": "runB",
            "fetched-at": "2026-04-22T16:30:05Z",
        })
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        # Seed a pdf-get fetch_log entry that references this sha
        # (same format the crawler writes on archive_put_failed, reused
        # here to seed an attribution source for the test).
        conn.execute(
            """INSERT INTO fetch_log
                 (ein, url_redacted, kind, fetch_status, fetched_at, notes)
               VALUES (?, ?, 'pdf-get', 'server_error', ?, ?)""",
            (
                "111111111",
                "https://orgA.example/report.pdf",
                "2026-04-22T16:29:00Z",
                f"archive_put_failed:TransientError sha={SHA}",
            ),
        )
        conn.commit()
        conn.close()

        import logging
        caplog.set_level(logging.WARNING)
        reconcile_s3.reconcile(
            db_path=str(db),
            uri=f"s3://{BUCKET}/pdfs",
            apply=True,
            client=client,
        )

    import sqlite3
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT source_org_ein, source_url_redacted "
            "FROM reports WHERE content_sha256=?", (SHA,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    # fetch_log (orgA) wins, not S3 metadata (orgB).
    assert row[0] == "111111111"
    assert row[1] == "https://orgA.example/report.pdf"
    # Mismatch was logged.
    assert any("RECONCILE_MISMATCH" in r.getMessage() for r in caplog.records)


def test_fetch_log_attribution_agreement_no_warn(env, tmp_path, caplog):
    """When fetch_log and S3 metadata agree, no mismatch warning."""
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        arch.put(SHA, PDF, {
            "source-url": "https://a.example/r.pdf",
            "ein": "111111111",
            "crawl-run-id": "r1",
            "fetched-at": "2026-04-22T16:30:05Z",
        })
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        conn.execute(
            """INSERT INTO fetch_log
                 (ein, url_redacted, kind, fetch_status, fetched_at, notes)
               VALUES (?, ?, 'pdf-get', 'server_error', ?, ?)""",
            (
                "111111111",
                "https://a.example/r.pdf",
                "2026-04-22T16:29:00Z",
                f"archive_put_failed:X sha={SHA}",
            ),
        )
        conn.commit()
        conn.close()

        import logging
        caplog.set_level(logging.WARNING)
        reconcile_s3.reconcile(
            db_path=str(db),
            uri=f"s3://{BUCKET}/pdfs",
            apply=True,
            client=client,
        )
    assert not any("RECONCILE_MISMATCH" in r.getMessage() for r in caplog.records)


def test_s3_only_fallback_warns(env, tmp_path, caplog):
    """No fetch_log entry → fall back to S3 metadata with a WARNING."""
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        arch.put(SHA, PDF, {
            "source-url": "https://orphan.example/r.pdf",
            "ein": "999999999",
            "crawl-run-id": "rX",
            "fetched-at": "2026-04-22T16:30:05Z",
        })
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        conn.close()

        import logging
        caplog.set_level(logging.WARNING)
        reconcile_s3.reconcile(
            db_path=str(db),
            uri=f"s3://{BUCKET}/pdfs",
            apply=False,
            client=client,
        )
    assert any("RECONCILE_S3_ONLY" in r.getMessage() for r in caplog.records)


def test_reconciled_row_is_visible_in_reports_public(env, tmp_path):
    """Round-4 review: reconciled orphans MUST land in reports_public
    (the view excludes platform_unverified). This exercises both the
    'metadata carries attribution' and 'metadata missing, default
    applied' paths and asserts both rows are view-visible after a
    classifier fills in the public-gating columns."""
    sha_with_md = "c" * 64
    sha_missing_md = "d" * 64
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        arch = s3a.S3Archive(BUCKET, "pdfs", region=REGION, client=client)
        # Fresh-crawl object: full metadata including attribution.
        arch.put(sha_with_md, PDF, {
            "source-url": "https://a.example/r.pdf",
            "ein": "111111111",
            "crawl-run-id": "r1",
            "fetched-at": "2026-04-22T16:30:05Z",
            "attribution-confidence": "own_domain",
            "discovered-via": "homepage-link",
        })
        # Legacy object: missing attribution/discovered fields.
        arch.put(sha_missing_md, PDF, {
            "source-url": "https://b.example/r.pdf",
            "ein": "222222222",
            "crawl-run-id": "r2",
            "fetched-at": "2026-04-22T16:30:05Z",
        })
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        conn.close()

        reconcile_s3.reconcile(
            db_path=str(db),
            uri=f"s3://{BUCKET}/pdfs",
            apply=True,
            client=client,
        )

    import sqlite3
    conn = sqlite3.connect(db)
    try:
        rows = {
            r[0]: r[1] for r in conn.execute(
                "SELECT content_sha256, attribution_confidence FROM reports"
            )
        }
        # Neither value should be platform_unverified (the view excludes it).
        assert rows[sha_with_md] == "own_domain"
        assert rows[sha_missing_md] == "platform_verified"

        # Simulate the classifier filling in the public-gating columns
        # so the view's other filters pass. This proves visibility is
        # not blocked by the attribution column.
        for sha in (sha_with_md, sha_missing_md):
            conn.execute(
                "UPDATE reports SET classification='annual', "
                "classification_confidence=0.95 WHERE content_sha256=?",
                (sha,),
            )
        conn.commit()
        visible = {
            r[0] for r in conn.execute(
                "SELECT content_sha256 FROM reports_public"
            )
        }
    finally:
        conn.close()
    assert sha_with_md in visible
    assert sha_missing_md in visible


def test_ac16_invalid_metadata_skipped(env, tmp_path, capsys):
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET)
        # Raw put: bypass S3Archive's validator so we can ship a bad EIN.
        client.put_object(
            Bucket=BUCKET,
            Key=f"pdfs/{SHA}.pdf",
            Body=PDF,
            Metadata={"ein": "bogus", "source-url": "x", "fetched-at": ""},
        )
        db = tmp_path / "reports.db"
        conn = schema.ensure_db(db)
        conn.close()

        reconcile_s3.reconcile(
            db_path=str(db),
            uri=f"s3://{BUCKET}/pdfs",
            apply=True,
            client=client,
        )
    out = capsys.readouterr().out
    assert "ORPHAN_INVALID_METADATA" in out
    # No row inserted.
    import sqlite3
    conn = sqlite3.connect(db)
    try:
        rows = list(conn.execute("SELECT content_sha256 FROM reports"))
    finally:
        conn.close()
    assert rows == []
