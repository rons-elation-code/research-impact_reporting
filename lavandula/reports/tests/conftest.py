"""Shared pytest fixtures for spec 0004 tests.

These fixtures are consumed by both unit and integration tests.
Phase 0 scaffolding: most tests import symbols that do not yet exist
and therefore ImportError / fail collection. That is the point —
Phase 1-6 implementation lands these symbols module by module.
"""
from __future__ import annotations

from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir():
    return FIXTURES


@pytest.fixture
def html_fixtures():
    return FIXTURES / "html"


@pytest.fixture
def pdf_fixtures():
    return FIXTURES / "pdf"


@pytest.fixture
def sitemap_fixtures():
    return FIXTURES / "sitemap"


@pytest.fixture
def tmp_reports_db(tmp_path):
    """Materialize a fresh reports DB and yield the connection."""
    from lavandula.reports import schema
    db = tmp_path / "reports.db"
    conn = schema.ensure_db(db)
    yield conn
    conn.close()


@pytest.fixture
def tmp_archive_dir(tmp_path):
    """A scratch raw/ directory with 0o700 permissions."""
    raw = tmp_path / "raw"
    raw.mkdir(parents=True)
    raw.chmod(0o700)
    return raw
