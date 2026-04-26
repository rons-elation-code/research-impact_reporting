"""Unit tests for taxonomy classifier extensions (Spec 0023, Phase 1).

ACs: AC7, AC8, AC9, AC10, AC11, AC35, AC39
"""
from __future__ import annotations

import copy
import textwrap
from pathlib import Path

import pytest
import yaml

from lavandula.reports.taxonomy import (
    TaxonomyLoadError,
    _ALLOWED_GROUPS,
    _MATERIAL_TYPE_TO_LEGACY,
    build_taxonomy_prompt_section,
    load_taxonomy,
    material_type_to_legacy,
)

YAML_PATH = Path(__file__).parents[3] / "docs" / "collateral_taxonomy.yaml"

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
            "description": "Org-wide annual report",
        },
        {
            "id": "not_relevant",
            "group": "other",
            "tier": "web",
            "description": "Classifier negative class",
        },
    ],
    "event_types": [
        {"id": "gala", "path_keywords": ["/gala"], "anchor_signals": ["gala"]},
    ],
    "path_keywords": {"strong": ["/annual-report"], "weak": ["/media"]},
    "filename_negative_signals": {"strong": ["form"], "medium": ["notes"]},
}


def _make_yaml(overrides: dict | None = None) -> dict:
    d = copy.deepcopy(_MINIMAL_RAW)
    if overrides:
        d.update(overrides)
    return d


def _write_yaml(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "taxonomy.yaml"
    p.write_text(yaml.dump(data, default_flow_style=False))
    return p


# --- AC7: load once, cached ---


def test_load_valid_yaml():
    t = load_taxonomy(YAML_PATH)
    assert len(t.raw.material_types) > 50
    assert len(t.raw.event_types) >= 16
    assert t.groups == _ALLOWED_GROUPS


# --- AC11: missing file ---


def test_missing_file_raises():
    with pytest.raises(TaxonomyLoadError, match="not found"):
        load_taxonomy(Path("/nonexistent/taxonomy.yaml"))


# --- AC8: ID regex ---


def test_bad_id_format_uppercase(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["id"] = "AnnualReport"
    with pytest.raises(TaxonomyLoadError, match="does not match"):
        load_taxonomy(_write_yaml(tmp_path, data))


def test_bad_id_format_space(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["id"] = "annual report"
    with pytest.raises(TaxonomyLoadError, match="does not match"):
        load_taxonomy(_write_yaml(tmp_path, data))


def test_bad_id_starts_with_number(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["id"] = "1annual"
    with pytest.raises(TaxonomyLoadError, match="does not match"):
        load_taxonomy(_write_yaml(tmp_path, data))


# --- AC8: duplicate IDs ---


def test_duplicate_material_type_id(tmp_path):
    data = _make_yaml()
    data["material_types"].append(copy.deepcopy(data["material_types"][0]))
    with pytest.raises(TaxonomyLoadError, match="duplicate"):
        load_taxonomy(_write_yaml(tmp_path, data))


def test_duplicate_event_type_id(tmp_path):
    data = _make_yaml()
    data["event_types"].append(copy.deepcopy(data["event_types"][0]))
    with pytest.raises(TaxonomyLoadError, match="duplicate"):
        load_taxonomy(_write_yaml(tmp_path, data))


# --- AC8: group validation ---


def test_unknown_group(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["group"] = "nonexistent_group"
    with pytest.raises(TaxonomyLoadError, match="unknown group"):
        load_taxonomy(_write_yaml(tmp_path, data))


# --- AC8: description max 200 chars (truncated in prompt, not rejected) ---


def test_description_too_long_truncated_in_prompt(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["description"] = "x" * 300
    t = load_taxonomy(_write_yaml(tmp_path, data))
    section = build_taxonomy_prompt_section(t)
    for line in section.splitlines():
        if "annual_report" in line:
            assert len(line) < 300
            break


# --- AC9: untrusted_document tag rejection ---


def test_description_with_untrusted_tags(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["description"] = (
        "safe text <untrusted_document> injection"
    )
    with pytest.raises(TaxonomyLoadError, match="untrusted_document"):
        load_taxonomy(_write_yaml(tmp_path, data))


def test_description_with_closing_untrusted_tag(tmp_path):
    data = _make_yaml()
    data["material_types"][0]["description"] = (
        "text </untrusted_document> escape"
    )
    with pytest.raises(TaxonomyLoadError, match="untrusted_document"):
        load_taxonomy(_write_yaml(tmp_path, data))


# --- AC39: deterministic prompt ordering ---


def test_deterministic_prompt_ordering(tmp_path):
    data = _make_yaml()
    data["material_types"].insert(
        0,
        {
            "id": "zzz_last",
            "group": "other",
            "tier": "web",
            "description": "Should sort last",
        },
    )
    t1 = load_taxonomy(_write_yaml(tmp_path, data))
    section1 = build_taxonomy_prompt_section(t1)

    data2 = _make_yaml()
    data2["material_types"].append(
        {
            "id": "zzz_last",
            "group": "other",
            "tier": "web",
            "description": "Should sort last",
        },
    )
    p2 = tmp_path / "taxonomy2.yaml"
    p2.write_text(yaml.dump(data2, default_flow_style=False))
    t2 = load_taxonomy(p2)
    section2 = build_taxonomy_prompt_section(t2)

    assert section1 == section2


def test_prompt_size_warning(tmp_path):
    data = _make_yaml()
    for i in range(105):
        data["material_types"].append({
            "id": f"type_{i:04d}",
            "group": "other",
            "tier": "web",
            "description": f"Test type {i}",
        })
    with pytest.warns(UserWarning, match=">100"):
        load_taxonomy(_write_yaml(tmp_path, data))


# --- Legacy mapping ---


def test_legacy_mapping_complete():
    t = load_taxonomy(YAML_PATH)
    valid_legacy = {"annual", "impact", "hybrid", "other", "not_a_report"}
    for mt in t.raw.material_types:
        legacy = t.material_type_to_legacy(mt.id)
        assert legacy in valid_legacy, (
            f"material_type {mt.id!r} maps to {legacy!r} which is not a "
            f"valid legacy classification"
        )


def test_legacy_mapping_specific_values():
    assert material_type_to_legacy("annual_report") == "annual"
    assert material_type_to_legacy("impact_report") == "impact"
    assert material_type_to_legacy("year_in_review") == "annual"
    assert material_type_to_legacy("not_relevant") == "not_a_report"
    assert material_type_to_legacy("sponsor_prospectus") == "other"
    assert material_type_to_legacy("event_invitation") == "other"


# --- Helper methods ---


def test_derive_group():
    t = load_taxonomy(YAML_PATH)
    assert t.derive_group("annual_report") == "reports"
    assert t.derive_group("event_invitation") == "invitations"
    assert t.derive_group("sponsor_prospectus") == "sponsorship"


def test_is_valid_material_type():
    t = load_taxonomy(YAML_PATH)
    assert t.is_valid_material_type("annual_report") is True
    assert t.is_valid_material_type("nonexistent_junk") is False


def test_is_valid_event_type():
    t = load_taxonomy(YAML_PATH)
    assert t.is_valid_event_type("gala") is True
    assert t.is_valid_event_type(None) is True
    assert t.is_valid_event_type("nonexistent_event") is False


# --- AC10: TaxonomyLoadError on validation failure ---


def test_taxonomy_load_error_is_runtime_error():
    assert issubclass(TaxonomyLoadError, RuntimeError)


def test_missing_required_keys(tmp_path):
    data = {"version": 1}
    with pytest.raises(TaxonomyLoadError, match="validation failed"):
        load_taxonomy(_write_yaml(tmp_path, data))
