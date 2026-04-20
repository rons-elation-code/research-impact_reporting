# nptechforgood.com Research Crawler — Developer Handoff

**Project:** Research-grade crawler and classifier for Nonprofit Tech for Good (nptechforgood.com).
**Status:** Partially deployed; overnight cron running. See "Current Status" below.
**Owner:** Ron (ronp@lavanduladesign.com)
**Location:** `~/research/nptech/` on this host.
**Last updated:** 2026-04-16

---

## 1. Goals

Build a private, internal research index of all nptechforgood.com editorial content, in a form suitable for:

- **Topic research** — "what are they writing about, and how often?"
- **Competitive attribution** — "how much of their content originates with us?" (deferred to Phase 2)
- **Visual-trend research** — preserving infographics, charts, and associated alt text
- **Ongoing monitoring** — incremental sync of new posts going forward

The output is a local corpus of Markdown files + image sidecar JSON + downloaded images, segregated by content type (editorial vs sponsored vs promotional vs guest vs research), ready to be loaded into a vector store for RAG or analyzed with traditional NLP tooling.

---

## 2. Context and Positioning

- nptechforgood.com is a **competitor** in the nonprofit technology / digital marketing space. Ron's company (Lavandula Design, `lavanduladesign.com`) operates in the same niche.
- The site publisher is Heather Mansfield. Ron attempted a partnership outreach previously; she did not respond.
- All nptech content is **public and non-gated**; most posts are also distributed through their social channels. Ron considers it public research material.
- **We do not identify ourselves in the crawl.** The User-Agent is generic but non-deceptive. We do not impersonate browsers or rotate IPs.
- **Content is for internal research only.** It must not be republished, fed into a public-facing model, or otherwise redistributed. The corpus is private.

---

## 3. Ethical and Technical Throttling

The site's `robots.txt` asks for `Crawl-delay: 3` (seconds). We use **10 seconds** — much slower than required — because:

1. A sustained crawler at the minimum rate is easier for their ops team to notice.
2. Slow and steady is invisible; bursts get blocked.
3. The archive is small enough (~1,068 items total) that 10s/request still finishes in 2–3 nights.

We also constrain crawling to **off-peak hours** (11 PM–5 AM US Central / 04:00–10:00 UTC), when their audience traffic is lowest.

Other ethical/operational guardrails in the code:
- Retry/backoff on `429` and `5xx`. Honors `Retry-After` header.
- `401`/`403` on media items → log and skip (some media is restricted).
- Resumable via checkpoint so a crash doesn't re-hit already-fetched URLs.
- Daily request cap configurable (currently disabled — see `config.py`).
- Skips WP `Disallow` paths (we never hit `/calendar/action*`, `/events/action*`, `/*?`).
- Does not fetch `/users` or `/comments` — unnecessary for research, raises footprint.

---

## 4. Site Discovery Findings (2026-04-15)

Tested with Playwright and direct API probing. Key facts:

| Finding | Detail |
|---|---|
| Post total | **883 posts** (verified: `X-WP-Total` header + Jetpack sitemap URL pattern count) |
| Page total | **185 pages** (many are actual editorial content published as WP Pages, not just About/Contact) |
| Oldest post | 2009-08-30 — ~17 years of archive |
| Publishing cadence | ~52 posts/year (weekly-ish curation, not a news feed) |
| CMS | WordPress 6.9.4 with Jetpack 15.6 |
| Sitemap | Jetpack-generated. `/sitemap.xml` → `sitemap-index-1.xml` + `sitemap-index-2.xml` + image + video sitemaps |
| REST API | **Open and unauthenticated** for `posts`, `pages`, `media`, `categories`, `tags` |
| Plan's original assumptions — **wrong** | `/sitemap_index.xml` is 404 (it's Jetpack, not Yoast). `/category/online-fundraising/` is 404 (correct slug is `/category/fundraising/`). `<div class="entry-footer">` does not exist on article pages. |

**Decision: skip HTML scraping entirely.** The WP REST API returns clean, structured content — no navigation chrome, no sidebar, no JS required. It's 10× faster and cleaner than Playwright-based scraping.

### Alt-text coverage findings

Surveyed 20 posts:
- Some posts have **excellent inline alt text** (one post: 5/5 imgs with detailed infographic descriptions).
- The **image-heaviest listicles** (e.g. "10 Instagram best practices" with 42 images) have **zero alt text**.
- No `<figcaption>` usage site-wide — just bare `<img class="wp-image-NNNN">`.
- `class="wp-image-NNNN"` ties back to `/wp-json/wp/v2/media/NNNN` where `alt_text`, `caption`, `title` can live as structured data (sometimes populated even when the inline HTML strips them).

**Implication:** We preserve both inline alt and media-library alt. For visual research where alt is empty, plan to vision-caption images in a post-processing step (see "Deferred Work" below).

### Photon URLs

All images are served via Jetpack Photon at `i0.wp.com/www.nptechforgood.com/...?resize=…&ssl=1`. Stripping the query string gets the full-resolution original. The crawler normalizes this.

---

## 5. Architecture

```
~/research/nptech/
├── config.py              # Throttle, paths, UA, fields
├── http_client.py         # ThrottledClient: 10s delay, retry/backoff, daily cap
├── crawler.py             # Posts + Pages + Media + Images. Checkpoint-resumable.
├── classify.py            # Rule-based multi-label classifier
├── extract.py             # Raw JSON → Markdown + images.json sidecar
├── run_nightly.sh         # Cron entrypoint: posts → pages → extract
├── venv/                  # Python 3.12 venv (requests, beautifulsoup4, markdownify, lxml)
├── raw/
│   ├── posts/             # {id}.json — REST response + image_refs
│   ├── media/             # {id}.json — cached media metadata
│   └── images/            # Downloaded image files ({post_id}-{idx}.ext)
├── clean/
│   └── posts/
│       ├── editorial/     # (default bucket for non-marketing content)
│       ├── sponsored/     # category 87
│       ├── promotional/   # webinars, certificate programs
│       ├── guest/         # guest-post category
│       ├── research/      # research, statistics, giving-report, open-data-project
│       ├── listicle/      # "101 Best Practices" and similar
│       └── page/          # items from /wp/v2/pages endpoint
├── state/
│   ├── categories.json    # 49 categories with IDs
│   ├── tags.json          # 48 tags
│   ├── crawler_checkpoint.json
│   └── request_counter.json
└── logs/
    └── nightly-YYYY-MM-DD.log
```

### Why this shape

- **`raw/` is append-only ground truth.** Never modify. If classification changes, re-run `extract.py`.
- **`clean/` is derived and disposable.** Can be rebuilt from `raw/` in seconds.
- **Sidecar JSON per post** keeps image metadata (media_id, dimensions, alt source, section_heading, local_file) separate from Markdown body. Markdown stays clean; structured data stays structured.
- **Classification is a property of the post**, stored in frontmatter AND used to route output files into subdirs. You can filter by `is_marketing: true` in frontmatter, OR just ignore `sponsored/` and `promotional/` folders.

---

## 6. Classification System

See `classify.py`. **Rule-based, multi-label, transparent.**

### By category (uses their own editorial taxonomy)

| Category ID | Slug | Classification | Is Marketing |
|---|---|---|---|
| 87 | `sponsored-post` | `sponsored` | ✅ |
| 90 | `webinar` | `promotional` | ✅ |
| 134 | `certificate-programs` | `promotional` | ✅ |
| 86 | `guest-post` | `guest` | ❌ |
| 80 | `research` | `research` | ❌ |
| 81 | `giving-report` | `research` | ❌ |
| 82 | `statistics` | `research` | ❌ |
| 118 | `open-data-project` | `research` | ❌ |
| 94 | `101-best-practices` | `listicle` | ❌ |
| (anything else) | — | `editorial` | ❌ |

### By title pattern (fallback signals)

- Promotional: titles starting with `Free|Announcing|New:|Now Available|Recordings:|Register Now|...` OR containing `webinar|certificate program|workshop|masterclass|summit`
- Listicle: `^\d+\s+.*\b(best practices|tips|ways|reasons|steps|strategies)\b`
- Research: `statistics|stats|data|report|research|study|survey|benchmarks|state of`

### Primary type tie-breaking

If a post matches multiple categories, the one with the lowest priority number wins as `primary_type`. Order: sponsored (1) → promotional (2) → guest (3) → research (4) → listicle (5) → editorial (9).

### Static pages (from `/wp/v2/pages`)

All get `primary_type: page`, `is_marketing: true`. Most are confirmation/thank-you flows or service pages. A handful (`website-statistics-for-nonprofits`, `ai-marketing-fundraising-statistics-for-nonprofits`) are research-flavored but we keep them in the `page` bucket for provenance clarity.

### How to adjust

All rules are in `classify.py` dictionaries and regexes — no database, no ML, trivially tweakable. After any change, rerun `./venv/bin/python extract.py` to reclassify the whole corpus.

---

## 7. Operational Setup

### Cron (crontab of user `ubuntu`)

```cron
# nptech nightly crawler: 11pm Central (04:00 UTC), 7 nights a week
0 4 * * * /home/ubuntu/research/nptech/run_nightly.sh
```

### `run_nightly.sh`

Runs in sequence, each with its own timeout:

1. **Posts backfill** — `timeout 21000` (5h50m). Resumes from `state/crawler_checkpoint.json`.
2. **Pages endpoint** — `timeout 1800` (30m). Skips already-fetched pages.
3. **Extraction** — no timeout. Processes all raw JSON into Markdown + sidecars.

All output tees to `logs/nightly-YYYY-MM-DD.log`.

### Throttle (in `config.py`)

```python
REQUEST_DELAY_SEC = 10.0
USER_AGENT = "Mozilla/5.0 (compatible; research-indexer/1.0)"
PER_PAGE = 100
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 5.0
DOWNLOAD_IMAGES = True
DAILY_REQUEST_CAP = None
```

If we ever need to back off further: change `REQUEST_DELAY_SEC` to 15 or 20. The whole thing is designed around a single knob.

---

## 8. Current Status (as of 2026-04-16 morning)

### Completed

- ✅ Full site recon done (Playwright + direct API)
- ✅ All 49 categories + 48 tags fetched (`state/categories.json`, `state/tags.json`)
- ✅ All 185 **Pages** crawled and extracted (clean/posts/page/)
- ✅ 3 **Posts** crawled and extracted as test (1 sponsored, 1 promotional, 1 guest)
- ✅ Image downloads: 263 files, 26 MB
- ✅ Classification system validated against test sample
- ✅ Cron installed and running nightly

### In progress

- ⏳ **Posts backfill** — 880 of 883 posts remaining. Expected to complete over 2 more nights (tonight 2026-04-16 and, if needed, 2026-04-17).
- The first nightly run (2026-04-15 → 2026-04-16 at 04:00 UTC) **failed to start the posts phase** due to a `timeout` syntax bug (`5h50m` is not valid on GNU coreutils). Fixed in `run_nightly.sh` — now uses seconds (`21000`). Pages phase ran successfully despite the bug.

### Known issues (monitoring)

- Media ID `40641` returns HTTP 401 — private/restricted attachment. Crawler handles it gracefully now (logs warning, skips).
- Some Pages are thank-you/confirmation flows. We fetch them anyway for completeness. They land in `clean/posts/page/` and can be ignored at consumption time.

---

## 9. How to Operate

### Check progress

```bash
# Last night's log
tail -40 ~/research/nptech/logs/nightly-$(date +%Y-%m-%d).log

# Checkpoint summary
cat ~/research/nptech/state/crawler_checkpoint.json | \
  python3 -c "import sys,json; d=json.load(sys.stdin); \
  print(f'posts fetched: {len(d[\"fetched_post_ids\"])} / 883'); \
  print(f'last page: {d[\"last_page\"]} / {d.get(\"total_pages\",\"?\")}')"

# Corpus inventory
cd ~/research/nptech && \
  echo "raw posts: $(ls raw/posts/*.json | wc -l)" && \
  echo "raw images: $(ls raw/images/ | wc -l)" && \
  echo "disk: $(du -sh raw/)" && \
  for d in clean/posts/*/; do \
    echo "  $(basename $d): $(find $d -name '*.md' | wc -l) posts"; \
  done
```

### Run a manual piece

```bash
cd ~/research/nptech

# Fetch just the taxonomy
./venv/bin/python crawler.py --taxonomy

# Fetch one page of 100 posts (for testing)
./venv/bin/python crawler.py --pages 1 --page 1

# Fetch a small sample
./venv/bin/python crawler.py --limit 5

# Fetch pages endpoint
./venv/bin/python crawler.py --pages-endpoint

# Full backfill (resumes from checkpoint)
./venv/bin/python crawler.py

# Re-extract all raw → clean (fast; pure transform)
./venv/bin/python extract.py

# Classification summary
./venv/bin/python classify.py
```

### Transitioning to incremental mode

Once the backfill is complete, switch the cron to use `modified_after` filter so it only grabs new or updated posts. This is **not yet implemented** — add an `--incremental` flag to `crawler.py` that:

1. Reads `state/last_modified_seen.txt`
2. Calls `/wp/v2/posts?modified_after={that_date}&orderby=modified&order=asc`
3. Updates the watermark at the end

Then change the cron to weekly or daily, with a much lower runtime budget.

---

## 10. Deferred Work (Phase 2 and beyond)

### Competitive attribution analysis (`attribute.py`)

**Design drafted, not built.** The third script in the original plan. Runs four signals against a corpus of our own content:

1. **Verbatim reuse** — MinHash + LSH over 5-word shingles, paragraph-level drilldown
2. **Paraphrased reuse** — sentence-transformer embeddings at heading-section granularity, filtered to exclude MinHash overlaps
3. **Link attribution** — count of outbound links to our domains + social handles
4. **Image reuse** — pHash over downloaded images against our image library

**Blocker:** This requires a `ingest_ours.py` script + a clean corpus of Lavandula Design's own content. Ron's content is scattered across WordPress, Google Drive, Mailchimp, social, etc. The inventory + normalization is ~60% of the work and has been deferred.

When starting this phase, begin with `ingest_ours.py` not `attribute.py`. Without a `ours/` corpus, `attribute.py` produces misleadingly optimistic "no overlap found" results.

### Vision captioning of low-alt images

The most image-dense posts (screenshot-heavy listicles) have zero alt text. For visual-trend research, this is a gap. Proposed approach:

- Batch job over `raw/images/` where the corresponding sidecar entry has `alt_source: "none"`
- Call Claude Vision (or similar) with a prompt like "Describe this image for a researcher cataloging nonprofit marketing visuals"
- Append `vision_caption` + `vision_caption_source` fields to the sidecar JSON
- Do NOT overwrite editor-provided alt text

### Vector store indexing

Not yet designed. Plan is to chunk on H2/H3 boundaries (using `section_heading` from sidecars) and embed into a local ChromaDB. One chunk per "best practice" item. Store classification + date + URL as metadata for filtering.

### RSS / incremental feed

The `/feed/` RSS endpoint works (~10 latest posts). We prefer `modified_after` via REST API because it gives the full list of changes since last run, not just the 10 newest. Not yet wired up — see "Transitioning to incremental mode" above.

---

## 11. Important Decisions Made (and Why)

| Decision | Rationale |
|---|---|
| Use WP REST API, not scraping | Site is WordPress; REST API returns clean content, no nav/sponsor chrome, no JS needed. 10× faster than Firecrawl/Crawl4AI. |
| 10s throttle, not 3s (robots.txt) | Competitor in a small industry; slow = invisible; archive is small enough that 10s still finishes in ~2 nights. |
| Do not identify User-Agent | Ron's choice: "part of the research is identifying how much of the content is ours." Identification was off the table. |
| Off-peak hours (11pm–5am Central) | Minimize any footprint on their ops observability; respectful timing. |
| Fetch full archive incl. sponsored | Sponsored posts are ~21% of corpus and analytically interesting ("what are their partners paying to say?"). Classification segregates them cleanly; consumer filters. |
| Include thank-you/confirmation pages | Ron said: capture everything. Trivial cost. Filter at read-time if unwanted. |
| Rule-based classifier, not ML | Uses their own editorial taxonomy as ground truth. Transparent, tweakable, deterministic. No training data needed. |
| Markdown + sidecar JSON (not Markdown alone) | Preserves structured image data (media_id, dimensions, alt source, section_heading) that would be lost in plain Markdown. |
| Download images locally | Enables image-reuse analysis (Phase 2) and vision captioning. Costs ~200 MB, acceptable. |
| Segregate output by `primary_type` subdirs | Makes "just the editorial research content" one-line to load. No need to filter by frontmatter field. |

---

## 12. Handoff Checklist for New Developer

- [ ] Read this document end-to-end.
- [ ] `ssh` to the host, `cd ~/research/nptech/`.
- [ ] Check the latest nightly log: `tail -40 logs/nightly-$(date +%Y-%m-%d).log`.
- [ ] Check the checkpoint and corpus inventory (commands in "How to Operate" above).
- [ ] Review `clean/posts/sponsored/2026-03-26-ripples-of-a-click*` — it's the exemplar of a clean extraction with full image metadata.
- [ ] Read `config.py`, `classify.py`, and `extract.py` — they are short and self-documenting.
- [ ] Verify `crontab -l` shows the nightly job.
- [ ] Once backfill is complete (checkpoint shows ~883 posts fetched), **do these three things**:
  1. Implement `--incremental` flag in `crawler.py`. Switch cron to weekly or daily.
  2. Decide whether to build `ingest_ours.py` + `attribute.py` (Phase 2). Ask Ron first.
  3. Decide whether to run vision captioning over low-alt images. Ask Ron first.
- [ ] If anything is broken or unclear, contact Ron at ronp@lavanduladesign.com.

---

## 13. Quick Reference

**Site:** `https://www.nptechforgood.com`
**API base:** `https://www.nptechforgood.com/wp-json/wp/v2`
**Key endpoints:** `/posts`, `/pages`, `/media/{id}`, `/categories`, `/tags`
**Sitemap:** `https://www.nptechforgood.com/sitemap.xml` (Jetpack)
**RSS:** `https://www.nptechforgood.com/feed/` (last ~10 posts only)
**Corpus size:** 883 posts + 185 pages = 1,068 items
**Est. final corpus disk:** ~300–400 MB (text + images)
**Crawl budget:** ~15 hours wall-clock at 10s throttle
**Current progress:** 185 pages done, 880 posts remaining (backfill in progress)
