"""Symlink-safe, atomic archive writer (AC9) + SHA dedup (AC10).

`write_pdf(bytes, sha, archive_dir)` writes to `raw/{sha}.pdf` via:
  1. Open a per-pid tmpfile with `O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|0o600`.
  2. Write + fsync fd.
  3. `os.lstat` the target — if it exists AS a symlink, refuse with
     ArchiveSecurityError.
  4. `os.replace(tmp, target)`, then fsync the dir fd.

If the target already exists (dedup), the function returns its path
without rewriting — same bytes encountered via multiple URLs share
one archived copy (AC10).
"""
from __future__ import annotations

import os
import stat
import tempfile
import uuid
from pathlib import Path

from . import config


class ArchiveSecurityError(RuntimeError):
    """Target path is a symlink or otherwise refuses safe write."""


def ensure_archive_dir(archive_dir: Path) -> Path:
    """Create `archive_dir` with mode 0o700 and return its real path."""
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(archive_dir, config.ARCHIVE_DIR_MODE)
    except OSError:
        pass
    real = Path(os.path.realpath(archive_dir))
    if not real.exists() or not real.is_dir():
        raise ArchiveSecurityError(f"archive_dir not a directory: {archive_dir!r}")
    return real


def _target_is_symlink(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(st.st_mode)


def write_pdf(pdf_bytes: bytes, content_sha256: str, *, archive_dir: Path) -> Path:
    """Write `pdf_bytes` to `archive_dir/{sha}.pdf` atomically.

    Refuses if the target path already exists as a symlink (AC9).
    Returns the target path. If target already exists with non-symlink
    semantics (AC10 dedup), returns without rewrite.
    """
    if len(content_sha256) != 64:
        raise ValueError("content_sha256 must be 64 hex chars")
    archive_dir = ensure_archive_dir(archive_dir)
    target = archive_dir / f"{content_sha256}.pdf"

    if _target_is_symlink(target):
        raise ArchiveSecurityError(
            f"target exists as symlink (refused): {target}"
        )

    if target.exists():
        # AC10 dedup: bytes already present; do not rewrite.
        return target

    # Write to a scratch tmpfile in the same directory so os.replace is atomic.
    tmp_name = f".tmp-{os.getpid()}-{uuid.uuid4().hex}.pdf"
    tmp_path = archive_dir / tmp_name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    # O_NOFOLLOW is only defined on POSIX; it'll KeyError via getattr on Windows.
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(tmp_path), flags | nofollow, config.PDF_MODE)
    try:
        with os.fdopen(fd, "wb", closefd=True) as f:
            f.write(pdf_bytes)
            f.flush()
            os.fsync(f.fileno())

        # Re-check the target is still not a symlink before rename.
        if _target_is_symlink(target):
            raise ArchiveSecurityError(
                f"target became a symlink mid-write: {target}"
            )
        os.replace(str(tmp_path), str(target))
    except Exception:
        # Scratch cleanup on failure.
        try:
            os.unlink(str(tmp_path))
        except FileNotFoundError:
            pass
        raise

    # fsync the directory so the rename is durable.
    try:
        dir_fd = os.open(str(archive_dir), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass

    return target


class LocalArchive:
    """Filesystem-backed archive backend (spec 0007).

    Adapts the pre-0007 `write_pdf` path to the Archive Protocol used
    by the crawler. Metadata is accepted but not persisted — local mode
    is for dev/test and the reports.db is the metadata store.
    """

    scheme = "local"

    def __init__(self, archive_dir):
        self.archive_dir = Path(archive_dir)

    def put(self, sha256: str, body: bytes, metadata: dict) -> None:
        write_pdf(body, sha256, archive_dir=self.archive_dir)

    def get(self, sha256: str) -> bytes:
        path = ensure_archive_dir(self.archive_dir) / f"{sha256}.pdf"
        return path.read_bytes()

    def head(self, sha256: str) -> dict | None:
        path = self.archive_dir / f"{sha256}.pdf"
        if not path.exists():
            return None
        return {"size": path.stat().st_size}

    def startup_probe(self) -> None:
        ensure_archive_dir(self.archive_dir)


__all__ = [
    "write_pdf",
    "ensure_archive_dir",
    "ArchiveSecurityError",
    "LocalArchive",
]
