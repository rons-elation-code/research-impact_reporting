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
