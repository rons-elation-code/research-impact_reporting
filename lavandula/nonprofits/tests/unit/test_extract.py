"""AC16, AC17, AC8b: HTML → ExtractedProfile."""
from pathlib import Path

import pytest

from lavandula.nonprofits.extract import extract


F = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "cn"


def test_rated_profile_fields_populated():
    html = (F / "profile-rated-4star.html").read_bytes()
    p = extract(html, ein="530196605")
    assert p.ein == "530196605"
    assert p.name == "American Red Cross"
    assert p.rating_stars == 4
    assert p.overall_score is not None and 90 <= p.overall_score <= 95
    assert p.beacons_completed == 4
    assert p.rated == 1
    assert p.total_revenue == 3_456_789_012
    assert p.total_expenses == 3_201_000_000
    assert p.program_expense_pct == pytest.approx(89.5)
    assert p.ntee_major == "M"
    assert p.ntee_code == "M20"
    assert p.state == "DC"
    assert p.website_url_raw is not None
    # After normalization (direct URL or unwrap of CN redirect wrapper),
    # redcross.org should be the canonical form.
    assert p.website_url == "https://www.redcross.org"
    assert p.website_url_reason is None
    assert p.parse_status == "ok"


def test_unrated_profile_sets_rated_0():
    html = (F / "profile-unrated.html").read_bytes()
    p = extract(html, ein="123456789")
    assert p.name == "Example Tiny Nonprofit"
    assert p.rating_stars is None
    assert p.overall_score is None
    assert p.rated == 0
    assert p.mission is not None


def test_wrapped_website_unwraps_and_strips_tracking():
    html = (F / "profile-wrapped-website.html").read_bytes()
    p = extract(html, ein="444444444")
    # website_url_raw holds the CN wrapper; normalized URL is the
    # unwrapped destination minus utm_*.
    assert p.website_url_raw is not None
    assert "redirect" in p.website_url_raw
    assert p.website_url == "https://example.org"
    assert p.website_url_reason is None


def test_missing_website_records_missing_reason():
    html = (F / "profile-no-website.html").read_bytes()
    p = extract(html, ein="111111111")
    assert p.website_url_raw is None
    assert p.website_url is None
    assert p.website_url_reason == "missing"


def test_truncated_html_sets_parse_status():
    html = (F / "profile-truncated.html").read_bytes()
    p = extract(html, ein="222222222")
    assert p.parse_status in ("partial", "unparsed")


def test_xxe_html_not_resolved(monkeypatch):
    import socket
    calls = []
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: calls.append(a),
    )
    html = (F / "xxe-html-mode.html").read_bytes()
    p = extract(html, ein="333333333")
    # No outbound network call from parsing HTML.
    assert calls == []
    # No file:///dev/null content leaked into name or mission.
    full = f"{p.name}{p.mission or ''}"
    assert "dev/null" not in full
