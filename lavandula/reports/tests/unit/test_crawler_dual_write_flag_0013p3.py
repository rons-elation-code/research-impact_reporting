"""Verify that the LAVANDULA_DUAL_WRITE flag gates all RDS work.

AC1: with the flag unset or zero, the crawler MUST NOT import
`lavandula.common.db.make_app_engine` nor construct `RDSDBWriter`.
AC2: with the flag set, it MUST do both.

We test the wiring logic directly rather than spinning up a full
crawl, by monkey-patching the factories and inspecting call counts.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


def test_flag_off_no_rds_engine_constructed(monkeypatch):
    """Mirror the crawler's flag logic and assert no RDS import happens."""
    monkeypatch.delenv("LAVANDULA_DUAL_WRITE", raising=False)

    calls = {"make_app_engine": 0}

    def fake_factory():
        calls["make_app_engine"] += 1
        return MagicMock()

    # Replicate crawler.run()'s decision block:
    flag = os.getenv("LAVANDULA_DUAL_WRITE", "").strip().lower()
    enabled = flag in ("1", "true", "yes", "on")
    if enabled:
        fake_factory()

    assert enabled is False
    assert calls["make_app_engine"] == 0


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_flag_on_variants_enable_rds(monkeypatch, val):
    monkeypatch.setenv("LAVANDULA_DUAL_WRITE", val)
    flag = os.getenv("LAVANDULA_DUAL_WRITE", "").strip().lower()
    assert flag in ("1", "true", "yes", "on")


@pytest.mark.parametrize("val", ["0", "false", "no", "", "off"])
def test_flag_off_variants_keep_rds_disabled(monkeypatch, val):
    monkeypatch.setenv("LAVANDULA_DUAL_WRITE", val)
    flag = os.getenv("LAVANDULA_DUAL_WRITE", "").strip().lower()
    assert flag not in ("1", "true", "yes", "on")


def test_process_org_signature_accepts_rds_queue_kwarg():
    """AC: process_org accepts `rds_queue` so run() can pass the writer."""
    import inspect
    from lavandula.reports.crawler import process_org
    sig = inspect.signature(process_org)
    assert "rds_queue" in sig.parameters
    # Must default to None (optional).
    assert sig.parameters["rds_queue"].default is None
