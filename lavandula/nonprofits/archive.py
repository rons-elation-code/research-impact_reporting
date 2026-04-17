"""Atomic + symlink-safe archive writes.

Layout:
  raw/cn/                              final files (mode 0o700 dir, 0o600 files)
  raw/cn/.tmp-{pid}-{uuid}/            per-process in-flight writes

Write sequence (per Claude HIGH-4 + Codex HIGH-2):
  1. os.open(tmp, O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW, 0o600) in tmp dir
  2. write, fsync(fd), close
  3. lstat(final_path) — if a symlink, halt
  4. os.replace(tmp, final)
  5. os.fsync(dir_fd)  # durability across power loss
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from .logging_utils import sanitize_exception


class ArchiveError(RuntimeError):
    pass


class SymlinkRefused(ArchiveError):
    """lstat found a symlink where we intended to write."""


def ensure_archive_dir(raw_cn: Path) -> Path:
    """Ensure raw/cn/ and our per-PID tmp subdir exist; return the tmp dir."""
    raw_cn.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(raw_cn, 0o700)
    except OSError:
        pass
    tmpdir = raw_cn / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(tmpdir, 0o700)
    except OSError:
        pass
    return tmpdir


def sweep_stale_tmpdirs(raw_cn: Path) -> int:
    """Remove leftover `.tmp-*` subdirs from prior crashed runs.

    Returns count of removed directories. We detect 'stale' heuristically:
    any `.tmp-*` directory NOT owned by the current PID.
    """
    removed = 0
    if not raw_cn.exists():
        return 0
    pid_marker = f".tmp-{os.getpid()}-"
    for entry in raw_cn.iterdir():
        if not entry.name.startswith(".tmp-"):
            continue
        if entry.name.startswith(pid_marker):
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError:
            pass
    return removed


def write_file(
    final_path: Path,
    body: bytes,
    *,
    tmpdir: Path,
) -> None:
    """Write bytes to `final_path` atomically and symlink-safely.

    Raises SymlinkRefused if `final_path` is already a symlink.
    Raises ArchiveError on any other I/O failure.
    """
    final_path = Path(final_path)
    tmp_name = final_path.name + ".tmp"
    tmp_path = tmpdir / tmp_name

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(str(tmp_path), flags, 0o600)
    except OSError as exc:
        raise ArchiveError(
            f"failed to open tmp {tmp_path}: {sanitize_exception(exc)}"
        ) from exc
    try:
        with os.fdopen(fd, "wb", closefd=True) as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise ArchiveError(
            f"failed to write {tmp_path}: {sanitize_exception(exc)}"
        ) from exc

    # Safety: if the final path exists and is a symlink, refuse.
    try:
        st = os.lstat(final_path)
    except FileNotFoundError:
        st = None
    if st is not None:
        import stat as _stat
        if _stat.S_ISLNK(st.st_mode):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise SymlinkRefused(
                f"refusing to write through symlink at {final_path}"
            )

    try:
        os.replace(tmp_path, final_path)
    except OSError as exc:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise ArchiveError(
            f"failed to replace {final_path}: {sanitize_exception(exc)}"
        ) from exc

    # Enforce file mode 0o600 (os.open may have been umasked).
    try:
        os.chmod(final_path, 0o600)
    except OSError:
        pass

    # Fsync the parent directory for durability.
    try:
        dir_fd = os.open(str(final_path.parent), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        dir_fd = None
    if dir_fd is not None:
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def archive_path_for_ein(raw_cn: Path, ein: str) -> Path:
    """Return raw/cn/{ein}.html — caller must pre-validate the EIN."""
    return raw_cn / f"{ein}.html"


def challenge_path_for_ein(raw_cn: Path, ein: str) -> Path:
    """Parallel path for Cloudflare challenge bodies."""
    return raw_cn / f"{ein}.challenge.html"
