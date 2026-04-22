"""Spec 0007 AC15 — metadata encoding tests (pure functions, no AWS)."""
from __future__ import annotations

from lavandula.reports.s3_archive import (
    _encode_s3_metadata,
    _truncate_respecting_percent_triplets,
)


def test_ac15_crlf_is_percent_encoded():
    raw = {"source-url": "https://a.example/\r\nx-amz-acl: public-read"}
    out = _encode_s3_metadata(raw)
    encoded = out["source-url"]
    assert "\r" not in encoded
    assert "\n" not in encoded
    # CRLF becomes %0D%0A (case-insensitive but stdlib emits uppercase).
    assert "%0D%0A" in encoded.upper()


def test_ac15_colons_slashes_are_encoded():
    raw = {"source-url": "https://a.example/path?q=1"}
    out = _encode_s3_metadata(raw)
    assert out["source-url"] == "https%3A%2F%2Fa.example%2Fpath%3Fq%3D1"


def test_ac15_non_ascii_url_is_encoded_safely():
    raw = {"source-url": "https://ex.org/café.pdf"}
    out = _encode_s3_metadata(raw)
    # café → %C3%A9 in UTF-8
    assert "%C3%A9" in out["source-url"].upper()


def test_ac15_truncation_does_not_split_triplet():
    # Force truncation inside a %XX.
    # Encoded URL whose 1024th char lands on '%'.
    head = "https%3A%2F%2Fex.org%2F"
    # Fill body with "a" until length 1022, then append "%2F" so char 1023 is '%'
    body = "a" * (1024 - len(head) - 2) + "%2F"  # ends with a %2F near the limit
    s = head + body
    assert len(s) > 0
    truncated = _truncate_respecting_percent_triplets(s, 1024)
    assert len(truncated) <= 1024
    # No dangling '%' or '%X' at tail
    assert not truncated.endswith("%")
    if len(truncated) >= 2:
        assert not (truncated[-2] == "%" and len(truncated) < 1024)


def test_ac15_truncation_handles_exact_percent_at_cut():
    # Build a string where limit lands exactly on a '%'.
    s = "a" * 1023 + "%2F" + "b" * 10
    out = _truncate_respecting_percent_triplets(s, 1024)
    # char at index 1023 is '%'; truncate should back off to before it
    assert out == "a" * 1023


def test_ac15_truncation_handles_percent_one_before_cut():
    # char at index 1022 is '%', limit=1024 leaves only '%X' at tail.
    s = "a" * 1022 + "%2F" + "b" * 10
    out = _truncate_respecting_percent_triplets(s, 1024)
    assert out == "a" * 1022  # backed off two chars


def test_ac15_non_ascii_non_url_key_is_dropped():
    raw = {"ein": "12345 678", "source-url": "https://a.example/"}
    out = _encode_s3_metadata(raw)
    assert "ein" not in out  # dropped
    assert "source-url" in out


def test_encoding_drops_none_values():
    out = _encode_s3_metadata({"ein": None, "crawl-run-id": "abc"})
    assert "ein" not in out
    assert out["crawl-run-id"] == "abc"
