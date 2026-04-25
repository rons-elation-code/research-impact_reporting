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

### Red Team Security Review (MANDATORY)
**Date**: 2026-04-25
**Commands**:
```
consult --model codex  --type red-team-spec spec 0022
consult --model claude --type red-team-spec spec 0022
```

**Verdicts**: REQUEST_CHANGES (Codex MEDIUM, Claude HIGH)

| Model | Verdict | Severity counts | Top issues |
|-------|---------|-----------------|------------|
| Codex | REQUEST_CHANGES (MEDIUM) | 0 CRIT, 2 HIGH, 3 MED, 1 LOW | Retry/backoff state machine ambiguity; conflation of `[]` vs header-only vs malformed CDX responses |
| Claude | REQUEST_CHANGES (HIGH) | **2 CRIT**, 4 HIGH, 6 MED, 4 LOW | **Query-param injection in CDX URL**; **path-traversal / URL smuggling in Wayback URL**; archive poisoning via Save Page Now; subdomain takeover history; single-empty too aggressive; missing active-content scan |

**All CRITICALs and HIGHs addressed in v3.** See v3 changelog below for the mapping.

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

### State machine: outcome classification

The Wayback fallback has two phases — CDX query, then per-PDF download — each with their own failure modes. The spec defines exactly one outcome class per (phase, failure-mode) combination, no overlapping retry budgets, no ambiguity.

**Phase 1: CDX query.** Single GET, no retries within a run (Wayback's CDX is reliable enough that retry-within-run isn't worth the complexity).

| CDX response | Outcome | DB action | Counter |
|---|---|---|---|
| HTTP 200 + JSON array with header + 0 data rows (`[[header],]` or `[]`) | `EMPTY` | `status='permanent_skip'`, notes='wayback_no_coverage' | `wayback_empty++` |
| HTTP 200 + JSON parses + ≥1 valid data row (after dedup) | `RECOVERED` | (depends on Phase 2) | (depends on Phase 2) |
| HTTP 200 + JSON parses + ≥1 row but ALL rows fail row-shape validation (len < 3) | `EMPTY` (treat malformed-after-validation as no usable coverage) | `status='permanent_skip'`, notes='wayback_no_coverage' | `wayback_empty++` |
| HTTP 200 + body fails to parse as JSON (HTML rate-limit, maintenance) | `ERROR` | `status='transient'` | `wayback_errors++` |
| HTTP non-2xx (4xx, 5xx, timeout, connection error) | `ERROR` | `status='transient'` | `wayback_errors++` |

**Phase 2: per-PDF download** (only entered if Phase 1 returned `RECOVERED` with N candidates).

Each PDF goes through the standard pipeline: HEAD → GET → magic-byte → structure validation → archive PUT → DB record. Per-PDF failures are recorded in `fetch_log` like any other download (no Wayback-specific accounting). The per-PDF retry policy is the SAME as Spec 0021's retry config (`RETRY_MAX_ATTEMPTS=3`, `RETRY_BACKOFF_SEC=(2.0, 8.0)`) — Wayback failures do not get extra retries beyond what direct downloads get.

**Org-level outcome after Phase 2:**

- **At least 1 PDF successfully archived AND its `upsert_report` flushed durably** → `status='ok'`, `wayback_recoveries++`. Standard Spec 0021 barrier semantics apply (org not marked complete until all download workers' futures resolve).
- **Zero PDFs successfully archived** (all N candidates failed validation/download) → `status='transient'`, `wayback_errors++`. Subject to attempt-cap auto-promotion. Rationale: it's possible the failures were transient (Wayback returning 5xx for the `id_` URLs even though CDX listed them); retry budget is appropriate.

This is **partial-success-permissive**: 1 of 30 PDFs landing is enough to mark the org `ok`. The 29 failed downloads are recorded in `fetch_log` for observability but don't change the org outcome.

### Decisions_log reason taxonomy (bounded enum)

For ops dashboards to measure cause distribution accurately, reason codes are a **stable, bounded enum**. Implementations must pick from this list verbatim; new values require a spec amendment:

**Reason codes for `wayback_query` events (why fallback fired):**

| Code | Meaning |
|---|---|
| `homepage_cloudflare_challenge` | Direct homepage returned 403 with `server: cloudflare` and/or `cf-mitigated` header |
| `homepage_4xx` | Direct homepage returned other 4xx (excluding 403-cloudflare) |
| `homepage_5xx` | Direct homepage returned 5xx |
| `homepage_network_error` | DNS failure, connection refused, TLS error, timeout |
| `homepage_size_capped` | Homepage exceeded `MAX_TEXT_BYTES` |
| `homepage_blocked_content_type` | Homepage Content-Encoding rejection or non-HTML response |

**Outcome codes for `wayback_query` events:**

| Code | Meaning |
|---|---|
| `recovered` | ≥1 PDF eventually archived |
| `empty` | CDX returned 0 usable rows |
| `error` | CDX request failed (timeout / 5xx / malformed) |
| `all_downloads_failed` | CDX returned candidates but all per-PDF downloads failed |

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
- **AC14**: Four-way DB outcome (revised post-red-team to add the two-strikes empty rule):
  - Wayback recovers ≥1 PDF → `crawled_orgs.status='ok'`, attempts++.
  - Wayback CDX errors (5xx/timeout/malformed/oversized) → `status='transient'`, `notes='wayback_error'`, attempts++ (subject to auto-promotion at `MAX_TRANSIENT_ATTEMPTS`).
  - Wayback CDX returns clean empty response, **first time** → `status='transient'`, `notes='wayback_no_coverage'`, attempts++.
  - Wayback CDX returns clean empty response, **second consecutive time** (existing row already has `notes='wayback_no_coverage'`) → `status='permanent_skip'`, attempts++.
  - All Wayback PDF downloads fail validation → `status='transient'`, `notes='wayback_all_downloads_failed'`, attempts++. (Distinct from CDX errors — the CDX query succeeded.)

### Security and SSRF
- **AC15**: SSRF check accepts `web.archive.org` and `archive.org` as connect targets. The original archived URL is metadata only, never used for outbound connections (it's stored in `original_source_url_redacted`, not handed to the HTTP client).
- **AC15.1**: **`original_source_url_redacted` is non-executable provenance metadata.** No code path may use this field as input to a fetcher, resolver, redirector, archive lookup, classifier prompt, or any external system call. This is a normative requirement enforced by code review and a static-analysis test (AC25.1) that greps for the column name in non-test code and confirms it appears only in DB-write or display contexts.
- **AC15.2**: **Domain validation before CDX query construction (CRITICAL).** Before interpolating `domain` into the CDX URL, validate it against `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$` (LDH characters and dots, RFC-1123 hostname format, max 253 chars total). Reject domains containing `?`, `&`, `=`, `#`, `/`, control chars, or anything else that could smuggle CDX query parameters. On rejection, the org is marked transient with `notes='wayback_invalid_domain'`. The validated domain is then `urllib.parse.quote()`-encoded as defense in depth.
- **AC15.3**: **CDX row validation before Wayback URL construction (CRITICAL).** For each CDX row, before constructing the `id_` URL:
  - `timestamp` must match `^\d{14}$` (exactly 14 digits, YYYYMMDDhhmmss format). Rows with malformed timestamps are skipped.
  - `original` must parse as a URL via `urlsplit()` with scheme in `{'http', 'https'}` and a non-empty `hostname`. Other schemes (file, javascript, data, ftp) are dropped.
  - `original` host must pass the same validation as AC15.2 (RFC-1123 hostname).
  - `original` length capped at 2048 chars (rejects oversized URLs that could bloat DB rows or smash buffers downstream).
  - Strip embedded credentials (`https://user:pass@host/path` → `https://host/path`) and URL fragment (`#section`) before storage.
  - When constructing the Wayback URL, `urllib.parse.quote(original, safe=":/?#[]@!$&'()*+,;=")` to neutralize injection via odd characters that survived earlier validation.
- **AC15.4**: **Capture-host ownership constraint (HIGH — archive poisoning mitigation).** For each accepted CDX row, parse `urlsplit(original).hostname` (the "capture host"). Require:
  - Capture host's eTLD+1 matches `seed_etld1` exactly. (Prevents accepting a CDX row where `original` is on an unrelated domain — possible if `matchType=domain` ever returns cross-domain captures due to a CDX bug or schema drift.)
  - At most `WAYBACK_MAX_DISTINCT_SUBDOMAINS` distinct capture hosts contribute to a single org's recovery (default 3). Excess subdomains are dropped after the first 3 unique hosts (sorted by max-timestamp DESC). This bounds the blast radius of historical subdomain takeover: even if one abandoned subdomain was compromised in 2019 and has 50 archived "PDFs", we'll only ingest from 2 other (presumably current) subdomains plus that one.
  - The apex (`{seed_etld1}` itself, no subdomain prefix) MUST be one of the 3 if it has any captures. (Prevents a scenario where 3 subdomains all on takeover-vulnerable infrastructure crowd out the apex.)
- **AC15.5**: **CDX response body cap.** CDX responses are subject to the same `MAX_TEXT_BYTES` cap as homepage / sitemap fetches (currently 5 MB). Responses exceeding the cap are rejected as `wayback_errors` rather than truncated.
- **AC15.6**: **Two-strikes empty rule (HIGH — selective-suppression mitigation).** A single clean-empty CDX response no longer immediately demotes the org to `permanent_skip`. Instead, an empty response writes `status='transient'` with `notes='wayback_no_coverage'`. Only after **two consecutive runs both observe empty** (i.e., the existing row has `status='transient'` AND `notes='wayback_no_coverage'` AND a new empty response is observed) does the upsert promote to `permanent_skip`. Implementation: detect this case in the upsert SQL CASE by checking the existing notes value, OR by a follow-up SQL `UPDATE WHERE notes='wayback_no_coverage' AND attempts >= 2`. Two-empty-response check protects against single-shot adversarial suppression and CDX rate-limit quirks.
- **AC16**: Content-Encoding whitelist (`gzip`, `identity`) applies as today.
- **AC17**: `is_pdf_magic` check applies — Wayback returning HTML disguised as PDF (e.g., a CDX maintenance page served at the `id_` URL) is rejected the same way as direct downloads.
- **AC17.1**: **Active-content scan is normative for Wayback PDFs (HIGH — archive-poisoning defense).** Every Wayback-sourced PDF MUST run through `scan_active_content` *before* archive PUT. PDFs with `pdf_has_javascript`, `pdf_has_launch`, or `pdf_has_uri_actions` set are rejected with `notes='wayback_active_content'` (not archived, not added to `reports`). Direct-crawl PDFs continue to record the flags but archive them; Wayback PDFs cannot, because they have higher base-rate poisoning risk. Embedded files (`pdf_has_embedded`) continue to be archived with the flag set (they're occasionally legitimate).
- **AC17.2**: **Honor `Retry-After` header on 429/503 from Wayback.** When `web.archive.org` returns 429 (rate-limited) or 503 (overloaded) with a `Retry-After` header, sleep for the indicated duration (capped at 60s) before the next request to that host. This avoids triggering Wayback's IP-ban escalation, which would deny the recovery path for ALL orgs simultaneously.
- **AC17.3**: **Single throttle bucket for Wayback hosts.** `web.archive.org` and `archive.org` share a single semaphore in `AsyncHostThrottle._semaphores` (both behind the same Fastly CDN; treating them as separate hosts would exceed Wayback's per-IP rate). Implementation: `host_overrides` map normalizes both hosts to the same key, e.g., `"archive.org"`.

### Observability
- **AC18**: `CrawlStats` adds three counters:
  - `wayback_attempts: int` — total CDX queries fired (one per CF-blocked org)
  - `wayback_recoveries: int` — orgs where Wayback yielded ≥1 PDF
  - `wayback_empty: int` — orgs where Wayback returned a clean empty response (no coverage)
  - `wayback_errors: int` — orgs where Wayback CDX failed (5xx/timeout/malformed)
  - All four are logged in the final crawl summary line.
- **AC19**: New `decisions_log` event type `wayback_query` records each CDX request with these fields:
  - `ein`, `domain`, `response_status` (HTTP code), `row_count_raw` (rows in response), `row_count_after_dedup`, `elapsed_ms`
  - `outcome`: one of `recovered`, `empty_first`, `empty_second_promoted`, `error`, `all_downloads_failed`, `invalid_domain` (bounded enum, drives ops dashboards)
  - `reason`: why fallback fired, from the AC3.1 enum
  - `capture_hosts`: distinct subdomains contributing to the candidate set (e.g., `["sloan.org", "reports.sloan.org"]`) — supports archive-poisoning forensics
- **AC19.1**: Per-PDF `fetch_log` rows for Wayback downloads include the CDX-supplied `digest` field (a SHA1) in `notes` as `wayback_digest:{sha1}`. If poisoning is later suspected, the same digest can be searched across all orgs to identify reused malicious captures.

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
- **AC24.4**: Integration test: stub CDX returns 200 with `[[header],]` (header-only, zero data rows) — assert classified as `EMPTY`, org marked `permanent_skip`.
- **AC24.5**: Integration test: stub CDX returns 5 rows but ALL have `len(row) < 3` (malformed shape) — assert classified as `EMPTY` with notes='wayback_no_coverage' (treat as no usable coverage, not error).
- **AC24.6**: Integration test: partial success — stub CDX returns 3 candidates, downloads succeed for 1, fail magic-byte for 1, return 5xx for 1. Assert org marked `status='ok'` (1 success suffices), `wayback_recoveries++`, the 1 successful PDF has its row in `reports`, the 2 failures appear in `fetch_log`.
- **AC24.7**: Idempotency test: re-run the Wayback fallback for an org that was previously recovered. Same PDFs upsert into existing rows (via `content_sha256` PK in `upsert_report`). No duplicate `reports` rows. `wayback_recoveries` increments again.
- **AC24.8**: Original URL canonicalization test: CDX returns rows with embedded credentials (`https://user:pw@host/x.pdf`), `javascript:` URL, fragment (`#chapter1`). Credentials stripped, javascript dropped, fragment stripped before storage. Assert resulting `original_source_url` is clean.
- **AC25**: SSRF test: confirm `web.archive.org` resolves and connects via `AsyncHostPinCache`; the original archived URL is never resolved or connected to.
- **AC25.1**: Static-analysis test: scan the codebase (excluding tests) for references to `original_source_url` and `original_source_url_redacted`; confirm all occurrences are in DB-write context (writing to the column) or display/logging context. Fails if any occurrence is in a fetch / resolve / redirect / classifier-prompt path.
- **AC25.2**: **Domain injection test (CRITICAL)**: pass a malicious seed_url whose hostname contains `&matchType=exact&filter=` — assert the validator rejects it before constructing the CDX URL, no outbound request is issued, org marked `wayback_invalid_domain`.
- **AC25.3**: **CDX row injection test (CRITICAL)**: stub CDX returns rows with `timestamp="../../etc"` (rejected as not 14-digit), `original="javascript:alert(1)"` (rejected as wrong scheme), `original="http://x.com\r\nHost: evil"` (rejected, parse fails or not an http(s) URL), `original="http://" + "a"*3000` (rejected, length cap). Assert all malicious rows skipped, valid rows accepted.
- **AC25.4**: **Capture-host enforcement test (HIGH)**: stub CDX returns 5 rows from 5 distinct subdomains. Assert only the apex + 2 most-recent subdomains are accepted (cap = 3), the other 2 dropped. Variant: CDX returns rows from `evil.com` for a `sloan.org` query (cross-domain). Assert all dropped — eTLD+1 mismatch.
- **AC25.5**: **Active-content rejection test (HIGH)**: stub a Wayback PDF with embedded JavaScript (`/JavaScript` token in body). Assert candidate rejected via `scan_active_content`, `notes='wayback_active_content'` recorded in `fetch_log`, no archive PUT, no `reports` row.
- **AC25.6**: **Two-strikes empty test (HIGH)**: simulate two consecutive runs both returning empty CDX. Assert run 1 writes `status='transient'` `notes='wayback_no_coverage'`; run 2 promotes to `status='permanent_skip'`.
- **AC25.7**: **Retry-After honor test**: stub Wayback returning 429 with `Retry-After: 5`. Assert next request to `web.archive.org` waits ≥5s (mockable via injected clock).

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

## Threat Model

The original draft treated Wayback as a benign mirror. Red-team review (Claude) correctly flagged that **Wayback's archive is partially attacker-influenced**: anyone can submit a URL via "Save Page Now" or have content captured during opportunistic crawls. With `matchType=domain` and no recency floor, this becomes a real training-data injection path:

1. **Save Page Now poisoning.** An attacker submits `https://abandoned-subdomain.example.org/fake-annual-report.pdf` (where `abandoned-subdomain` is a takeover-able CNAME for a nonprofit's apex domain). The PDF is real (passes magic-byte + structure validation), the served Content-Type is `application/pdf`, CDX captures it.
2. **Historical subdomain takeover.** `blog.example.org` was hijacked in 2019 and served attacker PDFs that Wayback captured. The org regained control years ago, but those captures still appear in our CDX results today.
3. **Selective empty-response suppression.** A network-positioned attacker (or a Wayback rate-limit quirk) returns an empty 200 for queries against specific orgs. With v2's "single empty → permanent_skip" rule, those orgs get silently demoted from one network blip.

These are addressed by AC15.3-AC15.6, AC14 (revised), and AC17.1 below.

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

**v3** (2026-04-25, post-red-team): Codex + Claude red-team consultation. Major security-driven changes:

1. **Threat model added.** Documents archive poisoning via Save Page Now, historical subdomain takeover, and selective empty-response suppression as in-scope adversaries.
2. **CRITICAL #1 (Claude): Query-parameter injection.** Added strict RFC-1123 hostname validation + `urllib.parse.quote()` on `domain` before CDX URL construction (AC15.2). Test AC25.2 verifies `evil.org&matchType=exact` is rejected.
3. **CRITICAL #2 (Claude): Path-traversal / URL smuggling.** Added per-row CDX validation: `timestamp` regex `^\d{14}$`, `original` URL parse + scheme check + length cap (2048) + credential/fragment stripping (AC15.3). Test AC25.3 covers traversal, JS scheme, header injection, oversized URLs.
4. **HIGH (Claude): Archive poisoning mitigation.** Added capture-host eTLD+1 match + `WAYBACK_MAX_DISTINCT_SUBDOMAINS=3` cap with apex-required (AC15.4). Bounds blast radius of subdomain takeover. Test AC25.4 covers cross-domain rejection and subdomain capping.
5. **HIGH (Claude): Two-strikes empty rule.** v2's "single empty → permanent_skip" was too aggressive. Now requires two consecutive empty responses across runs (AC14, AC15.6). Test AC25.6 covers the two-strikes flow. Mitigates selective-suppression risk.
6. **HIGH (Claude): Active-content scan normative for Wayback PDFs.** PDFs with `pdf_has_javascript`/`pdf_has_launch`/`pdf_has_uri_actions` are rejected outright for Wayback (not archived), unlike direct-crawl PDFs which are archived with the flags set. AC17.1 + test AC25.5.
7. **HIGH (Codex): State machine precision.** Added explicit two-phase outcome model: CDX query phase + per-PDF download phase, with no overlapping retry budgets. Distinguishes 4 org outcomes (recovered, error, empty_first, empty_second_promoted, all_downloads_failed). New "State machine: outcome classification" section.
8. **HIGH (Codex): Distinguish `[]` vs header-only vs malformed CDX responses.** Now explicitly: `[]` or header-only → EMPTY; malformed JSON or HTTP error → ERROR; all-rows-fail-validation → EMPTY (treat unusable coverage as no coverage).
9. **MEDIUM (Codex): Decisions_log reason taxonomy.** Bounded enum for `reason` (why fallback fired) and `outcome` (what happened). Listed in section "Decisions_log reason taxonomy".
10. **MEDIUM (Claude): CDX response body cap.** AC15.5 enforces `MAX_TEXT_BYTES=5MB` on CDX responses.
11. **MEDIUM (Claude): Honor `Retry-After` from Wayback.** AC17.2 + test AC25.7. Avoids triggering Wayback IP-ban escalation.
12. **MEDIUM (Claude): Single throttle bucket for Wayback hosts.** AC17.3 — both `web.archive.org` and `archive.org` share one semaphore (same Fastly fronting).
13. **MEDIUM (Claude): Log `digest` and `capture_host`.** AC19/AC19.1 — supports archive-poisoning forensics if a poisoned PDF is later detected.
14. **HIGH (Codex): `original_source_url_redacted` is non-executable.** AC15.1 + static-analysis test AC25.1 enforce that the field is never used as input to fetcher/resolver/redirector/classifier-prompt paths.

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
