# Spec 0026: 990 Leadership & Contractor Intelligence

**Status**: Draft
**Author**: Architect
**Date**: 2026-04-30

## Problem

Lavandula's pre-call briefings lack leadership context. We know the org's name, website, revenue, and what reports they publish — but not who runs the organization, who controls the budget, who their current vendors are, or how long the leadership team has been in place. This information is publicly available in IRS 990 filings but we don't extract it.

## Goals

1. **Extract named individuals** from IRS 990 Part VII Section A (officers, directors, trustees, key employees, highest compensated employees) and Section B (top independent contractors) into a `people` table keyed by EIN + tax period.

2. **Multi-year history** — Process whatever filing years the operator requests via `--years` (default: current year minus 4 through current year). The system stores all processed filings; there is no hard 5-year cap. Multi-year data enables tenure tracking and transition detection (new CDO = potential vendor opportunity).

3. **Contractor intelligence** — Capture independent contractor names, service descriptions, and compensation. This reveals existing agency relationships (design firms, fundraising consultants) before the sales call.

4. **Enrichment pipeline step** — Add a `990-enrich` phase to the pipeline that, given a set of EINs from `nonprofits_seed`, downloads and parses the relevant 990 XML filings from IRS TEOS.

5. **Pre-call briefing query support** — The `people` table schema supports leadership summary queries (CEO/ED tenure via multi-year history, board chair, development officer, board size, top contractors). Actual briefing generation UI/templates are a follow-up spec.

## Non-Goals

- Replacing ProPublica as the seed discovery source. ProPublica remains the source for org discovery (search by state, NTEE, revenue). IRS XML is for enrichment after discovery.
- Parsing Schedule J (supplemental compensation detail), Schedule L (interested persons), or Schedule O (narrative). These are valuable but out of scope for v1.
- Real-time data. 990 filings lag 6-18 months. This is archival intelligence, not live data.
- Parsing 990-EZ or 990-PF. Start with full 990 only (orgs >$200K revenue or >$500K assets — our ICP anyway).

## Data Source

**IRS TEOS (Tax Exempt Organization Search)**

- Index: `https://apps.irs.gov/pub/epostcard/990/xml/{YEAR}/index_{YEAR}.csv`
  - Columns: `RETURN_ID`, `FILING_TYPE`, `EIN`, `TAX_PERIOD`, `SUB_DATE`, `TAXPAYER_NAME`, `RETURN_TYPE`, `DLN`, `OBJECT_ID`, `XML_BATCH_ID`
  - Filter: `RETURN_TYPE = '990'` (exclude 990-EZ, 990-PF, 990-T)
- XML files: zipped in `{YEAR}_TEOS_XML_{MM}{A|B|C|D}.zip`, each containing `{OBJECT_ID}_public.xml`
- Years available: 2019–2026 (we want last 5 filing years per org)

**NOT using:**
- AWS S3 `s3://irs-form-990` — frozen since Dec 2021, no longer updated
- ProPublica API — doesn't expose Part VII person-level data (only aggregate compensation totals)

## Data Model

### Table: `lava_corpus.people`

One row per person per filing per org.

```sql
CREATE TABLE lava_corpus.people (
    id              SERIAL PRIMARY KEY,
    ein             TEXT NOT NULL,           -- FK to nonprofits_seed
    tax_period      TEXT NOT NULL,           -- e.g., "202312" (YYYYMM)
    object_id       TEXT NOT NULL,           -- IRS OBJECT_ID (unique filing identifier)
    person_name     TEXT NOT NULL,
    title           TEXT,
    person_type     TEXT NOT NULL,           -- 'officer', 'director', 'key_employee', 'highest_compensated', 'contractor'
    avg_hours_per_week  NUMERIC(5,1),
    reportable_comp     BIGINT,             -- from org (cents)
    related_org_comp    BIGINT,             -- from related orgs (cents)
    other_comp          BIGINT,             -- other compensation (cents)
    total_comp          BIGINT GENERATED ALWAYS AS (
        COALESCE(reportable_comp, 0) + COALESCE(related_org_comp, 0) + COALESCE(other_comp, 0)
    ) STORED,
    -- Contractor-specific fields (NULL for non-contractors)
    services_desc   TEXT,                   -- e.g., "Design services", "Fundraising consulting"
    -- Role flags (from 990 XML boolean indicators)
    is_officer          BOOLEAN DEFAULT FALSE,
    is_director         BOOLEAN DEFAULT FALSE,
    is_key_employee     BOOLEAN DEFAULT FALSE,
    is_highest_comp     BOOLEAN DEFAULT FALSE,
    is_former           BOOLEAN DEFAULT FALSE,
    -- Metadata
    extracted_at    TIMESTAMPTZ DEFAULT NOW(),
    run_id          TEXT
);

-- Indexes
CREATE INDEX idx_people_ein ON lava_corpus.people(ein);
CREATE INDEX idx_people_ein_period ON lava_corpus.people(ein, tax_period);
CREATE UNIQUE INDEX idx_people_dedup ON lava_corpus.people(ein, object_id, person_name, person_type);
```

**Design decisions:**

- **Compensation in cents** (BIGINT) not dollars — avoids floating-point issues, matches IRS precision.
- **`person_type`** is derived from the role flags for quick filtering. Priority: officer > key_employee > highest_compensated > director. The `is_former` flag is orthogonal — a former officer gets `person_type='officer'` with `is_former=TRUE`. This preserves the role for tenure queries while marking departure.
- **`object_id`** links back to the specific IRS filing for provenance.
- **Dedup index** on `(ein, object_id, person_name, person_type)` prevents duplicate entries on re-runs. Keyed on `object_id` (not `tax_period`) so original and amended filings for the same tax period coexist. Queries that want "current" data use `DISTINCT ON (ein, tax_period, person_name) ORDER BY sub_date DESC` via a join to `filing_index`.
- **No FK constraint** to `nonprofits_seed` — we may enrich EINs before they're fully seeded, and the single-operator pattern doesn't need referential integrity enforcement.
- **Upsert behavior**: `ON CONFLICT (ein, object_id, person_name, person_type) DO UPDATE SET` all mutable fields (title, compensation, flags, services_desc, extracted_at, run_id). Same filing reparsed = deterministic overwrite with same values. Parser output for a given XML file must be fully deterministic.
- **Name storage**: V1 stores `person_name` exactly as it appears in the IRS XML (after HTML tag stripping). No normalization. Tenure queries across years use exact string match, which will miss `Jane Smith` vs `Jane A. Smith`. This is a known limitation — name linkage/fuzzy matching is a follow-up enhancement, not a v1 requirement. Goal 2 is "supports tenure tracking" at best-effort quality, not guaranteed accuracy.

### Table: `lava_corpus.filing_index`

Track which filings we've processed to enable incremental runs.

```sql
CREATE TABLE lava_corpus.filing_index (
    object_id       TEXT PRIMARY KEY,       -- IRS OBJECT_ID
    ein             TEXT NOT NULL,
    tax_period      TEXT NOT NULL,
    return_type     TEXT NOT NULL,           -- '990', '990EZ', '990PF'
    sub_date        DATE,                   -- submission date, parsed from index CSV
    taxpayer_name   TEXT,
    xml_batch_id    TEXT,                   -- from index CSV, maps to zip filename
    status          TEXT DEFAULT 'indexed',  -- 'indexed', 'downloaded', 'parsed', 'skipped', 'error'
    error_message   TEXT,
    parsed_at       TIMESTAMPTZ,
    run_id          TEXT
);

CREATE INDEX idx_filing_ein ON lava_corpus.filing_index(ein);
CREATE INDEX idx_filing_status ON lava_corpus.filing_index(status);
```

**Status lifecycle:**
- `indexed` — Row inserted from TEOS index CSV, XML not yet downloaded
- `downloaded` — Zip downloaded and XML extracted to cache, not yet parsed
- `parsed` — Part VII parsed, people rows upserted
- `skipped` — Valid 990 but no Part VII section present (not an error)
- `error` — Download or parse failure, `error_message` populated

## XML Parsing

### Part VII Section A — Officers/Directors/Key Employees

```xml
<Form990PartVIISectionAGrp>
    <PersonNm>John Smith</PersonNm>
    <TitleTxt>CEO</TitleTxt>
    <AverageHoursPerWeekRt>40.00</AverageHoursPerWeekRt>
    <IndividualTrusteeOrDirectorInd>X</IndividualTrusteeOrDirectorInd>
    <OfficerInd>X</OfficerInd>
    <ReportableCompFromOrgAmt>487000</ReportableCompFromOrgAmt>
    <ReportableCompFromRltdOrgAmt>0</ReportableCompFromRltdOrgAmt>
    <OtherCompensationAmt>45000</OtherCompensationAmt>
</Form990PartVIISectionAGrp>
```

Map to `people` row:
- `person_name` = `<PersonNm>`
- `title` = `<TitleTxt>`
- `avg_hours_per_week` = `<AverageHoursPerWeekRt>`
- `is_officer` = `<OfficerInd>` present and value is "X" or "true" or "1"
- `is_director` = `<IndividualTrusteeOrDirectorInd>` present
- `is_key_employee` = `<KeyEmployeeInd>` present
- `is_highest_comp` = `<HighestCompensatedEmployeeInd>` present
- `is_former` = `<FormerOfcrDirectorTrusteeInd>` present
- `reportable_comp` = `<ReportableCompFromOrgAmt>` (already in whole dollars from IRS; store as cents × 100)
- `related_org_comp` = `<ReportableCompFromRltdOrgAmt>` × 100
- `other_comp` = `<OtherCompensationAmt>` × 100

### Part VII Section B — Independent Contractors

```xml
<ContractorCompensationGrp>
    <ContractorName>
        <BusinessName>
            <BusinessNameLine1Txt>Acme Design Group LLC</BusinessNameLine1Txt>
        </BusinessName>
    </ContractorName>
    <ServicesDesc>Design and communications</ServicesDesc>
    <CompensationAmt>80000</CompensationAmt>
</ContractorCompensationGrp>
```

Map to `people` row:
- `person_name` = `<BusinessNameLine1Txt>` (or `<PersonNm>` if individual contractor)
- `person_type` = `'contractor'`
- `services_desc` = `<ServicesDesc>`
- `reportable_comp` = `<CompensationAmt>` × 100
- All role flags = FALSE
- `avg_hours_per_week` = NULL

## Pipeline

### Step 1: Index Download

For each target year, download the TEOS index CSV. Filter to:
- `RETURN_TYPE = '990'`
- `EIN IN (SELECT ein FROM lava_corpus.nonprofits_seed)` (only our seeded orgs)

Insert matching rows into `filing_index` with `status='indexed'`. Skip rows where `object_id` already exists (idempotent).

### Step 2: XML Download + Parse

For each `filing_index` row with `status='indexed'`:
1. Look up `xml_batch_id` from the filing_index row. The `XML_BATCH_ID` column from the TEOS index CSV directly names the zip file: `{YEAR}_TEOS_XML_{XML_BATCH_ID}.zip`. Store this in `filing_index.xml_batch_id` during Step 1.
2. Download the zip if not already cached. Update status to `'downloaded'`.
3. Extract the specific XML file (`{OBJECT_ID}_public.xml`) from the zip using `zipfile.ZipFile` — read by name, don't extract the entire archive.
4. Parse Part VII Section A and Section B.
5. If Part VII is absent, set status to `'skipped'` and continue.
6. Upsert rows into `people` table.
7. Update `filing_index.status` to `'parsed'`, set `parsed_at`.

**Rate limiting**: Throttle zip downloads to 1 request per second against IRS TEOS. Cache zip files locally in a configurable directory (default: `~/.lavandula/990-cache/`). Log cache size at startup.

**Cache management**: Zip files are large (1-2GB each). The CLI logs total cache size on startup. Manual cleanup via `rm`. No automatic eviction — the operator decides when to clear cache.

**Error handling**: If XML parsing fails for a filing, set `filing_index.status='error'` with `error_message`, continue to next filing. Don't halt the pipeline for individual parse failures.

**Download integrity**: Write zip files atomically (download to `.tmp` suffix, rename on success). Verify HTTP Content-Length matches downloaded size. On mismatch or truncation, delete the partial file and retry. A failed parse does NOT trigger automatic redownload — the operator uses `--reparse` to retry from cached files or manually deletes the corrupt zip.

**Retry policy**: Retryable errors are HTTP 429, 5xx, and connection timeouts. Backoff: 2s, 4s, 8s (exponential, max 3 attempts). Non-retryable: 404 (mark filing as error), 403. Log each retry at WARNING.

**Security:**
- **XML parsing**: Use `defusedxml.ElementTree` exclusively. Do not use `lxml` or stdlib `xml.etree` — mandate `defusedxml` to prevent XXE by default.
- **Zip extraction**: Before reading any member, check `ZipInfo.file_size` against a 50MB cap (real 990 XMLs are <2MB). Reject members exceeding the cap. Validate member filenames match `{OBJECT_ID}_public.xml` pattern — reject names containing `..`, `/`, or path traversal.
- **Cache directory**: Must be an existing directory. CLI validates at startup. Reject symlinks.
- **Input sanitization**: All text fields from 990 XML (`person_name`, `title`, `services_desc`) are stored as plain text. The dashboard already uses Django's auto-escaping for template rendering, which prevents XSS. Do not use `|safe` or `mark_safe()` on any field sourced from 990 data. Strip HTML tags from stored values as defense-in-depth: `re.sub(r'<[^>]+>', '', value)`.
- **CLI input validation**: `--ein` validated as `^\d{9}$`. `--state` validated as `^[A-Z]{2}$`. `--years` validated as comma-separated 4-digit years. `--cache-dir` validated as existing directory, no symlinks. `run_id` is generated internally (UUID), never from user input.
- **Error messages**: `error_message` in `filing_index` must not include raw XML content or stack traces — only a sanitized summary (e.g., "Part VII parse error: missing PersonNm element").

### Step 3: Briefing Generation (Future)

Out of scope for this spec but the intended consumer. A query like:

```sql
-- Leadership snapshot for pre-call briefing
SELECT person_name, title, person_type,
       total_comp / 100.0 AS total_comp_dollars,
       services_desc,
       tax_period
FROM lava_corpus.people
WHERE ein = :ein
ORDER BY tax_period DESC, total_comp DESC;
```

## CLI Interface

```
python3 -m lavandula.nonprofits.tools.enrich_990 \
    --state NY \
    --years 2020,2021,2022,2023,2024 \
    --limit 100 \
    --cache-dir /tmp/lavandula-990/
```

**Flags:**
- `--state` — Filter EINs to orgs in this state (from `nonprofits_seed`)
- `--years` — Comma-separated filing years to process (default: last 5 years)
- `--limit` — Max orgs to process (for testing)
- `--ein` — Process a single EIN (for debugging)
- `--cache-dir` — Directory for cached zip files
- `--skip-download` — Parse only from cached files (offline mode)
- `--reparse` — Re-parse previously parsed filings

## Dashboard Integration

Add `990-enrich` as a phase in the pipeline orchestrator with parameters:
- `state` — state filter dropdown
- `years` — text input (comma-separated years)
- `limit` — integer input

## Acceptance Criteria

### Data Model
- AC1: `people` table exists with all specified columns and indexes
- AC2: `filing_index` table exists with all specified columns and indexes
- AC3: Dedup index prevents duplicate person entries for same filing
- AC4: `total_comp` computed column works correctly

### Index Processing
- AC5: Downloads TEOS index CSV for specified years
- AC6: Filters to RETURN_TYPE='990' and EINs in nonprofits_seed
- AC7: Inserts into filing_index idempotently (skips existing object_ids)

### XML Parsing
- AC8: Correctly parses Part VII Section A (officers/directors/key employees)
- AC9: Correctly parses Part VII Section B (independent contractors)
- AC10: Maps all XML fields to people table columns correctly
- AC11: Handles missing/optional fields gracefully (NULL, not error)
- AC12: Handles both "X" and "true"/"1" for boolean indicators
- AC13: Stores compensation in cents (× 100 from IRS dollar amounts)

### Pipeline
- AC14: CLI accepts --state, --years, --limit, --ein, --cache-dir flags
- AC15: Downloads and caches zip files with 1 req/sec throttle
- AC16: Extracts specific XML from zip without extracting entire archive
- AC17: Upserts people rows (ON CONFLICT update)
- AC18: Updates filing_index status through lifecycle (indexed → downloaded → parsed)
- AC19: Error in one filing doesn't halt pipeline
- AC20: --reparse flag re-processes previously parsed filings

### Error Handling
- AC21: Malformed XML → filing_index.status='error' with message
- AC22: Missing Part VII (some 990s don't have it) → skip gracefully, not error
- AC23: Network errors on download → retry with backoff, max 3 attempts
- AC24: Zip file not found for object_id → log warning, mark error, continue

### Dashboard
- AC25: 990-enrich phase appears in orchestrator COMMAND_MAP
- AC26: Dashboard form has state, years, limit inputs

### Tests
- AC27: Unit test: parse fixture XML with Part VII Section A → correct people rows
- AC28: Unit test: parse fixture XML with Part VII Section B → correct contractor rows
- AC29: Unit test: parse XML missing Part VII → empty result, no error
- AC30: Unit test: dedup on re-parse (same filing twice → same row count)
- AC31: Unit test: compensation cents conversion
- AC32: Integration test: download real TEOS index CSV, verify schema
- AC33: Unit test: person_type derivation from role flags
- AC34: XML parsing rejects DTDs and external entities; zip extraction rejects path-traversing members
- AC35: Unit test: contractor with `BusinessName` vs individual with `PersonNm` both parse correctly
- AC36: Unit test: amended filing for same EIN+tax_period stores both, query picks latest by sub_date
- AC37: Unit test: `is_former=TRUE` with `person_type='officer'` for former officers (not `person_type='former'`)
- AC38: Zip members exceeding 50MB uncompressed size are rejected
- AC39: HTML tags stripped from person_name, title, services_desc before storage
- AC40: Atomic zip download (tmp + rename), truncated downloads detected and cleaned up
- AC41: Retry with exponential backoff on 429/5xx, max 3 attempts
- AC42: CLI validates --ein, --state, --years, --cache-dir inputs

## Traps to Avoid

1. **IRS XML namespace variations** — Different filing years use different XML namespaces. Parse with namespace-agnostic methods (local name matching, not full namespace URIs).

2. **Compensation already in whole dollars** — IRS XML amounts are integers in dollars, not cents. Multiply by 100 for our cents storage. Don't double-convert.

3. **Contractor names are nested** — `<ContractorName>` can contain either `<BusinessName><BusinessNameLine1Txt>` or `<PersonNm>`. Handle both.

4. **Large zip files** — Monthly TEOS zips can be 1-2GB. Extract individual XML files by name, don't extract the entire archive into memory.

5. **Same person, multiple roles** — A person can be both officer AND director in the same filing. The XML has separate boolean indicators. Store all flags, derive `person_type` by priority.

6. **Filing amendments** — An org may file an amended 990 for the same tax period. The index CSV includes both original and amended. **Store all filings** — each has a unique `object_id`, so the dedup index `(ein, object_id, person_name, person_type)` handles this naturally. Queries that want "current" leadership join to `filing_index` and use `DISTINCT ON (ein, tax_period, person_name) ORDER BY sub_date DESC` to pick the latest amendment.

7. **EIN format** — IRS uses 9-digit EINs without dash. ProPublica/our seed uses the same format. No conversion needed, but validate.

## Consultation Log

### Round 1: Spec Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

8 findings, all addressed in v2:

1. **"Last 5 years" vs `--years` flag ambiguity** — Clarified: `--years` controls which years to process, default is last 5. No canonical selection rule needed beyond what the operator requests.
2. **Amendment handling unresolved** — Fixed: store all filings (each has unique `object_id`), queries use `DISTINCT ON ... ORDER BY sub_date DESC` for latest.
3. **`filing_index` lifecycle incomplete** — Added explicit status lifecycle documentation: indexed → downloaded → parsed/skipped/error.
4. **Dedup semantics underspecified** — Fixed: dedup index now on `(ein, object_id, person_name, person_type)` instead of `(ein, tax_period, ...)`. Amendments coexist naturally.
5. **Goal 5 vs "out of scope" conflict** — Reworded Goal 5 to "query support" — the table schema enables briefing queries, actual briefing UI is a follow-up spec.
6. **XML discovery ambiguous** — Fixed: `xml_batch_id` stored in `filing_index` from index CSV, maps directly to zip filename `{YEAR}_TEOS_XML_{XML_BATCH_ID}.zip`.
7. **Security controls missing** — Added: `defusedxml` for XXE prevention, zip member name validation for path traversal, cache directory validation.
8. **Test coverage gaps** — Added AC34-AC37: XXE/zip security, contractor name variants, amendment handling, is_former semantics.

**Claude** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

6 findings, all addressed in v2:

1. **Amendment handling undecided** — Same as Codex #2. Resolved: store both, query picks latest.
2. **XML/zip security** — Same as Codex #7. Added defusedxml + zip slip prevention.
3. **`is_former` in person_type priority** — Fixed: `is_former` is now orthogonal. Removed `'former'` from `person_type` enum. Former officers get `person_type='officer'` + `is_former=TRUE`.
4. **OBJECT_ID → zip mapping unspecified** — Same as Codex #6. Fixed via `xml_batch_id` column.
5. **`/tmp` default for large cache** — Changed default to `~/.lavandula/990-cache/`. Added cache size logging.
6. **`run_id` and `status='downloaded'` undefined** — Added `downloaded` to lifecycle documentation. `run_id` follows the existing pattern from other pipeline tools (set at CLI startup, passed through).

### Round 2: Red Team Security Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

7 findings, all addressed in v3:

1. **[HIGH] Upsert idempotency underspecified** — Fixed: explicit `ON CONFLICT DO UPDATE SET` for all mutable fields. Parser output for a given XML must be deterministic.
2. **[HIGH] Name matching for tenure tracking unreliable** — Fixed: explicitly documented as v1 limitation. Names stored as-is from IRS XML. Fuzzy matching is a follow-up.
3. **[MEDIUM] "Last 5 years" vs `--years` ambiguity** — Fixed: Goal 2 reworded. System processes whatever years operator requests, default is current year minus 4.
4. **[MEDIUM] Retry/backoff unspecified** — Fixed: added explicit retry policy (exponential backoff 2s/4s/8s, max 3 attempts, retryable error classes defined).
5. **[MEDIUM] Cache integrity** — Fixed: atomic downloads (tmp + rename), Content-Length verification, truncation detection.
6. **[MEDIUM] `sub_date` is TEXT but used for ordering** — Fixed: changed to DATE type, parsed from index CSV during Step 1.
7. **[LOW] `return_type` allows unused values** — Acceptable: filing_index stores the raw value from the index CSV for provenance. Pipeline filters on `status`, not `return_type`.

**Claude** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

3 CRITICAL, 6 HIGH findings, addressed in v3:

1. **[CRITICAL] Zip bomb DoS** — Fixed: check `ZipInfo.file_size` against 50MB cap before reading any member. AC38 added.
2. **[CRITICAL] Dashboard XSS from 990 fields** — Fixed: strip HTML tags before storage as defense-in-depth. Dashboard uses Django auto-escaping. Never use `|safe` on 990 data. AC39 added.
3. **[CRITICAL] XML parser choice** — Fixed: mandate `defusedxml` exclusively. Do not use `lxml` or stdlib `xml.etree`.
4. **[HIGH] No download integrity verification** — Fixed: atomic downloads, Content-Length check. AC40 added.
5. **[HIGH] CLI input validation missing** — Fixed: validation rules for --ein, --state, --years, --cache-dir. AC42 added.
6. **[HIGH] Cache symlink attacks** — Fixed: reject symlinks in cache directory validation.
7. **[HIGH] Error messages leak detail** — Fixed: sanitized summaries only, no raw XML or stack traces.
8. **[HIGH] `--skip-download` trusts cache** — Accepted risk: single-operator system, cache directory is under operator control. Documented.
9. **[HIGH] No certificate pinning** — Accepted risk: HTTPS to apps.irs.gov is sufficient for a single-operator enrichment tool. Certificate pinning would add complexity for minimal threat reduction.
