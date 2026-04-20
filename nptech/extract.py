"""Convert raw post JSON → clean Markdown + images.json sidecar.

Usage:
    python extract.py            # process all raw posts
    python extract.py --id 12345 # one post
"""
import argparse
import json
import logging
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString
from markdownify import markdownify

import config
from classify import classify, load_category_lookup
from http_client import setup_logging

log = logging.getLogger("nptech.extract")

SPONSOR_RE = re.compile(
    r"(sponsored by|thank you to our sponsors?|our partners at|"
    r"in partnership with|brought to you by)",
    re.IGNORECASE,
)


def load_media_cache() -> dict:
    """Load all fetched media JSON files keyed by ID."""
    cache = {}
    for p in config.RAW_MEDIA.glob("*.json"):
        try:
            data = json.loads(p.read_text())
            cache[int(p.stem)] = data
        except Exception as e:
            log.warning("bad media file %s: %s", p, e)
    return cache


def nearest_heading(img_tag) -> str | None:
    """Walk backward in the document to find the closest preceding h2/h3."""
    node = img_tag
    while node is not None:
        prev = node.previous_element
        while prev is not None:
            if getattr(prev, "name", None) in ("h2", "h3"):
                text = prev.get_text(strip=True)
                if text:
                    return text
            prev = prev.previous_element
        break
    return None


def resolve_alt(inline_alt: str, media_id: int | None, media_cache: dict) -> tuple[str, str]:
    """Return (alt_text, alt_source). Fall back to media library alt_text."""
    if inline_alt and inline_alt.strip():
        return inline_alt.strip(), "inline"
    if media_id and media_id in media_cache:
        m = media_cache[media_id]
        alt = (m.get("alt_text") or "").strip()
        if alt:
            return alt, "media_library"
        cap = ((m.get("caption") or {}).get("rendered") or "").strip()
        if cap:
            cap = BeautifulSoup(cap, "lxml").get_text(strip=True)
            if cap:
                return cap, "media_caption"
    return "", "none"


def strip_sponsor_paragraphs(soup: BeautifulSoup) -> int:
    """Drop paragraphs matching the sponsor regex. Returns count removed."""
    removed = 0
    for p in list(soup.find_all("p")):
        text = p.get_text(" ", strip=True)
        if text and SPONSOR_RE.search(text):
            p.decompose()
            removed += 1
    return removed


def normalize_photon(url: str) -> str:
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    if p.netloc.endswith("wp.com"):
        return urlunparse(p._replace(query=""))
    return url


def extract_images(soup: BeautifulSoup, media_cache: dict, image_refs: list[dict]) -> list[dict]:
    """Build structured image metadata. One entry per <img> in content order."""
    # Map wp-image-NNNN → local_file from crawler's image_refs
    local_file_by_id = {r["id"]: r.get("local_file")
                        for r in image_refs if r.get("id")}

    images = []
    for idx, img in enumerate(soup.find_all("img")):
        classes = img.get("class") or []
        media_id = None
        for c in classes:
            m = re.match(r"wp-image-(\d+)$", c)
            if m:
                media_id = int(m.group(1))
                break

        inline_alt = img.get("alt", "")
        alt, alt_source = resolve_alt(inline_alt, media_id, media_cache)

        src_raw = img.get("src", "")
        src_original = normalize_photon(src_raw)

        dims = {}
        if media_id and media_id in media_cache:
            md = media_cache[media_id].get("media_details") or {}
            dims = {"width": md.get("width"), "height": md.get("height")}

        images.append({
            "position": idx,
            "media_id": media_id,
            "src_display": src_raw,
            "src_original": src_original,
            "local_file": local_file_by_id.get(media_id) if media_id else None,
            "alt": alt,
            "alt_source": alt_source,
            "section_heading": nearest_heading(img),
            "width": dims.get("width"),
            "height": dims.get("height"),
        })
    return images


def html_to_markdown(html: str) -> str:
    """Convert HTML content to Markdown preserving images + basic structure."""
    return markdownify(
        html,
        heading_style="ATX",
        bullets="-",
        strip=["script", "style"],
    ).strip()


def clean_post(record: dict, media_cache: dict, cat_lookup: dict | None = None) -> tuple[str, dict]:
    """Return (markdown, sidecar_dict) for one raw post record."""
    post = record["post"]
    html = (post.get("content") or {}).get("rendered", "") or ""
    soup = BeautifulSoup(html, "lxml")

    removed = strip_sponsor_paragraphs(soup)
    images = extract_images(soup, media_cache, record.get("image_refs", []))

    # Re-serialize and convert to markdown
    cleaned_html = str(soup)
    body_md = html_to_markdown(cleaned_html)

    title = BeautifulSoup((post.get("title") or {}).get("rendered", ""),
                          "lxml").get_text(strip=True)
    excerpt = BeautifulSoup((post.get("excerpt") or {}).get("rendered", ""),
                            "lxml").get_text(strip=True)

    cls = classify(post, cat_lookup or {})
    frontmatter = {
        "id": post["id"],
        "slug": post.get("slug"),
        "title": title,
        "url": post.get("link"),
        "date": post.get("date"),
        "modified": post.get("modified"),
        "categories": post.get("categories", []),
        "category_slugs": cls["category_slugs"],
        "tags": post.get("tags", []),
        "author": post.get("author"),
        "primary_type": cls["primary_type"],
        "types": cls["types"],
        "is_marketing": cls["is_marketing"],
        "classification_signals": cls["signals"],
        "sponsor_paragraphs_removed": removed,
        "image_count": len(images),
    }

    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    fm_lines.append("---\n")
    md = "\n".join(fm_lines) + f"# {title}\n\n"
    if excerpt:
        md += f"> {excerpt}\n\n"
    md += body_md + "\n"

    sidecar = {
        "post_id": post["id"],
        "post_url": post.get("link"),
        "post_date": post.get("date"),
        "post_title": title,
        "images": images,
    }
    return md, sidecar


def process_one(raw_path: Path, media_cache: dict, cat_lookup: dict) -> None:
    record = json.loads(raw_path.read_text())
    post = record["post"]
    md, sidecar = clean_post(record, media_cache, cat_lookup)
    date_part = (post.get("date") or "")[:10]
    slug = post.get("slug") or f"post-{post['id']}"
    cls_primary = classify(post, cat_lookup)["primary_type"]
    base = f"{date_part}-{slug}"
    subdir = config.CLEAN_POSTS / cls_primary
    md_path = subdir / f"{base}.md"
    sc_path = subdir / f"{base}.images.json"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md)
    sc_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False))
    log.info("wrote %s (%d imgs, %d words, alt coverage %d/%d)",
             md_path.name,
             sidecar["images"].__len__(),
             len(md.split()),
             sum(1 for i in sidecar["images"] if i["alt"]),
             len(sidecar["images"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, help="extract one post by ID")
    args = ap.parse_args()
    setup_logging()
    media_cache = load_media_cache()
    cat_lookup = load_category_lookup()
    log.info("media cache: %d entries | categories: %d", len(media_cache), len(cat_lookup))
    if args.id:
        path = config.RAW_POSTS / f"{args.id}.json"
        if not path.exists():
            log.error("no raw post for id %s", args.id)
            sys.exit(1)
        process_one(path, media_cache, cat_lookup)
        return
    paths = sorted(config.RAW_POSTS.glob("*.json"))
    log.info("processing %d raw posts", len(paths))
    for p in paths:
        try:
            process_one(p, media_cache, cat_lookup)
        except Exception as e:
            log.exception("failed on %s: %s", p.name, e)


if __name__ == "__main__":
    main()
