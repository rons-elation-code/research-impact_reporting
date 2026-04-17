"""AC13 (challenge isolation), AC14 (cross-EIN redirect)."""
from unittest.mock import MagicMock

import pytest

from lavandula.nonprofits import fetcher
from lavandula.nonprofits.http_client import FetchResult


def _ok_result(body: bytes, final_url: str = "https://www.charitynavigator.org/ein/530196605",
               redirect_chain=None):
    return FetchResult(
        status="ok",
        http_status=200,
        body=body,
        final_url=final_url,
        redirect_chain=redirect_chain or [],
        bytes_read=len(body),
        attempts=1,
        elapsed_ms=100,
    )


def test_challenge_body_isolates(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = tmp_path / ".tmp"
    tmpdir.mkdir()
    mock_client = MagicMock()
    body = b"<html><title>Just a moment...</title><div id='cf-challenge'></div></html>"
    mock_client.get.return_value = _ok_result(body)

    outcome = fetcher.fetch_profile(
        mock_client, "530196605", raw_cn=raw, tmpdir=tmpdir,
    )
    assert outcome.fetch_status == "challenge"
    assert outcome.archive_path is None
    assert outcome.challenge_path is not None
    assert outcome.challenge_path.exists()
    # Main archive MUST NOT have been written.
    assert not (raw / "530196605.html").exists()


def test_cross_ein_redirect_populates_field(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = tmp_path / ".tmp"
    tmpdir.mkdir()
    mock_client = MagicMock()
    body = b"<html><h1 class='orgName'>Other Org</h1></html>"
    mock_client.get.return_value = _ok_result(
        body,
        final_url="https://www.charitynavigator.org/ein/222222222",
        redirect_chain=["https://www.charitynavigator.org/ein/222222222"],
    )
    outcome = fetcher.fetch_profile(
        mock_client, "111111111", raw_cn=raw, tmpdir=tmpdir,
    )
    assert outcome.fetch_status == "ok"
    assert outcome.redirected_to_ein == "222222222"
    # Archive keyed by the source EIN, not the target.
    assert (raw / "111111111.html").exists()


def test_ok_fetch_writes_archive(tmp_path):
    raw = tmp_path / "raw" / "cn"
    raw.mkdir(parents=True)
    tmpdir = tmp_path / ".tmp"
    tmpdir.mkdir()
    mock_client = MagicMock()
    body = b"<html><h1 class='orgName'>American Red Cross</h1></html>"
    mock_client.get.return_value = _ok_result(body)
    outcome = fetcher.fetch_profile(
        mock_client, "530196605", raw_cn=raw, tmpdir=tmpdir,
    )
    assert outcome.fetch_status == "ok"
    assert outcome.archive_path == raw / "530196605.html"
    assert outcome.content_sha256 is not None
    assert len(outcome.content_sha256) == 64
