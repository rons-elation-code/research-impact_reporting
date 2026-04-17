"""AC12, AC15, AC15a, AC15b: atomic + symlink-safe archive writes."""
import os
from pathlib import Path

import pytest

from lavandula.nonprofits import archive


def test_write_creates_file_mode_0o600(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = archive.ensure_archive_dir(raw)
    final = raw / "530196605.html"
    archive.write_file(final, b"<html/>", tmpdir=tmpdir)
    assert final.exists()
    assert final.read_bytes() == b"<html/>"
    mode = final.stat().st_mode & 0o777
    assert mode == 0o600


def test_symlink_refused(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = archive.ensure_archive_dir(raw)
    victim = tmp_path / "sensitive.txt"
    victim.write_text("DO NOT MODIFY")
    final = raw / "530196605.html"
    final.symlink_to(victim)
    with pytest.raises(archive.SymlinkRefused):
        archive.write_file(final, b"<html/>", tmpdir=tmpdir)
    # Victim untouched.
    assert victim.read_text() == "DO NOT MODIFY"


def test_atomic_overwrite(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = archive.ensure_archive_dir(raw)
    final = raw / "530196605.html"
    archive.write_file(final, b"<one/>", tmpdir=tmpdir)
    archive.write_file(final, b"<two/>", tmpdir=tmpdir)
    assert final.read_bytes() == b"<two/>"
    # No .tmp garbage left behind.
    stray = list(tmpdir.iterdir())
    assert stray == []


def test_sweep_stale_tmpdirs(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    fake_stale = raw / ".tmp-99999-aaaaaaaa"
    fake_stale.mkdir()
    (fake_stale / "orphan.html.tmp").write_text("leftover")
    removed = archive.sweep_stale_tmpdirs(raw)
    assert removed == 1
    assert not fake_stale.exists()


def test_tmpdir_isolated_per_pid(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    td = archive.ensure_archive_dir(raw)
    assert str(os.getpid()) in td.name
    # Mode 0o700
    assert (td.stat().st_mode & 0o777) == 0o700
