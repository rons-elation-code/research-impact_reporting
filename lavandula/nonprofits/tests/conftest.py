"""Shared pytest fixtures."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    from lavandula.nonprofits.schema import ensure_db
    db = tmp_path / "nonprofits.db"
    conn = ensure_db(db)
    yield conn
    conn.close()


@pytest.fixture
def tmp_archive(tmp_path):
    from lavandula.nonprofits import archive
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = archive.ensure_archive_dir(raw)
    yield raw, tmpdir


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent.parent / "tests" / "fixtures" / "cn"
