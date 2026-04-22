"""AC23 — JSON injection defense: batch input files go through json.dumps()."""
from __future__ import annotations

import json
from pathlib import Path

from lavandula.nonprofits.tools import batch_resolve as br


def test_malicious_name_round_trips_cleanly(tmp_path: Path) -> None:
    org = {
        "ein": "123456789",
        "name": 'test"\n"ein": [1,2,3], "x',
        "address": "1 Main St",
        "city": "Austin",
        "state": "TX",
        "zipcode": "78701",
        "ntee_code": "E20",
    }
    path = tmp_path / "batch-000-input.jsonl"
    br._write_batch_input(path, [org])
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    # The malicious payload did not sneak into the parsed structure:
    assert parsed["name"] == org["name"]
    assert parsed["ein"] == "123456789"
    # EIN remains a string, not the injected list.
    assert isinstance(parsed["ein"], str)


def test_all_fields_preserved_when_contain_quotes_and_newlines(
    tmp_path: Path,
) -> None:
    org = {
        "ein": "987654321",
        "name": "Acme\nCorp",
        "address": 'bldg "A"\n',
        "city": "X\\Y",
        "state": "TX",
        "zipcode": "00000",
        "ntee_code": "E20",
    }
    path = tmp_path / "b.jsonl"
    br._write_batch_input(path, [org])
    parsed = json.loads(path.read_text().splitlines()[0])
    assert parsed["name"] == "Acme\nCorp"
    assert parsed["address"] == 'bldg "A"\n'


def test_missing_fields_default_to_empty_string(tmp_path: Path) -> None:
    org = {"ein": "111111111"}
    path = tmp_path / "b.jsonl"
    br._write_batch_input(path, [org])
    parsed = json.loads(path.read_text().splitlines()[0])
    assert parsed["name"] == ""
    assert parsed["address"] == ""
