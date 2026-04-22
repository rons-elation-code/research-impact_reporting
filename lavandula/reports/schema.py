"""Thin compatibility shim after Spec 0017.

The canonical schema source is `lavandula/migrations/rds/*.sql`. Python
no longer carries an inline SCHEMA_SQL or an `ensure_db` / `init_schema`
helper — applying migrations is an operator step, not a runtime
responsibility.

Only `insert_raw_report_for_test` remains, as a test-only utility that
speaks SQLAlchemy `text()` so it works against both in-memory SQLite
(Category B unit tests) and a real Postgres engine (Category A).
"""
from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine


def insert_raw_report_for_test(
    engine_or_conn: Any,
    *,
    content_sha256: str,
    source_org_ein: str,
    attribution_confidence: str = "own_domain",
    classification: str | None = "annual",
    classification_confidence: float | None = 0.9,
    report_year: int | None = 2024,
    archived_at: str | None = None,
    pdf_has_javascript: int = 0,
    pdf_has_launch: int = 0,
    pdf_has_embedded: int = 0,
    pdf_has_uri_actions: int = 0,
    hosting_platform: str | None = None,
    discovered_via: str = "homepage-link",
    classifier_model: str = "claude-haiku-4-5",
    file_size_bytes: int = 1024,
    schema: str | None = "lava_impact",
) -> None:
    """Test-only helper: insert a pre-shaped row into `reports`.

    Production writes go through `db_writer.upsert_report`; this helper
    exists solely to let tests drive the public view and catalogue
    queries without rebuilding the full pipeline. Accepts either an
    `Engine` (uses `.begin()`) or an open SQLAlchemy `Connection`.

    `schema` prefix defaults to `lava_impact`; pass `None` for
    unqualified access (unit-test SQLite fixtures without a schema).
    """
    if archived_at is None:
        archived_at = (
            datetime.datetime.now(datetime.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )
    table = f"{schema}.reports" if schema else "reports"
    stmt = text(
        f"INSERT INTO {table} ("
        "  content_sha256, source_url_redacted, source_org_ein, "
        "  discovered_via, hosting_platform, attribution_confidence, "
        "  archived_at, content_type, file_size_bytes, classification, "
        "  classification_confidence, classifier_model, "
        "  pdf_has_javascript, pdf_has_launch, pdf_has_embedded, "
        "  pdf_has_uri_actions, report_year"
        ") VALUES ("
        "  :sha, :url, :ein, :disc, :platform, :attr, :archived, :ct, "
        "  :size, :class, :conf, :model, :js, :launch, :embed, :uri, "
        "  :year"
        ")"
    )
    params = {
        "sha": content_sha256,
        "url": f"https://example.org/report/{content_sha256[:8]}.pdf",
        "ein": source_org_ein,
        "disc": discovered_via,
        "platform": hosting_platform,
        "attr": attribution_confidence,
        "archived": archived_at,
        "ct": "application/pdf",
        "size": file_size_bytes,
        "class": classification,
        "conf": classification_confidence,
        "model": classifier_model,
        "js": pdf_has_javascript,
        "launch": pdf_has_launch,
        "embed": pdf_has_embedded,
        "uri": pdf_has_uri_actions,
        "year": report_year,
    }

    if isinstance(engine_or_conn, Engine):
        with engine_or_conn.begin() as conn:
            conn.execute(stmt, params)
    else:
        engine_or_conn.execute(stmt, params)


def connect(*args: Any, **kwargs: Any):
    """Back-compat shim (Spec 0017 AC6).

    Returns `make_app_engine()`. The pre-0017 SQLite `db_path` argument
    is accepted and ignored with a `DeprecationWarning`, so any leftover
    callsite keeps working while it's cleaned up in a later sweep.
    """
    import warnings
    if args or kwargs:
        warnings.warn(
            "lavandula.reports.schema.connect() ignores its arguments "
            "after Spec 0017; returning the production engine from "
            "lavandula.common.db.make_app_engine() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    from lavandula.common.db import make_app_engine
    return make_app_engine()


__all__ = ["insert_raw_report_for_test", "connect"]
