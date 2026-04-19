"""Query helpers + deletion / retention (AC22, AC22.1, AC23, AC24).

Per AC23, every consumer (teammates, Claude instances, coverage_report,
exports) queries the `reports_public` view. The base `reports` table is
accessed ONLY here + `db_writer.py` + `schema.py`. AC23 is enforced by
a grep rule in lint.sh + a unit test.

Deletion is a hard delete (AC22) — the PDF is unlinked, the row is
removed from `reports`, and the event is logged in `deletion_log`.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path

from . import config
from . import db_writer


def get_public_row(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
) -> sqlite3.Row | None:
    """Fetch a single row from `reports_public` by sha."""
    return conn.execute(
        "SELECT * FROM reports_public WHERE content_sha256 = ?",
        (content_sha256,),
    ).fetchone()


def latest_report_per_org(
    conn: sqlite3.Connection,
    *,
    ein: str,
) -> sqlite3.Row | None:
    """AC24 — deterministic latest-per-org selection.

    Orders by MAX(report_year) NULLS LAST, then archived_at,
    then classification_confidence, then content_sha256 for
    tiebreaking. Draws from `reports` (not the public view) so
    low-confidence / platform-unverified rows still participate
    when the caller explicitly asks for 'latest'.
    """
    row = conn.execute(
        """
        SELECT * FROM reports
         WHERE source_org_ein = ?
         ORDER BY (CASE WHEN report_year IS NULL THEN 1 ELSE 0 END) ASC,
                  report_year DESC,
                  archived_at DESC,
                  classification_confidence DESC,
                  content_sha256 ASC
         LIMIT 1
        """,
        (ein,),
    ).fetchone()
    return row


def _unlink_archive(archive_dir: Path, content_sha256: str) -> int:
    target = archive_dir / f"{content_sha256}.pdf"
    try:
        os.unlink(str(target))
        return 1
    except FileNotFoundError:
        return 0


def delete(
    conn: sqlite3.Connection,
    *,
    content_sha256: str,
    reason: str,
    operator: str,
    archive_dir: Path,
) -> None:
    """AC22 — hard delete: unlink PDF + delete row + log the deletion.

    Post-op: `SELECT FROM reports WHERE sha=?` returns 0 rows;
    `deletion_log` has exactly one new row with pdf_unlinked=1 if
    the file existed or 0 if it had already been removed.
    """
    pdf_unlinked = _unlink_archive(archive_dir, content_sha256)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM reports WHERE content_sha256 = ?",
            (content_sha256,),
        )
        db_writer.record_deletion(
            conn,
            content_sha256=content_sha256,
            reason=reason,
            operator=operator,
            pdf_unlinked=pdf_unlinked,
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def sweep_stale(
    conn: sqlite3.Connection,
    *,
    now_iso: str | None = None,
    retention_days: int | None = None,
    archive_dir: Path,
) -> int:
    """AC22.1 — delete rows older than `retention_days`.

    Invokes the same `delete()` path per row so every removal produces
    a deletion_log entry with reason='retention_expired'. Returns the
    count deleted.
    """
    if retention_days is None:
        retention_days = config.RETENTION_DAYS
    if now_iso is None:
        now_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )
    cutoff = datetime.datetime.fromisoformat(now_iso) - datetime.timedelta(
        days=retention_days
    )
    cutoff_iso = cutoff.isoformat()
    stale = conn.execute(
        "SELECT content_sha256 FROM reports WHERE archived_at < ?",
        (cutoff_iso,),
    ).fetchall()
    deleted = 0
    for row in stale:
        delete(
            conn,
            content_sha256=row[0],
            reason="retention_expired",
            operator="retention_sweep",
            archive_dir=archive_dir,
        )
        deleted += 1
    return deleted


__all__ = [
    "get_public_row",
    "latest_report_per_org",
    "delete",
    "sweep_stale",
]
