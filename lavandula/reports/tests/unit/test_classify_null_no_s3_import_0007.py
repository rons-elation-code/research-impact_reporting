"""Spec 0007 AC12 — classify_null must not touch the S3 archive."""
from __future__ import annotations

import ast
from pathlib import Path


def test_classify_null_does_not_import_s3_archive():
    root = Path(__file__).resolve().parents[2]  # lavandula/reports
    src = (root / "tools" / "classify_null.py").read_text()
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "s3_archive" in alias.name:
                    offenders.append(alias.name)
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "s3_archive" in mod:
                offenders.append(mod)
            for alias in node.names:
                if "s3_archive" in alias.name:
                    offenders.append(f"{mod}.{alias.name}")
    assert offenders == [], f"classify_null imports S3: {offenders}"
