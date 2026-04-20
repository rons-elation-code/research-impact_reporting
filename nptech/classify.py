"""Classify nptech posts into editorial / sponsored / promotional / etc.

Multi-label classifier, rule-based, transparent. Runs off the categories
the site's own editors assigned, with title-pattern fallbacks for posts
whose category alone doesn't reveal intent.
"""
import json
import re
from pathlib import Path

import config

# Category-ID-based rules. Source: state/categories.json
# Each entry: category_id -> (label, is_marketing, priority)
# Priority determines primary_type when multiple labels apply (lower wins).
CATEGORY_RULES = {
    87:  ("sponsored",    True,  1),  # sponsored-post
    90:  ("promotional",  True,  2),  # webinar
    134: ("promotional",  True,  2),  # certificate-programs
    86:  ("guest",        False, 3),  # guest-post
    80:  ("research",     False, 4),  # research
    82:  ("research",     False, 4),  # statistics
    81:  ("research",     False, 4),  # giving-report
    118: ("research",     False, 4),  # open-data-project
    94:  ("listicle",     False, 5),  # 101-best-practices
}

DEFAULT = ("editorial", False, 9)

# Title-pattern signals for posts whose category is only a topic tag.
TITLE_PROMOTIONAL = re.compile(
    r"^(free |announcing |new:?\b|now available|recordings?:|"
    r"join us|register now|save the date|last chance|"
    r"reminder:|upcoming:|live:|replay:|on demand:?\b)",
    re.IGNORECASE,
)
TITLE_PROMOTIONAL_KEYWORDS = re.compile(
    r"\b(webinar|mini-webinar|certificate program|online course|workshop|"
    r"bootcamp|masterclass|summit|conference)\b",
    re.IGNORECASE,
)
TITLE_LISTICLE = re.compile(
    r"^(\d+|\d+[a-z]*)\s+.*\b(best practices|tips|ways|reasons|ideas|"
    r"steps|strategies|tools|statistics|stats|trends|mistakes)\b",
    re.IGNORECASE,
)
TITLE_RESEARCH = re.compile(
    r"\b(statistics|stats|data|report|research|study|survey|findings|"
    r"benchmarks?|state of)\b",
    re.IGNORECASE,
)


def load_category_lookup() -> dict:
    path = config.STATE / "categories.json"
    if not path.exists():
        return {}
    cats = json.loads(path.read_text())
    return {c["id"]: c["slug"] for c in cats}


def classify(post: dict, cat_lookup: dict | None = None) -> dict:
    """Return classification dict for a post.

    Shape:
      {
        "primary_type": "sponsored",
        "types": ["sponsored"],
        "is_marketing": True,
        "category_slugs": ["fundraising", "sponsored-post"],
        "signals": ["category:sponsored-post"]
      }
    """
    cat_lookup = cat_lookup or {}
    cat_ids = post.get("categories") or []
    cat_slugs = [cat_lookup.get(cid, f"id:{cid}") for cid in cat_ids]

    # Static pages (from /wp/v2/pages) get their own bucket.
    if post.get("_is_static_page"):
        return {
            "primary_type": "page",
            "types": ["page"],
            "is_marketing": True,  # most static pages are services/about/contact
            "category_slugs": [],
            "signals": ["source:pages-endpoint"],
        }

    labels: list[tuple[str, bool, int, str]] = []  # (label, is_mkt, prio, signal)

    for cid in cat_ids:
        if cid in CATEGORY_RULES:
            label, is_mkt, prio = CATEGORY_RULES[cid]
            signal = f"category:{cat_lookup.get(cid, cid)}"
            labels.append((label, is_mkt, prio, signal))

    title_html = (post.get("title") or {}).get("rendered", "") or ""
    title = re.sub(r"<[^>]+>", "", title_html).strip()

    if TITLE_PROMOTIONAL.search(title) or TITLE_PROMOTIONAL_KEYWORDS.search(title):
        labels.append(("promotional", True, 2, f"title:{title[:60]}"))
    if TITLE_LISTICLE.search(title):
        labels.append(("listicle", False, 5, "title:listicle-pattern"))
    if TITLE_RESEARCH.search(title):
        labels.append(("research", False, 4, "title:research-keyword"))

    if not labels:
        labels.append((*DEFAULT, "default"))

    # Dedupe by label, keep the earliest signal
    seen: dict[str, tuple] = {}
    for lbl, is_mkt, prio, sig in labels:
        if lbl not in seen:
            seen[lbl] = (is_mkt, prio, sig)

    # Primary = lowest priority number
    primary = min(seen.items(), key=lambda kv: kv[1][1])[0]
    is_marketing = any(v[0] for v in seen.values())
    signals = [v[2] for v in seen.values()]

    return {
        "primary_type": primary,
        "types": sorted(seen.keys()),
        "is_marketing": is_marketing,
        "category_slugs": cat_slugs,
        "signals": signals,
    }


def main():
    """Reclassify all raw posts and write a summary report."""
    cat_lookup = load_category_lookup()
    rows = []
    for p in sorted(config.RAW_POSTS.glob("*.json")):
        rec = json.loads(p.read_text())
        post = rec["post"]
        cls = classify(post, cat_lookup)
        rows.append({
            "id": post["id"],
            "slug": post.get("slug"),
            "date": (post.get("date") or "")[:10],
            "primary_type": cls["primary_type"],
            "is_marketing": cls["is_marketing"],
            "types": cls["types"],
            "categories": cls["category_slugs"],
        })
    out = config.STATE / "classification_report.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    # Print summary
    counts: dict[str, int] = {}
    mkt = 0
    for r in rows:
        counts[r["primary_type"]] = counts.get(r["primary_type"], 0) + 1
        if r["is_marketing"]:
            mkt += 1
    print(f"Classified {len(rows)} posts")
    print(f"  marketing: {mkt} ({mkt/max(len(rows),1)*100:.1f}%)")
    for t, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:14s} {n}")


if __name__ == "__main__":
    main()
