"""Query helpers + deletion / retention (AC22, AC22.1, AC23, AC24).

Per AC23, consumers query the `lava_corpus.corpus_public` view. The
base `corpus` table is accessed only here + `db_writer.py`. Deletion
is a hard delete (AC22) — the PDF is unlinked, the row is removed
from `corpus`, and the event is logged in `deletion_log`.
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import config
from . import db_writer

_SCHEMA = "lava_corpus"


def get_public_row(engine: Engine, *, content_sha256: str) -> Any | None:
    """Fetch a single row from `corpus_public` by sha."""
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT * FROM {_SCHEMA}.corpus_public "
                 "WHERE content_sha256 = :sha"),
            {"sha": content_sha256},
        ).mappings().first()


def latest_report_per_org(engine: Engine, *, ein: str) -> Any | None:
    """AC24 — deterministic latest-per-org selection."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                f"SELECT * FROM {_SCHEMA}.corpus "
                " WHERE source_org_ein = :ein "
                " ORDER BY (CASE WHEN report_year IS NULL THEN 1 ELSE 0 END) ASC, "
                "          report_year DESC, "
                "          archived_at DESC, "
                "          classification_confidence DESC NULLS LAST, "
                "          content_sha256 ASC "
                " LIMIT 1"
            ),
            {"ein": ein},
        ).mappings().first()


def _unlink_archive(archive_dir: Path, content_sha256: str) -> int:
    target = archive_dir / f"{content_sha256}.pdf"
    try:
        os.unlink(str(target))
        return 1
    except FileNotFoundError:
        return 0


def delete(
    engine: Engine,
    *,
    content_sha256: str,
    reason: str,
    operator: str,
    archive_dir: Path,
) -> None:
    """AC22 — hard delete: unlink PDF + delete row + log the deletion."""
    pdf_unlinked = _unlink_archive(archive_dir, content_sha256)
    with engine.begin() as conn:
        conn.execute(
            text(f"DELETE FROM {_SCHEMA}.corpus "
                 "WHERE content_sha256 = :sha"),
            {"sha": content_sha256},
        )
        conn.execute(
            text(
                f"INSERT INTO {_SCHEMA}.deletion_log "
                "(content_sha256, deleted_at, reason, operator, pdf_unlinked) "
                "VALUES (:sha, :ts, :reason, :operator, :unlinked)"
            ),
            {
                "sha": content_sha256,
                "ts": datetime.datetime.now(datetime.timezone.utc)
                    .replace(microsecond=0).isoformat(),
                "reason": reason,
                "operator": operator,
                "unlinked": pdf_unlinked,
            },
        )


def sweep_stale(
    engine: Engine,
    *,
    now_iso: str | None = None,
    retention_days: int | None = None,
    archive_dir: Path,
) -> int:
    """AC22.1 — delete rows older than `retention_days`."""
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
    with engine.connect() as conn:
        stale = conn.execute(
            text(f"SELECT content_sha256 FROM {_SCHEMA}.corpus "
                 "WHERE archived_at < :cutoff"),
            {"cutoff": cutoff_iso},
        ).fetchall()
    deleted = 0
    for row in stale:
        delete(
            engine,
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
