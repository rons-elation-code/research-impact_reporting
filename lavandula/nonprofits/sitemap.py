"""Sitemap index + child sitemap parser with XXE defense.

Uses defusedxml for entity + DTD hardening. Falls back to an explicitly-
configured lxml parser if defusedxml is unavailable (rare in our env but
accommodated for Claude CRITICAL mitigation path).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

from . import config
from .url_utils import canonicalize_ein, ein_from_profile_url


class SitemapError(RuntimeError):
    """XML parsing or schema validation failure — caller halts."""


@dataclass(frozen=True)
class SitemapLoc:
    url: str
    lastmod: str | None


def _parse_xml_bytes(data: bytes):
    """Parse XML with XXE/DTD/external-entity safely disabled.

    Returns an ElementTree.Element. Raises SitemapError on malformed XML.
    """
    try:
        from defusedxml.ElementTree import fromstring as _fs
        return _fs(data)
    except ImportError:
        pass
    # Fallback: lxml explicitly locked down.
    try:
        from lxml import etree
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
            load_dtd=False,
            dtd_validation=False,
            recover=False,
        )
        return etree.fromstring(data, parser=parser)
    except Exception as exc:
        raise SitemapError(f"XML parse failed: {exc!r}") from exc
    except ImportError as exc:
        raise SitemapError("no safe XML parser available") from exc


def _parse_xml(data: bytes):
    try:
        return _parse_xml_bytes(data)
    except SitemapError:
        raise
    except Exception as exc:
        raise SitemapError(f"XML parse failed: {exc!r}") from exc


def _iter_locs(root) -> Iterator[SitemapLoc]:
    """Yield (url, lastmod) tuples from any sitemap or sitemap-index root."""
    # Namespace-agnostic: strip any `{ns}` prefix.
    for elem in root.iter():
        tag = _localname(elem.tag)
        if tag == "sitemap" or tag == "url":
            loc_text = None
            lastmod_text = None
            for child in elem:
                c_tag = _localname(child.tag)
                if c_tag == "loc" and child.text:
                    loc_text = child.text.strip()
                elif c_tag == "lastmod" and child.text:
                    lastmod_text = child.text.strip()
            if loc_text:
                yield SitemapLoc(url=loc_text, lastmod=lastmod_text)


def _localname(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_sitemap_index(xml_bytes: bytes) -> list[str]:
    """Return the list of child-sitemap URLs from a sitemap-index XML.

    Rejects URLs whose host is not our configured site host.
    """
    root = _parse_xml(xml_bytes)
    urls: list[str] = []
    for loc in _iter_locs(root):
        from urllib.parse import urlparse
        p = urlparse(loc.url)
        if p.scheme != "https":
            continue
        if p.hostname != config.SITE_HOST:
            continue
        urls.append(loc.url)
    if not urls:
        raise SitemapError("sitemap index has no child sitemaps on expected host")
    return urls


def parse_child_sitemap(xml_bytes: bytes) -> list[SitemapLoc]:
    """Return every /ein/{9-digit} URL from a child sitemap.

    Malformed/non-matching entries are silently filtered; callers using
    the returned tuples can assume the URL matches /ein/\\d{9}$.
    """
    root = _parse_xml(xml_bytes)
    out: list[SitemapLoc] = []
    for loc in _iter_locs(root):
        ein = ein_from_profile_url(loc.url)
        if ein is None:
            continue
        try:
            canonicalize_ein(ein)
        except ValueError:
            continue
        out.append(loc)
    return out


def enumerate_sitemap_entries(
    *,
    index_xml: bytes,
    child_fetcher,  # callable url -> bytes
    robots_policy,
    sitemap_label_from_url=None,
) -> Iterator[tuple[str, str, str | None]]:
    """Yield (ein, source_sitemap, lastmod) tuples.

    `child_fetcher(url)` must return the XML body for a child sitemap.
    Disallowed EINs (per robots_policy AND the config floor) are filtered
    BEFORE being yielded.
    """
    if sitemap_label_from_url is None:
        def sitemap_label_from_url(u: str) -> str:
            return u.rsplit("/", 1)[-1] or u

    child_urls = parse_sitemap_index(index_xml)
    seen: set[str] = set()
    for child_url in child_urls:
        label = sitemap_label_from_url(child_url)
        body = child_fetcher(child_url)
        if body is None:
            continue
        locs = parse_child_sitemap(body)
        for loc in locs:
            ein = ein_from_profile_url(loc.url)
            if not ein:
                continue
            if ein in seen:
                continue
            # Floor disallow
            if ein in config.DISALLOWED_EINS:
                continue
            # Robots disallow
            if robots_policy is not None:
                if not robots_policy.is_allowed(f"/ein/{ein}"):
                    continue
            seen.add(ein)
            yield ein, label, loc.lastmod
