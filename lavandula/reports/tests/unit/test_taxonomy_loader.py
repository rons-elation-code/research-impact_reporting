"""Unit tests for taxonomy YAML loader and validator (sub-phase 1.1)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lavandula.reports.taxonomy import Taxonomy, TaxonomyRaw, load_taxonomy

YAML_PATH = Path(__file__).parents[3] / "docs" / "collateral_taxonomy.yaml"

# Minimal valid YAML for validator tests — kept small so mutations are precise.
_MINIMAL_RAW: dict = {
    "version": 1,
    "thresholds": {
        "filename_score_accept": 0.8,
        "filename_score_reject": 0.2,
        "filename_score_weak_path_min": 0.5,
        "base_score": 0.5,
    },
    "signal_weights": {
        "strong_positive": 0.3,
        "medium_positive": 0.15,
        "strong_negative": -0.4,
        "medium_negative": -0.1,
        "year_bonus": 0.1,
    },
    "material_types": [
        {
            "id": "annual_report",
            "group": "reports",
            "tier": "web",
            "filename_signals": {"strong_positive": ["annual-report"]},
            "anchor_signals": ["annual report"],
        },
    ],
    "event_types": [
        {"id": "gala", "path_keywords": ["/gala"], "anchor_signals": ["gala"]},
    ],
    "path_keywords": {"strong": ["/annual-report"], "weak": ["/media"]},
    "filename_negative_signals": {"strong": ["form"], "medium": ["notes"]},
}


def _make_raw(**overrides: object) -> dict:
    """Return a deep copy of _MINIMAL_RAW with overrides applied."""
    import copy

    d = copy.deepcopy(_MINIMAL_RAW)
    for key, val in overrides.items():
        parts = key.split(".")
        target = d
        for p in parts[:-1]:
            target = target[p]
        target[parts[-1]] = val
    return d


def _load_from_dict(d: dict, tmp_path: Path) -> Taxonomy:
    p = tmp_path / "test.yaml"
    p.write_text(yaml.dump(d))
    return load_taxonomy(p)


# ---- Happy path: committed YAML loads correctly ----


def test_loads_committed_yaml_file():
    tax = load_taxonomy(YAML_PATH)
    assert len(tax.raw.material_types) > 50
    assert len(tax.path_keywords_strong) > 10
    assert len(tax.path_keywords_weak) > 0
    assert len(tax.anchor_keywords) > 10
    assert len(tax.filename_positive) > 10
    assert len(tax.filename_negative) > 10


# ---- Security: yaml.safe_load enforcement (AC16) ----


def test_rejects_yaml_load_unsafe_payload(tmp_path):
    """Attempt to load YAML with !!python/object/apply tag.

    yaml.safe_load must raise rather than executing the payload.
    """
    malicious = "!!python/object/apply:os.system ['echo pwned']"
    p = tmp_path / "evil.yaml"
    p.write_text(malicious)
    with pytest.raises((yaml.YAMLError, yaml.constructor.ConstructorError)):
        load_taxonomy(p)


# ---- Version pinning (AC17) ----


def test_rejects_unknown_version(tmp_path):
    d = _make_raw()
    d["version"] = 99
    with pytest.raises(Exception, match="99"):
        _load_from_dict(d, tmp_path)


# ---- Threshold ordering ----


def test_rejects_threshold_out_of_order(tmp_path):
    d = _make_raw()
    d["thresholds"]["filename_score_accept"] = 0.5
    d["thresholds"]["filename_score_reject"] = 0.5
    with pytest.raises(Exception):
        _load_from_dict(d, tmp_path)


def test_rejects_threshold_accept_below_half(tmp_path):
    d = _make_raw()
    d["thresholds"]["filename_score_accept"] = 0.4
    with pytest.raises(Exception):
        _load_from_dict(d, tmp_path)


def test_rejects_threshold_reject_above_half(tmp_path):
    d = _make_raw()
    d["thresholds"]["filename_score_reject"] = 0.6
    with pytest.raises(Exception):
        _load_from_dict(d, tmp_path)


# ---- Keyword validation (AC18) ----


def test_rejects_short_keyword(tmp_path):
    d = _make_raw()
    d["material_types"][0]["filename_signals"]["strong_positive"] = ["ar"]
    with pytest.raises(Exception, match="shorter than 3 chars"):
        _load_from_dict(d, tmp_path)


def test_rejects_regex_metachar_keyword(tmp_path):
    d = _make_raw()
    d["path_keywords"]["strong"] = ["/annual-report", ".*report"]
    with pytest.raises(Exception, match="regex metacharacters"):
        _load_from_dict(d, tmp_path)


def test_rejects_uppercase_keyword(tmp_path):
    d = _make_raw()
    d["material_types"][0]["filename_signals"]["strong_positive"] = [
        "Annual-Report"
    ]
    with pytest.raises(Exception, match="uppercase"):
        _load_from_dict(d, tmp_path)


def test_runtime_view_lowercases_anchor_signals(tmp_path):
    d = _make_raw()
    d["material_types"][0]["anchor_signals"] = ["Annual Report"]
    tax = _load_from_dict(d, tmp_path)
    assert "annual report" in tax.anchor_keywords
    assert "Annual Report" not in tax.anchor_keywords


# ---- Path keyword overlap ----


def test_rejects_strong_weak_path_overlap(tmp_path):
    d = _make_raw()
    d["path_keywords"]["strong"] = ["/media"]
    d["path_keywords"]["weak"] = ["/media"]
    with pytest.raises(Exception, match="overlap"):
        _load_from_dict(d, tmp_path)


# ---- Positive/negative signal collision ----


def test_rejects_positive_negative_collision(tmp_path):
    d = _make_raw()
    d["material_types"][0]["filename_signals"]["strong_positive"] = ["form"]
    d["filename_negative_signals"]["strong"] = ["form"]
    with pytest.raises(Exception, match="both positive and negative"):
        _load_from_dict(d, tmp_path)


# ---- Duplicate IDs ----


def test_rejects_duplicate_ids(tmp_path):
    d = _make_raw()
    d["material_types"].append(
        {"id": "annual_report", "group": "reports", "tier": "web"}
    )
    with pytest.raises(Exception, match="duplicate id"):
        _load_from_dict(d, tmp_path)


# ---- Event path_keywords aggregate into strong set ----


def test_event_path_keywords_aggregate_into_strong(tmp_path):
    d = _make_raw()
    d["path_keywords"]["strong"] = ["/annual-report"]
    d["event_types"] = [
        {"id": "gala", "path_keywords": ["/gala"], "anchor_signals": ["gala"]}
    ]
    tax = _load_from_dict(d, tmp_path)
    assert "/gala" in tax.path_keywords_strong
    assert "/annual-report" in tax.path_keywords_strong
