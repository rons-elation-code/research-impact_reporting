"""Candidate URL filter (AC2, AC3, AC4, AC8.1 link cap, AC12.3).

Given an HTML page and its referring URL, extract links, filter them
per the spec's ANCHOR_KEYWORDS / PATH_KEYWORDS / platform-signature
rules, and assign `attribution_confidence` per AC12.3.

The candidate cap (30 per org) is enforced at the extractor level —
callers aggregate multiple pages and should cap the final list again.
Per-page parse cap is MAX_PARSED_LINKS_PER_PAGE (10_000) per AC8.1.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag  # type: ignore

from . import config
from .decisions_log import log_decision
from .filename_grader import grade_filename
from .redirect_policy import etld1
from .taxonomy import current as _current_taxonomy
from .url_redact import canonicalize_url


class Decision(enum.Enum):
    ACCEPT_FILENAME_STRONG = "accept_filename_strong"
    ACCEPT_MIDDLE = "accept_middle"
    DROP_FILENAME_REJECT = "drop_filename_reject"
    DROP_NO_SIGNAL = "drop_no_signal"
    ACCEPT_PLATFORM = "accept_platform"
    DROP_CROSS_ORIGIN = "drop_cross_origin"

CANDIDATE_CAP_PER_ORG = config.CANDIDATE_CAP_PER_ORG


def _effective_anchor_text(a: Tag) -> str:
    """Combine visible text, title, aria-label, and img alt into one string."""
    visible = a.get_text(" ", strip=True) or ""
    title = (a.get("title") or "").strip()
    aria = (a.get("aria-label") or "").strip()
    alts = " ".join((img.get("alt") or "").strip() for img in a.find_all("img"))
    parts = [p for p in (visible, title, aria, alts.strip()) if p]
    return " ".join(parts).strip()

MAX_PARSED_LINKS_PER_PAGE = config.MAX_PARSED_LINKS_PER_PAGE
MAX_PDFS_PER_REPORT_SUBPAGE = config.MAX_PDFS_PER_REPORT_SUBPAGE


def _seed_brand_label(seed_etld1: str) -> str:
    """Return the seed's first 'meaningful' label.

    Example: seed_etld1='sagehillschool.org' → 'sagehillschool'.
             seed_etld1='example.co.uk' → 'example'.
    """
    return (seed_etld1 or "").split(".", 1)[0].lower()


def _host_first_label(host: str) -> str:
    """Return the host's first label, skipping common prefixes.

    Example: 'sagehillschool.myschoolapp.com' → 'sagehillschool'.
             'www.sagehillschool.myschoolapp.com' → 'sagehillschool'.
    """
    if not host:
        return ""
    parts = host.lower().split(".")
    # Skip leading 'www' if present.
    while parts and parts[0] in ("www", "www2"):
        parts = parts[1:]
    return parts[0] if parts else ""


# TICK-004 AC9: anti-noise patterns to reject from sitemap-derived
# candidates BEFORE keyword matching. CMS-generated sitemaps
# (especially WordPress news blogs) can list thousands of URLs;
# without this filter they'd flood the candidate pool with weak
# matches and reduce recall on actual reports via the per-org cap.
_SITEMAP_NOISE_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".mp4", ".mp3", ".zip", ".css", ".js",
)
_SITEMAP_NOISE_PATH_PATTERNS = (
    "/feed/", "/feed.xml", "/rss", "/atom.xml",
    "/category/", "/tag/", "/author/",
    "/wp-json/", "/xmlrpc.php",
)
import re as _re
_SITEMAP_ARCHIVE_RE = _re.compile(r"(/page/\d+/?|/\d{4}/\d{2}/?)$")


def _is_sitemap_noise(url: str) -> bool:
    """TICK-004 AC9: return True if URL should be dropped from
    sitemap-derived candidate pool before further classification."""
    path_lower = (urlsplit(url).path or "").lower()
    for suffix in _SITEMAP_NOISE_SUFFIXES:
        if path_lower.endswith(suffix):
            return True
    for pattern in _SITEMAP_NOISE_PATH_PATTERNS:
        if pattern in path_lower:
            return True
    if _SITEMAP_ARCHIVE_RE.search(path_lower):
        return True
    return False


def _is_cms_subdomain_match(host: str, seed_etld1: str) -> bool:
    """TICK-002 Fix 1: cross-origin host whose first subdomain label
    matches the seed's brand label (e.g.,
    sagehillschool.myschoolapp.com for seed sagehillschool.org).

    Rejects when the seed label is too short or in the generic-label
    blocklist, to prevent over-broad matching (e.g., seed 'www.abc.org'
    would otherwise match 'abc.anyhost.com').
    """
    seed_label = _seed_brand_label(seed_etld1)
    if len(seed_label) < config.CMS_LABEL_MIN_CHARS:
        return False
    if seed_label in config.CMS_LABEL_BLOCKLIST:
        return False
    host_label = _host_first_label(host)
    if len(host_label) < config.CMS_LABEL_MIN_CHARS:
        return False
    return host_label == seed_label


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


def _basename_from_url(url: str) -> str:
    path = urlsplit(url).path or ""
    segment = unquote(path.rsplit("/", 1)[-1]).strip()
    return segment


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
    is_cross_origin = bool(host and link_etld1 != seed_etld1)

    # TICK-002 Fix 1: cross-origin PDFs whose first subdomain label
    # matches the seed's brand label (CMS pattern) are accepted.
    # Only fires when parent is a report-anchor subpage AND the link
    # is a PDF — consistent with TICK-001's scoping.
    is_cms_match = (
        is_cross_origin
        and parent_is_report_anchor
        and _pdf_like(href)
        and _is_cms_subdomain_match(host, seed_etld1)
    )

    if is_cross_origin and not is_cms_match:
        return None

    tax = _current_taxonomy()

    # Filename heuristic grading
    basename = _basename_from_url(href)
    filename_score = (
        grade_filename(basename, tax) if basename else tax.thresholds.base_score
    )

    anchor_hit = _anchor_matches(anchor)

    # Tiered path matching — case-insensitive
    path_lower = (parsed.path or "").lower()
    strong_path_hit = any(kw in path_lower for kw in tax.path_keywords_strong)
    weak_path_hit = any(kw in path_lower for kw in tax.path_keywords_weak)

    def _log(decision: str, reason: str) -> None:
        log_decision({
            "url": href,
            "referring_page": referring_page_url,
            "basename": basename,
            "filename_score": round(filename_score, 3),
            "triage": (
                "accept" if filename_score >= tax.thresholds.filename_score_accept
                else "reject" if filename_score <= tax.thresholds.filename_score_reject
                else "middle"
            ),
            "strong_path_hit": strong_path_hit,
            "weak_path_hit": weak_path_hit,
            "anchor_text": anchor or "",
            "anchor_hit": anchor_hit,
            "decision": decision,
            "reason": reason,
        })

    # Three-tier filename triage
    if filename_score <= tax.thresholds.filename_score_reject:
        _log("drop", "filename_score<=reject")
        return None

    pdf_with_anchor = _pdf_like(href) and anchor_hit
    # TICK-001: relaxed PDF acceptance on report-anchor subpages.
    pdf_on_report_subpage = parent_is_report_anchor and _pdf_like(href)

    pass_ = (
        filename_score >= tax.thresholds.filename_score_accept
        or anchor_hit
        or strong_path_hit
        or (
            weak_path_hit
            and (
                anchor_hit
                or filename_score >= tax.thresholds.filename_score_weak_path_min
            )
        )
        or pdf_with_anchor
        or pdf_on_report_subpage
    )
    if not pass_:
        _log("drop", "no_signal")
        return None

    _log("accept", "signal_match")

    if is_cms_match:
        # TICK-002: CMS-subdomain PDF, treated as verified-platform
        # equivalent since the subdomain label matches the seed's
        # brand identity.
        return Candidate(
            url=href,
            anchor_text=anchor or "",
            referring_page_url=referring_page_url,
            discovered_via=discovered_via,
            hosting_platform="own-cms",
            attribution_confidence="platform_verified",
        )

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
    # TICK-002 Fix 4: i18n-dedup seen-set records paths WITH
    # leading-locale-prefix stripped. If /our-impact/ is seen first,
    # later /tl/our-impact/ hits the same dedup key and is dropped.
    seen_delocalized: set[str] = set()
    relaxed_pdf_count = 0
    anchors = soup.find_all("a", href=True, limit=MAX_PARSED_LINKS_PER_PAGE)
    for a in anchors:
        href_raw = a.get("href") or ""
        href = urljoin(base_url, href_raw.strip())
        anchor_text = _effective_anchor_text(a)
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
        canonical = canonicalize_url(c.url)
        if canonical in seen:
            continue
        # TICK-002 Fix 4: i18n dedup — key by de-localized canonical
        # so /our-impact/ and /tl/our-impact/ collapse regardless of
        # encounter order. First occurrence (in document order) wins.
        parsed_for_dedup = urlsplit(canonical)
        path_parts = (parsed_for_dedup.path or "").strip("/").split("/")
        if path_parts and path_parts[0].lower() in config.LOCALE_PREFIXES:
            delocalized_path = "/" + "/".join(path_parts[1:])
            delocalized_key = (
                f"{parsed_for_dedup.scheme}://{parsed_for_dedup.netloc}"
                f"{delocalized_path}"
            )
        else:
            delocalized_key = canonical
        if delocalized_key in seen_delocalized:
            continue
        seen.add(canonical)
        seen_delocalized.add(delocalized_key)
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


def classify_sitemap_url(
    *,
    url: str,
    seed_etld1: str,
    referring_page_url: str,
) -> Candidate | None:
    """TICK-004: classify a URL discovered via sitemap.xml.

    Applies the AC10 cross-origin decision table + AC9 anti-noise
    filter + URL-path/PDF-suffix keyword match (no anchor text
    exists in sitemap-derived discovery).
    """
    # Anti-noise rejection comes BEFORE anything else (AC9).
    if _is_sitemap_noise(url):
        return None

    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if not host:
        return None

    platform = _platform_for(host)
    # Platform allowlist: sitemap-only discovery → platform_unverified
    # per AC12.3 (the sitemap is not a homepage-anchor, so we can't
    # verify the linking-org owns the platform content). Platforms
    # are content-hosts; their own path structure IS the filter
    # signal (issuu.com/X/docs/Y, canva.com/design/Z), so no
    # additional keyword match required — mirrors homepage classify.
    if platform is not None:
        return Candidate(
            url=url,
            anchor_text="",
            referring_page_url=referring_page_url,
            discovered_via="sitemap",
            hosting_platform=platform,
            attribution_confidence="platform_unverified",
        )

    link_etld1 = etld1(host) if host else seed_etld1
    is_cross_origin = bool(host and link_etld1 != seed_etld1)

    # CMS-subdomain match (TICK-002 Fix 1), but for sitemap context
    # we only enforce the label-match if the URL is a PDF — non-PDF
    # pages on a CMS subdomain that happens to match the seed's
    # brand are still out of scope for sitemap-only discovery.
    if is_cross_origin:
        if _pdf_like(url) and _is_cms_subdomain_match(host, seed_etld1):
            return Candidate(
                url=url,
                anchor_text="",
                referring_page_url=referring_page_url,
                discovered_via="sitemap",
                hosting_platform="own-cms",
                attribution_confidence="platform_verified",
            )
        # Any other cross-origin host → drop (AC10).
        return None

    # Same-eTLD+1 non-platform URL: apply tiered path + filename filter.
    tax = _current_taxonomy()
    path_lower = (parsed.path or "").lower()
    strong_path_hit = any(kw in path_lower for kw in tax.path_keywords_strong)
    weak_path_hit = any(kw in path_lower for kw in tax.path_keywords_weak)
    basename = _basename_from_url(url)
    filename_score = (
        grade_filename(basename, tax) if basename else tax.thresholds.base_score
    )
    if filename_score <= tax.thresholds.filename_score_reject:
        return None
    pass_ = (
        _pdf_like(url)
        or strong_path_hit
        or (
            weak_path_hit
            and filename_score >= tax.thresholds.filename_score_weak_path_min
        )
    )
    if not pass_:
        return None

    return Candidate(
        url=url,
        anchor_text="",
        referring_page_url=referring_page_url,
        discovered_via="sitemap",
        hosting_platform="own-domain",
        attribution_confidence="own_domain",
    )


__all__ = [
    "Candidate",
    "extract_candidates",
    "classify_sitemap_url",
    "CANDIDATE_CAP_PER_ORG",
    "MAX_PARSED_LINKS_PER_PAGE",
]
