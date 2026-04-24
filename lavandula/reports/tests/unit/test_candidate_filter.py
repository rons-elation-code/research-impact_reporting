"""AC2, AC3, AC4, AC8.1 (link cap), AC12.3 — candidate filter rules."""
from __future__ import annotations

import pytest


BASIC_HTML = """
<html><body>
  <a href="/annual-report">Annual report</a>          <!-- match -->
  <a href="/about/impact/">Our Impact</a>             <!-- match -->
  <a href="/reports/2024.pdf">2024 Annual</a>         <!-- match -->
  <a href="/careers">Careers</a>                      <!-- no -->
  <a href="/donate">Donate</a>                        <!-- match (taxonomy) -->
  <a href="/news">News</a>                            <!-- no -->
  <a href="/blog">Blog</a>                            <!-- no -->
  <a href="/volunteer">Volunteer</a>                  <!-- no -->
  <a href="/year-in-review-2024.pdf">Year in Review</a> <!-- match via anchor -->
  <a href="/press">Press</a>                          <!-- match (weak path) -->
</body></html>
"""


def test_ac2_anchor_and_path_filter():
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html=BASIC_HTML,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    paths = sorted({c.url.split("example.org", 1)[1] for c in candidates})
    # Spec 0020: taxonomy expands keyword set; /donate now matches
    # via /donate strong path. /press is weak and lacks backing signal
    # (no anchor keyword match, no positive filename) so it's dropped.
    assert paths == [
        "/about/impact/",
        "/annual-report",
        "/donate",
        "/reports/2024.pdf",
        "/year-in-review-2024.pdf",
    ]


PLATFORM_HTML = """
<html><body>
  <a href="https://issuu.com/example/docs/2024-annual-report">Annual report</a>
  <a href="https://flipsnack.com/example/annual24">Flipsnack copy</a>
  <a href="https://www.canva.com/design/DAFxxx/view">Canva</a>
  <a href="https://randomsite.com/report.pdf">Random</a>
</body></html>
"""


def test_ac3_hosting_platform_signatures():
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html=PLATFORM_HTML,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    platforms = sorted({c.hosting_platform for c in candidates if c.hosting_platform})
    assert platforms == ["canva", "flipsnack", "issuu"]


def test_ac4_per_org_candidate_cap():
    """100 matching links → truncated to 30."""
    from lavandula.reports.candidate_filter import extract_candidates, CANDIDATE_CAP_PER_ORG
    links = "\n".join(
        f'<a href="/annual-report-{i}.pdf">Annual Report {i}</a>' for i in range(100)
    )
    html = f"<html><body>{links}</body></html>"
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert CANDIDATE_CAP_PER_ORG == 30
    assert len(candidates) <= 30


def test_ac8_1_max_parsed_links_per_page():
    """AC8.1 — HTML link extraction stops at MAX_PARSED_LINKS_PER_PAGE."""
    from lavandula.reports.candidate_filter import (
        extract_candidates,
        MAX_PARSED_LINKS_PER_PAGE,
    )
    assert MAX_PARSED_LINKS_PER_PAGE == 10_000
    links = "\n".join(
        f'<a href="/link-{i}">x</a>' for i in range(MAX_PARSED_LINKS_PER_PAGE + 100)
    )
    html = f"<html><body>{links}</body></html>"
    # Must not explode / should silently cap at MAX_PARSED_LINKS_PER_PAGE
    extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )


def test_ac12_3_platform_attribution_from_homepage():
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html='<a href="https://issuu.com/x/docs/2024">Annual</a>',
        base_url="https://redcross.org/",
        seed_etld1="redcross.org",
        referring_page_url="https://redcross.org/",
    )
    assert len(candidates) == 1
    assert candidates[0].attribution_confidence == "platform_verified"


def test_ac12_3_platform_attribution_from_forum_is_unverified():
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html='<a href="https://issuu.com/attacker/docs/red-cross-2024">Annual</a>',
        base_url="https://redcross.org/forum/somepost",
        seed_etld1="redcross.org",
        referring_page_url="https://redcross.org/forum/somepost",
    )
    assert len(candidates) == 1
    assert candidates[0].attribution_confidence == "platform_unverified"


def test_ac12_3_own_domain_attribution():
    from lavandula.reports.candidate_filter import extract_candidates
    candidates = extract_candidates(
        html='<a href="/annual-report/2024.pdf">Annual</a>',
        base_url="https://redcross.org/",
        seed_etld1="redcross.org",
        referring_page_url="https://redcross.org/",
    )
    assert len(candidates) == 1
    assert candidates[0].attribution_confidence == "own_domain"
