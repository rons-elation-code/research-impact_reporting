# Specification: Nonprofit Seed List Extraction

## Metadata
- **ID**: spec-2026-04-16-nonprofit-seed-list-extraction
- **Status**: draft
- **Created**: 2026-04-16

## Clarifying Questions Asked

The following exchange shaped this spec:

- **Q: What is the business goal?**
  A: Lavandula Design wants to offer nonprofit impact-report / annual-report design as a service. We
  first need a catalogue of existing reports to study design trends, build an inspiration library,
  and generate prospect lists. Harvesting reports requires a seed list of nonprofit websites; this
  spec produces that seed list.

- **Q: Where should the nonprofit list come from?**
  A: Ruled out: IRS BMF/990 XML (too administrative, no website URLs / too much enrichment needed),
  agency portfolios (too curated / too sliced), `nonprofitlocator.org` (verified: no website URLs on
  org pages). Selected: **Charity Navigator**, which exposes all ~48K rated-org profile URLs via a
  public sitemap index, and each profile page includes the org's website URL plus
  rating/revenue/sector metadata.

- **Q: API or scrape?**
  A: Scrape. Charity Navigator's commercial data API has non-public enterprise pricing and is
  overkill for a one-time extraction. Their public sitemap + `robots.txt` explicitly permit crawling
  `/ein/*` paths. Respectful throttle + identifying UA makes this an ethically acceptable one-time
  research extraction.

- **Q: What throttle and duration?**
  A: 3 seconds per request, single worker, no parallelism. ~40 hours wall-clock (weekend). This is
  polite by research-crawler standards and invisible to a Cloudflare-fronted site of this scale.

- **Q: What fields matter?**
  A: At minimum: `ein, name, website_url, rating, revenue, state`. Nice-to-have: `expenses,
  program_expense_ratio, sector (NTEE), city, address, mission_statement, year_founded`. Everything
  that helps filter for "orgs that commission designed reports."

- **Q: Scope boundary?**
  A: One-time extraction of the current sitemap snapshot. No ongoing monitoring in this project. A
  future project may refresh via API if needed.

## Problem Statement

Lavandula Design needs to build a catalogue of nonprofit annual / impact reports as the foundation
of a new service offering. That catalogue requires a seed list of nonprofit websites to harvest PDFs
from. No single public source provides `(nonprofit_name, website_url, quality_signal)` tuples at
sufficient scale and fidelity — IRS data lacks website URLs, agency portfolios only show a sliver,
and consumer-facing locator sites don't expose URL fields. **Charity Navigator's public sitemap is
the only source that provides all three, at enumerable scale (~48K orgs), for free.**

Without this seed list, the downstream report-harvesting bot has no targets. This spec unblocks that
entire product line.

## Current State

- We have zero structured data on nonprofit web presences.
- Existing infrastructure (`nptech/`) proves out the throttled-crawler pattern against a small
  WordPress target (~1,068 items), with checkpointing, logging, and off-peak operation.
- Charity Navigator's discovery surface was verified manually (see References):
  - `https://www.charitynavigator.org/extra-index.xml` → 48 child sitemaps (`Sitemap1.xml` …
    `Sitemap47.xml`)
  - Each child sitemap contains ~1,000 `<loc>` entries of the form
    `https://www.charitynavigator.org/ein/{EIN}`
  - `robots.txt` disallows `/search/`, `/profile/`, `/basket/`, and two specific EINs, but
    **explicitly permits `/ein/*`**
  - Individual profile pages (verified against EIN 530196605 = American Red Cross) render the
    organization's website URL, star rating, score, name, mission, and address
- No workaround exists today. Manually curating a list of 10K+ org URLs is not viable.

## Desired State

At the end of this project:

1. **SQLite database** at `lavandula/nonprofits/data/nonprofits.db` holding one row per extracted
   nonprofit profile. Schema defined below.
2. **Raw HTML archive** at `lavandula/nonprofits/raw/cn/{ein}.html` — one file per EIN,
   **overwritten on `--refresh`**. This is the simplest model and matches the one-shot scope. Delta
   awareness is handled in the DB via `last_fetched_at` (timestamp) and `content_sha256` (hash of
   the HTML at last fetch) — so we can detect content changes across runs without storing multiple
   snapshots.
   - **Decision (from Codex review)**: we do NOT keep timestamped per-run snapshots for v1. If
     delta-tracking becomes important, add a
     `lavandula/nonprofits/archive_snapshots/{run_id}/{ein}.html` tree in a future spec and point
     the DB's `content_sha256` history at it.
3. **Code layout** at `lavandula/nonprofits/`:
   - `crawler.py` — sitemap enumeration, profile fetching, checkpoint/resume
   - `extract.py` — HTML → structured fields (pure transform; no network)
   - `schema.py` — DB schema DDL + init
   - `report.py` — summary stats + `coverage_report.md` generation
   - `http_client.py` — throttled client (ideally shared via a future `common/http_client.py`; see
     Notes)
   - `config.py` — throttle, paths, UA, stop-condition thresholds
   - `HANDOFF.md` — operational doc
4. **Queryability**. The database supports segments such as:
   - `rating_stars >= 4 AND total_revenue >= 5_000_000` → prime prospects for designed reports
   - `ntee_major = 'A' AND state = 'NY'` → sector-targeted sales lists
   - `website_url IS NOT NULL` → orgs with a discoverable online presence (~90% expected)
5. **Handoff documentation** so the future report-harvesting project can consume `nonprofits.db`
   without guesswork.

### Data Schema (SQLite)

```sql
-- Primary table: one row per nonprofit profile.
CREATE TABLE IF NOT EXISTS nonprofits (
  ein                TEXT PRIMARY KEY,          -- 9-digit string, no dashes
  name               TEXT NOT NULL,
  website_url        TEXT,                      -- canonical, normalized URL; NULL if none or non-resolvable
  website_url_raw    TEXT,                      -- exactly as scraped (CN redirect wrapper or tracking params intact)

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

  -- Diagnostic / status fields (added 2026-04-17 per Codex red-team feedback)
  redirected_to_ein  TEXT,                      -- NULL for normal; if the profile 30x-redirected to a different EIN, record the target EIN here
  parse_status       TEXT NOT NULL DEFAULT 'ok',-- 'ok' | 'partial' | 'blocked' | 'challenge' | 'unparsed'
  website_url_reason TEXT,                      -- NULL if website_url is set; else one of: 'missing' | 'mailto' | 'tel' | 'social' | 'unwrap_failed' | 'invalid'

  last_fetched_at    TEXT NOT NULL,             -- ISO-8601 UTC
  content_sha256     TEXT NOT NULL,             -- SHA256 of the raw HTML at last fetch
  parse_version      INTEGER NOT NULL DEFAULT 1,-- bumped when the parser changes; lets us detect rows that need re-extraction

  CHECK (length(ein) = 9),
  CHECK (rating_stars IS NULL OR rating_stars BETWEEN 1 AND 4),
  CHECK (beacons_completed IS NULL OR beacons_completed BETWEEN 0 AND 4),
  CHECK (overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)),
  CHECK (parse_status IN ('ok','partial','blocked','challenge','unparsed')),
  CHECK (website_url_reason IS NULL OR website_url_reason IN ('missing','mailto','tel','social','unwrap_failed','invalid'))
);

CREATE INDEX idx_nonprofits_state        ON nonprofits(state);
CREATE INDEX idx_nonprofits_rating_stars ON nonprofits(rating_stars);
CREATE INDEX idx_nonprofits_ntee_major   ON nonprofits(ntee_major);
CREATE INDEX idx_nonprofits_revenue      ON nonprofits(total_revenue);
CREATE INDEX idx_nonprofits_parse_status ON nonprofits(parse_status);

-- Redirect handling policy (Codex HIGH-1):
-- When GET /ein/A returns a 30x redirect to /ein/B, we:
--   1. Follow the redirect and parse B's profile body
--   2. Write the row keyed by A (source-of-truth from sitemap enumeration)
--   3. Populate all fields from B's body content
--   4. Set redirected_to_ein = B
--   5. Write a second row keyed by B (independently) when B is enumerated on its own
-- Query-time deduplication groups rows by COALESCE(redirected_to_ein, ein).

-- Audit table: one row per HTTP fetch. Helps diagnose 429/403/retry patterns.
CREATE TABLE IF NOT EXISTS fetch_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ein            TEXT,
  url            TEXT NOT NULL,
  status_code    INTEGER,
  attempt        INTEGER NOT NULL,              -- 1-indexed; includes retries so metrics can distinguish them
  is_retry       INTEGER NOT NULL DEFAULT 0,    -- 0 for first attempt, 1 for any retry
  fetch_status   TEXT NOT NULL,                 -- 'ok' | 'not_found' | 'rate_limited' | 'forbidden' | 'challenge' | 'server_error' | 'network_error' | 'size_capped' | 'disallowed_by_robots'
  fetched_at     TEXT NOT NULL,
  elapsed_ms     INTEGER,
  bytes_read     INTEGER,                       -- for size-cap monitoring
  notes          TEXT,                          -- free text (e.g., "redirected to ein 123456789", "Retry-After: 120")
  error          TEXT,                          -- NULL on success, error class + message on failure
  CHECK (fetch_status IN ('ok','not_found','rate_limited','forbidden','challenge','server_error','network_error','size_capped','disallowed_by_robots'))
);
CREATE INDEX idx_fetch_log_ein           ON fetch_log(ein);
CREATE INDEX idx_fetch_log_status        ON fetch_log(fetch_status);
CREATE INDEX idx_fetch_log_is_retry      ON fetch_log(is_retry);

-- Sitemap enumeration cache (so we know the source of truth at enumeration time).
CREATE TABLE IF NOT EXISTS sitemap_entries (
  ein               TEXT PRIMARY KEY,
  source_sitemap    TEXT NOT NULL,  -- e.g., "Sitemap23.xml"
  first_seen_at     TEXT NOT NULL,
  lastmod           TEXT            -- from the sitemap entry, if present
);
```

**Nullability rules:**
- `ein, name, cn_profile_url, last_fetched_at, content_sha256, parse_version, parse_status` are
  **NEVER NULL**.
- `rating_stars, overall_score, beacons_completed, revenue, expenses, program_expense_pct` are NULL
  for unrated orgs — this is expected, not a bug.
- `website_url_raw` is NULL only if no URL was visible on the profile. `website_url` may be NULL
  even when `website_url_raw` is set (if the raw value could not be resolved to a canonical domain —
  then `website_url_reason` explains why).
- `redirected_to_ein` is NULL for rows whose fetch returned 200 from the requested EIN; populated
  only when the requested EIN 30x-redirected elsewhere.

### Website URL Normalization Policy (Codex red-team MEDIUM-1)

`website_url_raw` stores the scraped value as-is. `website_url` is derived by applying these rules
in order. If any rule reveals the URL is unusable, `website_url` is set NULL and
`website_url_reason` records why.

1. **Unwrap Charity Navigator redirects**: if the URL starts with
   `https://www.charitynavigator.org/redirect?` (or similar), extract the `to=` / destination
   parameter and URL-decode it. Apply unwrap recursively up to 3 levels; if still wrapped, set
   `reason='unwrap_failed'`.
2. **Reject non-HTTP schemes**: `mailto:`, `tel:`, `sms:`, `javascript:`, etc. → `reason='mailto'` /
   `'tel'` / `'invalid'` accordingly.
3. **Reject social-media-only links**: destinations whose canonical host is `facebook.com`,
   `twitter.com`, `x.com`, `instagram.com`, `linkedin.com`, `youtube.com`, `tiktok.com`,
   `threads.net` → `reason='social'`. (These are not the org's primary web presence; a future pass
   can mine them separately.)
4. **Lowercase the host** (RFC-3986 section 6.2.2.1).
5. **Remove default ports**: `:80` for `http`, `:443` for `https`.
6. **Punycode IDN hosts** to ASCII Compatible Encoding (e.g., `xn--`).
7. **Strip tracking parameters**: remove `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`,
   `utm_content`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `_ga`. Preserve all other query parameters.
8. **Remove trailing slash on root paths only** (`https://example.org/` → `https://example.org`). Do
   NOT strip trailing slashes elsewhere — `/foo/` and `/foo` are semantically different.
9. **Drop fragments** (`#section`). They never affect which resource we'd fetch.
10. **Validate** the result parses as a well-formed `http(s)` URL with a non-empty host. If not,
    `reason='invalid'`.

Examples:

| Input (`website_url_raw`) | Output (`website_url`) | `website_url_reason` |
|---|---|---|
| `https://redcross.org/?utm_source=charitynav` | `https://redcross.org` | NULL |
| `HTTPS://Redcross.Org:443/?fbclid=abc` | `https://redcross.org` | NULL |
| `mailto:info@example.org` | NULL | `mailto` |
| `https://facebook.com/xyzorg` | NULL | `social` |
| `https://www.charitynavigator.org/redirect?to=https%3A//redcross.org` | `https://redcross.org` | NULL |
| `https://example.org/annual-report/` | `https://example.org/annual-report/` | NULL |
| (empty) | NULL | `missing` |

## Stakeholders

- **Primary User**: Ron (Lavandula Design) — queries the DB for prospect research, feeds filtered
  URL lists into the report-harvesting bot.
- **Secondary Users**: Future Builder(s) implementing the report-harvesting project; Lavandula sales
  team (eventual consumers of filtered lists).
- **Technical Team**: Ron + AI agents in this repo.
- **Business Owner**: Ron (Lavandula Design).
- **External**: Charity Navigator is the data provider. We are not obligated to notify them, but we
  owe them respectful technical behavior.

## Success Criteria

Codex review feedback note (2026-04-16): we separate **parser correctness** (deterministic, testable
from fixtures) from **empirical source coverage** (dataset-dependent, reported but not gated on).
This prevents a correct implementation from "failing" the spec because Charity Navigator's profile
mix has different field-population rates than we guessed.

### Parser Correctness (GATING — must be met for approval)

- **Fixtures pass**: for every committed HTML fixture in `tests/fixtures/cn/`, the parser
  produces the expected `{ein, name, website, rating, score, beacons, revenue, state, sector}`
  tuple. 100% fixture accuracy is required; any discrepancy is a parser bug.
- **Fixture coverage**: fixtures include at minimum: rated 4-star org (e.g., Red Cross), rated
  1–3-star org, unrated org (still indexed but no evaluation), profile with missing website URL,
  profile with a Charity Navigator redirect-wrapped website URL, profile with tracking parameters on
  the website URL, a 404'd EIN page, a 301-redirect response, malformed/truncated HTML.
- **Sitemap parsing**: given the `extra-index.xml` fixture, enumerates all 48 child sitemap
  URLs; given a malformed XML fixture, raises a clear error rather than silently skipping.
- **Deduplication**: given a synthetic case where the same EIN appears in two different child
  sitemaps, the crawler fetches it exactly once.
- **Idempotency**: re-running the crawl against an existing checkpoint re-fetches nothing unless
  `--refresh` is passed.
- **Test coverage**: ≥ 80% line coverage on extraction / parsing modules (not the network
  layer).

### Empirical Source Coverage (REPORTED, not gated)

The crawl run produces a `coverage_report.md` stating observed values, not pass/fail thresholds:

- Total EINs enumerated from sitemap
- Profiles successfully fetched / failed / retried
- `website_url` populated: X%
- `rating` populated: X%
- `revenue` populated: X%
- `state` populated: X%

If any field drops below 50%, that triggers a manual review (not an automated failure) to
distinguish "extractor regression" from "dataset is just like that."

### Operational / Compliance (GATING)

- Effective sustained request rate ≤ 0.4 req/s.
- **Post-retry 429 rate < 1%** — defined precisely (Codex red-team LOW-1):
  - **Numerator**: count of `fetch_log` rows where `fetch_status='rate_limited'` AND the URL never
    subsequently succeeded within the same run (i.e., retries did not resolve the 429).
  - **Denominator**: count of distinct URLs attempted in this run (NOT total attempts — retries are
    already captured in the numerator logic).
  - **Exclusions**: challenge-body 200s are counted separately under `fetch_status='challenge'` and
    contribute to the halt-trigger count, not the 429-rate metric.
- **No CN-initiated IP block observed** during the run (if blocked mid-run, we'd see sustained
  403/challenge → stop conditions trigger halt → this criterion fails).
- Stop-condition halt: if any halt condition fires, the crawl exits with code 2 and writes
  `HALT-*.md`. A human must acknowledge before restart. (A halt is not itself a failure; hiding one
  would be.)
- robots.txt re-fetched at crawl start; any change to the `/ein/*` allowance halts the crawl.
- `lavandula/nonprofits/HANDOFF.md` exists, describing schema, how to query, how to refresh, and
  the contact protocol if Charity Navigator reaches out.

## Constraints

### Technical Constraints

- **Must reuse the throttled-client pattern** from `nptech/http_client.py` (or abstract it into a
  shared module). Do not introduce a second pattern.
- **Must honor `robots.txt`**. Explicitly check `/search/`, `/profile/`, `/basket/`, and the two
  disallowed EINs; skip them at the crawler level.
- **Must cache raw HTML** to disk before parsing, so the parser is a pure local transform.
- **Must be resumable**: a SIGINT / power loss / retry-exhaustion mid-run must not require
  restarting from zero.
- **Must not require secrets / auth** — pure public-data crawl.
- **Python 3.12+** (consistent with `nptech/`).

### Business Constraints

- No paid API subscription this quarter. If an ongoing refresh is needed later, that's a future
  spec.
- Output must be usable by a non-technical stakeholder (Ron) — i.e., SQL or a thin CLI, not a
  programmatic-only interface.

### Legal / Compliance Constraints

Our posture (tightened from "research use" framing per Claude review — Lavandula is a commercial
design studio, not an academic researcher, so the actual defense is **internal business use**, not
fair-use-for-research):

1. **Access is authorized.** The site's `robots.txt` permits `/ein/*`; there is no authentication
   gate.
2. **Factual data predominates.** We store EIN, org name, website URL, rating, revenue, expenses,
   state — none of which are copyrightable (IRS-derived or single-word metadata).
3. **Throttled below any reasonable abuse threshold.** 0.4 req/s sustained is two orders of
   magnitude below what a typical SEO tool would generate.
4. **No redistribution.** The raw HTML archive is internal. The derived database is used exclusively
   for Lavandula's own prospect research and (eventually) targeted outreach — not repackaged,
   resold, or republished.
5. **No republishing of editorial content.** We do not copy Charity Navigator's rating narrative,
   reviewer commentary, or ranking prose.

Mission statements require explicit treatment: they are the nonprofit's own marketing copy, but
Charity Navigator is often the source-of-record for them. We persist `mission` **for internal
segmentation only**. It is NEVER shown to a Charity Navigator competitor, included in any public
Lavandula output, or exported outside the internal DB. If this posture changes, we re-verify source
terms first.

**PII in raw archive (Claude red-team MEDIUM)**: the raw HTML archive contains CN-rendered
officer/director names and compensation figures as part of each profile. We do NOT extract these
into DB columns, but they are present in the archived HTML. This is acknowledged openly (not
hidden), and retention is scoped accordingly:
- Raw archive is access-restricted on disk (directory `0o700`, files `0o600`).
- Internal use only. No cloud backup. No sharing outside Ron's direct admin access.
- Once the DB has been validated and the downstream report-harvesting bot has consumed it, the raw
  archive becomes a deletion candidate (operator decision documented in `HANDOFF.md`).

## Assumptions

- Charity Navigator's sitemap reflects the complete set of profiles they intend to be publicly
  crawlable. Anything not in the sitemap is out of scope.
- Profile page HTML structure is stable enough that a BeautifulSoup + CSS-selector parser can handle
  it with occasional maintenance.
- The website URL on the profile is the org's canonical domain (not a UTM-tagged redirect).
- Charity Navigator does not aggressively rate-limit well-behaved crawlers at 0.3 req/s. (If they
  do, we detect via 429 and extend the throttle.)
- Network reliability on our host is adequate for a ~2-day continuous crawl (same host that runs
  `nptech/` nightly successfully).

## Solution Approaches

### Approach 1: Sitemap-driven scrape (RECOMMENDED)

**Description**: Fetch the sitemap index, enumerate the 48 child sitemaps, extract all `/ein/*`
URLs, then fetch each profile at a 3-second throttle and parse with BeautifulSoup. Raw HTML archived
to disk; parsed fields written to SQLite.

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

**Description**: License the commercial API; hit a clean JSON endpoint per EIN; skip HTML parsing
entirely.

**Pros**:
- Structured JSON, no fragile parsing
- Explicitly sanctioned access pattern
- Supports ongoing refresh as part of the product

**Cons**:
- Enterprise pricing (typical industry range USD 3K–25K per year) is prohibitive for a single one-time
  extraction
- Procurement overhead delays delivery by weeks
- We don't yet know if the downstream product will need ongoing refresh

**Estimated Complexity**: Low (but blocked on procurement)
**Risk Level**: Medium (vendor dependency, pricing surprise)

### Approach 3: Hybrid — scrape for seed, API for refresh

**Description**: Do Approach 1 now; defer API purchase to a future spec if ongoing freshness is
needed.

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

**Description**: Hand-pick 200–500 nonprofits from Chronicle of Philanthropy 400, Forbes Top 100,
Charity Navigator "Best Of" lists.

**Pros**:
- No crawl risk
- Curated for relevance

**Cons**:
- Order of magnitude smaller sample
- Biased toward the top tier — misses the mid-size orgs who are actually Lavandula's sweet-spot
  prospects
- Manual labor

**Estimated Complexity**: Low
**Risk Level**: Low (but low value)

### Recommendation

**Approach 3** — execute Approach 1 now, document the path to Approach 2 in case it's needed later.
That is what this spec scopes.

## Open Questions

### Resolved (from 2026-04-16 consultations)
- ✓ **User-Agent identity** → `Lavandula Design research crawler/1.0
  (+https://lavanduladesign.com; ronp@lavanduladesign.com)` — identifiable, contactable. See
  Security Considerations. (User may override to nptech-neutral style before implementation;
  documented.)
- ✓ **Code location** → `lavandula/nonprofits/` for code, `lavandula/nonprofits/data/` for DB,
  `lavandula/nonprofits/raw/cn/` for HTML archive, `lavandula/nonprofits/logs/`. All under one
  sibling top-level directory so the whole project is self-contained.
- ✓ **Rating / revenue / sector / non-rated profiles / deduplication** → handled in the Data
  Schema above. Rated orgs have non-NULL `rating_stars, overall_score, beacons_completed, revenue,
  expenses`; unrated orgs have `rated=0` and those fields NULL. Canonical-domain dedup is deferred
  to a post-processing pass (out of scope for v1).
- ✓ **--refresh semantics** → single file per EIN, overwritten. Delta awareness via
  `content_sha256` in DB. No timestamped snapshots in v1.
- ✓ **The two robots.txt-disallowed EINs** → `86-3371262` (both formats `863371262` and
  `86-3371262` are disallowed). Hardcoded in the disallow list; rechecked against live `robots.txt`
  at crawl start.

### Still Open (Non-Blocking)
- Can we extract `year_founded` from the profile? (Occasionally present. If easy, include; if
  not, skip for v1.)
- Does Charity Navigator expose board size, employee count, or other structural signals on the
  public profile? (Would help segment by org complexity.)
- Is there a 990-filing link on the profile we can preserve for cross-referencing with
  ProPublica Nonprofit Explorer?
- **Single-worker rationale** (raised by Claude review): the constraint is "simpler code AND
  avoid looking like a swarm from a single source IP." Two workers at half the rate would be
  operationally equivalent but harder to reason about for rate-limit and stop-condition logic.
  Keeping it single-worker as a simplicity choice, not a hard correctness requirement.

## Performance Requirements

- **Effective request rate**: ≤ 0.4 req/s sustained (equivalent to ≥ 2.5 s average interval,
  respecting the 3 s configured throttle with retry slack).
- **Total runtime**: ≤ 60 hours (allowing 50% padding over the 40-hour happy-path estimate for
  retries, backoff, and network variability).
- **Disk footprint**: ~15 GB for raw HTML archive (~48K × ~300 KB median), ~50 MB for SQLite DB.
- **Memory**: < 300 MB resident. We stream-parse sitemaps and process one profile at a time.
- **Resumability**: after SIGINT or crash, re-invoking the crawler must pick up within 1 profile of
  the last successful fetch.

## Security Considerations

- **Authentication**: none. All data is public.
- **Authorization**: we honor `robots.txt`, including the two explicitly-disallowed EINs.
- **Data privacy**: the extracted data is exclusively organizational, not personal. No PII is
  harvested. Staff names (if they appear on profiles) are public officer/director listings; we do
  not persist them.
- **Secrets management**: no secrets. Nothing to leak.
- **Audit**: every HTTP request is logged to `fetch_log` table with timestamp, URL, status code, and
  attempt number. Raw responses are on disk.
- **Reputation**: our crawler's behavior reflects on Lavandula Design. A poorly-behaved crawler
  creates reputational and legal risk.
- **Storage**: raw HTML archive stays local. No cloud upload, no redistribution. If we ever want to
  share derivative data, we re-evaluate terms first.

### User-Agent (resolved from Codex review)

```
Lavandula Design research crawler/1.0 (+https://lavanduladesign.com; ronp@lavanduladesign.com)
```

Rationale: identifiable and contactable. If Charity Navigator wants to reach us, they can. The
nptech precedent used a neutral UA because part of that research was about detecting how much of
nptechforgood's content originates with Lavandula — that motivation does not apply here. **If user
prefers a neutral UA, flip to the nptech-style string before implementation.**

### Stop Conditions (operationalized per Codex review)

The crawler MUST halt automatically and exit non-zero when any of the following fires. Each is
logged to `logs/` with the condition, a snapshot of recent requests, and a clear message.

| Condition | Threshold | Action |
|---|---|---|
| Consecutive HTTP 403 responses | ≥ 3 | Halt; require manual review before restart |
| Consecutive HTTP 429 responses | ≥ 5 (despite backoff) | Halt; require manual review |
| Any CAPTCHA / JS-challenge signature in response body | 1 (immediate) | Halt. Detection: look for `cf-challenge`, `__cf_chl_jschl_tk__`, `"captcha"` substring, `<title>*Just a moment*</title>`, Turnstile markers |
| robots.txt re-fetch shows `Disallow: /ein` or `/ein/*` appeared | 1 (immediate) | Halt |
| robots.txt fetch itself fails at startup | 1 (immediate) | Halt — do not proceed without a fresh robots.txt |
| Sustained `Retry-After` values > 300 s | 2 consecutive | Halt; we are being asked to slow down a lot, respect it |
| Cumulative elapsed runtime | > 72 hours | Halt; diagnose before extending |

**robots.txt policy (Codex red-team MEDIUM-2):**
- Re-fetched at crawler startup; cached for the process lifetime.
- **Stanza matching**: evaluate the MOST SPECIFIC `User-agent:` stanza whose token is a
  case-insensitive substring of our configured UA. If no specific match, fall back to `User-agent:
  *`. If multiple stanzas tie in specificity, **halt** — ambiguous robots semantics are a stop
  condition, not something to guess at.
- The hardcoded disallow list (the two EINs of the form `86-3371262` + `/search/`, `/profile/`,
  `/basket/`) is applied IN ADDITION to whatever the fresh fetch reveals. Our hardcoded list is a
  floor, not a ceiling.
- Parser must tolerate comments, blank lines, and order of directives per RFC 9309. A parse-time
  error is a halt condition.
- If `robots.txt` fetch returns 5xx, times out, or is unparseable, halt. Do not proceed on a stale
  cache.

**Provider complaint policy (Codex red-team LOW-2):**
- If Charity Navigator contacts Ron (ronp@lavanduladesign.com) directly to object during or after a
  crawl:
  1. **Immediate halt** of any running crawler process (SIGTERM; the process's signal handler must
     flush checkpoint + write `HALT-*.md`).
  2. Preserve all `fetch_log` entries for post-incident review.
  3. Review retention: decide whether the raw archive and derived DB are retained as-is, pruned, or
     deleted based on the nature of the objection.
  4. Only restart after written resolution; flipping to the paid API path (Approach 2) is the
     default response if they request we stop scraping.
  5. Document the incident in `lavandula/nonprofits/incidents/{date}-{subject}.md`.

**Graceful halt behavior:**
- Flush checkpoint to disk.
- Write a `HALT-{YYYY-MM-DD-HH-MM-SS}.md` file in `logs/` explaining the halt.
- Exit with code 2 (distinct from normal exit 0 and generic error exit 1) so cron wrappers can
  detect "crawler halted intentionally" vs. "crawler crashed."

## Test Scenarios

### Functional Tests
1. **Happy path**: given a known `/ein/530196605` (Red Cross) HTML fixture, the parser produces
   `{ein, name, website, rating, score, revenue, state}` matching expected values.
2. **Non-rated org**: given a fixture for an unrated profile, the parser produces name/mission but
   sets `rating_stars=None, rated=0`.
3. **Missing website**: given a fixture with no website URL visible, the parser records
   `website_url=None, website_url_raw=None` without raising.
4. **Sitemap index parse**: given the `extra-index.xml` fixture, the parser enumerates exactly 48
   child sitemap URLs.
5. **Child sitemap parse**: given a `Sitemap1.xml` fixture, the parser enumerates ~1,000 EIN URLs
   with valid `/ein/{9-digit-numeric}` format.
6. **Disallowed EIN filter**: given the two `robots.txt`-disallowed EINs (`863371262` in both dashed
   and undashed forms), the crawler skips them at enumeration time. Add a test that validates the
   disallow list exactly matches the hardcoded constants.
7. **Checkpoint resume**: given a partial run with 5,000 EINs in checkpoint, a second invocation
   processes only the remaining ~43K.
8. **Malformed EIN in sitemap**: a sitemap entry like `/ein/ABC12345` or `/ein/12345678` (8 digits)
   is logged and skipped, not written to DB.
9. **Non-`/ein/` URLs in sitemap**: entries that don't match `/ein/\d{9}` are filtered, not fatal.
10. **Profile redirects to a different EIN** (merger / renumbering). Behavior: persist the requested
    EIN (source of truth from sitemap) but record `redirected_to_ein` in a diagnostic column or in
    `fetch_log.notes`; no v1 schema column for this — just log.
11. **Wrapped website URL**: raw value like
    `https://www.charitynavigator.org/redirect?to=https%3A//redcross.org` is stored in
    `website_url_raw`; `website_url` holds the unwrapped `https://redcross.org` after normalization.
12. **Tracking-parameter URL**: `https://example.org/?utm_source=charitynav` → `website_url_raw`
    stores as-is; `website_url` strips `utm_*`, `fbclid`, `gclid` params but preserves other query
    strings.
13. **Non-HTTP website links** (mailto:, tel:, social-media profile URLs): `website_url_raw` stores
    as-is; `website_url` is NULL with a reason code.
14. **Malformed / truncated HTML**: a profile with an abruptly-cut-off body either parses as much as
    possible with missing fields set NULL, or raises `ProfileParseError` — whichever the spec picks.
    **Decision for v1: parse permissively, NULL the missing fields, log a warning with
    `parse_status='partial'`.**
15. **Cloudflare challenge page** (200 OK with challenge body): detected via signature match (see
    Stop Conditions), classified as `parse_status='blocked'`, NOT written to `nonprofits`, HTTP
    archive overwritten with a `.challenge.html` marker file to avoid poisoning a future re-parse.
16. **Duplicate EIN across sitemaps**: same EIN in two different child sitemaps triggers exactly one
    fetch and one DB row. Deterministic which sitemap's `source_sitemap` wins (first-seen
    precedence).
17. **Checkpoint file corruption / partial write**: on startup, a truncated or JSON-invalid
    checkpoint is renamed `checkpoint.corrupt-{timestamp}.json` and a fresh run starts; a warning is
    emitted.

### Non-Functional Tests
1. **Throttle enforcement**: 100 consecutive requests complete in ≥ 300 seconds (3 s × 100).
2. **Retry behavior**: injected 429 response triggers exponential backoff; injected 503 triggers
   retry; permanent 404 logs and marks `fetch_status='not_found'` in `fetch_log` and skips.
3. **Response-size cap (decompressed)**: a mocked gzip-compressed 50 KB response that decompresses
   to 10 MB is truncated at 5 MB (the cap applies to decompressed bytes, not wire bytes).
   `fetch_status='size_capped'`.
4. **EIN filesystem-safety validation**: EINs pulled from URLs are validated against `^[0-9]{9}` anchored end-of-string
   before being used as filenames. A synthetic EIN like `../../etc/passwd` is rejected at extraction
   time.
5. **Schema validation**: every row in `nonprofits.db` satisfies the declared schema (EIN is 9-digit
   string, rating_stars is 1–4 or NULL, enum columns have valid values, etc.). Use SQLite `CHECK`
   constraints AND a post-run validator script.
6. **Stop-condition triggers**: integration test injects 3 consecutive 403s → crawler halts with
   exit code 2, writes `HALT-*.md`. Same for 5 consecutive 429s, for a challenge-body signature, for
   a `robots.txt` re-fetch newly disallowing `/ein/`, for disk-space below 5 GB, and for cumulative
   archive size above 50 GB.

### Security Tests (added from Claude red-team)
7. **XXE prevention (CRITICAL fixture)**: parse a fixture `tests/fixtures/cn/xxe-sitemap.xml`
   containing `<!DOCTYPE ... [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>`. Assert the parser does
   NOT resolve the entity; assert `/etc/passwd` content does NOT appear anywhere in the crawler's
   output, DB, or logs.
8. **XXE SSRF prevention**: parse a fixture with `<!ENTITY xxe SYSTEM "http://127.0.0.1:1/">`.
   Assert NO outbound network request is made as a side effect of parsing (capture with a mock).
9. **Decompression bomb**: a mocked gzip-compressed response whose decompressed body is 500 MB is
   rejected with `fetch_status='size_capped'`; peak memory during the test stays below 50 MB.
10. **Cross-host redirect rejection**: mocked response `302 Location: http://attacker.example.org/`
    → crawler does NOT fetch `attacker.example.org`; records `fetch_status='server_error'` with note
    `"cross-host redirect to attacker.example.org"`.
11. **Scheme-downgrade redirect rejection**: mocked response `302 Location:
    http://www.charitynavigator.org/...` (note `http` not `https`) → not followed.
12. **TLS verification enforcement**: integration test issues startup self-test against
    `https://expired.badssl.com`; asserts the request fails with a cert error. If it succeeds, the
    crawler halts at startup.
13. **Symlink refusal**: pre-plant a symlink at `raw/cn/530196605.html` pointing to
    `/tmp/sensitive`. Run crawler targeted at that EIN. Assert crawler halts with `HALT-*.md`;
    `/tmp/sensitive` is NOT modified.
14. **SQL parameterization**: insert a mission statement containing `'; DROP TABLE nonprofits; --`;
    assert the table still exists afterwards; assert the stored mission is byte-identical to the
    input.
15. **Log injection**: a remote `Retry-After` header containing `\r\nFAKE_LOG_LINE` is sanitized
    before being written to `fetch_log.notes` or disk logs.
16. **Single-instance lock**: spawn two crawler processes simultaneously; first acquires the lock
    and runs; second exits with code 3 and a clear "already running" message.
17. **File permissions**: after a run, assert `nonprofits.db` is mode `0o600`; `raw/cn/` is mode
    `0o700`; one-sample archive file is mode `0o600`.
18. **Cookie non-persistence**: issue two sequential GETs; the second request's outgoing headers
    contain NO `Cookie:` header even if the first response set one.

### Integration Tests
1. **Dry-run mode** (`--limit 10 --no-archive`): completes in < 45 s, exits cleanly, produces a
   parseable DB of 10 rows without mutating the real archive.
2. **Full pipeline against staging fixtures**: end-to-end with 20 real-site HTML fixtures covering:
   rated, unrated, missing-website, wrapped-URL, tracking-URL, mailto-URL, cross-EIN-redirect,
   challenge-body, truncated-HTML, and at least 2 fixtures per NTEE major category we care about.

## Dependencies

- **External Services**: Charity Navigator (`www.charitynavigator.org`) — HTTPS only, public.
- **Internal Systems**: none required. Optional: factor the throttled-client from
  `nptech/http_client.py` into a shared `common/http_client.py`.
- **Libraries**:
  - `requests` (already used by nptech)
  - `beautifulsoup4` + `lxml` (already used by nptech)
  - Python stdlib `sqlite3`
  - `tenacity` or in-house retry (in-house preferred for consistency with nptech)
- **Dev-time**: `pytest` + fixtures directory.

## References

- Interactive site-verification conversation (2026-04-16): confirmed sitemap structure, robots.txt,
  profile-page fields.
- Charity Navigator sitemap index: `https://www.charitynavigator.org/extra-index.xml`
- Charity Navigator robots.txt: `https://www.charitynavigator.org/robots.txt`
- Red Cross reference profile: `https://www.charitynavigator.org/ein/530196605`
- Existing crawler precedent: `nptech/crawler.py`, `nptech/http_client.py`, `nptech/HANDOFF.md`
- Downstream consumer (future spec): report-harvesting bot that consumes `nonprofits.db` as its seed
  list.

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
**Models Consulted**: GPT-5 Codex ✅, Claude ✅, Gemini Pro ❌ (quota-exhausted — 10 retry attempts
over ~4.5 min failed with "exhausted capacity on this model"; fell back to Claude as secondary
reviewer)

**Codex Verdict**: `REQUEST_CHANGES` (HIGH confidence)
**Claude Verdict**: `COMMENT` (HIGH confidence)

**Key Feedback (overlapping — both reviewers raised):**
- Data schema was described piecemeal across four sections; consolidate into a single "Data Schema"
  subsection with column / type / nullability / source (both raised)
- Raw archive versioning semantics (`--refresh` vs append-only) ambiguous; pick one explicitly (both
  raised)
- Retry / halt / stop-condition policy under-specified; bounds builder behavior (both raised)
- Success criteria mixed parser correctness with dataset coverage; split them so a correct parser
  can't "fail" because of source-side field gaps (Codex)
- Two "Critical (Blocks Progress)" items (UA identity, code location) left unresolved; resolve
  before builder hands (Codex + Claude)

**Additional Feedback from Codex:**
- Expand tests to cover redirects, malformed HTML, duplicate EINs, wrapped/tracking URLs

**Additional Feedback from Claude:**
- Name the two disallowed EINs explicitly (they are `86-3371262`)
- Mission-statement storage conflicts with "no republishing editorial content" framing; add an
  internal-use-only carve-out
- "Research use" is a stretch for a commercial design studio; reframe as "internal business use" for
  a stronger actual defense
- "Zero 429 reports" is outside our control; restate as measurable (< 1% 429 after retry, no IP
  block observed)
- Justify the single-worker constraint (simplicity / avoid-swarm-appearance, not hard correctness)
- Add Cloudflare-challenge-200-OK fixture and test — most likely silent-failure mode
- Defensive hardening: validate EIN against `^[0-9]{9}` anchored end-of-string before filesystem use; add response-size cap
  (5 MB); detect challenge bodies and write to a separate marker file, never overwrite archive

**Sections Updated:**
- Success Criteria — split into Parser Correctness (gating) and Empirical Coverage (reported)
- Desired State — added full SQLite Data Schema section with DDL; resolved directory layout
- Security Considerations — added UA resolution, explicit Stop Conditions table, Defensive Hardening
  subsection
- Legal / Compliance Constraints — rewrote with tighter "internal business use" framing; added
  mission-statement carve-out
- Open Questions — moved resolved items to a Resolved subsection with rationale
- Test Scenarios — added 10 new edge cases (tests #8–17), plus 3 new non-functional tests (size cap,
  EIN validation, stop-condition integration)
- Risks table — fixed "different hosts" typo (now "different destination domains")

### Second Consultation (After Human Review)
**Date**: _pending — runs after human comments on the multi-agent-reviewed spec_
**Models Consulted**: will attempt Gemini first (quota permitting), with Codex + Claude as backups
**Key Feedback**: _to be populated_
**Sections Updated**: _to be populated_

### Red Team Security Review (MANDATORY)
**Date**: 2026-04-17
**Commands**:
```
consult --model codex  --type red-team-spec spec 0001   # ✅ REQUEST_CHANGES, HIGH
consult --model claude --type red-team-spec spec 0001   # ✅ REQUEST_CHANGES, HIGH
consult --model gemini --type red-team-spec spec 0001   # ❌ quota-exhausted (3 attempts)
```
**Reviewers**: GPT-5 Codex (7 findings) + Claude (15 findings). Gemini Pro attempted three times and
was consistently quota-locked; per SPIDER protocol we would ideally have Gemini's independent take,
but the two independent reviewers we did secure between them covered substantially different threat
surfaces (Codex: schema/operational consistency; Claude: adversarial-content defenses including XXE,
decompression bombs, cross-host redirects, TLS, filesystem attacks). Proceeding with the
two-reviewer result as documented.

**Findings (from Codex):**

#### Codex HIGH-1 — Redirect handling had no persistence target
- **Issue**: redirect behavior was referenced in tests but the schema had no column
  to record it.
- **Impact**: builder would invent ad-hoc storage; cross-EIN redirects (mergers,
  renumbering) would produce silent data loss or duplication.
- **Mitigation**: added `nonprofits.redirected_to_ein TEXT NULL` plus explicit
  redirect policy (parse B's body into A's row, record B in the column, dedup at
  query time).

#### Codex HIGH-2 — Tests referenced schema fields that didn't exist
- **Issue**: tests asserted behavior on `parse_status` / `fetch_status` but neither
  field was in the schema.
- **Impact**: spec was self-inconsistent; builder could not implement required
  test assertions.
- **Mitigation**: added `nonprofits.parse_status TEXT NOT NULL` (enum: ok /
  partial / blocked / challenge / unparsed) and `fetch_log.fetch_status TEXT NOT
  NULL` (enum: ok / not_found / rate_limited / forbidden / challenge /
  server_error / network_error / size_capped / disallowed_by_robots). Both with
  CHECK constraints.

#### Codex MEDIUM-1 — URL normalization rules under-specified
- **Issue**: host case, ports, trailing slashes, IDN, fragments all unspecified.
- **Impact**: downstream dedup / querying inconsistent across re-runs; different
  implementers would produce different outputs.
- **Mitigation**: added an explicit 10-rule "Website URL Normalization Policy"
  subsection with example table. Rejects social-only links with `reason='social'`.

#### Codex MEDIUM-2 — robots.txt stanza-matching unspecified
- **Issue**: ambiguous behavior when the site serves agent-specific directives vs
  `User-agent: *`.
- **Impact**: we're using a named UA; CN could serve us stricter rules we'd
  silently ignore.
- **Mitigation**: added robots.txt policy — most-specific substring match wins;
  multiple ties in specificity = halt; parse failures = halt; hardcoded list is a
  floor not a ceiling.

#### Codex MEDIUM-3 — Archive writes not atomic, no disk-space check
- **Issue**: mid-write crash would leave torn files in the archive; disk-full
  mid-run would silently corrupt DB and archive.
- **Mitigation**: atomic writes via `{ein}.html.tmp` + `os.replace`. Preflight
  requires ≥ 50 GB free; runtime check halts if free space drops below 5 GB.

#### Codex LOW-1 — "< 1% 429 rate" metric ambiguous
- **Issue**: numerator / denominator / retry handling not defined, so success
  criterion wasn't reproducibly measurable between runs.
- **Mitigation**: defined precisely in terms of `fetch_log` columns — numerator
  is unresolved `rate_limited` URLs; denominator is distinct URLs attempted;
  challenge 200s excluded (counted separately under their own status).

#### Codex LOW-2 — No runtime policy for provider complaint during a crawl
- **Issue**: implicit expectation that a human would notice and manually stop the
  crawler if CN reaches out mid-run.
- **Mitigation**: added Provider Complaint Policy — SIGTERM flushes checkpoint
  and writes `HALT-*.md`; incident documented in `incidents/`; default response
  is pivot to paid API (Approach 2).

**Findings (from Claude):**

#### Claude CRITICAL-1 — XXE in XML sitemap parsing
- **Issue**: default `lxml` configuration resolves external entities.
- **Attack**: tampered sitemap with `<!ENTITY xxe SYSTEM "file:///etc/passwd">`
  yields local file disclosure; `SYSTEM "http://169.254.169.254/..."` yields
  SSRF against cloud metadata.
- **Mitigation**: mandated `defusedxml`, OR an explicitly-configured
  `lxml.etree.XMLParser(resolve_entities=False, no_network=True,
  huge_tree=False)`. XXE fixture test required.

#### Claude HIGH-3 — Size cap didn't specify "decompressed" (gzip bomb)
- **Issue**: 50 KB wire / 10 GB decompressed would OOM BeautifulSoup before the
  raw-bytes cap could fire.
- **Mitigation**: cap applies to decompressed bytes. Streamed decompression with
  incremental byte counter; abort on threshold. Test: mocked 500 MB
  decompressed-size response is rejected.

#### Claude HIGH-4 — Cross-host / scheme redirect following not restricted
- **Issue**: a compromised redirect could point our fetcher at `http://attacker`
  (leaking our identifiable UA / email) or `file://` (local read).
- **Mitigation**: redirects restricted to host exactly
  `www.charitynavigator.org` and scheme `https`. Manual redirect-chain handling.
  Test: cross-host 302 is not followed.

#### Claude HIGH-5 — TLS verification not explicitly mandated
- **Issue**: a builder might silence `verify=False` to work around a CI or
  corporate-proxy cert issue, accepting MITM tampered content.
- **Mitigation**: verification mandated; startup self-test against
  `expired.badssl.com` must fail; CI lint rule bans `verify=False`.

#### Claude HIGH-6 — `os.replace` follows symlinks
- **Issue**: a local attacker could pre-plant
  `raw/cn/{ein}.html -> /home/ubuntu/.ssh/authorized_keys`; next crawl would
  overwrite the symlink target.
- **Mitigation**: pre-write `os.lstat` refuses symlinks; write via `O_NOFOLLOW`;
  archive directory mode `0o700`. Test: pre-planted symlink triggers halt.

#### Claude MEDIUM-4 — SQL parameterization not required in spec
- **Issue**: string-concatenated SQL would be injectable once attacker content
  landed in mission/name/address (it eventually will).
- **Mitigation**: mandated `?` placeholder binding; string concat in any write
  path is a halt-worthy review defect. Test: mission containing
  `'; DROP TABLE --` round-trips byte-identical.

#### Claude MEDIUM-5 — Cookie / session persistence unspecified
- **Issue**: CN tracking cookies would accumulate across 48K requests —
  fingerprinting plus state leakage into checkpoints and logs.
- **Mitigation**: clear cookies after every fetch; warn on `Set-Cookie`. Test:
  sequential GETs carry no `Cookie:` header.

#### Claude MEDIUM-6 — Log injection via URL / `Retry-After` / error strings
- **Issue**: CR/LF in remote-sourced strings enables forged log lines; ANSI
  escapes enable terminal spoofing when an operator `cat`s a log.
- **Mitigation**: strip control characters (`\x00-\x1f\x7f`) and truncate to 500
  chars; apply to all remote-sourced log writes. Test: `Retry-After` header with
  CRLF is sanitized.

#### Claude MEDIUM-7 — PII in raw archive inconsistent with stated posture
- **Issue**: raw HTML contains CN-rendered officer/director names and
  compensation figures, even though we don't extract them into DB columns.
- **Mitigation**: acknowledged in Legal/Compliance; access-controlled via
  `0o700` directory / `0o600` files; retention scoped; no cloud backup;
  deletion-candidate once DB is validated.

#### Claude MEDIUM-8 — No single-instance lock
- **Issue**: two accidentally-concurrent crawler processes would race on
  checkpoint, DB, and archive.
- **Mitigation**: `fcntl.flock` on `.crawler.lock`; second instance exits code 3.
  Test: two concurrent processes — one halts cleanly with a clear message.

#### Claude MEDIUM-9 — `website_url_reason` had no CHECK constraint
- **Issue**: inconsistency with peer enum columns invites typos → silent schema
  drift.
- **Mitigation**: added `CHECK (website_url_reason IS NULL OR website_url_reason
  IN ('missing','mailto','tel','social','unwrap_failed','invalid'))`.

#### Claude LOW-3 — Log rotation / retention absent
- **Mitigation**: `RotatingFileHandler` with 100 MB × 5 files; `HALT-*.md` files
  retained indefinitely (they're the forensic record).

#### Claude LOW-4 — DB / archive file permissions unspecified
- **Mitigation**: mode `0o600` for DB and archive files; `0o700` for directories.
  Test verifies post-run.

#### Claude LOW-5 — Subresource fetching not explicitly forbidden
- **Mitigation**: explicit ban — the parser operates on HTML text only; it
  never resolves `<img>`, `<iframe>`, `<script>`, `<link>`, or any other
  subresource.

#### Claude LOW-6 — UA contains direct email address
- **Issue**: if `ronp@lavanduladesign.com` becomes a spam target, rotation
  requires refactoring.
- **Mitigation**: noted; user may rotate to `crawler-contact@lavanduladesign.com`
  alias before implementation.

#### Claude LOW-7 — No cumulative archive-size cap
- **Issue**: a pathological case (every response at the 5 MB ceiling) could
  reach 240 GB total.
- **Mitigation**: halt when `du raw/` exceeds 50 GB during a run.

**All 22 findings (Codex 7 + Claude 15, including 1 CRITICAL) resolved in the spec.**

**Verdict**: APPROVE (all findings resolved). Codex originally REQUEST_CHANGES (7 findings)
and Claude originally REQUEST_CHANGES (15 findings, including 1 CRITICAL); every issue was
addressed in the spec body. Gemini Pro was quota-locked across 3 attempts; a Gemini Flash
pass on the subsequent plan document returned APPROVE without new spec-level findings,
providing partial third-reviewer validation.

### Defensive Hardening (from Claude + Codex reviews)

Items surfaced as proactive security hardening, not active threats:

- **EIN filesystem safety**: every EIN is validated against `^[0-9]{9}` anchored end-of-string before being used as a
  filename. Even though the sitemap is trusted, a compromised intermediary or a regex bug could
  inject `../../etc/passwd`. Guard at extraction time; write fixture test.
- **Response size cap**: the HTTP client enforces a **5 MB max DECOMPRESSED response body** (Claude
  red-team HIGH — a 5 MB raw cap would still permit a gzip bomb that decompresses to GB).
  Implementation: stream decompression with an incremental byte counter; abort once threshold
  exceeded. Log `fetch_status='size_capped'` and skip.
- **Challenge-body poisoning**: a 200 OK response containing a Cloudflare challenge body must NOT be
  written to the `{ein}.html` archive as if it were a profile. We detect the challenge and write a
  separate `{ein}.challenge.html` marker file. The main `{ein}.html` path is never overwritten with
  challenge content.
- **Checkpoint file integrity**: on startup, a corrupt/truncated checkpoint is renamed
  `checkpoint.corrupt-{timestamp}.json`, not silently discarded. Operator can inspect.
- **Atomic archive writes (Codex red-team MEDIUM-3)**: every `{ein}.html` write goes to
  `{ein}.html.tmp` first and is atomically renamed on close. A crash mid-write leaves the old (or
  no) file intact.
- **Symlink-safe archive writes (Claude red-team HIGH)**: before writing any archive file,
  `os.lstat` the destination; if it exists and is a symlink, refuse and halt. Create each temp file
  with `os.open(path, O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW, 0o600)`. Archive directory created with
  mode `0o700`. This prevents a local attacker or stray symlink from redirecting writes elsewhere on
  the filesystem.
- **Disk-space stop condition (Codex red-team MEDIUM-3)**:
  - **Preflight**: at startup, verify ≥ 50 GB free on the partition holding `raw/`. If less, refuse
    to start.
  - **Runtime check**: before each archive write, verify ≥ 5 GB free. If below, halt cleanly with
    `fetch_status='server_error'` and `HALT-disk_low-*.md`.
  - **Cumulative archive cap (Claude red-team LOW)**: halt if total `raw/cn/` exceeds 50 GB during a
    run (defensive against every response hitting the 5 MB cap = 48K × 5 MB = 240 GB worst case).

### Adversarial-Content Hardening (from Claude red-team)

The threat model originally assumed Charity Navigator responses are benign. That's unsafe — CN is
Cloudflare-fronted; every byte we parse is attacker-controlled-by-proxy if CN is ever compromised,
MITMed, or serves a tampered CDN cache.

- **XXE defense (Claude red-team CRITICAL)**: ALL XML parsing (the sitemap index and all 48 child
  sitemaps) MUST use `defusedxml` **OR** an explicitly-configured
  `lxml.etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)`. Default `lxml`
  will process external entities and fetch them on some platforms — a tampered sitemap with
  `<!DOCTYPE ... [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>` yields local file disclosure or SSRF
  against `169.254.169.254` (cloud metadata).
  - **Test**: fixture `tests/fixtures/cn/xxe-sitemap.xml` contains an external-entity reference to
    `file:///etc/passwd`. Parser MUST NOT resolve it; content MUST NOT appear anywhere in output.

- **Cross-host redirect restriction (Claude red-team HIGH)**: the HTTP client MUST only follow
  redirects where the target satisfies BOTH (a) scheme is `https`, AND (b) host is exactly
  `www.charitynavigator.org`. Any other redirect target → do NOT follow, record
  `fetch_status='server_error'` with note `"cross-host redirect to {host}"`, skip the EIN.
  - Rationale: a compromised redirect could point our fetcher at `http://attacker.com/...` or
    `file:///...`; we would archive attacker content under a legitimate EIN filename, corrupting the
    seed list. Additionally, our UA contains `ronp@lavanduladesign.com` — leaking that to an
    attacker-controlled third party is a credential-in-UA leak.
  - `requests` follows cross-host redirects by default; implementation MUST override via
    `allow_redirects=False` plus manual redirect chain handling, or via a `Session`-level hook that
    validates each redirect target.

- **TLS certificate verification (Claude red-team HIGH)**: certificate verification MUST be enabled;
  `verify=False` or any equivalent bypass is a defect.
  - **Startup self-test**: issue one GET against `https://expired.badssl.com` and assert the request
    fails. If it succeeds, halt (verification is silently disabled somewhere — e.g., by
    `REQUESTS_CA_BUNDLE` pointing at an attacker CA).
  - Add as a lint rule in the implementation: any literal `verify=False` in the codebase is a CI
    failure.

- **No subresource fetching (Claude red-team LOW, elevated)**: the parser MUST operate on raw HTML
  text only. It MUST NOT resolve, fetch, or render `<img>`, `<iframe>`, `<script>`, `<link>`,
  `<object>`, `<embed>`, or any other subresource referenced in the HTML. Only the top-level `GET
  /ein/{ein}` is permitted per profile.

### Operational Hygiene (from Claude red-team)

- **SQL parameterization (Claude red-team MEDIUM)**: ALL SQL writes MUST use `sqlite3` parameter
  binding (`?` placeholders). String-concatenated SQL (f-strings, `.format()`, `%s`) in any write
  path is a halt-worthy defect. Test: round-trip a mission statement containing `'; DROP TABLE
  nonprofits; --` and assert the string survives byte-identical in `nonprofits.mission`.

- **Cookie handling (Claude red-team MEDIUM)**: do NOT persist cookies across requests. After each
  successful fetch, `session.cookies.clear()`. If `Set-Cookie` arrives, log a single-line warning
  with the cookie name (not value). Rationale: a persistent cookie jar would fingerprint our traffic
  and accumulate 48K sessions' worth of state in checkpoints/logs.

- **Log-injection sanitation (Claude red-team MEDIUM)**: every remote-sourced string written to
  `fetch_log.notes`, `fetch_log.error`, or disk logs must be sanitized: strip control characters
  (`\x00-\x1f\x7f`) and truncate to 500 chars. Prevents CR/LF injection (forged log lines) and
  ANSI-escape-sequence injection (terminal spoofing when operator `cat`s a log).

- **Single-instance lock (Claude red-team MEDIUM)**: on startup, acquire exclusive `fcntl.flock` on
  `lavandula/nonprofits/.crawler.lock`. If held, exit cleanly (exit code 3 = "already running").
  Prevents two accidentally-concurrent crawlers racing on checkpoint, DB, and archive.

- **File permissions (Claude red-team LOW)**:
  - `nonprofits.db` created with mode `0o600` (owner read/write only).
  - `raw/cn/` directory created with mode `0o700`.
  - `logs/` directory created with mode `0o700`.
  - Checkpoint + HALT files created with mode `0o600`.

- **Log rotation (Claude red-team LOW)**: use `logging.handlers.RotatingFileHandler` with a 100 MB
  cap per file, keep the last 5 runs' logs. `HALT-*.md` files are small and retained indefinitely
  (never rotate — they're the forensic record of why a run stopped).

- **PII-in-raw-archive acknowledgement (Claude red-team MEDIUM)**: the raw HTML we archive contains
  CN-rendered officer/director names and compensation figures, even though we don't extract these
  into DB columns. We acknowledge this openly in the Legal / Compliance section (already done there)
  and scope retention accordingly:
  - Raw archive is access-restricted (dir mode `0o700`, files `0o600`).
  - Internal use only; NO cloud backup, NO sharing.
  - Once the DB is validated and the downstream report-harvesting bot has consumed it, the raw
    archive is a candidate for deletion. Retention decision is documented in `HANDOFF.md`.

## Approval
- Technical Lead Review
- Product Owner Review (Ron)
- Stakeholder Sign-off
- Expert AI Consultation Complete
- Red Team Security Review Complete (no unresolved findings)

## Notes

- This spec is deliberately **narrowly scoped** to a one-shot extraction. The natural next specs
  are:
  1. **Report-harvesting bot** (pulls PDFs from each org website)
  2. **Report classification + cataloguing** (structures the catalogue)
  3. **Design-idea catalogue** (expands scope to general marketing materials)
- The project will re-use and generalize the nptech crawler pattern. Some refactoring of nptech is
  possible as a side effect (factor out the throttled client). That is out of scope here; if it
  proves useful, file a separate TICK.
- **The human must approve this spec before planning begins.** AI agents must not self-promote the
  status from `conceived` to `specified`.

---

## Amendments

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->

### TICK-001: Pivot seed source from full-sitemap to curated lists (2026-04-17)

**Summary**: Replace the default seed-enumeration strategy from
"fetch all 48 Charity Navigator sitemaps" to "scrape CN's curated
Best Charities category pages." The full-sitemap approach, as
originally specified, enumerates 2.3M EINs and would take ~82 days
of continuous crawling at 3s throttle — economically infeasible
and ethically questionable given CN monetizes the data. The curated
paths enumerate ~3K–7K pre-filtered rated orgs, which is the scope
Lavandula actually needs for the downstream report-harvesting bot.

**Problem Addressed**:

Four findings from the 50-EIN validation run on 2026-04-17
invalidated the original scope assumptions:

1. **Real sitemap size is 48×, not 1× of the original estimate**.
   WebFetch's truncated response during initial recon showed
   "~1,000 URLs" per child sitemap; the actual count is ~49,000 per
   sitemap (confirmed: one sitemap is 6.6 MB uncompressed XML with
   ~49K `<loc>` entries). Total corpus: **2,302,615 unique EINs**,
   not ~48K.
2. **Crawl is infeasible at 3s throttle** — 2.3M × 3s = ~82 days
   continuous. Disk requirement also 45× higher than planned.
3. **Sample quality is dreadful on the tail**. Validation #1 sampled
   the lowest-EIN 50 rows (order `(first_seen_at, ein)`) and got 50
   tiny religious ministries with 0% rating coverage, 0% revenue,
   0% state — all `parse_status='partial'`. CN indexes 10× more
   orgs than it rates; a random sample is dominated by unrated
   small orgs.
4. **CN monetizes bulk data**. Scraping 2.3M profiles without
   paying would be a defensible ToS violation even at polite rates;
   sampling their curated recommendations is arguably ethical fair
   use.

**Spec Changes**:

- **Scope** (major): corpus size revised from ~48K to ~3K–7K
  pre-rated orgs via CN's Best Charities category pages.
- **Desired State**: crawler accepts a configurable `--source`
  (`sitemap` legacy | `curated-lists` new default).
- **Success Criteria / Empirical Coverage**: expected
  `rating_stars` population for curated-list sample is ≥ 95% (was
  "reported, not gated"); `website_url` ≥ 80% (was ≥ 70% reported
  from full sitemap).
- **Legal / Compliance**: updated posture — we use CN's own
  curation as a recommender, not as bulk-data competitor. The
  contact-protocol paragraph now mentions the outreach to Laura
  Minniear (sent 2026-04-17) as the good-faith gesture.
- **Open Questions**: new item — whether to keep the sitemap
  enumerator as a legacy/opt-in path or delete it. Proposed:
  keep, guarded behind `--source=sitemap` for future research use
  with paid-API signing.
- **Defensive Hardening**: add a unit test that asserts
  `--source=curated-lists` does NOT fetch any sitemap files.

**Plan Changes**:

- **New module**: `lavandula/nonprofits/curated_lists.py` — fetches
  the `/discover-charities/best-charities/*` index pages, extracts
  `/ein/{EIN}` links from each, insertions into `sitemap_entries`
  with `source_sitemap='curated:{category}'` so the same fetch/
  extract path downstream just works.
- **CLI flag**: `--source {sitemap,curated-lists}` (default
  `curated-lists`).
- **New test fixtures**: snapshots of 2–3 Best Charities category
  HTML pages + a test that asserts parser extracts N EIN links.
- **New ACs** (AC34–AC36): enumerator discovers at least 1,000 EINs
  from public index pages; no sitemap fetch occurs with
  `--source=curated-lists`; revised coverage metrics on the
  resulting sample.
- **Bug fix (already applied in master)**: `--start-ein` filter
  moved from Python-post-LIMIT to SQL-pre-LIMIT
  (`db_writer.unfetched_sitemap_entries`), so `--start-ein +
  --limit` combination behaves as a user would reasonably expect.

**Not changing**:

- The HTTP client (TLS self-test, size cap, cookie policy,
  redirect restrictions) — untouched.
- The profile parser (`extract.py`) — CN profile pages are the
  same URLs regardless of how we find them.
- The schema — `source_sitemap` column already holds a string, so
  `curated:highly-rated` fits.
- Report / HANDOFF structure — same tables, same output.
- File permissions, SQL parameterization, log sanitization,
  flock, stop conditions — all unchanged.

**Review**: See `reviews/0001-nonprofit-seed-list-extraction.md`
(updates forthcoming under Amendments).

---
