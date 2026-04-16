---
spec_id: "0001"
title: "Nonprofit Seed List Extraction"
version: 2
features: []
---

# Specification: Nonprofit Seed List Extraction

## Metadata
- **ID**: spec-2026-04-16-nonprofit-seed-list-extraction
- **Status**: draft
- **Created**: 2026-04-16

## Clarifying Questions Asked

The following exchange shaped this spec:

- **Q: What is the business goal?**
  A: Lavandula Design wants to offer nonprofit impact-report / annual-report design as a service. We first need a catalogue of existing reports to study design trends, build an inspiration library, and generate prospect lists. Harvesting reports requires a seed list of nonprofit websites; this spec produces that seed list.

- **Q: Where should the nonprofit list come from?**
  A: Ruled out: IRS BMF/990 XML (too administrative, no website URLs / too much enrichment needed), agency portfolios (too curated / too sliced), `nonprofitlocator.org` (verified: no website URLs on org pages). Selected: **Charity Navigator**, which exposes all ~48K rated-org profile URLs via a public sitemap index, and each profile page includes the org's website URL plus rating/revenue/sector metadata.

- **Q: API or scrape?**
  A: Scrape. Charity Navigator's commercial data API has non-public enterprise pricing and is overkill for a one-time extraction. Their public sitemap + `robots.txt` explicitly permit crawling `/ein/*` paths. Respectful throttle + identifying UA makes this an ethically acceptable one-time research extraction.

- **Q: What throttle and duration?**
  A: 3 seconds per request, single worker, no parallelism. ~40 hours wall-clock (weekend). This is polite by research-crawler standards and invisible to a Cloudflare-fronted site of this scale.

- **Q: What fields matter?**
  A: At minimum: `ein, name, website_url, rating, revenue, state`. Nice-to-have: `expenses, program_expense_ratio, sector (NTEE), city, address, mission_statement, year_founded`. Everything that helps filter for "orgs that commission designed reports."

- **Q: Scope boundary?**
  A: One-time extraction of the current sitemap snapshot. No ongoing monitoring in this project. A future project may refresh via API if needed.

## Problem Statement

Lavandula Design needs to build a catalogue of nonprofit annual / impact reports as the foundation of a new service offering. That catalogue requires a seed list of nonprofit websites to harvest PDFs from. No single public source provides `(nonprofit_name, website_url, quality_signal)` tuples at sufficient scale and fidelity — IRS data lacks website URLs, agency portfolios only show a sliver, and consumer-facing locator sites don't expose URL fields. **Charity Navigator's public sitemap is the only source that provides all three, at enumerable scale (~48K orgs), for free.**

Without this seed list, the downstream report-harvesting bot has no targets. This spec unblocks that entire product line.

## Current State

- We have zero structured data on nonprofit web presences.
- Existing infrastructure (`nptech/`) proves out the throttled-crawler pattern against a small WordPress target (~1,068 items), with checkpointing, logging, and off-peak operation.
- Charity Navigator's discovery surface was verified manually (see References):
  - `https://www.charitynavigator.org/extra-index.xml` → 48 child sitemaps (`Sitemap1.xml` … `Sitemap47.xml`)
  - Each child sitemap contains ~1,000 `<loc>` entries of the form `https://www.charitynavigator.org/ein/{EIN}`
  - `robots.txt` disallows `/search/`, `/profile/`, `/basket/`, and two specific EINs, but **explicitly permits `/ein/*`**
  - Individual profile pages (verified against EIN 530196605 = American Red Cross) render the organization's website URL, star rating, score, name, mission, and address
- No workaround exists today. Manually curating a list of 10K+ org URLs is not viable.

## Desired State

At the end of this project:

1. **SQLite database** at `lavandula/nonprofits/data/nonprofits.db` holding one row per extracted nonprofit profile. Schema defined below.
2. **Raw HTML archive** at `lavandula/nonprofits/raw/cn/{ein}.html` — one file per EIN, **overwritten on `--refresh`**. This is the simplest model and matches the one-shot scope. Delta awareness is handled in the DB via `last_fetched_at` (timestamp) and `content_sha256` (hash of the HTML at last fetch) — so we can detect content changes across runs without storing multiple snapshots.
   - **Decision (from Codex review)**: we do NOT keep timestamped per-run snapshots for v1. If delta-tracking becomes important, add a `lavandula/nonprofits/archive_snapshots/{run_id}/{ein}.html` tree in a future spec and point the DB's `content_sha256` history at it.
3. **Code layout** at `lavandula/nonprofits/`:
   - `crawler.py` — sitemap enumeration, profile fetching, checkpoint/resume
   - `extract.py` — HTML → structured fields (pure transform; no network)
   - `schema.py` — DB schema DDL + init
   - `report.py` — summary stats + `coverage_report.md` generation
   - `http_client.py` — throttled client (ideally shared via a future `common/http_client.py`; see Notes)
   - `config.py` — throttle, paths, UA, stop-condition thresholds
   - `HANDOFF.md` — operational doc
4. **Queryability**. The database supports segments such as:
   - `rating_stars >= 4 AND total_revenue >= 5_000_000` → prime prospects for designed reports
   - `ntee_major = 'A' AND state = 'NY'` → sector-targeted sales lists
   - `website_url IS NOT NULL` → orgs with a discoverable online presence (~90% expected)
5. **Handoff documentation** so the future report-harvesting project can consume `nonprofits.db` without guesswork.

### Data Schema (SQLite)

```sql
-- Primary table: one row per nonprofit profile.
CREATE TABLE IF NOT EXISTS nonprofits (
  ein                TEXT PRIMARY KEY,          -- 9-digit string, no dashes
  name               TEXT NOT NULL,
  website_url        TEXT,                      -- canonical domain if resolvable; NULL if none on profile
  website_url_raw    TEXT,                      -- exactly as scraped (may be a CN redirect wrapper or contain tracking params)

  rating_stars       INTEGER,                   -- 1..4 or NULL if unrated
  overall_score      REAL,                      -- 0..100 or NULL
  beacons_completed  INTEGER,                   -- 0..4 or NULL
  rated              INTEGER NOT NULL DEFAULT 0,-- 1 if the org has a formal CN rating, 0 otherwise

  total_revenue      INTEGER,                   -- whole dollars, from latest available fiscal year
  total_expenses     INTEGER,
  program_expense_pct REAL,                     -- 0..100, percent

  ntee_major         TEXT,                      -- single letter (A..Z) when extractable
  ntee_code          TEXT,                      -- 3-4 char NTEE code if visible
  cn_cause           TEXT,                      -- CN's own cause label (string)

  city               TEXT,
  state              TEXT,                      -- 2-letter postal abbrev
  address            TEXT,                      -- full street line as shown

  mission            TEXT,

  cn_profile_url     TEXT NOT NULL,             -- https://www.charitynavigator.org/ein/{ein}

  last_fetched_at    TEXT NOT NULL,             -- ISO-8601 UTC
  content_sha256     TEXT NOT NULL,             -- SHA256 of the raw HTML at last fetch
  parse_version      INTEGER NOT NULL DEFAULT 1 -- bumped when the parser changes; lets us detect rows that need re-extraction
);

CREATE INDEX idx_nonprofits_state        ON nonprofits(state);
CREATE INDEX idx_nonprofits_rating_stars ON nonprofits(rating_stars);
CREATE INDEX idx_nonprofits_ntee_major   ON nonprofits(ntee_major);
CREATE INDEX idx_nonprofits_revenue      ON nonprofits(total_revenue);

-- Audit table: one row per HTTP fetch. Helps diagnose 429/403/retry patterns.
CREATE TABLE IF NOT EXISTS fetch_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ein            TEXT,
  url            TEXT NOT NULL,
  status_code    INTEGER,
  attempt        INTEGER NOT NULL,
  fetched_at     TEXT NOT NULL,
  elapsed_ms     INTEGER,
  error          TEXT       -- NULL on success, error class on failure
);
CREATE INDEX idx_fetch_log_ein ON fetch_log(ein);

-- Sitemap enumeration cache (so we know the source of truth at enumeration time).
CREATE TABLE IF NOT EXISTS sitemap_entries (
  ein               TEXT PRIMARY KEY,
  source_sitemap    TEXT NOT NULL,  -- e.g., "Sitemap23.xml"
  first_seen_at     TEXT NOT NULL,
  lastmod           TEXT            -- from the sitemap entry, if present
);
```

**Nullability rules:**
- `ein, name, cn_profile_url, last_fetched_at, content_sha256, parse_version` are **NEVER NULL**.
- `rating_stars, overall_score, beacons_completed, revenue, expenses, program_expense_pct` are NULL for unrated orgs — this is expected, not a bug.
- `website_url_raw` is NULL only if no URL was visible on the profile. `website_url` may be NULL even when `website_url_raw` is set (if the raw value could not be resolved to a canonical domain).

## Stakeholders

- **Primary User**: Ron (Lavandula Design) — queries the DB for prospect research, feeds filtered URL lists into the report-harvesting bot.
- **Secondary Users**: Future Builder(s) implementing the report-harvesting project; Lavandula sales team (eventual consumers of filtered lists).
- **Technical Team**: Ron + AI agents in this repo.
- **Business Owner**: Ron (Lavandula Design).
- **External**: Charity Navigator is the data provider. We are not obligated to notify them, but we owe them respectful technical behavior.

## Success Criteria

Codex review feedback note (2026-04-16): we separate **parser correctness** (deterministic, testable from fixtures) from **empirical source coverage** (dataset-dependent, reported but not gated on). This prevents a correct implementation from "failing" the spec because Charity Navigator's profile mix has different field-population rates than we guessed.

### Parser Correctness (GATING — must be met for approval)

- [ ] **Fixtures pass**: for every committed HTML fixture in `tests/fixtures/cn/`, the parser produces the expected `{ein, name, website, rating, score, beacons, revenue, state, sector}` tuple. 100% fixture accuracy is required; any discrepancy is a parser bug.
- [ ] **Fixture coverage**: fixtures include at minimum: rated 4-star org (e.g., Red Cross), rated 1–3-star org, unrated org (still indexed but no evaluation), profile with missing website URL, profile with a Charity Navigator redirect-wrapped website URL, profile with tracking parameters on the website URL, a 404'd EIN page, a 301-redirect response, malformed/truncated HTML.
- [ ] **Sitemap parsing**: given the `extra-index.xml` fixture, enumerates all 48 child sitemap URLs; given a malformed XML fixture, raises a clear error rather than silently skipping.
- [ ] **Deduplication**: given a synthetic case where the same EIN appears in two different child sitemaps, the crawler fetches it exactly once.
- [ ] **Idempotency**: re-running the crawl against an existing checkpoint re-fetches nothing unless `--refresh` is passed.
- [ ] **Test coverage**: ≥ 80% line coverage on extraction / parsing modules (not the network layer).

### Empirical Source Coverage (REPORTED, not gated)

The crawl run produces a `coverage_report.md` stating observed values, not pass/fail thresholds:

- Total EINs enumerated from sitemap
- Profiles successfully fetched / failed / retried
- `website_url` populated: X%
- `rating` populated: X%
- `revenue` populated: X%
- `state` populated: X%

If any field drops below 50%, that triggers a manual review (not an automated failure) to distinguish "extractor regression" from "dataset is just like that."

### Operational / Compliance (GATING)

- [ ] Effective sustained request rate ≤ 0.4 req/s.
- [ ] **Post-retry 429 rate < 1%** of total requests (measured from `fetch_log`).
- [ ] **No CN-initiated IP block observed** during the run (if blocked mid-run, we'd see sustained 403/challenge → stop conditions trigger halt → this criterion fails).
- [ ] Stop-condition halt: if any halt condition fires, the crawl exits with code 2 and writes `HALT-*.md`. A human must acknowledge before restart. (A halt is not itself a failure; hiding one would be.)
- [ ] robots.txt re-fetched at crawl start; any change to the `/ein/*` allowance halts the crawl.
- [ ] `lavandula/nonprofits/HANDOFF.md` exists, describing schema, how to query, how to refresh, and the contact protocol if Charity Navigator reaches out.

## Constraints

### Technical Constraints

- **Must reuse the throttled-client pattern** from `nptech/http_client.py` (or abstract it into a shared module). Do not introduce a second pattern.
- **Must honor `robots.txt`**. Explicitly check `/search/`, `/profile/`, `/basket/`, and the two disallowed EINs; skip them at the crawler level.
- **Must cache raw HTML** to disk before parsing, so the parser is a pure local transform.
- **Must be resumable**: a SIGINT / power loss / retry-exhaustion mid-run must not require restarting from zero.
- **Must not require secrets / auth** — pure public-data crawl.
- **Python 3.12+** (consistent with `nptech/`).

### Business Constraints

- No paid API subscription this quarter. If an ongoing refresh is needed later, that's a future spec.
- Output must be usable by a non-technical stakeholder (Ron) — i.e., SQL or a thin CLI, not a programmatic-only interface.

### Legal / Compliance Constraints

Our posture (tightened from "research use" framing per Claude review — Lavandula is a commercial design studio, not an academic researcher, so the actual defense is **internal business use**, not fair-use-for-research):

1. **Access is authorized.** The site's `robots.txt` permits `/ein/*`; there is no authentication gate.
2. **Factual data predominates.** We store EIN, org name, website URL, rating, revenue, expenses, state — none of which are copyrightable (IRS-derived or single-word metadata).
3. **Throttled below any reasonable abuse threshold.** 0.4 req/s sustained is two orders of magnitude below what a typical SEO tool would generate.
4. **No redistribution.** The raw HTML archive is internal. The derived database is used exclusively for Lavandula's own prospect research and (eventually) targeted outreach — not repackaged, resold, or republished.
5. **No republishing of editorial content.** We do not copy Charity Navigator's rating narrative, reviewer commentary, or ranking prose.

Mission statements require explicit treatment: they are the nonprofit's own marketing copy, but Charity Navigator is often the source-of-record for them. We persist `mission` **for internal segmentation only**. It is NEVER shown to a Charity Navigator competitor, included in any public Lavandula output, or exported outside the internal DB. If this posture changes, we re-verify source terms first.

## Assumptions

- Charity Navigator's sitemap reflects the complete set of profiles they intend to be publicly crawlable. Anything not in the sitemap is out of scope.
- Profile page HTML structure is stable enough that a BeautifulSoup + CSS-selector parser can handle it with occasional maintenance.
- The website URL on the profile is the org's canonical domain (not a UTM-tagged redirect).
- Charity Navigator does not aggressively rate-limit well-behaved crawlers at 0.3 req/s. (If they do, we detect via 429 and extend the throttle.)
- Network reliability on our host is adequate for a ~2-day continuous crawl (same host that runs `nptech/` nightly successfully).

## Solution Approaches

### Approach 1: Sitemap-driven scrape (RECOMMENDED)

**Description**: Fetch the sitemap index, enumerate the 48 child sitemaps, extract all `/ein/*` URLs, then fetch each profile at a 3-second throttle and parse with BeautifulSoup. Raw HTML archived to disk; parsed fields written to SQLite.

**Pros**:
- Uses only public, sitemap-advertised URLs (explicit crawl permission)
- No auth, no API key, no paid tier
- Reuses nptech's crawler pattern — minimal new architecture
- Raw archive decouples fetching from parsing (re-parse is free)
- Checkpoint + resume is well-understood from nptech

**Cons**:
- ~40 hours wall-clock
- HTML parser is fragile to site redesigns (mitigated by raw archive)
- No way to detect newly-added profiles without re-pulling the sitemap

**Estimated Complexity**: Low-Medium
**Risk Level**: Low

### Approach 2: Paid Charity Navigator Data Feed

**Description**: License the commercial API; hit a clean JSON endpoint per EIN; skip HTML parsing entirely.

**Pros**:
- Structured JSON, no fragile parsing
- Explicitly sanctioned access pattern
- Supports ongoing refresh as part of the product

**Cons**:
- Enterprise pricing (typical industry range $3K–$25K/yr) is prohibitive for a single one-time extraction
- Procurement overhead delays delivery by weeks
- We don't yet know if the downstream product will need ongoing refresh

**Estimated Complexity**: Low (but blocked on procurement)
**Risk Level**: Medium (vendor dependency, pricing surprise)

### Approach 3: Hybrid — scrape for seed, API for refresh

**Description**: Do Approach 1 now; defer API purchase to a future spec if ongoing freshness is needed.

**Pros**:
- Minimizes commitment
- Delivers usable seed list this weekend
- Learns whether refresh is actually needed before paying

**Cons**:
- Requires two migrations (scrape → API) if API comes later
- Slight risk of API schema differing from scraped fields

**Estimated Complexity**: Low
**Risk Level**: Low

### Approach 4: Manual curation

**Description**: Hand-pick 200–500 nonprofits from Chronicle of Philanthropy 400, Forbes Top 100, Charity Navigator "Best Of" lists.

**Pros**:
- No crawl risk
- Curated for relevance

**Cons**:
- Order of magnitude smaller sample
- Biased toward the top tier — misses the mid-size orgs who are actually Lavandula's sweet-spot prospects
- Manual labor

**Estimated Complexity**: Low
**Risk Level**: Low (but low value)

### Recommendation

**Approach 3** — execute Approach 1 now, document the path to Approach 2 in case it's needed later. That is what this spec scopes.

## Open Questions

### Resolved (from 2026-04-16 consultations)
- [x] **User-Agent identity** → `Lavandula Design research crawler/1.0 (+https://lavanduladesign.com; ronp@lavanduladesign.com)` — identifiable, contactable. See Security Considerations. (User may override to nptech-neutral style before implementation; documented.)
- [x] **Code location** → `lavandula/nonprofits/` for code, `lavandula/nonprofits/data/` for DB, `lavandula/nonprofits/raw/cn/` for HTML archive, `lavandula/nonprofits/logs/`. All under one sibling top-level directory so the whole project is self-contained.
- [x] **Rating / revenue / sector / non-rated profiles / deduplication** → handled in the Data Schema above. Rated orgs have non-NULL `rating_stars, overall_score, beacons_completed, revenue, expenses`; unrated orgs have `rated=0` and those fields NULL. Canonical-domain dedup is deferred to a post-processing pass (out of scope for v1).
- [x] **--refresh semantics** → single file per EIN, overwritten. Delta awareness via `content_sha256` in DB. No timestamped snapshots in v1.
- [x] **The two robots.txt-disallowed EINs** → `86-3371262` (both formats `863371262` and `86-3371262` are disallowed). Hardcoded in the disallow list; rechecked against live `robots.txt` at crawl start.

### Still Open (Non-Blocking)
- [ ] Can we extract `year_founded` from the profile? (Occasionally present. If easy, include; if not, skip for v1.)
- [ ] Does Charity Navigator expose board size, employee count, or other structural signals on the public profile? (Would help segment by org complexity.)
- [ ] Is there a 990-filing link on the profile we can preserve for cross-referencing with ProPublica Nonprofit Explorer?
- [ ] **Single-worker rationale** (raised by Claude review): the constraint is "simpler code AND avoid looking like a swarm from a single source IP." Two workers at half the rate would be operationally equivalent but harder to reason about for rate-limit and stop-condition logic. Keeping it single-worker as a simplicity choice, not a hard correctness requirement.

## Performance Requirements

- **Effective request rate**: ≤ 0.4 req/s sustained (equivalent to ≥ 2.5 s average interval, respecting the 3 s configured throttle with retry slack).
- **Total runtime**: ≤ 60 hours (allowing 50% padding over the 40-hour happy-path estimate for retries, backoff, and network variability).
- **Disk footprint**: ~15 GB for raw HTML archive (~48K × ~300 KB median), ~50 MB for SQLite DB.
- **Memory**: < 300 MB resident. We stream-parse sitemaps and process one profile at a time.
- **Resumability**: after SIGINT or crash, re-invoking the crawler must pick up within 1 profile of the last successful fetch.

## Security Considerations

- **Authentication**: none. All data is public.
- **Authorization**: we honor `robots.txt`, including the two explicitly-disallowed EINs.
- **Data privacy**: the extracted data is exclusively organizational, not personal. No PII is harvested. Staff names (if they appear on profiles) are public officer/director listings; we do not persist them.
- **Secrets management**: no secrets. Nothing to leak.
- **Audit**: every HTTP request is logged to `fetch_log` table with timestamp, URL, status code, and attempt number. Raw responses are on disk.
- **Reputation**: our crawler's behavior reflects on Lavandula Design. A poorly-behaved crawler creates reputational and legal risk.
- **Storage**: raw HTML archive stays local. No cloud upload, no redistribution. If we ever want to share derivative data, we re-evaluate terms first.

### User-Agent (resolved from Codex review)

```
Lavandula Design research crawler/1.0 (+https://lavanduladesign.com; ronp@lavanduladesign.com)
```

Rationale: identifiable and contactable. If Charity Navigator wants to reach us, they can. The nptech precedent used a neutral UA because part of that research was about detecting how much of nptechforgood's content originates with Lavandula — that motivation does not apply here. **If user prefers a neutral UA, flip to the nptech-style string before implementation.**

### Stop Conditions (operationalized per Codex review)

The crawler MUST halt automatically and exit non-zero when any of the following fires. Each is logged to `logs/` with the condition, a snapshot of recent requests, and a clear message.

| Condition | Threshold | Action |
|---|---|---|
| Consecutive HTTP 403 responses | ≥ 3 | Halt; require manual review before restart |
| Consecutive HTTP 429 responses | ≥ 5 (despite backoff) | Halt; require manual review |
| Any CAPTCHA / JS-challenge signature in response body | 1 (immediate) | Halt. Detection: look for `cf-challenge`, `__cf_chl_jschl_tk__`, `"captcha"` substring, `<title>*Just a moment*</title>`, Turnstile markers |
| robots.txt re-fetch shows `Disallow: /ein` or `/ein/*` appeared | 1 (immediate) | Halt |
| robots.txt fetch itself fails at startup | 1 (immediate) | Halt — do not proceed without a fresh robots.txt |
| Sustained `Retry-After` values > 300 s | 2 consecutive | Halt; we are being asked to slow down a lot, respect it |
| Cumulative elapsed runtime | > 72 hours | Halt; diagnose before extending |

**robots.txt policy:**
- Re-fetched at crawler startup; cached for the process lifetime.
- The hardcoded disallow list (the two specific EINs + `/search/`, `/profile/`, `/basket/`) is overlaid with whatever the fresh fetch reveals.
- If robots.txt fetch returns 5xx or times out, halt. Do not proceed on a stale cache.

**Graceful halt behavior:**
- Flush checkpoint to disk.
- Write a `HALT-{YYYY-MM-DD-HH-MM-SS}.md` file in `logs/` explaining the halt.
- Exit with code 2 (distinct from normal exit 0 and generic error exit 1) so cron wrappers can detect "crawler halted intentionally" vs. "crawler crashed."

## Test Scenarios

### Functional Tests
1. **Happy path**: given a known `/ein/530196605` (Red Cross) HTML fixture, the parser produces `{ein, name, website, rating, score, revenue, state}` matching expected values.
2. **Non-rated org**: given a fixture for an unrated profile, the parser produces name/mission but sets `rating_stars=None, rated=0`.
3. **Missing website**: given a fixture with no website URL visible, the parser records `website_url=None, website_url_raw=None` without raising.
4. **Sitemap index parse**: given the `extra-index.xml` fixture, the parser enumerates exactly 48 child sitemap URLs.
5. **Child sitemap parse**: given a `Sitemap1.xml` fixture, the parser enumerates ~1,000 EIN URLs with valid `/ein/{9-digit-numeric}` format.
6. **Disallowed EIN filter**: given the two `robots.txt`-disallowed EINs (`863371262` in both dashed and undashed forms), the crawler skips them at enumeration time. Add a test that validates the disallow list exactly matches the hardcoded constants.
7. **Checkpoint resume**: given a partial run with 5,000 EINs in checkpoint, a second invocation processes only the remaining ~43K.
8. **Malformed EIN in sitemap**: a sitemap entry like `/ein/ABC12345` or `/ein/12345678` (8 digits) is logged and skipped, not written to DB.
9. **Non-`/ein/` URLs in sitemap**: entries that don't match `/ein/\d{9}` are filtered, not fatal.
10. **Profile redirects to a different EIN** (merger / renumbering). Behavior: persist the requested EIN (source of truth from sitemap) but record `redirected_to_ein` in a diagnostic column or in `fetch_log.notes`; no v1 schema column for this — just log.
11. **Wrapped website URL**: raw value like `https://www.charitynavigator.org/redirect?to=https%3A//redcross.org` is stored in `website_url_raw`; `website_url` holds the unwrapped `https://redcross.org` after normalization.
12. **Tracking-parameter URL**: `https://example.org/?utm_source=charitynav` → `website_url_raw` stores as-is; `website_url` strips `utm_*`, `fbclid`, `gclid` params but preserves other query strings.
13. **Non-HTTP website links** (mailto:, tel:, social-media profile URLs): `website_url_raw` stores as-is; `website_url` is NULL with a reason code.
14. **Malformed / truncated HTML**: a profile with an abruptly-cut-off body either parses as much as possible with missing fields set NULL, or raises `ProfileParseError` — whichever the spec picks. **Decision for v1: parse permissively, NULL the missing fields, log a warning with `parse_status='partial'`.**
15. **Cloudflare challenge page** (200 OK with challenge body): detected via signature match (see Stop Conditions), classified as `parse_status='blocked'`, NOT written to `nonprofits`, HTTP archive overwritten with a `.challenge.html` marker file to avoid poisoning a future re-parse.
16. **Duplicate EIN across sitemaps**: same EIN in two different child sitemaps triggers exactly one fetch and one DB row. Deterministic which sitemap's `source_sitemap` wins (first-seen precedence).
17. **Checkpoint file corruption / partial write**: on startup, a truncated or JSON-invalid checkpoint is renamed `checkpoint.corrupt-{timestamp}.json` and a fresh run starts; a warning is emitted.

### Non-Functional Tests
1. **Throttle enforcement**: 100 consecutive requests complete in ≥ 300 seconds (3 s × 100).
2. **Retry behavior**: injected 429 response triggers exponential backoff; injected 503 triggers retry; permanent 404 logs and marks `fetch_status='not_found'` in `fetch_log` and skips.
3. **Response-size cap**: a mocked 10 MB response body is truncated at 5 MB (spec-configured limit). Prevents a pathological/malicious response from OOMing BeautifulSoup.
4. **EIN filesystem-safety validation**: EINs pulled from URLs are validated against `^\d{9}$` before being used as filenames. A synthetic EIN like `../../etc/passwd` is rejected at extraction time.
5. **Schema validation**: every row in `nonprofits.db` satisfies the declared schema (EIN is 9-digit string, rating_stars is 1–4 or NULL, etc.). Use SQLite `CHECK` constraints AND a post-run validator script.
6. **Stop-condition triggers**: integration test injects 3 consecutive 403s → crawler halts with exit code 2, writes `HALT-*.md`. Same for 5 consecutive 429s, for a challenge-body signature, and for a `robots.txt` re-fetch newly disallowing `/ein/`.

### Integration Tests
1. **Dry-run mode** (`--limit 10 --no-archive`): completes in < 45 s, exits cleanly, produces a parseable DB of 10 rows without mutating the real archive.
2. **Full pipeline against staging fixtures**: end-to-end with 20 real-site HTML fixtures covering: rated, unrated, missing-website, wrapped-URL, tracking-URL, mailto-URL, cross-EIN-redirect, challenge-body, truncated-HTML, and at least 2 fixtures per NTEE major category we care about.

## Dependencies

- **External Services**: Charity Navigator (`www.charitynavigator.org`) — HTTPS only, public.
- **Internal Systems**: none required. Optional: factor the throttled-client from `nptech/http_client.py` into a shared `common/http_client.py`.
- **Libraries**:
  - `requests` (already used by nptech)
  - `beautifulsoup4` + `lxml` (already used by nptech)
  - Python stdlib `sqlite3`
  - `tenacity` or in-house retry (in-house preferred for consistency with nptech)
- **Dev-time**: `pytest` + fixtures directory.

## References

- Interactive site-verification conversation (2026-04-16): confirmed sitemap structure, robots.txt, profile-page fields.
- Charity Navigator sitemap index: `https://www.charitynavigator.org/extra-index.xml`
- Charity Navigator robots.txt: `https://www.charitynavigator.org/robots.txt`
- Red Cross reference profile: `https://www.charitynavigator.org/ein/530196605`
- Existing crawler precedent: `nptech/crawler.py`, `nptech/http_client.py`, `nptech/HANDOFF.md`
- Downstream consumer (future spec): report-harvesting bot that consumes `nonprofits.db` as its seed list.

## Risks and Mitigation

| Risk | Probability | Impact | Mitigation Strategy |
|------|------------|--------|---------------------|
| Charity Navigator adds anti-bot challenges (Cloudflare Turnstile, JS challenge) mid-run | Low | High | Throttle is already so low that we shouldn't trigger it. If we do, pause, switch UA, add delay, or escalate to API conversation. |
| Site redesigns the profile page structure, breaking the parser | Medium | Medium | Raw HTML archive lets us re-parse without re-crawling. Parser isolated behind a single module so fixes are local. |
| `lastmod` dates across the sitemap reveal we're catching profiles mid-update | Low | Low | One-shot snapshot is acceptable; a future refresh pass can reconcile. |
| Scope creep: user asks "while we're at it, also grab…" during the run | Medium | Medium | This spec is explicitly one-shot and narrow. Additional sources are separate specs. |
| Legal / ToS complaint from Charity Navigator | Low | Medium | robots.txt compliance is our strongest defense. If a takedown request arrives, we respect it immediately and pivot to API approach. |
| Host disk fills during 15GB archive write | Low | High | Pre-flight disk-space check; archive target on a partition with ≥ 50 GB free. |
| Crawl runs into tonight's nptech cron window (04:00 UTC) and competes for resources | Low | Low | Both are I/O-bound with tiny CPU. Bandwidth is non-overlapping (different destination domains). Run anyway. |

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: 2026-04-16
**Models Consulted**: GPT-5 Codex ✅, Claude ✅, Gemini Pro ❌ (quota-exhausted — 10 retry attempts over ~4.5 min failed with "exhausted capacity on this model"; fell back to Claude as secondary reviewer)

**Codex Verdict**: `REQUEST_CHANGES` (HIGH confidence)
**Claude Verdict**: `COMMENT` (HIGH confidence)

**Key Feedback (overlapping — both reviewers raised):**
- Data schema was described piecemeal across four sections; consolidate into a single "Data Schema" subsection with column / type / nullability / source (both raised)
- Raw archive versioning semantics (`--refresh` vs append-only) ambiguous; pick one explicitly (both raised)
- Retry / halt / stop-condition policy under-specified; bounds builder behavior (both raised)
- Success criteria mixed parser correctness with dataset coverage; split them so a correct parser can't "fail" because of source-side field gaps (Codex)
- Two "Critical (Blocks Progress)" items (UA identity, code location) left unresolved; resolve before builder hands (Codex + Claude)

**Additional Feedback from Codex:**
- Expand tests to cover redirects, malformed HTML, duplicate EINs, wrapped/tracking URLs

**Additional Feedback from Claude:**
- Name the two disallowed EINs explicitly (they are `86-3371262`)
- Mission-statement storage conflicts with "no republishing editorial content" framing; add an internal-use-only carve-out
- "Research use" is a stretch for a commercial design studio; reframe as "internal business use" for a stronger actual defense
- "Zero 429 reports" is outside our control; restate as measurable (< 1% 429 after retry, no IP block observed)
- Justify the single-worker constraint (simplicity / avoid-swarm-appearance, not hard correctness)
- Add Cloudflare-challenge-200-OK fixture and test — most likely silent-failure mode
- Defensive hardening: validate EIN against `^\d{9}$` before filesystem use; add response-size cap (5 MB); detect challenge bodies and write to a separate marker file, never overwrite archive

**Sections Updated:**
- Success Criteria — split into Parser Correctness (gating) and Empirical Coverage (reported)
- Desired State — added full SQLite Data Schema section with DDL; resolved directory layout
- Security Considerations — added UA resolution, explicit Stop Conditions table, Defensive Hardening subsection
- Legal / Compliance Constraints — rewrote with tighter "internal business use" framing; added mission-statement carve-out
- Open Questions — moved resolved items to a Resolved subsection with rationale
- Test Scenarios — added 10 new edge cases (tests #8–17), plus 3 new non-functional tests (size cap, EIN validation, stop-condition integration)
- Risks table — fixed "different hosts" typo (now "different destination domains")

### Second Consultation (After Human Review)
**Date**: _pending — runs after human comments on the multi-agent-reviewed spec_
**Models Consulted**: will attempt Gemini first (quota permitting), with Codex + Claude as backups
**Key Feedback**: _to be populated_
**Sections Updated**: _to be populated_

### Red Team Security Review (MANDATORY)
**Date**: _pending_
**Command**: `consult --model gemini --type red-team-spec spec 0001`
**Findings**: _to be populated after run_

| Severity | Issue | Attack Vector | Mitigation |
|----------|-------|---------------|------------|
| CRITICAL | _pending_ | _pending_ | _pending_ |
| HIGH | _pending_ | _pending_ | _pending_ |
| MEDIUM | _pending_ | _pending_ | _pending_ |
| LOW | _pending_ | _pending_ | _pending_ |

**Verdict**: _pending_

### Defensive Hardening (from Claude review)

Items surfaced as proactive security hardening, not active threats:

- **EIN filesystem safety**: every EIN is validated against `^\d{9}$` before being used as a filename. Even though the sitemap is trusted, a compromised intermediary or a regex bug could inject `../../etc/passwd`. Guard at extraction time; write fixture test.
- **Response size cap**: the HTTP client enforces a **5 MB max response body**. A pathological or malicious proxy response could OOM BeautifulSoup. Streamed reads with a hard cap; exceeding = log and skip.
- **Challenge-body poisoning**: a 200 OK response containing a Cloudflare challenge body must NOT be written to the `{ein}.html` archive as if it were a profile. We detect the challenge and write a separate `.challenge.html` marker file (not substituted for a future real profile).
- **Checkpoint file integrity**: on startup, a corrupt/truncated checkpoint is renamed with a timestamp, not silently discarded. Operator can inspect.

## Approval
- [ ] Technical Lead Review
- [ ] Product Owner Review (Ron)
- [ ] Stakeholder Sign-off
- [ ] Expert AI Consultation Complete
- [ ] Red Team Security Review Complete (no unresolved findings)

## Notes

- This spec is deliberately **narrowly scoped** to a one-shot extraction. The natural next specs are:
  1. **Report-harvesting bot** (pulls PDFs from each org website)
  2. **Report classification + cataloguing** (structures the catalogue)
  3. **Design-idea catalogue** (expands scope to general marketing materials)
- The project will re-use and generalize the nptech crawler pattern. Some refactoring of nptech is possible as a side effect (factor out the throttled client). That is out of scope here; if it proves useful, file a separate TICK.
- **The human must approve this spec before planning begins.** AI agents must not self-promote the status from `conceived` to `specified`.

---

## Amendments

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->
