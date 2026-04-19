"""Candidate URL filter (AC2, AC3, AC4, AC8.1 link cap, AC12.3).

Given an HTML page and its referring URL, extract links, filter them
per the spec's ANCHOR_KEYWORDS / PATH_KEYWORDS / platform-signature
rules, and assign `attribution_confidence` per AC12.3.

The candidate cap (30 per org) is enforced at the extractor level —
callers aggregate multiple pages and should cap the final list again.
Per-page parse cap is MAX_PARSED_LINKS_PER_PAGE (10_000) per AC8.1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup  # type: ignore

from . import config
from .redirect_policy import etld1
from .url_redact import canonicalize_url

CANDIDATE_CAP_PER_ORG = config.CANDIDATE_CAP_PER_ORG
MAX_PARSED_LINKS_PER_PAGE = config.MAX_PARSED_LINKS_PER_PAGE
MAX_PDFS_PER_REPORT_SUBPAGE = config.MAX_PDFS_PER_REPORT_SUBPAGE


@dataclass(frozen=True)
class Candidate:
    url: str
    anchor_text: str
    referring_page_url: str
    discovered_via: str                 # 'homepage-link' | 'subpage-link' | 'sitemap' | 'hosting-platform'
    hosting_platform: str | None        # 'issuu'|'flipsnack'|'canva'|'own-domain'|None
    attribution_confidence: str         # 'own_domain' | 'platform_verified' | 'platform_unverified'


_PLATFORM_HOSTS = {
    "issuu.com": "issuu",
    "flipsnack.com": "flipsnack",
    "canva.com": "canva",
    "www.canva.com": "canva",
}


def _platform_for(host: str) -> str | None:
    h = host.lower()
    if h in _PLATFORM_HOSTS:
        return _PLATFORM_HOSTS[h]
    if h.endswith(".issuu.com"):
        return "issuu"
    if h.endswith(".flipsnack.com"):
        return "flipsnack"
    if h.endswith(".canva.com"):
        return "canva"
    return None


def _anchor_matches(anchor: str) -> bool:
    a = (anchor or "").lower()
    return any(kw in a for kw in config.ANCHOR_KEYWORDS)


def _path_matches(path: str) -> bool:
    p = (path or "").lower()
    return any(kw in p for kw in config.PATH_KEYWORDS)


def _pdf_like(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _is_ugc_referrer(referring_page_url: str) -> bool:
    """AC12.3 — discover-via-UGC-surface → platform_unverified."""
    path = (urlsplit(referring_page_url).path or "").lower()
    return any(sig in path for sig in config.UGC_PATH_SIGNATURES)


def _classify_link(
    *,
    anchor: str,
    href: str,
    referring_page_url: str,
    seed_etld1: str,
    discovered_via: str,
    parent_is_report_anchor: bool = False,
) -> Candidate | None:
    """Return a Candidate if `href` matches any filter, else None.

    TICK-001: When `parent_is_report_anchor` is True (caller has
    determined the containing subpage itself matched report anchor/
    path patterns), accept any same-eTLD+1 PDF-suffix link regardless
    of anchor text or path keywords. Platform allowlist (AC12.2/
    AC12.3) and cross-origin policy preserved exactly.
    """
    parsed = urlsplit(href)
    host = parsed.hostname or ""
    platform = _platform_for(host)

    # Platform URL: always a candidate IF not in UGC context.
    if platform is not None:
        attribution = "platform_verified"
        if _is_ugc_referrer(referring_page_url):
            attribution = "platform_unverified"
        elif discovered_via not in ("homepage-link", "subpage-link", "hosting-platform"):
            attribution = "platform_unverified"
        return Candidate(
            url=href,
            anchor_text=anchor or "",
            referring_page_url=referring_page_url,
            discovered_via="hosting-platform",
            hosting_platform=platform,
            attribution_confidence=attribution,
        )

    # Non-platform link: must be on-domain AND match anchor/path filter.
    link_etld1 = etld1(host) if host else seed_etld1
    if host and link_etld1 != seed_etld1:
        # Cross-origin non-platform link: out of scope.
        return None

    anchor_hit = _anchor_matches(anchor)
    path_hit = _path_matches(parsed.path)
    pdf_with_anchor = _pdf_like(href) and anchor_hit
    # TICK-001: relaxed PDF acceptance on report-anchor subpages.
    pdf_on_report_subpage = parent_is_report_anchor and _pdf_like(href)
    if not (anchor_hit or path_hit or pdf_with_anchor or pdf_on_report_subpage):
        return None

    hosting = "own-domain" if not host or link_etld1 == seed_etld1 else None
    return Candidate(
        url=href,
        anchor_text=anchor or "",
        referring_page_url=referring_page_url,
        discovered_via=discovered_via,
        hosting_platform=hosting,
        attribution_confidence="own_domain",
    )


def extract_candidates(
    *,
    html: str,
    base_url: str,
    seed_etld1: str,
    referring_page_url: str,
    discovered_via: str = "homepage-link",
    parent_is_report_anchor: bool = False,
) -> list[Candidate]:
    """Parse `html`, iterate `<a>` tags up to MAX_PARSED_LINKS_PER_PAGE,
    return a deduped list capped at CANDIDATE_CAP_PER_ORG.

    The caller aggregates across homepage + subpages and should apply a
    second cap over the final union.

    TICK-001: When `parent_is_report_anchor` is True, PDF-suffix links
    inside this page are accepted without requiring anchor/path
    keyword matches (bounded by MAX_PDFS_PER_REPORT_SUBPAGE).
    Non-PDF links still require the strict filter; homepage and
    non-report-anchor subpages are unchanged.
    """
    soup = BeautifulSoup(html or "", "lxml")
    candidates: list[Candidate] = []
    seen: set[str] = set()
    relaxed_pdf_count = 0
    anchors = soup.find_all("a", href=True, limit=MAX_PARSED_LINKS_PER_PAGE)
    for a in anchors:
        href_raw = a.get("href") or ""
        href = urljoin(base_url, href_raw.strip())
        anchor_text = a.get_text(" ", strip=True) or ""
        # TICK-001: enforce per-subpage PDF cap BEFORE _classify_link
        # for links that would only pass via the relaxed rule.
        effective_parent_flag = parent_is_report_anchor
        if (
            parent_is_report_anchor
            and _pdf_like(href)
            and not _anchor_matches(anchor_text)
            and not _path_matches(urlsplit(href).path)
            and relaxed_pdf_count >= MAX_PDFS_PER_REPORT_SUBPAGE
        ):
            # Cap reached — don't let this link through via relaxed rule.
            # Disable the flag locally so the strict filter still applies.
            effective_parent_flag = False
        c = _classify_link(
            anchor=anchor_text,
            href=href,
            referring_page_url=referring_page_url,
            seed_etld1=seed_etld1,
            discovered_via=discovered_via,
            parent_is_report_anchor=effective_parent_flag,
        )
        if c is None:
            continue
        # Dedup on canonical form.
        canonical = canonicalize_url(c.url)
        if canonical in seen:
            continue
        seen.add(canonical)
        candidates.append(c)
        # Track relaxed-rule admissions for the per-subpage cap.
        if (
            parent_is_report_anchor
            and _pdf_like(href)
            and not _anchor_matches(anchor_text)
            and not _path_matches(urlsplit(href).path)
            and c.hosting_platform == "own-domain"
        ):
            relaxed_pdf_count += 1
        if len(candidates) >= CANDIDATE_CAP_PER_ORG:
            break
    return candidates


__all__ = [
    "Candidate",
    "extract_candidates",
    "CANDIDATE_CAP_PER_ORG",
    "MAX_PARSED_LINKS_PER_PAGE",
]
