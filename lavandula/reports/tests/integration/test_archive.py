"""AC9 — symlink-safe atomic archive write; AC10 — dedup via content_sha256."""
from __future__ import annotations

import hashlib
import os

import pytest


def test_ac9_atomic_write_rejects_symlink_at_target(tmp_archive_dir):
    from lavandula.reports.archive import write_pdf
    payload = b"%PDF-1.7\n" + b"x" * 1000
    sha = hashlib.sha256(payload).hexdigest()
    # Pre-plant a symlink AT the target location.
    target = tmp_archive_dir / f"{sha}.pdf"
    (tmp_archive_dir / "evil.txt").write_bytes(b"owned")
    os.symlink(str(tmp_archive_dir / "evil.txt"), str(target))
    from lavandula.reports.archive import ArchiveSecurityError
    with pytest.raises((OSError, ArchiveSecurityError)):
        write_pdf(payload, sha, archive_dir=tmp_archive_dir)


def test_ac9_happy_path_writes_0o600(tmp_archive_dir):
    from lavandula.reports.archive import write_pdf
    payload = b"%PDF-1.7\n" + b"x" * 1000
    sha = hashlib.sha256(payload).hexdigest()
    path = write_pdf(payload, sha, archive_dir=tmp_archive_dir)
    assert path.exists()
    assert path.read_bytes() == payload
    assert (os.stat(path).st_mode & 0o777) == 0o600


def test_ac10_sha_dedup_single_archive_row(tmp_archive_dir):
    """Writing the same bytes twice returns the same path without rewrite."""
    from lavandula.reports.archive import write_pdf
    payload = b"%PDF-1.7\n" + b"y" * 2048
    sha = hashlib.sha256(payload).hexdigest()
    p1 = write_pdf(payload, sha, archive_dir=tmp_archive_dir)
    mtime1 = os.stat(p1).st_mtime_ns
    p2 = write_pdf(payload, sha, archive_dir=tmp_archive_dir)
    assert p1 == p2
    # Second call must be a no-op — no rewrite.
    assert os.stat(p2).st_mtime_ns == mtime1
