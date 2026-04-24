"""Tests for decisions log (sub-phase 1.6, AC11)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_decisions_log_writes_jsonl(tmp_path):
    """Log records are valid JSONL with expected fields."""
    import lavandula.reports.decisions_log as dl

    dl._logger = None
    with patch.object(dl, "_init") as mock_init:
        import logging

        logger = logging.getLogger("test.decisions.write")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in logger.handlers[:]:
            logger.removeHandler(h)
        handler = logging.FileHandler(tmp_path / "test.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        mock_init.return_value = logger

        dl.log_decision({
            "url": "https://example.org/report.pdf",
            "basename": "report.pdf",
            "filename_score": 0.8,
            "triage": "accept",
            "decision": "accept",
            "reason": "signal_match",
            "strong_path_hit": True,
            "weak_path_hit": False,
            "anchor_text": "annual report",
            "anchor_hit": True,
        })
        handler.flush()

    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["basename"] == "report.pdf"
    assert record["decision"] == "accept"
    assert "ts" in record


def test_decisions_log_redacts_url_fields(tmp_path):
    """URL fields are redacted; raw URLs never appear in output."""
    import lavandula.reports.decisions_log as dl

    dl._logger = None
    with patch.object(dl, "_init") as mock_init:
        import logging

        logger = logging.getLogger("test.decisions.redact")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in logger.handlers[:]:
            logger.removeHandler(h)
        handler = logging.FileHandler(tmp_path / "test.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        mock_init.return_value = logger

        dl.log_decision({
            "url": "https://example.org/x?token=secret123",
            "referring_page": "https://example.org/page?session=abc",
            "triage": "accept",
        })
        handler.flush()

    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert "url" not in record
    assert "referring_page" not in record
    assert "url_redacted" in record
    assert "secret123" not in record["url_redacted"]
    assert "referring_page_redacted" in record
    assert "abc" not in record["referring_page_redacted"]


def test_decisions_log_drops_unknown_fields(tmp_path):
    """Fields not in the allowlist are silently dropped."""
    import lavandula.reports.decisions_log as dl

    dl._logger = None
    with patch.object(dl, "_init") as mock_init:
        import logging

        logger = logging.getLogger("test.decisions.drop")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for h in logger.handlers[:]:
            logger.removeHandler(h)
        handler = logging.FileHandler(tmp_path / "test.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        mock_init.return_value = logger

        dl.log_decision({
            "password": "hunter2",
            "triage": "accept",
            "internal_state": {"key": "val"},
        })
        handler.flush()

    lines = (tmp_path / "test.jsonl").read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert "password" not in record
    assert "internal_state" not in record
    assert record["triage"] == "accept"


def test_log_emitted_on_drop_filename_reject(tmp_path):
    """A rejected candidate emits a decision record with decision=drop."""
    import lavandula.reports.decisions_log as dl

    records: list[dict] = []
    original_log = dl.log_decision

    def capture(record: dict) -> None:
        records.append(record)

    with patch.object(dl, "log_decision", side_effect=capture):
        from lavandula.reports.candidate_filter import extract_candidates

        # Use a patched log_decision to capture the decision
        import lavandula.reports.candidate_filter as cf
        original_cf_log = cf.log_decision
        cf.log_decision = capture
        try:
            html = '<html><body><a href="/media/Ram_Coloring_Page.pdf">Stuff</a></body></html>'
            extract_candidates(
                html=html,
                base_url="https://example.org/",
                seed_etld1="example.org",
                referring_page_url="https://example.org/",
            )
        finally:
            cf.log_decision = original_cf_log

    drop_records = [r for r in records if r.get("decision") == "drop"]
    assert len(drop_records) >= 1
    assert drop_records[0]["reason"] == "filename_score<=reject"
