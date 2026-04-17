"""HTML → extracted profile fields. Pure local transform; no network.

The CN profile page has evolved over time; this parser tries multiple
selector strategies for each field and returns `parse_status='partial'`
when some (but not all) fields are present, or `'unparsed'` when the
core fields (name) are missing entirely.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from . import config
from .url_normalize import normalize as normalize_url
from .url_utils import canonicalize_ein

STATE_ABBREV_RE = re.compile(r"\b([A-Z]{2})\b")
NTEE_RE = re.compile(r"\b([A-Z])([0-9]{1,3})\b")
MONEY_RE = re.compile(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)")


@dataclass
class ExtractedProfile:
    ein: str
    name: str
    mission: str | None = None
    website_url: str | None = None
    website_url_raw: str | None = None
    website_url_reason: str | None = None
    rating_stars: int | None = None
    overall_score: float | None = None
    beacons_completed: int | None = None
    rated: int = 0
    total_revenue: int | None = None
    total_expenses: int | None = None
    program_expense_pct: float | None = None
    ntee_major: str | None = None
    ntee_code: str | None = None
    cn_cause: str | None = None
    city: str | None = None
    state: str | None = None
    address: str | None = None
    parse_status: str = "ok"


def _soup(html: bytes | str) -> BeautifulSoup:
    """Construct a BeautifulSoup parser with no custom entity resolver.

    lxml >= 4.9.1 disables DTD loading by default; we never inject a
    custom resolver so XXE-style HTML is inert.
    """
    if isinstance(html, bytes):
        return BeautifulSoup(html, "lxml")
    return BeautifulSoup(html, "lxml")


def _text(node) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _first(soup: BeautifulSoup, selectors: list[str]) -> Any:
    for sel in selectors:
        node = soup.select_one(sel)
        if node is not None:
            return node
    return None


def _parse_money(value: str) -> int | None:
    if not value:
        return None
    m = MONEY_RE.search(value)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(round(num))


def _parse_percent(value: str) -> float | None:
    if not value:
        return None
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", value)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if v < 0 or v > 100:
        return None
    return v


def _extract_json_blocks(soup: BeautifulSoup) -> list[dict]:
    """Collect every JSON-LD and `__NEXT_DATA__`-style script block."""
    out: list[dict] = []
    for script in soup.find_all("script"):
        t = script.get("type", "")
        if t == "application/ld+json":
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, list):
                out.extend(d for d in data if isinstance(d, dict))
            elif isinstance(data, dict):
                out.append(data)
        elif script.get("id") == "__NEXT_DATA__":
            try:
                data = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict):
                out.append(data)
    return out


def _find_name(soup: BeautifulSoup, json_blocks: list[dict]) -> str | None:
    for block in json_blocks:
        if block.get("@type") in ("NGO", "Organization", "NonprofitOrganization"):
            name = block.get("name")
            if name:
                return name.strip()
        if "name" in block and "ein" in block:
            return str(block["name"]).strip()
    candidates = [
        "h1.orgName",
        "h1.organization-name",
        "h1[data-cy='org-name']",
        "h1",
        "meta[property='og:title']",
    ]
    for sel in candidates:
        node = soup.select_one(sel)
        if node is None:
            continue
        if sel.startswith("meta"):
            content = node.get("content", "").strip()
            if content:
                return content
        else:
            txt = _text(node)
            if txt:
                return txt
    title = soup.find("title")
    if title and title.text:
        return title.text.strip()
    return None


def _find_mission(soup: BeautifulSoup, json_blocks: list[dict]) -> str | None:
    for block in json_blocks:
        desc = block.get("description")
        if desc and isinstance(desc, str) and len(desc) > 20:
            return desc.strip()
    for sel in [
        "[data-cy='mission-statement']",
        ".mission-statement",
        ".org-mission",
        "section.mission p",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        if txt:
            return txt
    # Meta description fallback.
    meta = soup.select_one("meta[name='description']")
    if meta and meta.get("content"):
        return meta["content"].strip()
    return None


def _find_website(soup: BeautifulSoup, json_blocks: list[dict]) -> str | None:
    for block in json_blocks:
        url = block.get("url") or block.get("sameAs")
        if isinstance(url, list):
            for u in url:
                if isinstance(u, str) and "charitynavigator.org" not in u:
                    return u
        elif isinstance(url, str) and "charitynavigator.org" not in url:
            return url
    for sel in [
        "a[data-cy='org-website']",
        "a.org-website",
        "a[href*='/redirect?']",
        ".organization-website a",
    ]:
        node = soup.select_one(sel)
        if node and node.get("href"):
            return node["href"]
    # Find any external link labeled "Website" or similar.
    for a in soup.find_all("a", href=True):
        label = _text(a).lower()
        if "website" in label or "visit site" in label:
            return a["href"]
    return None


def _find_rating(soup: BeautifulSoup, json_blocks: list[dict]) -> tuple[int | None, float | None, int | None]:
    rating_stars = None
    overall_score = None
    beacons = None
    for block in json_blocks:
        agg = block.get("aggregateRating") if isinstance(block, dict) else None
        if isinstance(agg, dict):
            rv = agg.get("ratingValue")
            if isinstance(rv, (int, float)):
                overall_score = float(rv)
            elif isinstance(rv, str):
                try:
                    overall_score = float(rv)
                except ValueError:
                    pass
    # Look for a "Star rating X" / "X stars" signal in HTML.
    for sel in [
        "[data-cy='rating-stars']",
        ".rating-stars",
        ".star-rating",
        ".overall-rating",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        m = re.search(r"([1-4])\s*(?:-?\s*star|/\s*4)", txt, re.I)
        if m:
            rating_stars = int(m.group(1))
            break
        aria = node.get("aria-label", "") if node else ""
        m2 = re.search(r"([1-4])\s*star", aria, re.I)
        if m2:
            rating_stars = int(m2.group(1))
            break
    for sel in [
        "[data-cy='overall-score']",
        ".overall-score",
        ".rating-score",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        m = re.search(r"([0-9]{1,3}(?:\.[0-9]+)?)", txt)
        if m:
            try:
                val = float(m.group(1))
                if 0 <= val <= 100:
                    overall_score = val
                    break
            except ValueError:
                pass
    for sel in [
        "[data-cy='beacons-completed']",
        ".beacons-completed",
        ".beacons-count",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        m = re.search(r"([0-4])\s*of\s*4", txt, re.I)
        if m:
            beacons = int(m.group(1))
            break
    return rating_stars, overall_score, beacons


def _find_financials(soup: BeautifulSoup, json_blocks: list[dict]) -> tuple[int | None, int | None, float | None]:
    revenue = None
    expenses = None
    pct = None
    for sel in [
        "[data-cy='total-revenue']",
        ".total-revenue",
        "[data-cy='revenue']",
    ]:
        node = soup.select_one(sel)
        val = _parse_money(_text(node))
        if val is not None:
            revenue = val
            break
    for sel in [
        "[data-cy='total-expenses']",
        ".total-expenses",
    ]:
        node = soup.select_one(sel)
        val = _parse_money(_text(node))
        if val is not None:
            expenses = val
            break
    for sel in [
        "[data-cy='program-expense-ratio']",
        ".program-expense-ratio",
    ]:
        node = soup.select_one(sel)
        val = _parse_percent(_text(node))
        if val is not None:
            pct = val
            break
    # Table-based fallback: look for rows with labels.
    if revenue is None or expenses is None:
        for row in soup.select("tr, dl > *"):
            txt = _text(row).lower()
            if revenue is None and "total revenue" in txt:
                val = _parse_money(_text(row))
                if val is not None:
                    revenue = val
            if expenses is None and "total expenses" in txt:
                val = _parse_money(_text(row))
                if val is not None:
                    expenses = val
    return revenue, expenses, pct


def _find_location(soup: BeautifulSoup, json_blocks: list[dict]) -> tuple[str | None, str | None, str | None]:
    city = None
    state = None
    address = None
    for block in json_blocks:
        addr = block.get("address") if isinstance(block, dict) else None
        if isinstance(addr, dict):
            city = addr.get("addressLocality") or city
            state = addr.get("addressRegion") or state
            street = addr.get("streetAddress")
            if street:
                address = street
    for sel in [
        "[data-cy='org-address']",
        ".org-address",
        ".address",
        "address",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        if txt and address is None:
            address = txt
        if txt and state is None:
            m = STATE_ABBREV_RE.search(txt)
            if m:
                state = m.group(1)
    return city, state, address


def _find_ntee(soup: BeautifulSoup, json_blocks: list[dict]) -> tuple[str | None, str | None, str | None]:
    ntee_major = None
    ntee_code = None
    cause = None
    for sel in [
        "[data-cy='ntee-code']",
        ".ntee-code",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        m = NTEE_RE.search(txt)
        if m:
            ntee_major = m.group(1)
            ntee_code = f"{m.group(1)}{m.group(2)}"
    for sel in [
        "[data-cy='cn-cause']",
        ".cause-label",
        ".ntee-category",
    ]:
        node = soup.select_one(sel)
        txt = _text(node)
        if txt:
            cause = txt
            break
    # Whole-page fallback for ntee major.
    if ntee_major is None:
        body_text = soup.get_text(" ", strip=True)
        m = re.search(r"\bNTEE[:\s]+([A-Z])([0-9]{1,3})?\b", body_text)
        if m:
            ntee_major = m.group(1)
            if m.group(2):
                ntee_code = f"{m.group(1)}{m.group(2)}"
    return ntee_major, ntee_code, cause


def extract(html: bytes | str, *, ein: str) -> ExtractedProfile:
    """Parse CN profile HTML into an ExtractedProfile.

    Permissive: missing fields are set to None with parse_status='partial';
    a missing name downgrades to 'unparsed' and falls back to title text.
    """
    canonical_ein = canonicalize_ein(ein)
    soup = _soup(html)
    json_blocks = _extract_json_blocks(soup)

    name = _find_name(soup, json_blocks)
    mission = _find_mission(soup, json_blocks)
    website_raw = _find_website(soup, json_blocks)
    rating_stars, overall_score, beacons = _find_rating(soup, json_blocks)
    revenue, expenses, pct = _find_financials(soup, json_blocks)
    city, state, address = _find_location(soup, json_blocks)
    ntee_major, ntee_code, cause = _find_ntee(soup, json_blocks)

    website_url, reason = normalize_url(website_raw)

    rated = 1 if (rating_stars is not None or overall_score is not None) else 0

    parse_status = "ok"
    # Missing name is severe.
    if not name:
        name = f"(unknown ein {canonical_ein})"
        parse_status = "unparsed"
    else:
        # Core-field "partial" heuristic: no website AND no address AND no
        # revenue/rating suggests a truncated page.
        core_signals = [website_raw, address, state, revenue, rating_stars, overall_score]
        if sum(1 for x in core_signals if x) <= 1:
            parse_status = "partial"

    return ExtractedProfile(
        ein=canonical_ein,
        name=name,
        mission=mission,
        website_url=website_url,
        website_url_raw=website_raw,
        website_url_reason=reason,
        rating_stars=rating_stars,
        overall_score=overall_score,
        beacons_completed=beacons,
        rated=rated,
        total_revenue=revenue,
        total_expenses=expenses,
        program_expense_pct=pct,
        ntee_major=ntee_major,
        ntee_code=ntee_code,
        cn_cause=cause,
        city=city,
        state=state,
        address=address,
        parse_status=parse_status,
    )


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
