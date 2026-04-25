# Spec 0022 — Wayback Machine CDX Fallback for Cloudflare-blocked Sites

**Status**: Conceived (initial draft)
**Author**: Architect
**Created**: 2026-04-25
**Dependencies**: 0021 (Async I/O Crawler Pipeline)

---

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: 2026-04-25
**Models Consulted**: Codex ✅, Claude ✅ (Gemini quota exhausted)
**Commands**:
```
consult --model codex  --type spec-review spec 0022
consult --model claude --type spec-review spec 0022
```

**Verdicts**: REQUEST_CHANGES (Codex, HIGH), COMMENT (Claude, HIGH)

| Model | Verdict | Top issues |
|-------|---------|------------|
| Codex | REQUEST_CHANGES | `collapse=urlkey` returns earliest capture (not latest); limit/cap inconsistency; 0-PDFs handling underspecified; throttle math hand-wavy; redirect handling missing; schema redaction unspecified; attribution enum consumers not enumerated; missing malformed-data tests; observability needs reason codes |
| Claude | COMMENT | Subdomain matching missing (`matchType=domain`); throttle math wrong (~26min worst case, not 50s); `from=` cutoff contradicts goal of recovering historical reports; JSON parse failure on 200 unhandled; verify `attribution_confidence` is not an enum |

All issues addressed in v2. See changelog below.

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
  &matchType=domain                 (include subdomains: reports.sloan.org, cdn.example.org)
  &filter=mimetype:application/pdf
  &filter=statuscode:200
  &output=json
  &limit=500                        (cap response; client-side dedup picks most recent N)
```

**Why these filters:**
- `matchType=domain` — many nonprofits host reports on subdomains (`reports.sloan.org`, `assets.example.org`, `cdn.foundation.org`). Without this, recovery is silently incomplete. The cost is a wider response, mitigated by `limit=500`.
- `mimetype:application/pdf` — Wayback indexes the original Content-Type. **Trade-off:** sites that served PDFs as `application/octet-stream` or `binary/octet-stream` will be missed. Acceptable: the magic-byte check (`is_pdf_magic`) downstream handles those when they're discovered through the regular crawl path; for the fallback, we accept the slight under-recovery in exchange for a clean filter. Documented in Open Questions.
- `statuscode:200` — exclude 404/410 captures (broken at archive time).
- **No `from=` filter.** The original draft set `from=20200101`, but historical 990s and annual reports from 2010-2019 are *valuable* training data, not stale. Removing the cutoff lets us recover endfund.org's 2012-2013 reports observed in the diagnostic sample. The `limit=500` plus client-side cap of 30 unique PDFs already bounds the response.
- `limit=500` — bound the response payload. CDX default ordering is `(urlkey ASC, timestamp ASC)`, which means using `collapse=urlkey` would give us the *earliest* capture per URL, not the latest. We deliberately do NOT use `collapse` and instead fetch up to 500 raw rows, then dedup client-side (see next section).

Response is a JSON array of `[urlkey, timestamp, original, mimetype, statuscode, digest, length]` rows, with a header row.

### Client-side dedup and capture selection

Because CDX returns multiple captures per unique URL sorted ASC by timestamp, the client must:

1. Skip the JSON header row.
2. Group remaining rows by `urlkey`.
3. Within each group, pick the row with the **maximum timestamp** (the most recent capture).
4. Sort the resulting unique-PDF list by max-timestamp DESC (most recent first).
5. Take the first `WAYBACK_MAX_PDFS_PER_ORG` (default **30**, matching `CANDIDATE_CAP_PER_ORG`).

This gives us "the 30 most-recently-archived unique PDFs from the domain". If the site has fewer than 500 captures total, we get the full set deduped. If the site has more than 500, we miss the long tail of older PDFs — acceptable, since training data prefers recent material anyway.

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

Wayback's documented rate limits are ~15 req/sec sustained per IP, much higher than the 1-req-per-3-seconds the default `AsyncHostThrottle` would impose on `web.archive.org`. Using the default throttle would create a real bottleneck:

- 17 CF-blocked orgs × (1 CDX query + up to 30 PDF downloads each) = up to 527 sequential Wayback requests
- At 1 req / 3 sec = ~26 minutes of serialized work, far exceeding the rest of the crawl.

**Decision:** introduce a Wayback-specific throttle that allows higher concurrency:

- New config: `WAYBACK_REQUEST_DELAY_SEC = 0.25` (4 req/sec, 25% of Wayback's documented rate limit — leaves comfortable headroom).
- `AsyncHostThrottle` extended to accept a per-host override map. The host `web.archive.org` (and `archive.org`) gets 0.25s; everything else stays at 3.0s.
- At 4 req/sec, the worst-case 17-org × 31-request batch finishes in ~130 seconds — still serial, but no longer dominant.

If Wayback returns 429 or 503, we back off and apply standard retry semantics (Spec 0021's `RETRY_BACKOFF_SEC`). After two consecutive 429s on Wayback, abandon the org as transient (don't retry mid-run).

### Pipeline integration

The Wayback fetcher returns `Candidate` instances identical to direct discovery, except:

- `url` → the Wayback raw-bytes URL (`https://web.archive.org/web/{timestamp}id_/{original}`)
- `referring_page_url` → the original homepage URL (for attribution traceability)
- `discovered_via` → `'wayback'`
- `hosting_platform` → `'wayback'`
- `attribution_confidence` → `'wayback_archive'` (new value; documents that this is an archived copy, not a live retrieval)

Downloads, magic-byte checks, structure validation, archive PUTs, and DB writes are all the existing pipeline — no special-casing.

### Resume + retry semantics (interaction with Spec 0021's status/attempts)

Three distinct outcomes drive three distinct DB states. **The behavior on "0 PDFs found" is a deliberate product decision** — a clean empty CDX response is highly unlikely to change on the next run, so retrying is wasted work.

- **Wayback recovers ≥1 PDF** → `crawled_orgs.status='ok'`, `attempts` increments. Done.
- **Wayback CDX errors** (5xx, timeout, malformed JSON, 429 rate-limit) → `status='transient'`, `attempts` increments. Subsequent runs retry. Auto-promoted to `permanent_skip` after `MAX_TRANSIENT_ATTEMPTS`.
- **Wayback CDX returns clean empty response (0 rows)** → `status='permanent_skip'` immediately, with reason `wayback_no_coverage`. Rationale: Wayback's archive is an external state we don't control, but a clean "no captures" response on a properly-formed query is stable across days/weeks. Retrying the same query for an org that has zero archived PDFs is pure waste. Operators can manually re-trigger by setting `--refresh` if they suspect coverage has improved (e.g., after submitting URLs to Wayback's "Save Page Now").

## Acceptance Criteria

### Detection
- **AC1**: Wayback fallback fires only when `(not homepage_ok) AND (not robots_disallowed_all) AND (len(candidates) == 0)` after direct discovery.
- **AC2**: Wayback fallback does NOT fire when direct discovery succeeded with 1+ candidates, even if some subpages 403'd.
- **AC3**: Wayback fallback does NOT fire when robots.txt blocks crawling. **This is a deliberate product rule**, not an inference about Wayback's permissions: we treat robots-blocked as the org's explicit "do not crawl me" signal and we honor it across paths, including archived copies. Operators who want to override can flag specific orgs manually (out of scope here).
- **AC3.1**: The reason the fallback fired is recorded in `decisions_log` (e.g., `direct_homepage_403`, `direct_homepage_5xx`, `direct_homepage_dns_fail`). This drives operational metrics and lets us measure CF-block prevalence vs other failure modes.

### CDX query
- **AC4**: Single CDX GET per org with `matchType=domain` (subdomain inclusion), `filter=mimetype:application/pdf`, `filter=statuscode:200`, `output=json`, `limit=500`. **No `from=` filter** (preserves historical reports). **No `collapse=urlkey`** (would return earliest, not latest, capture).
- **AC5**: Response is parsed as JSON; first row is the header and skipped. **AC5.1**: If the response is HTTP 200 but the body fails to parse as JSON (e.g., CDX rate-limit HTML, maintenance page, malformed), treat identically to a non-200 response — Wayback failure → org marked transient.
- **AC5.2**: Defensive parsing — extra columns are ignored; rows shorter than the minimum required (`urlkey`, `timestamp`, `original`) are skipped silently. Schema drift on Wayback's side does not crash the crawler.
- **AC6**: CDX query timeout = 15s. On timeout or non-2xx response, treat the org as transient (no Wayback recovery this run).
- **AC7**: After client-side dedup (group by `urlkey`, pick max timestamp per group, sort by timestamp DESC), the candidate list is capped at `WAYBACK_MAX_PDFS_PER_ORG` (default 30, matching `CANDIDATE_CAP_PER_ORG`).

### Download
- **AC8**: Each PDF downloaded via the `id_` modifier URL: `https://web.archive.org/web/{timestamp}id_/{original}`.
- **AC9**: Wayback URLs flow through the existing `AsyncHTTPClient` with manual gzip decompression, magic-byte check, and `_validate_pdf_structure`.
- **AC10**: Wayback-specific per-host throttle: `web.archive.org` and `archive.org` use `WAYBACK_REQUEST_DELAY_SEC = 0.25` (4 req/sec) instead of the default 3.0s. All other hosts unchanged.
- **AC10.1**: Wayback redirects (3xx within `web.archive.org`, e.g., to a different snapshot timestamp) are followed up to `MAX_REDIRECTS`. Cross-host redirects from Wayback to a non-archive.org host are rejected as `blocked_redirect` — Wayback should never redirect to the original site, and if it does we don't want to suddenly hit a CF-blocked target without going through the proper channel.

### Attribution and DB writes
- **AC11**: Each Wayback-discovered candidate has `discovered_via='wayback'`, `hosting_platform='wayback'`, `attribution_confidence='wayback_archive'`, AND `original_source_url` set to the original (target) URL preserved from the CDX `original` column.
- **AC12**: `reports.source_url_redacted` records the redacted Wayback URL (for fetch reproducibility). `reports.referring_page_url_redacted` records the org's original homepage URL.
- **AC13**: New column `reports.original_source_url_redacted TEXT NULL` (Migration 005). Same `redact_url()` treatment as other URL fields. Existing rows backfill to NULL (correct: pre-Wayback rows have no separate original-source URL distinct from `source_url_redacted`).
- **AC13.1**: `attribution_confidence` is currently a free-text column (no DB CHECK constraint). Adding the `'wayback_archive'` value requires no schema change. The migration explicitly verifies no CHECK constraint exists; if one is added in the future, migration 005 must include the new value.
- **AC13.2**: All consumers of `attribution_confidence` accept the new value: `candidate_filter._is_html_subpage_candidate` (treats wayback as non-subpage, correct), the `discover_via` selector in `_pick_discovered_via` (wayback paths use the `discovered_via='wayback'` directly, no further mapping), and any reporting/dashboard queries (verified via grep — no enum-style filters on `attribution_confidence` in the codebase as of this spec).
- **AC14**: Three-way DB outcome:
  - Wayback recovers ≥1 PDF → `crawled_orgs.status='ok'`, attempts++
  - Wayback CDX errors (5xx/timeout/malformed) → `status='transient'`, attempts++ (subject to auto-promotion at `MAX_TRANSIENT_ATTEMPTS`)
  - Wayback CDX returns clean empty response → `status='permanent_skip'` immediately, with `notes='wayback_no_coverage'` for diagnostic clarity

### Security and SSRF
- **AC15**: SSRF check accepts `web.archive.org` and `archive.org` as connect targets. The original archived URL is metadata only, never used for outbound connections (it's stored in `original_source_url_redacted`, not handed to the HTTP client).
- **AC16**: Content-Encoding whitelist (`gzip`, `identity`) applies as today.
- **AC17**: `is_pdf_magic` check applies — Wayback returning HTML disguised as PDF (e.g., a CDX maintenance page served at the `id_` URL) is rejected the same way as direct downloads.

### Observability
- **AC18**: `CrawlStats` adds three counters:
  - `wayback_attempts: int` — total CDX queries fired (one per CF-blocked org)
  - `wayback_recoveries: int` — orgs where Wayback yielded ≥1 PDF
  - `wayback_empty: int` — orgs where Wayback returned a clean empty response (no coverage)
  - `wayback_errors: int` — orgs where Wayback CDX failed (5xx/timeout/malformed)
  - All four are logged in the final crawl summary line.
- **AC19**: New `decisions_log` event type `wayback_query` records each CDX request with: ein, domain, response status, row count after dedup, elapsed_ms, outcome (`recovered`, `empty`, `error`), and the reason fallback fired (from AC3.1).

### Testing
- **AC20**: Unit test: CDX response parsing — header skip, client-side dedup picks max timestamp per urlkey, sort by timestamp DESC, cap at `WAYBACK_MAX_PDFS_PER_ORG`.
- **AC20.1**: Unit test: malformed CDX response — non-JSON body, JSON with truncated rows (length < 3), JSON with extra columns, empty array. All handled gracefully without raising.
- **AC21**: Unit test: detection logic — fires only under AC1 condition, never under AC2 (partial success) or AC3 (robots-blocked).
- **AC22**: Unit test: candidate construction — correct Wayback URL format with `id_` modifier, all attribution fields populated, `original_source_url` preserved.
- **AC23**: Integration test: stub homepage returns 403 with `server: cloudflare`; stub CDX returns 3 PDF rows (different urlkeys); assert 3 candidates with correct fields, downloads succeed, all 3 reports written to DB with `discovered_via='wayback'` and `original_source_url_redacted` matching the CDX `original` field.
- **AC24**: Integration test: stub CDX returns clean empty array; assert org marked `status='permanent_skip'` with `notes='wayback_no_coverage'`, no PDFs downloaded, `wayback_empty` counter incremented.
- **AC24.1**: Integration test: stub CDX returns 5xx; assert org marked `status='transient'`, attempts incremented, `wayback_errors` counter incremented.
- **AC24.2**: Integration test: stub CDX returns 200 but with HTML body (rate-limit page); assert handled as `wayback_errors` (not a Python exception).
- **AC24.3**: Integration test: Wayback `id_` URL returns HTML disguised as PDF (wrong magic bytes); assert candidate rejected via `is_pdf_magic`, no archive PUT, no report row.
- **AC25**: SSRF test: confirm `web.archive.org` resolves and connects via `AsyncHostPinCache`; the original archived URL is never resolved or connected to.

### Performance
- **AC26**: Wayback CDX query adds at most 15s per CF-blocked org (timeout). Successful queries typically <2s.
- **AC27**: With `WAYBACK_REQUEST_DELAY_SEC=0.25`, the worst-case Wayback batch (17 orgs × ~31 reqs each = ~527 reqs) completes in ~130s. Acceptable: this runs concurrently with the other 83 orgs' direct downloads, so wall-clock impact is negligible (the slowest org dominates regardless).

## Technical Implementation Sketch

**New file**: `lavandula/reports/wayback_fallback.py` (~200 lines)

```python
class WaybackOutcome(str, Enum):
    RECOVERED = "recovered"  # ≥1 PDF found, candidates returned
    EMPTY     = "empty"       # CDX returned 0 rows cleanly (no archive coverage)
    ERROR     = "error"       # CDX timeout / 5xx / malformed JSON

async def discover_via_wayback(
    *,
    seed_url: str,
    seed_etld1: str,
    client: AsyncHTTPClient,
    ein: str,
) -> tuple[WaybackOutcome, list[Candidate]]:
    """Query Wayback CDX for PDFs under the domain. Returns the outcome
    plus candidates pointing at web.archive.org raw-bytes URLs.
    """
    domain = urlsplit(seed_url).hostname or seed_etld1
    cdx_url = (
        f"https://web.archive.org/cdx/search/cdx?"
        f"url={domain}/*&"
        f"matchType=domain&"
        f"filter=mimetype:application/pdf&"
        f"filter=statuscode:200&"
        f"output=json&"
        f"limit=500"
    )
    r = await client.get(cdx_url, kind="wayback-cdx")
    if r.status != "ok" or not r.body:
        return (WaybackOutcome.ERROR, [])
    try:
        rows = json.loads(r.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (WaybackOutcome.ERROR, [])
    if not isinstance(rows, list) or len(rows) < 2:
        return (WaybackOutcome.EMPTY, [])  # header only or empty array

    # Skip header. Group by urlkey, pick max timestamp per group, sort DESC.
    by_urlkey: dict[str, list] = {}
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        urlkey = row[0]
        prev = by_urlkey.get(urlkey)
        if prev is None or row[1] > prev[1]:  # max timestamp per urlkey
            by_urlkey[urlkey] = row

    if not by_urlkey:
        return (WaybackOutcome.EMPTY, [])

    deduped = sorted(by_urlkey.values(), key=lambda r: r[1], reverse=True)
    deduped = deduped[: config.WAYBACK_MAX_PDFS_PER_ORG]

    candidates = []
    for row in deduped:
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
Add `wayback_attempts: int = 0`, `wayback_recoveries: int = 0`, `wayback_empty: int = 0`, `wayback_errors: int = 0`.

**Modify**: `lavandula/reports/async_host_throttle.py::AsyncHostThrottle`
Accept an optional `host_overrides: dict[str, float]` map. `web.archive.org` and `archive.org` use `WAYBACK_REQUEST_DELAY_SEC=0.25` from config.

## Traps to Avoid

1. **Don't crawl Wayback's HTML view.** It's wrapped in an iframe with the Wayback toolbar. The `id_` modifier is required for raw bytes.
2. **Don't trigger Wayback fallback on partial success.** If direct crawl found 5 PDFs but the homepage 403'd on a sub-page, we already have data — don't double-fetch via Wayback.
3. **Don't lose original URL provenance.** The `reports.source_url_redacted` will point to web.archive.org; the *original* URL must be preserved separately in `original_source_url_redacted` for downstream training-data attribution. Otherwise we lose the link to the actual organization's content.
4. **Don't bypass robots.txt via Wayback.** If a site's `robots.txt` denies us, that's a deliberate decision — using Wayback to circumvent it is rude and possibly a TOS violation. AC3 enforces this as a deliberate product rule (we're not inferring archival permissions; we're honoring the org's "do not crawl me" signal across all paths).
5. **Don't mix attribution fields.** Set `discovered_via='wayback'` AND `hosting_platform='wayback'` AND `attribution_confidence='wayback_archive'` together. Downstream filters and dashboards key on each; setting only one creates inconsistent records.
6. **Don't assume Wayback PDFs are current.** Snapshots can be years stale. The training-data downstream may want to weight Wayback-sourced data lower or version-flag them. Out of scope for this spec but documented for future use.
7. **Don't use `collapse=urlkey` on the CDX query.** It returns the *earliest* capture per URL (CDX default sort is `urlkey ASC, timestamp ASC`), which is the opposite of what we want. Fetch raw rows and dedup client-side, picking max timestamp per urlkey.
8. **Don't omit `matchType=domain`.** Default match is exact-host only, missing `reports.example.org`, `cdn.example.org`, etc. Many nonprofits split content across subdomains.
9. **Don't retry Wayback on a clean empty response.** If CDX returns 0 rows for a site that we successfully queried, that's a stable answer — the site has no archived PDFs. AC14 marks it `permanent_skip` immediately rather than burning the 3-attempt transient budget on a query that won't change.
10. **Don't follow cross-host Wayback redirects.** If Wayback ever redirects from `web.archive.org` to a non-archive.org host (e.g., directly to the live site), reject as `blocked_redirect` — we don't want to suddenly hit a CF-blocked target without going through proper detection.
11. **Don't catch JSON parse errors as if they were 200 responses.** If CDX returns 200 with HTML (rate-limit page, maintenance), the body won't parse. Treat parse failures identically to 5xx — Wayback failure → transient.

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

1. **Should Wayback fallback also fire when direct crawl returns a site_error (5xx, DNS failure) but not a Cloudflare 403?** Yes — same gate (zero candidates + homepage not OK). The cause of homepage failure is recorded in `decisions_log` for ops visibility (AC3.1) but doesn't gate the fallback decision.
2. **MIME filter coverage trade-off.** `filter=mimetype:application/pdf` may miss PDFs originally served with `application/octet-stream` or `binary/octet-stream`. We accept this rather than broaden the filter (which would force us to download non-PDF content and rely solely on magic-byte rejection). For the typical nonprofit annual report use case, the standard MIME is dominant. Re-evaluate if recovery rate is materially below the 70-80% sample estimate.
3. **Should `wayback_archive` candidates flow through the LLM classifier the same way?** Yes — they're real PDFs. The classifier output (and `report_year` extraction) doesn't care where the PDF came from.
4. **Tag the `crawled_orgs` row with a flag indicating "this org was recovered via Wayback"?** Deferred. Operators can derive this from `reports.discovered_via='wayback'` joined to `crawled_orgs`. If a future dashboard needs a faster lookup, add a `discovery_source TEXT` column then.
5. **30-PDF cap on high-coverage orgs.** Sloan.org has 50+ archived PDFs; we cap at `WAYBACK_MAX_PDFS_PER_ORG=30`. This matches the direct-crawl `CANDIDATE_CAP_PER_ORG`, so the trade-off is consistent. Re-evaluate if material training-data is being missed for high-value orgs.

## Changelog

**v1** (2026-04-25): Initial draft.

**v2** (2026-04-25, post-review): Codex + Claude consultation. Major changes:
1. Dropped `collapse=urlkey` (returns earliest capture, not latest). Fetch up to 500 raw rows, dedup client-side by max(timestamp) per urlkey, sort DESC, cap at 30.
2. Added `matchType=domain` to catch subdomain-hosted PDFs (`reports.sloan.org`, `cdn.example.org`).
3. Dropped the `from=20200101` filter — historical reports (2010-2019) are valuable training data.
4. Added Wayback-specific throttle (`WAYBACK_REQUEST_DELAY_SEC=0.25`) to fix the 26-min serialization issue. Default 3.0s would create a real bottleneck.
5. Made the "0 PDFs" outcome a deliberate product decision: clean empty response → `permanent_skip` immediately, not transient (avoids wasting retry budget on stable empty results).
6. Clarified robots gate as a deliberate product rule, not an inference about Wayback's permissions.
7. Added explicit handling for: cross-host Wayback redirects (rejected), JSON parse failures (treated as Wayback error), CDX schema drift (defensive parsing).
8. Verified `attribution_confidence` is free-text (not enum), no schema change needed for `'wayback_archive'`.
9. Split observability counters: `wayback_attempts`, `wayback_recoveries`, `wayback_empty`, `wayback_errors`. Added reason code (`AC3.1`) for why fallback fired.
10. Added missing test coverage: malformed CDX JSON, schema drift, non-PDF body returned by `id_` URL, 5xx responses.
11. Updated implementation sketch to return `(WaybackOutcome, candidates)` tuple so the caller can drive AC14's three-way DB outcome.
