# ABANDONED — see 0004-site-crawl-report-catalogue.md

This spec (and its companion, the other abandoned 0002 / 0003) was
abandoned on 2026-04-19 after a developer outside the SPIDER review
loop proposed a materially better architecture: crawl known nonprofit
websites directly (using robots.txt + sitemap + homepage-link
extraction with anchor-text / URL-path filtering) instead of using a
search-engine API.

That approach:
- Drops the Google Custom Search API (no paid search budget)
- Shrinks the threat model (seed URLs come from a trusted allowlist,
  not adversary-gameable SERPs) — closing most of the SSRF and
  malicious-content concerns flagged in two rounds of red-team review
- Uses 0001's curated nonprofit list as the seed, making 0001 central
  rather than a helper
- Uses Haiku-class LLM calls for the judgment step (is this PDF an
  annual/impact report?) rather than a hand-rolled design-score
  rubric

The content below is preserved as-is for reference; do not implement
against it.

---

# Specification: Nonprofit Report Catalogue (Topic Plugin on 0002)

## Metadata

- **ID**: spec-2026-04-17-nonprofit-report-catalogue
- **Status**: draft
- **Created**: 2026-04-17
- **Depends on**: 0002-corpus-search-engine
- **Supersedes**: the topic-specific portions of
  0002-report-search-agent.abandoned.md

## Clarifying Questions Asked

- **Q: Why separate from the engine?**
  A: The abandoned 0002 bundled generic pipeline concerns (SSRF,
  sandbox, budget, content-type) with topic-specific concerns
  (report query library, PDF field extraction, design scoring, org
  attribution). Review surfaced that the pipeline belongs in a
  reusable engine. This spec is the thin topic plugin that consumes
  the engine.

- **Q: What is the deliverable experience?**
  A: A queryable SQLite catalogue of well-designed nonprofit
  annual/impact reports that Ron can filter by year, sector,
  design_score, and org. Each row cites its source URL and archived
  PDF. It doubles as a prospect list of orgs that demonstrably
  commission designed reports.

- **Q: Strict scope?**
  A: US-focused, PDF-format reports from fiscal years 2023-2025,
  from nonprofit organizations. Not: for-profit reports, academic
  annual reports, interactive HTML microsites, reports behind
  member-only paywalls, international orgs (deferred).

- **Q: What's explicitly the engine's job, not ours?**
  A: Search, fetch, sandbox, archive, dedup, budget, robots, TLS,
  SQL hygiene, log sanitation. We import those; we don't
  reimplement them.

## Problem Statement

Lavandula Design needs a working design-inspiration library + a
prospect list of orgs that commission designed reports. 0001 gave
us a nonprofit directory but not reports. The abandoned 0002 tried
to do everything from scratch. This spec, on top of 0002 engine,
does only the topic-specific work: which queries to run, how to
parse PDFs for meaningful report-level fields, how to score design
quality, and how to attribute reports to their authoring orgs.

## Current State

- 0001 committed: nonprofit directory with optional CN ratings.
- 0002 engine (planned): generic search-to-catalogue pipeline.
- 0003 (this spec): nothing yet.

## Desired State

At the end of this project:

1. **`lavandula/reports/` package** — a topic plugin consumed by
   `corpus_search`. Modules:
   - `queries.py` — hardcoded static query library.
   - `pdf_extractor.py` — `Extractor` implementation for
     `application/pdf` delegating to the engine's sandbox runner.
     Returns typed fields: `page_count, image_count, word_count,
     text_sample, embedded_font_names, toc_present, pdf_creator,
     pdf_producer, pdf_creation_date`.
   - `classifier.py` — `is_real_report` + `design_score` heuristic.
   - `attribution.py` — `org_name, org_confidence, org_ein` from
     PDF content + URL domain + cross-reference against 0001's
     `nonprofits` table.
   - `schema_ext.py` — the topic companion table (`nonprofit_reports`)
     keyed by `content_sha256`.
   - `catalogue.py` — query helpers for downstream use
     (`top_design_scores`, `reports_by_sector`, `prospect_list`).
   - `report.py` — `coverage_report.md` generator.
   - `HANDOFF.md`, `README.md`, fixtures, lockfile.
2. **Companion table** on the engine's SQLite:

Schema revisions from multi-agent red-team review (addresses
Claude CRITICAL #4, #5, #6, Gemini CRITICAL #1, plus HIGH items):

- `text_sample_raw` **REMOVED** from this table. Unredacted PDF text
  is never stored in the analytics DB. If internal review requires
  the original text, open the archived PDF directly in a controlled
  viewer. This closes the "load-bearing name" concern on
  `text_sample_raw` — the DB has one text column, and its semantics
  are unambiguous.
- `text_sample` is renamed `text_sample_scrubbed` for honesty about
  the limited regex-based scrubbing (emails, phones, SSNs).
- PDF active-content detection columns added.
- Hard per-field size limits added as CHECK constraints.
- Attribution columns tightened: `org_ein` only populates when two
  independent attribution paths agree AND the hosting domain
  resolves to a known-org domain.
- A `deletion_log` append-only table added for takedown auditability.

```sql
CREATE TABLE IF NOT EXISTS nonprofit_reports (
  content_sha256      TEXT PRIMARY KEY,

  -- Extracted from PDF content (deterministic, strictly size-bounded)
  page_count          INTEGER,
  image_count         INTEGER,
  word_count          INTEGER,
  text_sample_scrubbed TEXT,               -- first 2KB, regex-scrubbed for emails/phones/SSNs; see Security
  embedded_fonts_json TEXT,                -- JSON array of font descriptors, truncated to 4KB
  toc_present         INTEGER,             -- 0/1
  pdf_creator         TEXT,                -- <= 200 chars (size-capped at sandbox boundary)
  pdf_producer        TEXT,                -- <= 200 chars
  pdf_creation_date   TEXT,                -- ISO-8601 if parseable

  -- PDF active-content flags (per Claude red-team HIGH #H15)
  pdf_has_javascript  INTEGER NOT NULL DEFAULT 0,  -- /JavaScript or /JS action present
  pdf_has_launch      INTEGER NOT NULL DEFAULT 0,  -- /Launch action present
  pdf_has_embedded    INTEGER NOT NULL DEFAULT 0,  -- /EmbeddedFile present
  pdf_has_uri_actions INTEGER NOT NULL DEFAULT 0,  -- /URI actions present
  pdf_has_aa          INTEGER NOT NULL DEFAULT 0,  -- /AA additional actions present
  pdf_has_xfa         INTEGER NOT NULL DEFAULT 0,  -- XFA forms present
  pdf_risk_flags_json TEXT,                        -- JSON with details for forensic filter

  -- Derived
  report_year         INTEGER,             -- inferred fiscal year
  report_type         TEXT,                -- enum below
  sector              TEXT,                -- inferred NTEE major letter (A..Z) or 'unknown'
  sector_confidence   REAL,                -- 0..1

  -- Classification
  is_real_report      INTEGER NOT NULL DEFAULT 1,
  design_score        REAL,                -- 0..1
  design_signals_json TEXT,                -- per-signal values, <= 2KB

  -- Attribution (per Claude red-team CRITICAL #6)
  org_name            TEXT,                -- <= 400 chars
  org_name_source     TEXT,                -- enum: 'pdf-title'|'url-domain'|'pdf-xmp'|'cross-ref-0001'
  org_confidence      REAL,                -- 0..1; <= 0.5 unless org_domain_matched = 1
  org_ein             TEXT,                -- 9-digit; NULL unless two paths agree + domain check passes
  org_domain_matched  INTEGER NOT NULL DEFAULT 0,  -- 1 if hosting domain matches org's known domain in 0001
  org_attribution_paths_json TEXT,         -- which paths contributed, for audit

  -- Bookkeeping
  extractor_version   INTEGER NOT NULL DEFAULT 1,
  classifier_version  INTEGER NOT NULL DEFAULT 1,

  FOREIGN KEY (content_sha256) REFERENCES corpus_items(content_sha256),

  -- Hard size bounds (per Claude red-team CRITICAL #5)
  CHECK (pdf_creator IS NULL OR length(pdf_creator) <= 200),
  CHECK (pdf_producer IS NULL OR length(pdf_producer) <= 200),
  CHECK (org_name IS NULL OR length(org_name) <= 400),
  CHECK (text_sample_scrubbed IS NULL OR length(text_sample_scrubbed) <= 2048),
  CHECK (embedded_fonts_json IS NULL OR length(embedded_fonts_json) <= 4096),
  CHECK (design_signals_json IS NULL OR length(design_signals_json) <= 2048),
  CHECK (pdf_risk_flags_json IS NULL OR length(pdf_risk_flags_json) <= 2048),
  CHECK (org_attribution_paths_json IS NULL OR length(org_attribution_paths_json) <= 1024),

  CHECK (is_real_report IN (0,1)),
  CHECK (pdf_has_javascript IN (0,1)),
  CHECK (pdf_has_launch IN (0,1)),
  CHECK (pdf_has_embedded IN (0,1)),
  CHECK (pdf_has_uri_actions IN (0,1)),
  CHECK (pdf_has_aa IN (0,1)),
  CHECK (pdf_has_xfa IN (0,1)),
  CHECK (org_domain_matched IN (0,1)),
  CHECK (design_score IS NULL OR (design_score >= 0 AND design_score <= 1)),
  CHECK (org_confidence IS NULL OR (org_confidence >= 0 AND org_confidence <= 1)),
  CHECK (sector_confidence IS NULL OR (sector_confidence >= 0 AND sector_confidence <= 1)),
  CHECK (report_type IS NULL OR report_type IN ('annual','impact','hybrid','other')),
  CHECK (org_name_source IS NULL OR org_name_source IN
         ('pdf-title','url-domain','pdf-xmp','cross-ref-0001'))
);

CREATE INDEX idx_nreports_year   ON nonprofit_reports(report_year);
CREATE INDEX idx_nreports_type   ON nonprofit_reports(report_type);
CREATE INDEX idx_nreports_design ON nonprofit_reports(design_score);
CREATE INDEX idx_nreports_ein    ON nonprofit_reports(org_ein);
CREATE INDEX idx_nreports_sector ON nonprofit_reports(sector);

-- Read-only safe view for teammates / Claude instances / exports
-- (per Claude red-team HIGH #H18, Gemini red-team HIGH #H20).
-- Includes no _raw columns (there are none left), and excludes
-- PII-adjacent fields that should not surface in shared contexts.
CREATE VIEW IF NOT EXISTS nonprofit_reports_public AS
  SELECT content_sha256, page_count, image_count, word_count,
         report_year, report_type, sector, sector_confidence,
         is_real_report, design_score,
         org_name, org_name_source, org_confidence, org_ein,
         org_domain_matched, extractor_version, classifier_version
  FROM nonprofit_reports;

-- Deletion audit log (per Claude red-team HIGH #H19).
-- Append-only. Every catalogue row deletion MUST also write here
-- and trigger an archived-PDF unlink with fsync on the parent dir.
CREATE TABLE IF NOT EXISTS deletion_log (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  content_sha256   TEXT NOT NULL,           -- not a FK; the row may already be gone
  deleted_at       TEXT NOT NULL,
  reason           TEXT,                    -- free text, sanitized, <= 500 chars
  operator         TEXT,                    -- env USER or provided flag
  pdf_unlinked     INTEGER NOT NULL,        -- 0 if file was missing, 1 if unlinked
  CHECK (pdf_unlinked IN (0,1))
);
```

3. **Query library** (hardcoded in `queries.py`):

```
"annual report" nonprofit {year} filetype:pdf
"impact report" nonprofit {year} filetype:pdf
"{sector} annual report" {year} filetype:pdf
"{sector} impact report" {year} filetype:pdf
```

where:
- `year` ∈ `{2023, 2024, 2025}`
- `sector` ∈ `{foundation, health, education, environment,
  humanitarian, arts, civil rights, veterans, animal welfare,
  religion}` (10 sectors)

Total: `(2 generic + 2 × 10 sector) × 3 years = 66 queries`.
Providers cap at 100 results/query, paginated to 10 pages, yielding
up to 66 × 100 = **6,600 raw results**. After dedup + filtering,
target is **3K–5K cataloged reports**.

We explicitly do NOT add `site:.org` — it systematically excludes
nonprofits on `.us`, `.edu` affiliates, hosted platforms
(Squarespace, Webflow, S3). Coverage vs noise tradeoff accepted;
`is_real_report` filter downstream does the cleanup.

4. **PII handling** — the extracted `text_sample_raw` column holds
   the first 2 KB of PDF text verbatim (used internally). A parallel
   `text_sample` column is the PII-scrubbed version (regex removes
   emails, SSNs, phone numbers, full addresses). UIs and exports
   SHALL use `text_sample`, never `text_sample_raw`. Documented in
   HANDOFF.md.

5. **Design score rubric** — explicit formula, fixture-validated:

```
signals (each bool or 0..1 normalized):
  s1 = page_count in [8, 80]                            weight 0.10
  s2 = image_count / max(page_count, 1) >= 0.5          weight 0.20
  s3 = len(embedded_fonts) >= 4                         weight 0.15
  s4 = toc_present                                      weight 0.10
  s5 = word_count / max(page_count, 1) in [80, 450]     weight 0.15
  s6 = pdf_creator contains any of ['InDesign',
       'Illustrator', 'Affinity', 'Scribus']            weight 0.20
  s7 = not pdf_creator.startswith('Microsoft Word')     weight 0.10

design_score = clip(sum(weight_i * s_i), 0, 1)
design_signals_json = {s1: ..., s2: ..., ..., weights_version: 1}
```

These weights ARE committed in code. Fixture tests assert
exact-decimal scores on 10 labelled PDFs (6 known-good design, 4
known-tax-filing / news-article). AC1 passes if the sum of errors
across fixtures is < 0.05 each.

Classifier version 1; incrementing bumps `classifier_version` for
all re-scored rows.

6. **Attribution heuristic** — precedence:

```
1. PDF Dublin-Core metadata dc:creator / Title if it names an org
2. Title-page text match against 0001's nonprofits.name with
   high-confidence fuzzy match (Levenshtein ratio >= 0.85)
3. URL domain → {nonprofits.website_domain} fuzzy match
4. NULL, org_confidence = 0
```

Each path sets `org_name_source` accordingly. Cross-reference to
0001 populates `org_ein` when path 2 or 3 resolves.

## Stakeholders

- **Primary**: Ron (consumes the catalogue).
- **Secondary**: future Lavandula teammates; Claude instances doing
  style queries against the DB.
- **External**: source sites hosting the PDFs (engine handles robots
  + throttle). Search providers (engine handles budget + rate limit).

## Success Criteria

### Correctness (GATING — topic-specific, engine ACs are in 0002)

- **AC1** — Extractor round-trip: for each of 10 committed PDF
  fixtures, `pdf_extractor.extract()` produces the expected
  extraction dict matching fixture-expected JSON (byte-identical for
  deterministic fields; `design_score` within ±0.05 of expected).
- **AC2** — Fixture coverage: 10 PDFs covering: 3 well-designed
  annual reports, 2 impact reports, 1 hybrid, 1 Microsoft-Word
  output ugly report, 1 tax filing, 1 news article PDF, 1 scanned
  image-only PDF. (PDFs either public-domain originals or
  handmade synthetic tests; fixture-hash manifest committed.)
- **AC3** — Classifier precision ≥ 85% on the committed
  100-PDF labelled set (`locard/tests/0003-labels.jsonl`). Labels
  are hand-reviewed by Ron + one AI reviewer; committed to repo.
- **AC4** — Attribution: for the 10 extractor-fixture PDFs,
  `org_name` matches expected value with `org_confidence >= 0.7` on
  at least 8 of 10. Test explicit.
- **AC5** — Sector inference: for the 10 fixtures, `sector` matches
  expected on at least 8 of 10; fixtures span ≥ 5 distinct sectors.
- **AC6** — Year inference: for fixtures where the year is visibly
  in the title or filename, `report_year` matches expected exactly.
  Fixtures where year is only in body text need not match exactly
  (`report_year` may be NULL).
- **AC7** — Topic isolation: all plugin fetches go through the
  engine; `grep "import requests" lavandula/reports/` returns zero
  matches; CI lint check enforces.
- **AC8** — Content-addressable dedup: ingesting the same PDF via 3
  different URLs yields exactly 1 `nonprofit_reports` row and 3
  `corpus_item_urls` rows (engine-level, but asserted at plugin
  integration level).
- **AC9** — PII scrubbing (narrow, honest scope — per Claude red-team
  CRITICAL + Gemini red-team HIGH): fixture with emails, phone
  numbers, SSNs → `text_sample_scrubbed` has those replaced with
  `<EMAIL>`, `<PHONE>`, `<SSN>`. **There is no `text_sample_raw`
  column.** The column name `text_sample_scrubbed` is deliberately
  narrower than "sanitized" to avoid overstating protection: names
  and addresses are NOT regex-removed (too error-prone per Gemini
  red-team). Operators who need names-free samples must parse the
  original archived PDF in a separate controlled context.
- **AC9.1** — PII library (per Gemini red-team HIGH): the scrubbing
  implementation uses the `scrubadub` library (or equivalent
  well-maintained alternative pinned in the lockfile) for the three
  PII types listed above, not hand-rolled regex, to reduce
  regex-edge-case misses. Custom regex is only the fallback for
  types scrubadub doesn't handle.
- **AC9.2** — Scrubbing test suite (per Gemini red-team HIGH):
  extended test fixture file with 20+ PII edge cases
  (international phone formats, obfuscated emails `user [at]
  domain`, hyphenated SSNs, plus known false positives that must
  NOT be scrubbed). Committed in `tests/fixtures/pii_edges/`.
- **AC10** — Re-score stability: bumping `classifier_version` and
  re-running scores against the same archive yields deterministic
  results per fixture.
- **AC13** — Active-content detection (per Claude red-team HIGH
  #H15): extractor records `pdf_has_javascript`,
  `pdf_has_launch`, `pdf_has_embedded`, `pdf_has_uri_actions`,
  `pdf_has_aa`, `pdf_has_xfa`. A PDF containing any of these is
  NOT refused wholesale (some designed reports legitimately embed
  fonts/resources), but the flag column is set. `top_design_scores()`
  defaults to `WHERE pdf_has_javascript=0 AND pdf_has_launch=0`.
- **AC14** — Attribution spoofing defense (per Claude red-team
  CRITICAL #6): `org_confidence > 0.5` requires TWO independent
  attribution paths to agree (e.g., Dublin-Core AND domain match).
  `org_ein` is populated ONLY when `org_domain_matched=1` AND at
  least one content-derived path also agrees. Spoofing test:
  craft a PDF with `dc:creator = "ACME Foundation"` hosted on
  `attacker.example.org`; assert `org_ein IS NULL`,
  `org_confidence <= 0.5`, `org_domain_matched = 0`.
- **AC15** — Design-score gaming defense (per Claude red-team HIGH
  #H16): `catalogue.top_design_scores()` query filter defaults to
  `org_confidence >= 0.7 OR org_domain_matched = 1`. PDFs with
  max design-score but no attribution do NOT surface in the
  default top-N. Documented in HANDOFF.md.
- **AC16** — Deletion round-trip (per Claude red-team HIGH #H19):
  `catalogue.delete(content_sha256, reason)`:
  1. Unlinks the archived PDF file (via engine API).
  2. `DELETE FROM nonprofit_reports WHERE content_sha256 = ?`
  3. `DELETE FROM corpus_item_urls WHERE content_sha256 = ?`
  4. `DELETE FROM topic_corpus_items WHERE content_sha256 = ?
     AND topic = 'nonprofit-reports'`
  5. If no other topics reference this content, also delete from
     `corpus_items`.
  6. `INSERT INTO deletion_log (...)` with reason + operator +
     whether the unlink succeeded.
  AC asserts: after delete, all five tables return 0 rows for the
  sha256; `deletion_log` has exactly 1 row; the PDF file no longer
  exists.
- **AC17** — Public view usage (per Claude red-team HIGH #H18):
  exports, coverage_report.md generation, and any Claude-targeted
  query context use `nonprofit_reports_public` view, NOT the base
  table. Test: grep of `lavandula/reports/` rejects any query
  string against `nonprofit_reports` outside `catalogue.py`.
- **AC18** — Coverage-report markdown escaping (per Claude red-team
  CRITICAL #5 — markdown injection): `report.py` HTML-escapes every
  PDF-sourced string (`pdf_creator`, `pdf_producer`, `org_name`,
  `text_sample_scrubbed`) before embedding into
  `coverage_report.md`. Backticks, brackets, pipes, and angle
  brackets are escaped. Test: a fixture with `pdf_creator =
  "[click](javascript:alert(1))"` produces a markdown file where
  the string appears escaped as literal text.
- **AC19** — Fixture manifest integrity (per Claude red-team HIGH
  #H17): `tests/fixtures/fixtures.sha256` records hashes of every
  committed fixture. `tests/conftest.py` verifies all hashes at
  test-session start; mismatch fails the session. The manifest's
  own SHA is additionally pinned as a module constant in
  `schema_ext.py` (`FIXTURES_MANIFEST_SHA256`); a diff in both
  files is required for a malicious fixture swap to pass review.
- **AC20** — Retention policy (per Claude red-team HIGH #H19):
  `config.RETENTION_DAYS` (default 365). A nightly housekeeping
  command removes any `corpus_items` row (and derived data) whose
  `archived_at` is older than the retention window, via the
  deletion round-trip (AC16). Documented in HANDOFF.md.

### Empirical Coverage (REPORTED, not gated)

- Coverage report shows: unique reports catalogued, distribution by
  year / sector / design_score bucket, top 20 orgs by report count,
  percentage with `org_ein` populated (cross-ref hit rate against
  0001).

### Operational

- **AC11** — Budget: full-pass total spend ≤ USD 500 (configurable
  via engine's budget cap; halts before overspend).
- **AC12** — Runtime: full-pass ≤ 12 hours wall-clock on a single
  host at engine-default throttle.

## Constraints

### Technical

- **Must consume 0002's engine**; no direct HTTP calls.
- **Extractor runs in engine's sandbox** (0002 AC5); local imports
  of pypdf are only inside the sandboxed subprocess payload.
- **Schema-migration discipline**: `extractor_version` /
  `classifier_version` integer columns bumped whenever the
  extraction dict or scoring rubric changes in code; makes re-runs
  deterministic and versionable.
- **Python 3.12+**, same pinning as 0002.

### Legal / Compliance

- **Replaces the abandoned 0002's "public-domain-for-research"
  claim** with a narrower stance: publicly-retrieved copyrighted
  PDFs retained locally for internal design reference only. No
  redistribution. No republication. Deletion-on-request honored.
- `HANDOFF.md` captures retention policy: raw PDFs retained until
  operator purges; derived catalogue rows may survive a raw purge
  provided only `content_sha256` is kept as a cross-ref.
- **PII fields** (AC9) declared per the engine's plugin contract.

## Assumptions

- Google CSE (the v1 engine provider) will surface most public
  nonprofit report PDFs on the specified queries. Unknown-unknowns:
  orgs that publish reports only via subscriber email, or only
  on interactive HTML microsites, are out of reach for v1.
- Fiscal year is derivable from title / filename / first-page text
  in ≥ 70% of cases. The other 30% store NULL.
- 0001 `nonprofits` table is available for cross-ref attribution.
  If 0001 hasn't populated, attribution path 2+3 degrade to
  `NULL, org_confidence=0`; not a hard failure.

## Solution Approaches

### Approach 1: Static query library + 0002 engine + rubric-based scoring (RECOMMENDED)

**Description**: as drafted above.

**Pros**:
- Simplest, most auditable classifier.
- Fast to implement.
- Entirely deterministic given fixture seed.
- No ML training data needed.

**Cons**:
- Weighted-rubric scoring will not perfectly rank subtle design
  quality. Acceptable for v1; revisit if needed.

### Approach 2: ML classifier (e.g., fine-tuned Claude-Haiku on labelled PDFs)

**Description**: small classifier trained on ~500 hand-labeled PDFs.

**Pros**: potentially higher precision on subjective quality.

**Cons**: training data cost, inference cost per PDF, reproducibility
overhead, opaque model. Deferred to a future TICK after v1.

**Rejected for v1.**

### Recommendation

**Approach 1.**

## Open Questions

### Critical
- none.

### Important
- Fixture-PDF licensing. Proposed: mix of (a) public-domain
  originals we can legally commit, and (b) handmade synthetic PDFs
  generated via ReportLab at test-setup time. Plan phase decides
  per-fixture.
- Labelled-100 source. Proposed: Ron curates 100 by hand over 2
  days; committed as `locard/tests/0003-labels.jsonl`; one AI
  reviewer cross-validates a random 20 of them.

### Nice to know
- Should `attribution.py` attempt logo OCR for orgs with only a
  visual title page? (Deferred; low precision v1.)

## Performance Requirements

- Extractor per-PDF wall time ≤ 20 s (sandbox kills at 30 s).
- Classifier per-row wall time ≤ 100 ms.
- DB writes batched at 50 rows.
- Coverage report generation ≤ 30 s on a 10 K-row DB.

## Security Considerations

- Inherits all 0002 defenses.
- **Extractor NEVER follows in-PDF URI actions.** Stated explicitly
  per Claude red-team review of abandoned 0002.
- **Query library is static code.** If a future TICK moves the
  query list to a config file or user input, add allowlist validation
  (`[a-z0-9 -]{1,40}`) to prevent operator-injected query
  modifications (e.g., `site:attacker.com`) that pivot results.
- **Fixture integrity**: committed PDFs carry a `fixtures.sha256`
  manifest; test harness verifies each fixture's hash before
  opening it. Prevents a tampered fixture from silently masking a
  regression.
- **PII in archive**: raw PDFs may contain donor names, addresses,
  phone numbers. Retention + encryption-at-rest posture inherited
  from 0002 (host must have disk encryption enabled;
  `HANDOFF.md` documents the operator's check).

## Test Scenarios

### Unit
- PDF extractor output dict shape for each fixture.
- Classifier scoring math per-signal and aggregate.
- Attribution precedence logic with mocked 0001 lookups.
- PII-redaction regex correctness.

### Integration
- Full pipeline: mock search → mock origin → sandbox extract →
  classify → attribute → DB row. One representative fixture.
- Re-score without refetch: increment classifier_version; verify
  all rows re-scored and bumped.
- Dedup: same PDF via 3 URLs → 1 row + 3 corpus_item_urls.

### Manual / Review
- 100-PDF labelled set precision measurement. Labels in
  `locard/tests/0003-labels.jsonl`. Rerun precision check any time
  the classifier changes.

## Dependencies

- **0002 engine** (hard dep).
- `pypdf >= 4.0` (already in 0002's lockfile; plugin imports it
  inside the sandbox payload).
- `reportlab` (dev-only, for generating synthetic fixtures).
- Standard lib `re`, `json`, `sqlite3`.

## References

- 0002 engine spec.
- Abandoned 0002 review findings that motivated the split.
- 0001 nonprofits catalogue (cross-ref target for attribution).

## Risks and Mitigation

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Design-score rubric too noisy | Medium | Medium | Fixtures + labelled set gate AC3; rubric is iterable via classifier_version |
| Attribution miss-rate too high | Medium | Medium | Three-path precedence; store `org_confidence`; ≥8/10 fixture gate |
| Query library yields too few real reports | Medium | Medium | Static library is extensible; add sectors/synonyms in a TICK |
| 0001 cross-ref unavailable | Low | Low | Graceful degradation to `org_ein=NULL`; plugin remains useful |
| Fixture licensing complications | Low | Medium | Prefer synthetic PDFs; limit real-PDF fixtures to public-domain or explicitly-licensed |
| Classifier drift between versions | Medium | Low | `classifier_version` column + deterministic rubric + re-score test |

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: pending
**Models Consulted**: Codex, Claude, Gemini Flash
**Key Feedback**: pending

### Red Team Security Review (MANDATORY)
**Date**: pending
**Command**: `consult --model gemini --type red-team-spec spec 0003`
**Findings**: pending
**Verdict**: pending

## Approval

- Technical Lead Review
- Product Owner Review (Ron)
- Stakeholder Sign-off
- Expert AI Consultation Complete
- Red Team Security Review Complete (no unresolved findings)
- 0002 engine spec approved AND at minimum `specified` before this
  spec moves to `planned`.

## Notes

- This is a topic plugin, not a standalone project. Most of the
  hard security work is in 0002.
- 0001 (nonprofits directory) and 0003 (reports catalogue) become
  symmetric consumers: both import from 0002 engine, both populate
  topic-specific companion tables. 0001 migration to the shared
  engine is a later TICK; not gated by this spec.

---

## Amendments

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->
