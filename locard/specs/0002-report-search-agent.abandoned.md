# ABANDONED — see 0002-corpus-search-engine.md + 0003-nonprofit-report-catalogue.md

This spec was split on 2026-04-17 after multi-agent review returned
3 CRITICAL + 10 HIGH findings. Most findings were pipeline-level
(SSRF, parser isolation, budget enforcement) and belong in a
generic shared engine (new 0002). The topic-specific parts (report
queries, design scoring, org attribution) moved to a separate
plugin spec (0003).

The content below is preserved as-is for reference; do not
implement against it.

---

# Specification: Report Search Agent

## Metadata

- **ID**: spec-2026-04-17-report-search-agent
- **Status**: draft
- **Created**: 2026-04-17
- **Supersedes**: parts of spec 0001 (specifically, the assumption that
  report discovery requires org-level enumeration first)

## Clarifying Questions Asked

- **Q: Why switch from the 0001 approach?**
  A: Spec 0001 built an enumerate-nonprofits-then-hunt-their-sites pipeline.
  The 2026-04-17 validation exposed two problems: (a) CN's sitemap has
  ~2.3M entries with ~50% 404 rate and only a small rated subset, and
  (b) even with rated-org enumeration, finding reports on each org's site
  requires per-site HTML scraping with no uniformity. Searching engines
  directly for `"annual report" filetype:pdf` short-circuits both
  problems — search indexes have already done the hard work of finding
  the reports, and org attribution is derivable from the report itself.

- **Q: What IS the deliverable product?**
  A: A queryable catalogue at `lavandula/reports/data/reports.db` with
  one row per nonprofit report, tagged with year, sector (inferred),
  page count, design-signal score, and the attributed organization.
  Lavandula uses this for (1) design inspiration library and (2) a
  qualified prospect list where every listed org has **demonstrably**
  commissioned a designed report recently.

- **Q: What's the relationship to 0001?**
  A: 0001 is not abandoned — it stays as a prospect-list helper for
  org-level filtering (sector, state, rating). But 0002 is now the
  core product. The 0001 HTTP client, schema patterns, archive-safety
  primitives, and lint/CI infrastructure are all reusable.

- **Q: What search provider?**
  A: To be decided in the plan phase. Candidates: Google Custom Search
  API (reliable, USD 5 per 1000 queries), SerpAPI (easier setup), Bing Web
  Search API (cheaper at scale), or a self-hosted search-engine scraper
  (risky, fragile). Cost-tolerance range: USD 100-500 for an initial
  comprehensive pass. Open Question in the spec.

- **Q: Scope boundary?**
  A: v1 targets US nonprofit annual/impact reports from the last 3
  fiscal years (2023-2025). Not covering: international orgs (later),
  academic annual reports (universities are a different design niche),
  shareholder reports for for-profits.

## Problem Statement

Lavandula Design is a nonprofit-annual-report design studio. To grow the
practice, Lavandula needs two related assets:

1. **A design inspiration library** of well-designed nonprofit impact /
   annual reports to study and reference.
2. **A qualified prospect list** of nonprofits that demonstrably commission
   designed reports (as opposed to any nonprofit).

Both assets are, fundamentally, answered by the same question: *which
nonprofits have published a well-designed annual or impact report
recently?* That's what a search agent can find directly.

The spec 0001 approach answered a different question: *which nonprofits
exist?* That's upstream of our actual need and orders of magnitude larger
than the target set.

## Current State

- **From spec 0001**: we have a working throttled HTTP client, atomic
  filesystem write primitives, SQL parameterization pattern, lint gate,
  sanitized-logging helper, and a 120-test harness pattern to reuse.
- **Zero reports captured** as of this spec's creation.
- **Search-engine indexes** (Google, Bing) already have most public
  nonprofit reports indexed and ranked. We have never exercised them.

## Desired State

At the end of this project:

1. **`lavandula/reports/` directory tree** with:
   - `search.py` — search-engine caller (paginated, rate-limited)
   - `fetch.py` — PDF downloader with size cap and content-type check
   - `extract.py` — extracts org name, year, page count, image density,
     text from downloaded PDFs
   - `classify.py` — scores each PDF for "is this actually a designed
     nonprofit report?" vs. news article, tax filing, blog post
   - `catalogue.py` — SQLite writer + query helpers
   - `report.py` — summary stats + duplication analysis
   - `crawler.py` — orchestration: queries → search → fetch → extract →
     classify → catalogue
   - `HANDOFF.md`, `README.md`, `requirements.txt`, `lint.sh`,
     `.python-version`
2. **A SQLite DB** at `lavandula/reports/data/reports.db` with schema
   below.
3. **A raw archive** at `lavandula/reports/raw/pdfs/{sha256}.pdf` (content-
   addressable to deduplicate across URLs), ~30-100 KB per report on
   average, so ~3-10 GB for a 30K-report catalogue.
4. **A queryable catalogue** supporting filters like:
   - `design_score >= 0.7 AND year >= 2023` → the exemplar library
   - `sector = 'health' AND page_count > 20` → per-sector prospect
     depth
   - `org_name NOT NULL` → the prospect list subset

### Data Schema (SQLite, to refine in plan)

```sql
CREATE TABLE IF NOT EXISTS reports (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  content_sha256    TEXT NOT NULL UNIQUE,    -- dedup key
  source_url        TEXT NOT NULL,            -- canonical fetched URL
  source_urls_all   TEXT,                     -- JSON array of all URLs we saw for this content
  search_query      TEXT,                     -- which query surfaced it first
  fetched_at        TEXT NOT NULL,            -- ISO-8601 UTC
  content_type      TEXT,                     -- MIME
  file_size_bytes   INTEGER NOT NULL,

  -- Derived from PDF content
  page_count        INTEGER,
  image_count       INTEGER,                  -- embedded raster + vector images
  text_word_count   INTEGER,
  text_sample       TEXT,                     -- first 2K of text for grep

  -- Derived attribution
  org_name          TEXT,                     -- best guess from content + URL
  org_ein           TEXT,                     -- if resolvable via 0001 catalogue
  org_confidence    REAL,                     -- 0-1 how sure we are

  -- Temporal
  report_year       INTEGER,                  -- the fiscal year the report covers
  report_type       TEXT,                     -- 'annual' | 'impact' | 'hybrid' | 'other'

  -- Classification
  is_real_report    INTEGER NOT NULL DEFAULT 1, -- 0 = likely not a report (news article etc.)
  design_score      REAL,                       -- 0-1 heuristic design-quality signal
  design_signals    TEXT,                       -- JSON of the individual signal values

  CHECK (length(content_sha256) = 64),
  CHECK (is_real_report IN (0,1)),
  CHECK (design_score IS NULL OR (design_score >= 0 AND design_score <= 1)),
  CHECK (org_confidence IS NULL OR (org_confidence >= 0 AND org_confidence <= 1))
);

CREATE INDEX idx_reports_year   ON reports(report_year);
CREATE INDEX idx_reports_type   ON reports(report_type);
CREATE INDEX idx_reports_design ON reports(design_score);
CREATE INDEX idx_reports_ein    ON reports(org_ein);

-- Audit log (one row per search API call + per PDF fetch)
CREATE TABLE IF NOT EXISTS fetch_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  kind            TEXT NOT NULL,   -- 'search' | 'pdf'
  query_or_url    TEXT NOT NULL,
  status_code     INTEGER,
  fetched_at      TEXT NOT NULL,
  elapsed_ms      INTEGER,
  notes           TEXT,
  error           TEXT,
  CHECK (kind IN ('search','pdf'))
);

-- Queries we've already run (so resume doesn't re-hit the API)
CREATE TABLE IF NOT EXISTS search_queries_done (
  query_text      TEXT NOT NULL,
  run_at          TEXT NOT NULL,
  results_count   INTEGER NOT NULL,
  PRIMARY KEY (query_text, run_at)
);
```

## Stakeholders

- **Primary User**: Ron / Lavandula Design — consumes the catalogue for
  design reference and prospect outreach.
- **Secondary**: future Lavandula team members, possibly contractors
  who need a style-reference library.
- **Technical Team**: Ron + AI agents.
- **External**: the search provider (Google / Bing / SerpAPI) — we pay
  them per query. The host orgs whose reports we download — we treat
  their PDFs as public-domain-for-research (the distinction from 0001
  is we're consuming individual published PDFs, not bulk data).

## Success Criteria

### Parser Correctness (GATING)

- **AC1**: given a committed PDF fixture (a known designed nonprofit
  annual report), `extract.py` produces the expected tuple
  (org_name, year, page_count, image_count, word_count, design_score)
  matching fixture-expected values.
- **AC2**: fixture coverage includes: (a) a well-designed 40-page annual
  report, (b) a hybrid impact/annual, (c) a minimalist 8-page report,
  (d) an ugly tax-filing-style report (expected `design_score < 0.3`),
  (e) a news article PDF *about* a report (expected `is_real_report=0`),
  (f) a scanned-image-only PDF (no extractable text; `word_count=0`),
  (g) a corrupted / truncated PDF (graceful fail, logged).
- **AC3**: classifier correctly flags news-article and tax-filing PDFs
  as `is_real_report=0` with at least 85% precision on the fixture set.
- **AC4**: dedup via `content_sha256` prevents the same PDF from being
  cataloged twice even when surfaced by multiple URLs.

### Empirical Coverage (REPORTED, not gated)

The crawl run produces a `coverage_report.md` stating:

- Total searches executed
- Total unique PDFs downloaded
- PDFs classified as real reports (vs. discarded)
- Distribution by `design_score`, by inferred `sector`, by `report_year`
- Top 20 orgs by report count
- Orgs in the catalogue that also appear in the 0001 prospect list
  (confirms cross-reference works)

Target for v1 (expected, not gated): 5K–15K unique reports from 2K–5K
distinct orgs.

### Operational / Compliance (GATING)

- **AC5**: total search-API spend stays under a user-approved cap (default
  USD 200 for the initial run).
- **AC6**: search-API rate-limit errors trigger exponential backoff
  with a hard halt after 5 consecutive rate-limit responses.
- **AC7**: PDF fetches respect the target site's robots.txt (we're
  fetching individual PDFs from many different hosts, so robots.txt
  is checked per-host with 24h cache).
- **AC8**: per-host concurrency is 1 and per-host throttle is 3s (same
  as spec 0001), preventing us from DDoS-ing any single host.
- **AC9**: response-body cap: 50 MB per PDF (larger than spec 0001
  because designed annual reports can be image-heavy).
- **AC10**: zero `verify=False` anywhere in the code; TLS self-test at
  startup halts if verification is disabled (inherited from 0001
  pattern).

### Security (GATING)

- **AC11**: PDFs are treated as untrusted input: we do NOT invoke
  anything on the PDF content other than a parser with known-safe
  configuration (no PDF JavaScript execution, no embedded-file
  extraction).
- **AC12**: file-system writes use the atomic + symlink-safe pattern
  from spec 0001 (`os.open(O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW)`,
  atomic rename, `fsync(dir_fd)`).
- **AC13**: SQL writes use `?` parameter binding exclusively
  (inherited from 0001; assert via lint).
- **AC14**: downloaded PDFs stored with mode 0o600, archive dir 0o700.

## Constraints

### Technical

- **Must reuse** the 0001 HTTP client, sanitize/log helper, lint gate,
  and lockfile pattern.
- **Python 3.12+** (same as 0001).
- **PDF library**: `pypdf` (pure Python, well-maintained, no JS execution)
  OR `pdfminer.six` (higher quality text extraction). Choice in plan.
- **Search library**: pure-stdlib `requests`; no SDK required. Query
  provider abstracted behind a single `SearchProvider` interface so
  we can swap Google ↔ Bing ↔ SerpAPI without code changes.
- **Must be resumable** — search progress + fetched URLs persisted;
  re-run picks up where left off.
- **Must bound search-API spend** — per-run cost estimate logged;
  halt before exceeding a configurable cap.

### Business

- No contract commitments with a search provider this quarter; pay-as-
  you-go only.
- Budget cap: USD 500 for the initial comprehensive pass. Sampling runs
  up to USD 50.

### Legal / Compliance

- **Public PDFs are downloaded for internal research / design reference
  only.** No redistribution. No republication. Raw archive stays local.
- **We respect robots.txt per host** for PDF fetches (search APIs have
  their own ToS).
- **Content attribution**: we record org_name and source_url for every
  cataloged report so any future use has clear attribution.
- **Search provider ToS**: we comply with the provider's query-rate and
  caching terms.

## Assumptions

- Search engines surface most public nonprofit reports on queries like
  `"annual report" nonprofit 2025 filetype:pdf`. **Unknown-unknown**:
  some orgs may publish reports only via paid-subscriber email or
  member portals — those are out of reach.
- PDF-based reports dominate vs. interactive HTML microsites. Some
  design-forward orgs publish reports as standalone websites — those
  are out of scope for v1.
- Report year is inferable from title or filename in ≥80% of cases.

## Solution Approaches

### Approach 1: Google Custom Search + pypdf (RECOMMENDED)

**Description**: programmatic Google Custom Search API over a curated
query set; `pypdf` for PDF text + metadata extraction.

**Pros**:
- Google's index is the largest and best-ranked for this kind of content
- Custom Search API is stable and well-documented
- `pypdf` is safe by default (no JavaScript execution)

**Cons**:
- ~USD 5 per 1000 queries = USD 250 for a 50K-query pass
- Custom Search has a 100-query/day free tier, then paid

**Estimated Complexity**: Medium
**Risk Level**: Low

### Approach 2: Bing Web Search API

**Description**: same shape but Bing instead of Google.

**Pros**:
- ~USD 3 per 1000 queries (cheaper)
- Microsoft is investing heavily in this API

**Cons**:
- Index is smaller; we may miss some reports
- Less stable than Google (historical deprecations)

### Approach 3: SerpAPI (Wrapper over Google)

**Description**: pay SerpAPI for a normalized Google-results API.

**Pros**:
- Abstraction layer handles HTML-changes at Google
- Structured JSON responses

**Cons**:
- ~USD 50 per 5000 queries (more expensive)
- Extra vendor dependency

### Approach 4: Build our own search-engine HTML scraper

**Description**: scrape Google/Bing result pages directly.

**Pros**:
- Free

**Cons**:
- Fragile and ToS-violating
- Will be rate-limited / blocked quickly
- Reputational risk

**Rejected.**

### Recommendation

**Approach 1** (Google Custom Search) for v1. Architected so swapping
to Bing or SerpAPI is a provider-class change, not a rewrite.

## Open Questions

### Critical (Blocks Progress)

- (none — the critical decisions are in the Solution Approach)

### Important (Affects Design)

- **Design-score heuristic**: what exactly makes a PDF "well designed"?
  Candidate signals:
  1. Page count 8-60 (shorter = ad; longer = probably tax filing)
  2. Image-to-text-block ratio ≥ 0.15
  3. Font variety (heuristic: number of unique font descriptors) ≥ 4
  4. Has a table of contents
  5. Not exclusively black-on-white
  6. Typography markers (non-Arial/Times body text)
  Weighting and thresholds TBD; initial implementation uses a simple
  weighted sum with fixture-tuned weights.
- **Query library**: what's the complete set of search keywords we use?
  First cut:
  - `"annual report" nonprofit {year} filetype:pdf site:.org`
  - `"impact report" nonprofit {year} filetype:pdf site:.org`
  - `"{sector} annual report" {year} filetype:pdf site:.org` × sectors
  - (foundation, health, education, environment, humanitarian,
    arts, civil-rights, veterans)
  - Years: 2023, 2024, 2025
  - Approx: 3 report-types × 10 sectors × 3 years = 90 queries per pass,
    paginated to 10 result pages each = 900 API calls per full pass.
- **Org attribution**: when we can't confidently derive `org_name` from
  the PDF, do we (a) store NULL and move on, (b) reverse-search the
  PDF URL's domain to an org, (c) cross-reference against spec 0001's
  `nonprofits` table via fuzzy match on name+state? Probably (a) for
  v1; (c) for v2.

### Nice-to-Know (Optimization)

- Can we enrich with logo OCR to catch reports that don't name the org
  in text form (logo-only title pages)?
- Should we cluster reports into "design families" by year to detect
  orgs that use the same template year-over-year?

## Performance Requirements

- Effective search-API rate: ≤ 1 query/sec (Google's default quota).
- Effective PDF-fetch rate: ≤ 0.33 req/sec per host (3s throttle).
- Memory: < 500 MB resident at peak (pypdf parse of a 50 MB PDF).
- Disk: ~10 GB for a v1 catalogue (30K reports × 300 KB median).
- Runtime per full pass: ~4-8 hours.

## Security Considerations

- Inherits all 0001 defenses: TLS self-test, cross-host redirect block,
  decompressed size cap, cookie non-persistence, SQL parameterization,
  log injection sanitation, atomic+symlink-safe writes, single-instance
  flock, disk-space stop conditions.
- **PDF-specific**:
  - No JavaScript execution in the parser.
  - No embedded-file extraction.
  - 50 MB hard size cap; bail on oversize.
  - Content-type validation: accept only `application/pdf`, redirect
    wrappers unwrapped once, else skip.
- **Secrets**: search API key stored in environment variable, never
  committed to git. `.env.example` checked in with placeholder values.

## Test Scenarios (summary — detail in plan)

- 20+ PDF fixtures covering design quality spectrum
- Synthetic malformed/corrupt PDFs to test graceful failure
- Mocked search API responses for deterministic tests
- Dedup test: same PDF via 3 different URLs → 1 catalogue row
- Classifier spot-check: 100 manually-labeled PDFs, measure precision

## Dependencies

- Google Custom Search API (paid)
- `pypdf >= 4.0` (PDF parsing, Apache-licensed, pure Python)
- `requests >= 2.31` (HTTP)
- Existing `lavandula/nonprofits/` for cross-reference at lookup time
- Spec 0001 infrastructure patterns

## References

- Spec 0001 and its SPIDER artifacts
- Google Custom Search API docs (to be cited in plan)
- `pypdf` documentation
- Charity Navigator outreach to Laura Minniear (2026-04-17)
  — if her response comes through with alternative data access, we
  can compare quality vs. this search-based path

## Risks and Mitigation

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Search-API quota or pricing changes mid-project | Low | Medium | Provider abstraction allows swap; budget cap halts before overspend |
| Google blocks our client (unlikely via paid API) | Very Low | High | Paid API = explicit license; exponential backoff |
| Design-score heuristic is too noisy | Medium | Medium | Fixture-tuned thresholds; manual review of 100-row sample before scaling |
| PDF parser chokes on newer PDF features | Low | Low | graceful fail, log, skip |
| Disk pressure from retained PDFs | Medium | Medium | Content-addressable storage + periodic dedup sweep |
| Org-attribution is wrong for a fraction of reports | Medium | Low | Store `org_confidence`; low-confidence rows flagged for review |
| Legal pushback on PDF retention | Very Low | Medium | Internal research use only; ready-to-delete if requested |

## Consultation Log

### First Consultation (After Initial Draft)

**Date**: pending
**Models Consulted**: TBD (Codex, Claude, Gemini Flash)
**Key Feedback**: pending
**Sections Updated**: pending

### Second Consultation (After Human Review)

**Date**: pending
**Models Consulted**: pending
**Key Feedback**: pending
**Sections Updated**: pending

### Red Team Security Review (MANDATORY)

**Date**: pending
**Command**: `consult --model gemini --type red-team-spec spec 0002`
**Findings**: pending

**Verdict**: pending

## Approval

- Technical Lead Review
- Product Owner Review (Ron)
- Stakeholder Sign-off
- Expert AI Consultation Complete
- Red Team Security Review Complete (no unresolved findings)

## Notes

- This spec explicitly supersedes the "enumerate orgs, then hunt
  reports" assumption in 0001. 0001 stays as a prospect-list helper
  (and its SPIDER artifacts + 96 tests are still valid), but 0002 is
  now the core product.
- **The human must approve this spec before planning begins.** AI
  agents must not self-promote the status from `conceived` to
  `specified`.

---

## Amendments

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->
