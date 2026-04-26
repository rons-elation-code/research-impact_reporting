"""Drift tests: YAML ↔ CHECK constraint ↔ legacy mapping sync (Spec 0023).

ACs: AC28, AC32, AC33
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lavandula.reports.taxonomy import (
    _MATERIAL_TYPE_TO_LEGACY,
    load_taxonomy,
)
from lavandula.reports.tools.validate_taxonomy_check import (
    _load_yaml_ids,
    validate,
)

_ROOT = Path(__file__).resolve().parents[4]
YAML_PATH = _ROOT / "lavandula" / "docs" / "collateral_taxonomy.yaml"
MIGRATION_PATH = (
    _ROOT / "lavandula" / "migrations" / "rds" / "007_classifier_expansion.sql"
)


# --- AC28: bidirectional YAML ↔ CHECK constraint match ---


def test_all_material_types_in_check_constraint():
    errors = validate(YAML_PATH, MIGRATION_PATH)
    mt_errors = [e for e in errors if "reports_mt_chk" in e]
    assert not mt_errors, f"material_type drift: {mt_errors}"


def test_all_event_types_in_check_constraint():
    errors = validate(YAML_PATH, MIGRATION_PATH)
    et_errors = [e for e in errors if "reports_et_chk" in e]
    assert not et_errors, f"event_type drift: {et_errors}"


def test_all_groups_in_check_constraint():
    errors = validate(YAML_PATH, MIGRATION_PATH)
    mg_errors = [e for e in errors if "reports_mg_chk" in e]
    assert not mg_errors, f"material_group drift: {mg_errors}"


# --- AC32: every material_type has a legacy mapping ---


def test_all_material_types_have_legacy_mapping():
    """Every material_type in the YAML must map to a valid legacy value."""
    t = load_taxonomy(YAML_PATH)
    valid_legacy = {"annual", "impact", "hybrid", "other", "not_a_report"}
    for mt in t.raw.material_types:
        legacy = t.material_type_to_legacy(mt.id)
        assert legacy in valid_legacy, (
            f"material_type {mt.id!r} maps to {legacy!r} — "
            f"not a valid legacy classification"
        )


def test_report_group_types_have_explicit_legacy_mapping():
    """Material types in the 'reports' group must have explicit entries
    in _MATERIAL_TYPE_TO_LEGACY — defaulting to 'other' would be wrong
    for types that semantically are annual/impact reports."""
    t = load_taxonomy(YAML_PATH)
    report_group_types = [
        mt for mt in t.raw.material_types if mt.group == "reports"
    ]
    assert report_group_types, "no material_types in 'reports' group?"
    missing = []
    for mt in report_group_types:
        if mt.id not in _MATERIAL_TYPE_TO_LEGACY:
            missing.append(mt.id)
    assert not missing, (
        f"report-group material_type(s) missing explicit legacy mapping "
        f"(would silently default to 'other'): {missing}"
    )


# --- AC33: adding a type to YAML without updating CHECK fails ---


def test_yaml_check_constraint_full_validation():
    """Run the full validation — zero errors expected."""
    errors = validate(YAML_PATH, MIGRATION_PATH)
    assert not errors, f"YAML ↔ CHECK drift: {errors}"
