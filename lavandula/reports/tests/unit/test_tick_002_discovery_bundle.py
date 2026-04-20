"""TICK-002 — Discovery-layer improvements bundle.

Covers AC1-AC9 from the TICK-002 amendment. AC10 ('all existing
tests still pass') is covered by running the full suite.
"""
from __future__ import annotations


# ---------------------------------------------------------------
# AC1-AC3: CMS-subdomain rule (Fix 1)
# ---------------------------------------------------------------


def test_ac1_cms_subdomain_match_accepted():
    """sagehillschool.myschoolapp.com PDF accepted when seed is
    sagehillschool.org AND parent is a report-anchor subpage."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="https://sagehillschool.myschoolapp.com/ftpimages/145/download/download_10878708.pdf">Download</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://www.sagehillschool.org/giving/annual-fund",
        seed_etld1="sagehillschool.org",
        referring_page_url="https://www.sagehillschool.org/giving/annual-fund",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    assert len(candidates) == 1
    c = candidates[0]
    assert c.hosting_platform == "own-cms"
    assert c.attribution_confidence == "platform_verified"
    assert "myschoolapp.com" in c.url


def test_ac2_cms_subdomain_mismatch_rejected():
    """Host whose first label does NOT match seed is still dropped."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="https://randomschool.myschoolapp.com/reports/2024.pdf">Download</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://www.sagehillschool.org/giving/annual-fund",
        seed_etld1="sagehillschool.org",
        referring_page_url="https://www.sagehillschool.org/giving/annual-fund",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    assert candidates == []


def test_ac3_cms_short_label_rejected():
    """Seed label shorter than CMS_LABEL_MIN_CHARS (4) → rule skipped."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="https://abc.myschoolapp.com/reports/2024.pdf">Download</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://www.abc.org/reports",
        seed_etld1="abc.org",  # seed label "abc" is 3 chars → blocked
        referring_page_url="https://www.abc.org/reports",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    assert candidates == []


def test_ac3_cms_generic_label_rejected():
    """Seed label in blocklist (www, en, app, etc.) → rule skipped."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="https://www.myschoolapp.com/reports/2024.pdf">Download</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://www.some-org.org/reports",
        # If we just naively took "www" from seed_etld1="www.some-org.org"
        # as the label, we'd accept www.anything.com. Guard prevents this.
        seed_etld1="www.some-org.org",
        referring_page_url="https://www.some-org.org/reports",
        discovered_via="subpage-link",
        parent_is_report_anchor=True,
    )
    assert candidates == []


# ---------------------------------------------------------------
# AC4-AC6: Retry logic (Fix 2)
# ---------------------------------------------------------------


def test_ac4_ac5_ac6_retry_policy_import():
    """Confirm config exposes the TICK-002 retry knobs."""
    from lavandula.reports import config
    assert "network_error" in config.RETRY_STATUSES
    assert "server_error" in config.RETRY_STATUSES
    assert "homepage" in config.RETRY_KINDS
    assert "subpage" in config.RETRY_KINDS
    assert "sitemap" in config.RETRY_KINDS
    # PDF fetches NOT retryable (AC6)
    assert "pdf-get" not in config.RETRY_KINDS
    assert "pdf-head" not in config.RETRY_KINDS
    # 3 total attempts (1 initial + 2 retries) per AC5
    assert config.RETRY_MAX_ATTEMPTS == 3


# ---------------------------------------------------------------
# AC7: Subpage cap bump (Fix 3)
# ---------------------------------------------------------------


def test_ac7_max_subpages_is_10():
    from lavandula.reports import config
    assert config.MAX_SUBPAGES_PER_ORG == 10


# ---------------------------------------------------------------
# AC8: i18n path dedup (Fix 4)
# ---------------------------------------------------------------


def test_ac8_i18n_dedup_drops_localized_variant():
    """/our-impact/ first, then /tl/our-impact/ → second dropped."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/our-impact/">Our Impact</a>'
        '<a href="/tl/our-impact/">Aming Epekto</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    urls = [c.url for c in candidates]
    # Only one survives the dedup
    impact_urls = [u for u in urls if "our-impact" in u]
    assert len(impact_urls) == 1


def test_ac8_i18n_dedup_order_independent():
    """Reverse order: /tl/our-impact/ first, /our-impact/ second.
    Only one survives (first wins by document order)."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/tl/our-impact/">Aming Epekto</a>'
        '<a href="/our-impact/">Our Impact</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    impact_urls = [c.url for c in candidates if "our-impact" in c.url]
    assert len(impact_urls) == 1


def test_ac8_i18n_non_locale_prefix_not_deduped():
    """/impact/stories/ vs /our-impact/ → different pages, both kept."""
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/our-impact/">Our Impact</a>'
        '<a href="/impact/stories/">Impact Stories</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    paths = {c.url.split("example.org")[1] for c in candidates}
    assert "/our-impact/" in paths
    assert "/impact/stories/" in paths


# ---------------------------------------------------------------
# AC9: Expanded keywords (Fix 5)
# ---------------------------------------------------------------


def test_ac9_path_keyword_resources():
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/resources/guide">Resources</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    urls = [c.url for c in candidates]
    assert any("/resources/" in u for u in urls)


def test_ac9_anchor_keyword_our_work():
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/some-page/">Our Work</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert len(candidates) == 1


def test_ac9_path_keyword_press():
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/press">Press</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert len(candidates) == 1


def test_ac9_anchor_yearbook():
    from lavandula.reports.candidate_filter import extract_candidates
    html = (
        '<html><body>'
        '<a href="/community/page">Community Report</a>'
        '</body></html>'
    )
    candidates = extract_candidates(
        html=html,
        base_url="https://example.org/",
        seed_etld1="example.org",
        referring_page_url="https://example.org/",
    )
    assert len(candidates) == 1
