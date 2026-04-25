# Spec 0022 — Wayback Machine CDX Fallback for Cloudflare-blocked Sites

**Status**: Conceived (initial draft)
**Author**: Architect
**Created**: 2026-04-25
**Dependencies**: 0021 (Async I/O Crawler Pipeline)

---

## Consultation Log

(To be populated during the spec-review and red-team rounds.)

---

## Problem Statement

The async crawler validation run on 2026-04-25 (Spec 0021) crawled 100 orgs and recorded **17 transient failures** (17%). Diagnosis showed that **all sampled failures were Cloudflare bot-challenge responses**, not network outages:

```
HTTP/2 403
server: cloudflare
cf-mitigated: challenge
content-length: 5485   ← JavaScript challenge page body
```

This is not transient in the colloquial sense — these sites are deliberately blocking automated requests via Cloudflare's bot-management. Three observations:

1. **Not User-Agent dependent**: tested with the project's UA, a browser UA, and the curl default — all returned 403 with the same `cf-mitigated: challenge` response.
2. **Not solvable by adding browser-like headers**: the challenge is a JavaScript "checking your browser" page that requires actual JS execution.
3. **Persistent across retries**: the same orgs returned 403 on every retry attempt; the 3-attempt cap (Spec 0021 follow-up) correctly auto-promotes them to `permanent_skip`.

At national scale (~100K orgs), 17% silently lost is unacceptable. Most of these orgs have valuable annual reports we want to ingest.

**Wayback Machine has these reports.** The `web.archive.org/cdx/search/cdx` API was queried for 5 sampled sites and confirmed:

| Site | Wayback PDFs available |
|---|---|
| sloan.org | 50+ archived PDFs incl. annual program updates and 990s, sizes 1-15 MB, captures within last few months |
| endfund.org | Annual reports 2012-2013, financial summaries 2012-2015, latest re-captures 2026-01-05 |
| cbcny.org | Homepage archived, PDFs available via deeper crawl (not yet measured) |
| rffund.org | 1 PDF, broken (404) — essentially no coverage |
| ktstrust.org | No homepage snapshot, likely no coverage |

Recovery is uneven by site, but at our 5-site sample, ~3-4 of 5 have recoverable PDF data. National recovery rate is estimated at **70-80% of CF-blocked orgs**, lifting overall coverage from ~83% to **~95-96%**.

## Goals

- When a nonprofit site cannot be crawled directly (Cloudflare 403 or zero-candidate transient failure), query Wayback CDX for archived PDFs under the domain and download via web.archive.org.
- Discovered PDFs flow through the existing fetch → validate → archive → record pipeline unchanged. Only the *source* differs.
- Tag candidates with `discovered_via='wayback'` and `hosting_platform='wayback'` for downstream traceability.
- Orgs that successfully recover via Wayback are marked `status='ok'`, not `transient`. They count as completed crawls.

## Non-goals

- **Not a Cloudflare bypass**. We do not attempt to defeat the JS challenge. Wayback is a separate data path, not a workaround.
- **Not a full Wayback mirror**. We don't crawl Wayback's HTML snapshots — only query the CDX API for PDF assets directly.
- **Not always-on**. Wayback fallback only fires when direct crawl fails. We don't supplement successful crawls with Wayback to keep cost down and avoid double-counting.
- **No commercial scraping integration**. The 4-5% long tail (orgs with no Wayback presence) is left for a future spec if needed.

## Architecture

### When the fallback fires

The async crawler's discovery already produces a `DiscoveryResult(candidates, homepage_ok, robots_disallowed_all)`. Extend this with a new field `wayback_eligible: bool` set to True when:

- `not homepage_ok` (homepage GET returned non-200)
- AND `not robots_disallowed_all` (robots.txt didn't block everything — if it did, Wayback shouldn't be queried either, that's a permanent decision)
- AND `not candidates` (sitemap also produced nothing useful)

Equivalent today, this is the same condition that triggers the AC23 transient classification. We branch BEFORE writing the transient row.

### Wayback CDX query

Single GET to:
```
https://web.archive.org/cdx/search/cdx
  ?url={domain}/*
  &filter=mimetype:application/pdf
  &filter=statuscode:200
  &from=20200101                    (last ~5 years; arbitrary but bounded)
  &output=json
  &collapse=urlkey                  (deduplicate; one row per unique URL, latest capture)
  &limit=200                        (cap response size)
```

**Why these filters:**
- `mimetype:application/pdf` — only PDFs (not HTML/CSS/JS)
- `statuscode:200` — exclude 404/410 captures (broken at archive time)
- `from=20200101` — recent captures more likely to reflect current PDFs; older captures may be irrelevant or removed
- `collapse=urlkey` — dedup by canonical URL form, keeps just one capture per logical PDF
- `limit=200` — bound the response. A nonprofit site rarely has more than 200 archived PDFs and we already cap candidates per org via `CANDIDATE_CAP_PER_ORG` (= 30 today).

Response is a JSON array of `[urlkey, timestamp, original, mimetype, statuscode, digest, length]` rows, with a header row.

### Download via Wayback raw-bytes endpoint

For each row, construct:
```
https://web.archive.org/web/{timestamp}id_/{original}
                            ^^^^^      ^^^
                            timestamp  id_ modifier = serve raw bytes, no Wayback chrome
```

The `id_` modifier is critical — without it, Wayback wraps the response in an iframe with their toolbar, breaking PDF parsing.

These URLs are validated by the existing `url_guard.is_address_allowed()` against the resolved IP of `web.archive.org`, which is public and passes SSRF checks. The original (target) URL is metadata only; we never connect to it.

### Throttling

- Wayback's documented rate limits: ~15 req/sec sustained per IP for CDX, similar for capture downloads.
- Use the existing `AsyncHostThrottle` keyed on `web.archive.org` — it'll naturally serialize to ~1 req every `REQUEST_DELAY_SEC` (3s default), well under Wayback's limit.
- Apply the same per-host throttle to all Wayback hosts (`web.archive.org`, `archive.org`).

### Pipeline integration

The Wayback fetcher returns `Candidate` instances identical to direct discovery, except:

- `url` → the Wayback raw-bytes URL (`https://web.archive.org/web/{timestamp}id_/{original}`)
- `referring_page_url` → the original homepage URL (for attribution traceability)
- `discovered_via` → `'wayback'`
- `hosting_platform` → `'wayback'`
- `attribution_confidence` → `'wayback_archive'` (new value; documents that this is an archived copy, not a live retrieval)

Downloads, magic-byte checks, structure validation, archive PUTs, and DB writes are all the existing pipeline — no special-casing.

### Resume + retry semantics (interaction with Spec 0021's status/attempts)

- If Wayback recovers PDFs → org gets `status='ok'` row in `crawled_orgs`. Done.
- If Wayback also fails (network error, no PDFs found) → fall through to the existing transient path; row written with `status='transient'`. Subsequent runs retry the full pipeline (direct crawl first, Wayback if direct fails).
- If Wayback finds 0 PDFs (clean response, just empty) → also write `status='transient'` once (sites do sometimes get archived but never have PDFs captured). After `MAX_TRANSIENT_ATTEMPTS`, auto-promotion to `permanent_skip` kicks in as today.

## Acceptance Criteria

### Detection
- **AC1**: Wayback fallback fires only when `(not homepage_ok) AND (not robots_disallowed_all) AND (len(candidates) == 0)` after direct discovery.
- **AC2**: Wayback fallback does NOT fire when direct discovery succeeded with 1+ candidates, even if some subpages 403'd.
- **AC3**: Wayback fallback does NOT fire when robots.txt blocks crawling — that's a deliberate permanent signal.

### CDX query
- **AC4**: Single CDX GET per org with the exact query string defined above.
- **AC5**: Response parsed as JSON; first row treated as header and skipped.
- **AC6**: CDX query timeout = 15s. On timeout or non-200 response, treat the org as transient (no Wayback recovery).
- **AC7**: Response capped at `WAYBACK_MAX_PDFS_PER_ORG` (default 30, matching `CANDIDATE_CAP_PER_ORG`).

### Download
- **AC8**: Each PDF downloaded via the `id_` modifier URL.
- **AC9**: Wayback URLs flow through the existing `AsyncHTTPClient` with manual gzip decompression, magic-byte check, and `_validate_pdf_structure`.
- **AC10**: Per-host throttle applies to `web.archive.org` (i.e., we don't hammer Wayback at 200 concurrent — it serializes through the throttle).

### Attribution and DB writes
- **AC11**: Each Wayback-discovered candidate has `discovered_via='wayback'`, `hosting_platform='wayback'`, `attribution_confidence='wayback_archive'`.
- **AC12**: `reports.source_url_redacted` records the Wayback URL (so we can reproduce the fetch). `reports.referring_page_url_redacted` records the original homepage.
- **AC13**: A new column `reports.original_source_url_redacted` records the original (target) URL of the archived PDF, separate from the Wayback URL. Migration adds it as nullable text.
- **AC14**: When Wayback recovers ≥1 PDF, the org's `crawled_orgs` row gets `status='ok'`. When Wayback returns 0 PDFs or fails, `status='transient'` (subject to attempt-cap auto-promotion).

### Security and SSRF
- **AC15**: SSRF check accepts `web.archive.org` and `archive.org` as connect targets. The original archived URL is metadata only, never used for connection.
- **AC16**: Content-Encoding whitelist (`gzip`, `identity`) applies as today.
- **AC17**: `is_pdf_magic` check applies — Wayback returning HTML disguised as PDF is rejected the same way as direct downloads.

### Observability
- **AC18**: `CrawlStats` adds `wayback_recoveries: int` (count of orgs where Wayback yielded ≥1 PDF) and `wayback_attempts: int` (count of CDX queries). Logged in the final summary line.
- **AC19**: A new `decisions_log` event type `wayback_query` records each CDX request with response row count and elapsed time.

### Testing
- **AC20**: Unit test: CDX response parsing (header skip, dedup by urlkey, cap at limit).
- **AC21**: Unit test: detection logic (only fires under the AC1 condition, never under AC2/AC3).
- **AC22**: Unit test: candidate construction (correct URL format, attribution fields, original_source_url preserved).
- **AC23**: Integration test: stub homepage returns 403 with `server: cloudflare`; stub Wayback CDX returns 3 PDFs; assert 3 candidates produced with correct fields.
- **AC24**: Integration test: stub Wayback CDX returns 0 rows; assert org marked `status='transient'`, no PDFs downloaded.
- **AC25**: SSRF test: confirm `web.archive.org` resolves and connects; original URL never resolved.

### Performance
- **AC26**: Wayback CDX query adds at most 15s per CF-blocked org (timeout). Successful queries typically <2s.
- **AC27**: Wayback PDF download throughput is bottlenecked by Wayback's per-host throttle, not our pipeline. At 200 concurrent CF-blocked orgs, we serialize through ~1 Wayback req/3s, so a 17-org batch finishes in ~50s.

## Technical Implementation Sketch

**New file**: `lavandula/reports/wayback_fallback.py` (~200 lines)

```python
async def discover_via_wayback(
    *,
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    ein: str,
) -> list[Candidate]:
    """Query Wayback CDX for PDFs under the domain. Returns candidates
    pointing at web.archive.org raw-bytes URLs.
    """
    domain = urlsplit(seed_url).hostname or seed_etld1
    cdx_url = (
        f"https://web.archive.org/cdx/search/cdx?"
        f"url={domain}/*&"
        f"filter=mimetype:application/pdf&"
        f"filter=statuscode:200&"
        f"from=20200101&"
        f"output=json&"
        f"collapse=urlkey&"
        f"limit={config.WAYBACK_MAX_PDFS_PER_ORG}"
    )
    r = await client.get(cdx_url, kind="wayback-cdx")
    if r.status != "ok" or not r.body:
        return []
    rows = json.loads(r.body.decode("utf-8"))
    if not rows or len(rows) < 2:
        return []
    header, *data = rows
    candidates = []
    for row in data:
        if len(row) < 3:
            continue
        timestamp, original = row[1], row[2]
        wayback_url = f"https://web.archive.org/web/{timestamp}id_/{original}"
        candidates.append(Candidate(
            url=wayback_url,
            referring_page_url=seed_url,
            discovered_via="wayback",
            hosting_platform="wayback",
            attribution_confidence="wayback_archive",
            anchor_text=original,
            original_source_url=original,  # new field
        ))
    return candidates
```

**Modify**: `lavandula/reports/async_discover.py`
Add a branch after the standard discovery: if `(not result.homepage_ok) and (not result.robots_disallowed_all) and (not result.candidates)`, call `discover_via_wayback` and merge results.

**Modify**: `lavandula/reports/candidate_filter.py::Candidate`
Add `original_source_url: str | None = None` field.

**Migration 005**: Add `reports.original_source_url_redacted TEXT NULL`.

**Modify**: `lavandula/reports/url_guard.py`
Confirm `web.archive.org` and `archive.org` resolve to public IPs and pass `is_address_allowed`. (They do — they're CDN-fronted on Fastly. No code change expected; just an integration test to lock it in.)

**Modify**: `lavandula/reports/async_crawler.py::CrawlStats`
Add `wayback_recoveries: int = 0` and `wayback_attempts: int = 0`.

## Traps to Avoid

1. **Don't crawl Wayback's HTML view.** It's wrapped in an iframe with the Wayback toolbar. The `id_` modifier is required for raw bytes.
2. **Don't trigger Wayback fallback on partial success.** If direct crawl found 5 PDFs but the homepage 403'd on a sub-page, we already have data — don't double-fetch via Wayback.
3. **Don't lose original URL provenance.** The `reports.source_url_redacted` will point to web.archive.org; the *original* URL must be preserved separately for downstream training-data attribution. Otherwise we lose the link to the actual organization's content.
4. **Don't bypass robots.txt via Wayback.** If a site's `robots.txt` denies us, that's a deliberate decision — using Wayback to circumvent it is rude and possibly a TOS violation. AC3 enforces this.
5. **Don't set `discovered_via='wayback'` for the URL field but `homepage-link` for the attribution; downstream filters key on these.** Be consistent.
6. **Don't assume Wayback PDFs are current.** Snapshots can be years stale. The training-data downstream may want to weight Wayback-sourced data lower or version-flag them. Out of scope for this spec but documented for future use.
7. **Don't query Wayback CDX without a `from=` filter.** Without it, the response can be huge (decades of captures). The `from=20200101` plus `collapse=urlkey` keeps responses bounded.
8. **Don't retry Wayback on every transient failure.** If Wayback returns 0 PDFs once, retrying won't help — that org genuinely has no archived data. The attempts cap (Spec 0021 follow-up) handles this naturally.

## Security Considerations

- **No new SSRF surface.** Wayback URLs all point at `web.archive.org` (a public CDN-fronted service). The original archived URL is metadata only and never used for outbound connections. Our existing `AsyncHostPinCache` resolver validates the connect target.
- **Content trust.** Wayback content is served as the original site served it at archive time. Active-content scanning (`scan_active_content`) and PDF structure validation (`_validate_pdf_structure`) apply unchanged.
- **License**. Wayback's CDX API is public and documented. No API key required for our query rate. Their TOS allows automated queries within reasonable limits; our throttle stays well below.
- **Stale data risk for training**. A 3-year-old archived PDF may not represent the org's current public position. Downstream training-data consumers should weight `discovered_via='wayback'` records appropriately. This is captured in the schema (new `original_source_url` field, Wayback timestamp implicit in `source_url_redacted`).

## Out of Scope (deferred to future specs)

- Commercial scraping APIs (ScraperAPI, ScrapingBee, BrightData) for sites with no Wayback coverage.
- Headless browser fallback for stubborn cases.
- Wayback's "Save Page Now" API to trigger fresh archives of orgs that haven't been captured recently.
- Cloudflare verified-bot program enrollment (months-long process; long-term play).

## Open Questions

1. Should Wayback fallback also fire when direct crawl returns a site_error (5xx, DNS failure) but not a Cloudflare 403? **Tentative answer**: yes — same gate (no candidates, no homepage_ok). The spec's AC1 covers this.
2. When CDX returns >30 PDFs for one site, which to pick? **Tentative answer**: the most recent capture of each unique URL (CDX returns sorted; `collapse=urlkey` already gives one row per unique URL with the latest capture). Take all up to the limit.
3. Should `wayback_archive` candidates flow through the LLM classifier the same way? **Yes** — they're real PDFs. The classifier output (and `report_year` extraction) doesn't care where the PDF came from.
4. Tag the `crawled_orgs` row with a flag indicating "this org was recovered via Wayback"? **Probably yes** for ops dashboards, but kept out of the AC list to avoid scope creep. Could add a `discovery_source TEXT` column in a follow-up.

## Changelog

(v1: initial draft 2026-04-25)
