"""nptechforgood crawler — REST API based, throttled, resumable.

Usage:
    python crawler.py --limit 5          # test run: fetch 5 posts
    python crawler.py --page 1 --pages 1 # one page of 100
    python crawler.py                    # full archive, resumable
    python crawler.py --taxonomy         # fetch categories/tags only
    python crawler.py --pages-endpoint   # fetch static Pages (About etc.)
"""
import argparse
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import config
from http_client import ThrottledClient, setup_logging, DailyCapReached

log = logging.getLogger("nptech.crawler")


def normalize_photon_url(url: str) -> str:
    """Strip Jetpack Photon resize/ssl query params; return best-available URL."""
    if not url:
        return url
    p = urlparse(url)
    # For i0/i1/i2.wp.com URLs, dropping the query gets the full-size image.
    if p.netloc.endswith("wp.com"):
        return urlunparse(p._replace(query=""))
    return url


def checkpoint_path() -> Path:
    return config.STATE / "crawler_checkpoint.json"


def load_checkpoint() -> dict:
    p = checkpoint_path()
    if p.exists():
        return json.loads(p.read_text())
    return {"fetched_post_ids": [], "last_page": 0, "total_pages": None}


def save_checkpoint(cp: dict) -> None:
    checkpoint_path().parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path().write_text(json.dumps(cp, indent=2))


def fetch_taxonomy(client: ThrottledClient) -> None:
    """Fetch category/tag lookups. Cheap and informative."""
    for tax in ("categories", "tags"):
        log.info("fetching %s", tax)
        all_items = []
        page = 1
        while True:
            resp = client.get(
                f"{config.API}/{tax}",
                params={"per_page": config.PER_PAGE, "page": page},
            )
            if resp.status_code == 400:  # past the last page
                break
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break
            all_items.extend(items)
            total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        out = config.STATE / f"{tax}.json"
        out.write_text(json.dumps(all_items, indent=2))
        log.info("saved %d %s to %s", len(all_items), tax, out)


def fetch_media(client: ThrottledClient, media_id: int) -> dict | None:
    """Fetch a single media item. Cached by ID."""
    path = config.RAW_MEDIA / f"{media_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    resp = client.get(f"{config.API}/media/{media_id}")
    if resp.status_code in (401, 403, 404):
        log.warning("media %s: %s (skipping)", media_id, resp.status_code)
        return None
    resp.raise_for_status()
    data = resp.json()
    path.write_text(json.dumps(data, indent=2))
    return data


IMG_RE = re.compile(r'<img\s[^>]*>', re.IGNORECASE)
SRC_RE = re.compile(r'\bsrc="([^"]+)"', re.IGNORECASE)
CLASS_ID_RE = re.compile(r'\bclass="[^"]*wp-image-(\d+)', re.IGNORECASE)


def extract_image_refs(html: str) -> list[dict]:
    """Parse <img> tags from post HTML and return [{id, src}, ...]."""
    refs = []
    for m in IMG_RE.finditer(html or ""):
        tag = m.group(0)
        src_m = SRC_RE.search(tag)
        id_m = CLASS_ID_RE.search(tag)
        if not src_m:
            continue
        refs.append({
            "id": int(id_m.group(1)) if id_m else None,
            "src": normalize_photon_url(src_m.group(1)),
            "src_original": src_m.group(1),
        })
    return refs


def download_image(client: ThrottledClient, url: str, post_id: int, idx: int) -> str | None:
    """Download an image, return local filename (relative to raw/images)."""
    p = urlparse(url)
    suffix = Path(p.path).suffix.lower() or ".bin"
    filename = f"{post_id}-{idx:02d}{suffix}"
    dest = config.RAW_IMAGES / filename
    if dest.exists():
        return filename
    resp = client.get(url, stream=True)
    if resp.status_code != 200:
        log.warning("image fetch failed %s: %s", url, resp.status_code)
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
    return filename


def fetch_pages(client: ThrottledClient) -> None:
    """Fetch the Pages endpoint (static pages: About, Services, etc.).

    Pages are saved under raw/posts/ with the same structure as posts so
    extract.py processes them identically. They get classified as 'page'
    by a title signal rather than a category.
    """
    log.info("fetching /pages")
    page = 1
    fetched = 0
    while True:
        resp = client.get(
            f"{config.API}/pages",
            params={
                "per_page": config.PER_PAGE,
                "page": page,
                "_fields": config.POST_FIELDS,
            },
        )
        if resp.status_code == 400:
            break
        resp.raise_for_status()
        items = resp.json()
        if not items:
            break
        total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
        total = int(resp.headers.get("X-WP-Total", "0"))
        log.info("pages endpoint page %d/%d (total: %d)", page, total_pages, total)
        for item in items:
            # Skip items already fetched (resume after interruption)
            if (config.RAW_POSTS / f"{item['id']}.json").exists():
                continue
            # Tag the record so extract can distinguish Pages from Posts
            item.setdefault("categories", [])
            item["_is_static_page"] = True
            process_post(client, item)
            fetched += 1
        if page >= total_pages:
            break
        page += 1
    log.info("fetched %d pages", fetched)


def fetch_posts_page(client: ThrottledClient, page: int) -> tuple[list[dict], int]:
    """Fetch one page of posts. Returns (items, total_pages)."""
    resp = client.get(
        f"{config.API}/posts",
        params={
            "per_page": config.PER_PAGE,
            "page": page,
            "orderby": "date",
            "order": "desc",
            "_fields": config.POST_FIELDS,
        },
    )
    resp.raise_for_status()
    total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
    total_posts = int(resp.headers.get("X-WP-Total", "0"))
    log.info("page %d/%d (total posts: %d)", page, total_pages, total_posts)
    return resp.json(), total_pages


def process_post(client: ThrottledClient, post: dict) -> None:
    """Save raw post JSON, expand media where alt is missing, download images."""
    pid = post["id"]
    raw_path = config.RAW_POSTS / f"{pid}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    # Extract image refs from content
    html = (post.get("content") or {}).get("rendered", "")
    refs = extract_image_refs(html)

    # Featured media
    fm_id = post.get("featured_media")
    if fm_id:
        refs.insert(0, {"id": fm_id, "src": None, "src_original": None,
                        "featured": True})

    # Fetch media metadata for each referenced image (caches by ID)
    media_map = {}
    for ref in refs:
        if ref.get("id"):
            m = fetch_media(client, ref["id"])
            if m:
                media_map[ref["id"]] = m
                if ref.get("featured") and not ref.get("src"):
                    src = m.get("source_url")
                    if src:
                        ref["src"] = normalize_photon_url(src)
                        ref["src_original"] = src

    # Download images
    if config.DOWNLOAD_IMAGES:
        for idx, ref in enumerate(refs):
            if ref.get("src"):
                ref["local_file"] = download_image(client, ref["src"], pid, idx)

    # Save post + image refs together
    record = {"post": post, "image_refs": refs}
    raw_path.write_text(json.dumps(record, indent=2))
    log.info("post %d (%s): %d images", pid, post.get("slug", "")[:60], len(refs))


def crawl(limit: int | None, start_page: int | None, max_pages: int | None,
          taxonomy: bool, pages_endpoint: bool) -> None:
    client = ThrottledClient()

    if taxonomy:
        fetch_taxonomy(client)
        return

    if pages_endpoint:
        fetch_pages(client)
        return

    cp = load_checkpoint()
    page = start_page or (cp["last_page"] + 1) or 1
    fetched_ids = set(cp.get("fetched_post_ids", []))
    processed_this_run = 0

    try:
        while True:
            items, total_pages = fetch_posts_page(client, page)
            cp["total_pages"] = total_pages
            if not items:
                log.info("no items on page %d — done", page)
                break
            for post in items:
                pid = post["id"]
                if pid in fetched_ids:
                    continue
                process_post(client, post)
                fetched_ids.add(pid)
                processed_this_run += 1
                if limit and processed_this_run >= limit:
                    log.info("hit --limit %d", limit)
                    return
            cp["last_page"] = page
            cp["fetched_post_ids"] = sorted(fetched_ids)
            save_checkpoint(cp)
            if page >= total_pages:
                log.info("reached final page %d", page)
                break
            if max_pages and (page - (start_page or 1) + 1) >= max_pages:
                log.info("hit --pages %d", max_pages)
                break
            page += 1
    except DailyCapReached as e:
        log.warning("%s — stopping", e)
    finally:
        cp["fetched_post_ids"] = sorted(fetched_ids)
        save_checkpoint(cp)
        log.info("total requests today: %d", client.requests_today)
        log.info("total posts in checkpoint: %d", len(fetched_ids))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="stop after N new posts")
    ap.add_argument("--page", type=int, help="starting page (default: resume)")
    ap.add_argument("--pages", type=int, help="max pages to fetch this run")
    ap.add_argument("--taxonomy", action="store_true",
                    help="fetch categories/tags and exit")
    ap.add_argument("--pages-endpoint", action="store_true",
                    help="fetch /pages (static pages) and exit")
    ap.add_argument("--no-images", action="store_true",
                    help="skip image downloads")
    args = ap.parse_args()
    setup_logging()
    if args.no_images:
        config.DOWNLOAD_IMAGES = False
    crawl(args.limit, args.page, args.pages, args.taxonomy, args.pages_endpoint)


if __name__ == "__main__":
    main()
