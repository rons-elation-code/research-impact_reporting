"""Spec 0007 AC11 — pure argparse tests for --archive / --archive-dir."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from lavandula.reports import archive as _archive
from lavandula.reports import crawler
from lavandula.reports import s3_archive as _s3a


def _build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=None)
    ap.add_argument("--archive-dir", default=None)
    ap.add_argument("--s3-region", default=None)
    return ap


def _resolve(argv):
    ap = _build_parser()
    args = ap.parse_args(argv)
    return crawler._resolve_archive(ap, args)


def test_ac11_neither_flag_errors():
    with pytest.raises(SystemExit):
        _resolve([])


def test_ac11_both_flags_error():
    with pytest.raises(SystemExit):
        _resolve(["--archive", "/tmp/a", "--archive-dir", "/tmp/b"])


def test_ac11_archive_dir_rejects_s3():
    with pytest.raises(SystemExit):
        _resolve(["--archive-dir", "s3://bucket/prefix"])


def test_ac11_archive_dir_accepts_path(tmp_path):
    arch = _resolve(["--archive-dir", str(tmp_path)])
    assert isinstance(arch, _archive.LocalArchive)


def test_ac11_archive_absolute_path(tmp_path):
    arch = _resolve(["--archive", str(tmp_path)])
    assert isinstance(arch, _archive.LocalArchive)


def test_ac11_archive_s3_uri(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    arch = _resolve(["--archive", "s3://mybucket/pdfs"])
    assert isinstance(arch, _s3a.S3Archive)
    assert arch.bucket == "mybucket"
    assert arch.prefix == "pdfs"


def test_ac11_archive_relative_path_rejected():
    with pytest.raises(SystemExit):
        _resolve(["--archive", "relative/path"])
