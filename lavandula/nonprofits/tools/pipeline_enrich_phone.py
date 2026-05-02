"""CLI entry point for phone number enrichment (Spec 0031).

Usage:
    python -m lavandula.nonprofits.tools.pipeline_enrich_phone --state TX [OPTIONS]

Finds phone numbers for resolved orgs that lack one, using search snippets
and website contact pages.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time

from lavandula.common.db import make_app_engine
from lavandula.common.secrets import SecretUnavailable, get_serpex_api_key
from lavandula.nonprofits.phone_extract import extract_phone
from lavandula.nonprofits.web_search import (
    RateLimiter,
    SearchConfig,
    SearchError,
    get_search_stats,
    reset_search_stats,
    search,
    validate_engines,
)

log = logging.getLogger(__name__)

_SCHEMA = "lava_corpus"

_CONTACT_PATHS = ["/contact", "/about", "/about-us"]


def _fetch_contact_page(base_url: str) -> str | None:
    """Fetch contact/about page from org website, return text or None."""
    import requests as http_requests

    base_url = base_url.rstrip("/")
    for path in _CONTACT_PATHS:
        url = base_url + path
        try:
            resp = http_requests.get(url, timeout=15, allow_redirects=True)
            if resp.status_code == 200 and len(resp.content) < 2_000_000:
                text = resp.text
                text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.I)
                text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:10000]
        except Exception:
            continue
    return None


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="pipeline_enrich_phone",
        description="Enrich resolved orgs with phone numbers via search.",
    )
    parser.add_argument("--state", default=None, help="Filter to orgs in this state")
    parser.add_argument("--limit", type=int, default=0, help="Max orgs to process (0 = no limit)")
    parser.add_argument("--search-engines", default="brave", help="Comma-separated engines")
    parser.add_argument("--allow-tollfree", action="store_true", help="Allow toll-free numbers")
    parser.add_argument("--serpex-api-key", default=None, help="Serpex API key (literal)")
    parser.add_argument("--search-qps", type=float, default=1.0, help="Search queries per second")
    args = parser.parse_args(argv)

    try:
        engines = validate_engines(args.search_engines.split(","))
    except ValueError as exc:
        parser.error(str(exc))

    try:
        if args.serpex_api_key:
            api_key = args.serpex_api_key
        else:
            api_key = get_serpex_api_key()
    except SecretUnavailable as exc:
        print(f"ERROR: Serpex API key unavailable: {exc}", file=sys.stderr)
        sys.exit(1)

    search_config = SearchConfig(
        backend="serpex",
        engines=engines,
        api_key=api_key,
        qps=args.search_qps,
    )
    rate_limiter = RateLimiter(args.search_qps)

    from sqlalchemy import text

    engine = make_app_engine()
    reset_search_stats()

    try:
        # Load resolved orgs without phone
        sql = (
            f"SELECT ein, name, city, state, website_url "
            f"FROM {_SCHEMA}.nonprofits_seed "
            f"WHERE resolver_status = 'resolved' AND phone IS NULL"
        )
        params: dict = {}
        if args.state:
            sql += " AND state = :state"
            params["state"] = args.state
        sql += " ORDER BY ein"
        if args.limit > 0:
            sql += " LIMIT :lim"
            params["lim"] = args.limit

        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()

        orgs = [
            {
                "ein": row[0],
                "name": row[1] or "",
                "city": row[2] or "",
                "state": row[3] or "",
                "website_url": row[4] or "",
            }
            for row in rows
        ]

        log.info("Loaded %d orgs for phone enrichment", len(orgs))
        if not orgs:
            print("No orgs to process.")
            return

        t_start = time.monotonic()
        found_snippet = 0
        found_website = 0
        not_found = 0

        for i, org in enumerate(orgs, 1):
            ein = org["ein"]
            name = org["name"]
            city = org["city"]
            state = org["state"]
            website_url = org["website_url"]

            # Search for phone
            sanitized_name = re.sub(r'"', "", name).strip()
            query = f'"{sanitized_name}" {city} {state} phone number'

            phone = None
            phone_source = None

            try:
                results = search(query, config=search_config, rate_limiter=rate_limiter)
            except SearchError as exc:
                log.warning("Search error for ein=%s: %s", ein, exc)
                not_found += 1
                continue

            # Priority 1: extract from snippets
            all_snippets = " ".join(r.snippet for r in results if r.snippet)
            phone = extract_phone(
                all_snippets,
                allow_tollfree=args.allow_tollfree,
                org_name=name,
            )
            if phone:
                phone_source = "search_snippet"

            # Priority 2: fetch website contact page
            if not phone and website_url:
                page_text = _fetch_contact_page(website_url)
                if page_text:
                    phone = extract_phone(
                        page_text,
                        allow_tollfree=args.allow_tollfree,
                        org_name=name,
                    )
                    if phone:
                        phone_source = "website_extract"

            if phone:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            f"UPDATE {_SCHEMA}.nonprofits_seed "
                            f"SET phone = :phone, phone_source = :source "
                            f"WHERE ein = :ein"
                        ),
                        {"phone": phone, "source": phone_source, "ein": ein},
                    )
                if phone_source == "search_snippet":
                    found_snippet += 1
                else:
                    found_website += 1
                log.info("[%d] ein=%s phone=%s source=%s", i, ein, phone, phone_source)
            else:
                not_found += 1
                log.debug("[%d] ein=%s no phone found", i, ein)

        wall_time = time.monotonic() - t_start
        total = found_snippet + found_website + not_found

        print(f"\n--- Phone Enrichment Summary ---")
        print(f"Wall time: {wall_time:.1f}s")
        print(f"Processed: {total}")
        print(f"Found (snippet): {found_snippet}")
        print(f"Found (website): {found_website}")
        print(f"Not found: {not_found}")
        if total > 0:
            print(f"Hit rate: {(found_snippet + found_website) / total * 100:.1f}%")

        s_stats = get_search_stats()
        if s_stats.estimated_credits > 0:
            print(f"Estimated credits: {s_stats.estimated_credits}")

    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
