"""AC22: single-instance flock."""
import os

from lavandula.nonprofits.crawler import _acquire_lock


def test_flock_second_attempt_fails(tmp_path):
    lock = tmp_path / ".crawler.lock"
    fd1 = _acquire_lock(lock)
    assert fd1 is not None
    fd2 = _acquire_lock(lock)
    assert fd2 is None
    os.close(fd1)
    # Now the lock is released and a second attempt can acquire.
    fd3 = _acquire_lock(lock)
    assert fd3 is not None
    os.close(fd3)
