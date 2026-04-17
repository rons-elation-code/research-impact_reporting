"""AC24, AC24a, AC24b: checkpoint integrity, corruption handling, retention cap."""
import json

import pytest

from lavandula.nonprofits import checkpoint


def test_roundtrip(tmp_path):
    state = checkpoint.CheckpointState(
        started_at="2026-04-17T00:00:00", last_ein="530196605",
        fetched_count=10, failed_count=2,
    )
    checkpoint.save(
        state,
        path=tmp_path / "checkpoint.json",
        key_path=tmp_path / ".crawler.key",
    )
    loaded = checkpoint.load(
        path=tmp_path / "checkpoint.json",
        key_path=tmp_path / ".crawler.key",
    )
    assert loaded.last_ein == "530196605"
    assert loaded.fetched_count == 10


def test_corrupt_json_rotated(tmp_path):
    cp = tmp_path / "checkpoint.json"
    key = tmp_path / ".crawler.key"
    cp.write_text("not-json{{")
    state = checkpoint.load(path=cp, key_path=key)
    # File was rotated
    assert not cp.exists()
    assert any(p.name.startswith("checkpoint.corrupt-") for p in tmp_path.iterdir())
    # Fresh state
    assert state.fetched_count == 0


def test_hmac_mismatch_rotated(tmp_path):
    state = checkpoint.CheckpointState(started_at="x")
    checkpoint.save(
        state,
        path=tmp_path / "checkpoint.json",
        key_path=tmp_path / ".crawler.key",
    )
    # Tamper with the payload.
    doc = json.loads((tmp_path / "checkpoint.json").read_text())
    doc["payload"]["fetched_count"] = 999999
    (tmp_path / "checkpoint.json").write_text(json.dumps(doc))
    loaded = checkpoint.load(
        path=tmp_path / "checkpoint.json",
        key_path=tmp_path / ".crawler.key",
    )
    # Rotated to corrupt + fresh state (fetched_count=0, not 999999).
    assert loaded.fetched_count == 0


def test_corrupt_retention_cap(tmp_path):
    cp = tmp_path / "checkpoint.json"
    key = tmp_path / ".crawler.key"
    # Trigger 7 rotations by writing bad JSON and calling load().
    for _ in range(7):
        cp.write_text("bogus")
        checkpoint.load(path=cp, key_path=key)
    corrupt = sorted(tmp_path.glob("checkpoint.corrupt-*.json"))
    assert len(corrupt) <= 5
