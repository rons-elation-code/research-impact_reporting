"""Unit tests for RunManifest / fingerprint / lock (Spec 0008)."""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

from lavandula.nonprofits.batch_manifest import (
    BatchState,
    ManifestCorruptError,
    RunManifest,
    RunnerLockedError,
    compute_fingerprint,
    fingerprint_diff,
    locked,
)


def _ns(**kw) -> argparse.Namespace:
    defaults = dict(
        db="/tmp/seeds.db",
        state=None,
        ntee_major=None,
        revenue_min=None,
        revenue_max=None,
        max_orgs=500,
        batch_size=50,
        model="haiku",
        re_resolve=False,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_manifest_round_trip_preserves_fields(tmp_path: Path) -> None:
    m = RunManifest(
        run_id="abcdef",
        started_at="2026-04-22T14:30:00Z",
        fingerprint="0123456789abcdef",
        args={"db": "/x", "model": "haiku"},
        total_orgs=100,
        batches=[BatchState(id=0, ein_first="1"*9, ein_last="2"*9,
                            input_count=50, state="pending"),
                 BatchState(id=1, ein_first="3"*9, ein_last="4"*9,
                            input_count=50, state="complete",
                            completed_count=50)],
    )
    path = tmp_path / "RUN_MANIFEST.json"
    m.save(path)
    loaded = RunManifest.load(path)
    assert loaded.run_id == m.run_id
    assert loaded.fingerprint == m.fingerprint
    assert len(loaded.batches) == 2
    assert loaded.batches[1].state == "complete"


def test_atomic_save_leaves_no_partial_file(tmp_path: Path) -> None:
    path = tmp_path / "RUN_MANIFEST.json"
    m = RunManifest(run_id="a", started_at="t", fingerprint="f",
                    args={}, total_orgs=0, batches=[])
    m.save(path)
    # No .tmp file should remain.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    # Content parses cleanly.
    assert json.loads(path.read_text())["run_id"] == "a"


def test_load_raises_on_bad_json(tmp_path: Path) -> None:
    path = tmp_path / "RUN_MANIFEST.json"
    path.write_text("not json {{{")
    with pytest.raises(ManifestCorruptError):
        RunManifest.load(path)


def test_load_raises_on_bad_batch_state(tmp_path: Path) -> None:
    path = tmp_path / "RUN_MANIFEST.json"
    path.write_text(json.dumps({
        "run_id": "a", "started_at": "t", "fingerprint": "f",
        "args": {}, "total_orgs": 1,
        "batches": [{"id": 0, "ein_first": "1"*9, "ein_last": "2"*9,
                     "input_count": 1, "completed_count": 0,
                     "state": "not-a-state"}],
    }))
    with pytest.raises(ManifestCorruptError):
        RunManifest.load(path)


def test_fingerprint_stable_across_arg_order() -> None:
    a = _ns(state=["NY", "MA"])
    b = _ns(state=["MA", "NY"])
    assert compute_fingerprint(a, 1) == compute_fingerprint(b, 1)


def test_fingerprint_differs_on_db_path_change(tmp_path: Path) -> None:
    a = _ns(db=str(tmp_path / "one.db"))
    b = _ns(db=str(tmp_path / "two.db"))
    assert compute_fingerprint(a, 1) != compute_fingerprint(b, 1)


def test_fingerprint_differs_on_prompt_version_bump() -> None:
    a = _ns()
    assert compute_fingerprint(a, 1) != compute_fingerprint(a, 2)


def test_fingerprint_diff_lists_changed_fields() -> None:
    manifest_args = {
        "db_path_canonical": "/tmp/a.db",
        "state": ["NY"],
        "ntee_major": [],
        "revenue_min": None,
        "revenue_max": None,
        "max_orgs": 500,
        "batch_size": 50,
        "model": "haiku",
        "re_resolve": False,
        "prompt_version": 1,
    }
    # Change state + max_orgs.
    args = _ns(state=["MA"], max_orgs=1000)
    args.db = "/tmp/a.db"
    diff = fingerprint_diff(manifest_args, args, 1)
    names = {d[0] for d in diff}
    assert names == {"state", "max_orgs"}


def test_lock_blocks_second_runner(tmp_path: Path) -> None:
    path = tmp_path / "RUN_MANIFEST.json"
    with locked(path):
        with pytest.raises(RunnerLockedError):
            with locked(path):
                pass


def test_lock_released_after_context_exit(tmp_path: Path) -> None:
    path = tmp_path / "RUN_MANIFEST.json"
    with locked(path):
        pass
    # Second acquisition succeeds.
    with locked(path):
        pass
