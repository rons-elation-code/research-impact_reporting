"""Validate that collateral_taxonomy.yaml and migration CHECK constraints match.

Usage:
    python -m lavandula.reports.tools.validate_taxonomy_check --generate
    python -m lavandula.reports.tools.validate_taxonomy_check --validate

Exit code 0 on match, 1 on drift with diff output.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _load_yaml_ids(yaml_path: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (material_type_ids, group_ids, event_type_ids) from YAML."""
    import yaml
    with yaml_path.open() as f:
        data = yaml.safe_load(f)
    mt_ids = sorted({mt["id"] for mt in data["material_types"]})
    groups = sorted({mt["group"] for mt in data["material_types"]})
    et_ids = sorted({et["id"] for et in data["event_types"]})
    return mt_ids, groups, et_ids


def _format_sql_in_list(ids: list[str], indent: str = "    ") -> str:
    return ",\n".join(f"{indent}'{i}'" for i in ids)


def _parse_check_constraint(sql: str, constraint_name: str) -> set[str]:
    """Extract the IN-list values from a named CHECK constraint in SQL."""
    pattern = rf"CONSTRAINT\s+{re.escape(constraint_name)}\s+CHECK\s*\([^)]*IN\s*\((.*?)\)\s*\)"
    match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
    if not match:
        return set()
    raw = match.group(1)
    return {m.group(1) for m in re.finditer(r"'([^']+)'", raw)}


def generate(yaml_path: Path) -> str:
    mt_ids, groups, et_ids = _load_yaml_ids(yaml_path)
    lines = [
        "-- reports_mt_chk (material_type)",
        _format_sql_in_list(mt_ids),
        "",
        "-- reports_mg_chk (material_group)",
        _format_sql_in_list(groups),
        "",
        "-- reports_et_chk (event_type)",
        _format_sql_in_list(et_ids),
    ]
    return "\n".join(lines)


def validate(yaml_path: Path, migration_path: Path) -> list[str]:
    """Compare YAML IDs against migration CHECK constraints. Returns errors."""
    mt_ids, groups, et_ids = _load_yaml_ids(yaml_path)
    sql = migration_path.read_text()

    errors = []
    for name, yaml_set, constraint_name in [
        ("material_type", set(mt_ids), "reports_mt_chk"),
        ("material_group", set(groups), "reports_mg_chk"),
        ("event_type", set(et_ids), "reports_et_chk"),
    ]:
        sql_set = _parse_check_constraint(sql, constraint_name)
        if not sql_set:
            errors.append(f"{constraint_name}: constraint not found in migration SQL")
            continue
        only_yaml = yaml_set - sql_set
        only_sql = sql_set - yaml_set
        if only_yaml:
            errors.append(
                f"{constraint_name}: in YAML but not in SQL: {sorted(only_yaml)}"
            )
        if only_sql:
            errors.append(
                f"{constraint_name}: in SQL but not in YAML: {sorted(only_sql)}"
            )
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--generate", action="store_true",
                       help="Print sorted SQL IN-list literals from YAML")
    group.add_argument("--validate", action="store_true",
                       help="Compare YAML against migration SQL bidirectionally")
    ap.add_argument("--yaml", type=Path, default=None)
    ap.add_argument("--migration", type=Path, default=None)
    args = ap.parse_args()

    root = _project_root()
    yaml_path = args.yaml or root / "lavandula" / "docs" / "collateral_taxonomy.yaml"
    migration_path = (
        args.migration
        or root / "lavandula" / "migrations" / "rds" / "007_classifier_expansion.sql"
    )

    if args.generate:
        print(generate(yaml_path))
        return 0

    errors = validate(yaml_path, migration_path)
    if errors:
        print("DRIFT DETECTED:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    print("OK: YAML and migration CHECK constraints match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
