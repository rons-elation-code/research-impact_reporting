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

6. **Schedule J compensation detail** — Parse `IRS990ScheduleJ` to capture the granular compensation breakdown (base salary, bonus, deferred comp, nontaxable benefits) per officer/key employee. Part VII reports a single `ReportableCompFromOrgAmt` total; Schedule J splits that into components. This reveals bonus structures and deferred comp — signals of organizational sophistication and budget flexibility.

## Non-Goals

- Replacing ProPublica as the seed discovery source. ProPublica remains the source for org discovery (search by state, NTEE, revenue). IRS XML is for enrichment after discovery.
- Parsing Schedule L (interested persons) or Schedule O (narrative). These are valuable but out of scope for v1.
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
    -- Schedule J compensation breakdown (NULL if Schedule J not filed or person not listed)
    base_comp           BIGINT,             -- BaseCompensationFilingOrgAmt (cents)
    bonus               BIGINT,             -- BonusFilingOrganizationAmount (cents)
    other_reportable    BIGINT,             -- OtherCompensationFilingOrgAmt (cents)
    deferred_comp       BIGINT,             -- DeferredCompensationFlngOrgAmt (cents)
    nontaxable_benefits BIGINT,             -- NontaxableBenefitsFilingOrgAmt (cents)
    total_comp_sch_j    BIGINT,             -- TotalCompensationFilingOrgAmt (cents, from Schedule J)
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
- **`person_type`** is derived from the role flags for quick filtering. Priority: officer > key_employee > highest_compensated > director. If none of these flags are set, use `person_type='listed'` (the person was listed in Part VII but had no recognized role flag — this can happen with trustees or unusual flag combinations). The `is_former` flag is orthogonal — a former officer gets `person_type='officer'` with `is_former=TRUE`. This preserves the role for tenure queries while marking departure.
- **`object_id`** links back to the specific IRS filing for provenance.
- **Dedup index** on `(ein, object_id, person_name, person_type)` prevents duplicate entries on re-runs. Keyed on `object_id` (not `tax_period`) so original and amended filings for the same tax period coexist. Queries that want "current" data use `DISTINCT ON (ein, tax_period, person_name) ORDER BY return_ts DESC NULLS LAST` via a join to `filing_index`. Note: a single person can have at most one row per `person_type` per filing. The role-priority derivation is deterministic (same XML always produces the same `person_type`), so the dedup key is stable across re-parses. Schedule J matching uses `(person_name, object_id)` — it updates the existing Part VII row regardless of `person_type`.
- **No FK constraint** to `nonprofits_seed` — we may enrich EINs before they're fully seeded, and the single-operator pattern doesn't need referential integrity enforcement.
- **Upsert behavior**: `ON CONFLICT (ein, object_id, person_name, person_type) DO UPDATE SET` all mutable fields (title, compensation, flags, services_desc, extracted_at, run_id). Same filing reparsed = deterministic overwrite with same values. Parser output for a given XML file must be fully deterministic.
- **Name storage**: V1 stores `person_name` exactly as it appears in the IRS XML after: (1) XML entity decoding (handled by the parser — `&amp;` → `&`), (2) HTML tag stripping, (3) whitespace collapse (strip leading/trailing, collapse internal runs to single space). No case normalization — names stored in whatever case the IRS XML uses (typically uppercase). Tenure queries across years use exact string match, which will miss `Jane Smith` vs `Jane A. Smith`. This is a known limitation — name linkage/fuzzy matching is a follow-up enhancement, not a v1 requirement. Goal 2 is "supports tenure tracking" at best-effort quality, not guaranteed accuracy. Schedule J matching uses the same normalized name for lookup.
- **Mixed "party" table**: The `people` table intentionally stores both natural persons (officers, directors, employees) and business entities (contractors) in v1. Consumers must check `person_type='contractor'` to distinguish — `person_name` may hold a company name, and compensation fields have different semantics (contractors have only `reportable_comp` from `CompensationAmt`, no Schedule J breakdown). This is a pragmatic choice for v1; a separate `contractors` table is a possible follow-up if query patterns diverge significantly.
- **Current-snapshot queries are best-effort**: The `DISTINCT ON (ein, tax_period, person_name) ORDER BY return_ts DESC NULLS LAST` pattern is approximate analytics, not audit-grade identity continuity. Two distinct people with the same name in the same filing period will collapse to one row. Contractor rows are not deduplicated across amendments by name (they may change between filings). Downstream consumers should treat these queries as "good enough for pre-call briefings" not authoritative.
- **Malformed row handling**: A Part VII entry missing `PersonNm` is skipped (log WARNING, continue to next entry). A contractor entry with neither `BusinessNameLine1Txt` nor `PersonNm` is skipped. Missing optional fields (compensation, hours, title) become NULL. The filing is still marked `parsed` — individual row skips are not filing-level errors. Only XML parse failures (malformed XML, missing root element) set `status='error'`.
- **Boolean indicator parsing**: Role flag indicators (`OfficerInd`, `IndividualTrusteeOrDirectorInd`, etc.) are parsed case-insensitively after whitespace trimming. Values `"X"`, `"x"`, `"true"`, `"TRUE"`, `"1"` all count as TRUE. Absent element = FALSE.

### Table: `lava_corpus.filing_index`

Track which filings we've processed to enable incremental runs.

```sql
CREATE TABLE lava_corpus.filing_index (
    object_id       TEXT PRIMARY KEY,       -- IRS OBJECT_ID
    ein             TEXT NOT NULL,
    tax_period      TEXT NOT NULL,           -- YYYYMM from index CSV (e.g., '202312')
    return_type     TEXT NOT NULL,           -- '990', '990EZ', '990PF', '990T'
    sub_date        TEXT,                    -- submission year from index CSV (e.g., '2024')
    return_ts       TIMESTAMPTZ,            -- ReturnTs from XML header (full timestamp, for amendment ordering)
    is_amended      BOOLEAN DEFAULT FALSE,  -- AmendedReturnInd from XML header
    taxpayer_name   TEXT,
    xml_batch_id    TEXT,                   -- from index CSV, maps to zip filename
    filing_year     INTEGER NOT NULL,       -- TEOS directory year (e.g., 2024) — needed to construct zip URL
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
- `parsed` — Part VII (and Schedule J if present) parsed, people rows upserted
- `skipped` — Valid 990 but no Part VII section present (not an error)
- `error` — Download or parse failure, `error_message` populated

**Status transition rules:**
- If a zip is already cached on disk when processing begins, the filing transitions directly from `indexed` → `downloaded` (no re-download). The zip is considered valid if it exists and is a readable zip file.
- `--reparse` resets `parsed` and `skipped` rows back to `downloaded`, clears `error_message` and `parsed_at`, then re-runs the parse step. `error` rows are also re-processed by `--reparse`.
- Re-parsing overwrites `people` rows via the upsert — deterministic output means same result.

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
- All Schedule J fields = NULL (contractors are not in Schedule J)

### Schedule J — Compensation Detail

Schedule J is filed by orgs that answer "Yes" to Part IV line 23 (compensation >$150K). Not all 990s include it. When present, it provides per-person compensation breakdown for officers, directors, trustees, key employees, and highest compensated employees listed in Part VII Section A.

```xml
<IRS990ScheduleJ>
  <RltdOrgOfficerTrstKeyEmplGrp>
    <PersonNm>DAVID DIMMETT ED D</PersonNm>
    <TitleTxt>PRESIDENT &amp; CEO</TitleTxt>
    <BaseCompensationFilingOrgAmt>428443</BaseCompensationFilingOrgAmt>
    <CompensationBasedOnRltdOrgsAmt>0</CompensationBasedOnRltdOrgsAmt>
    <BonusFilingOrganizationAmount>71000</BonusFilingOrganizationAmount>
    <BonusRelatedOrganizationsAmt>0</BonusRelatedOrganizationsAmt>
    <OtherCompensationFilingOrgAmt>0</OtherCompensationFilingOrgAmt>
    <OtherCompensationRltdOrgsAmt>0</OtherCompensationRltdOrgsAmt>
    <DeferredCompensationFlngOrgAmt>46300</DeferredCompensationFlngOrgAmt>
    <DeferredCompRltdOrgsAmt>0</DeferredCompRltdOrgsAmt>
    <NontaxableBenefitsFilingOrgAmt>24207</NontaxableBenefitsFilingOrgAmt>
    <NontaxableBenefitsRltdOrgsAmt>0</NontaxableBenefitsRltdOrgsAmt>
    <TotalCompensationFilingOrgAmt>569950</TotalCompensationFilingOrgAmt>
    <TotalCompensationRltdOrgsAmt>0</TotalCompensationRltdOrgsAmt>
  </RltdOrgOfficerTrstKeyEmplGrp>
</IRS990ScheduleJ>
```

**Merge strategy**: Schedule J entries are matched to existing `people` rows by `(person_name, object_id)`. After parsing Part VII Section A, iterate over `RltdOrgOfficerTrstKeyEmplGrp` entries in Schedule J and UPDATE the matching `people` row with the compensation breakdown fields. If no Part VII row matches (name mismatch), log a warning and skip — do not create a new `people` row from Schedule J alone, since Part VII is the authoritative person list. If ALL Schedule J entries fail to match (100% mismatch), log at ERROR level — this likely indicates a parser bug or normalization drift rather than benign name variance. The filing still gets `status='parsed'` (the Part VII data is valid), but the error-level log alerts the operator to investigate.

Map to `people` row (UPDATE existing Part VII row):
- `base_comp` = `<BaseCompensationFilingOrgAmt>` × 100
- `bonus` = `<BonusFilingOrganizationAmount>` × 100
- `other_reportable` = `<OtherCompensationFilingOrgAmt>` × 100
- `deferred_comp` = `<DeferredCompensationFlngOrgAmt>` × 100
- `nontaxable_benefits` = `<NontaxableBenefitsFilingOrgAmt>` × 100
- `total_comp_sch_j` = `<TotalCompensationFilingOrgAmt>` × 100

**Note**: Schedule J `TotalCompensationFilingOrgAmt` may differ from Part VII's `total_comp` (which sums reportable + related + other). Schedule J includes deferred comp and nontaxable benefits that Part VII excludes. Both are stored — Part VII's `total_comp` is the IRS-reportable figure, Schedule J's `total_comp_sch_j` is total economic compensation.

## Pipeline

### Step 1: Index Download

For each target year, download the TEOS index CSV. Filter to:
- `RETURN_TYPE = '990'`
- `EIN IN (SELECT ein FROM lava_corpus.nonprofits_seed)` (only our seeded orgs)

Insert matching rows into `filing_index` with `status='indexed'`. Skip rows where `object_id` already exists (idempotent).

### Step 2: XML Download + Parse

For each `filing_index` row with `status='indexed'`:
1. Look up `xml_batch_id` from the filing_index row. The `XML_BATCH_ID` column from the TEOS index CSV IS the zip filename stem (e.g., `2024_TEOS_XML_01A`). The zip URL is `https://apps.irs.gov/pub/epostcard/990/xml/{filing_year}/{xml_batch_id}.zip`. Store this in `filing_index.xml_batch_id` during Step 1.
2. Download the zip if not already cached. Update status to `'downloaded'`.
3. Extract the specific XML file (`{OBJECT_ID}_public.xml`) from the zip using `zipfile.ZipFile` — read by name, don't extract the entire archive.
4. Parse Part VII Section A and Section B.
5. If Part VII is absent, set status to `'skipped'` and continue.
6. Upsert rows into `people` table.
7. Parse Schedule J if present — match entries to Part VII rows by name, update compensation breakdown fields.
8. Update `filing_index.status` to `'parsed'`, set `parsed_at`.

**Batch grouping**: Step 2 MUST group `filing_index` rows by `(filing_year, xml_batch_id)` before processing. Open each zip once, extract all matching `{OBJECT_ID}_public.xml` members in a single pass, then close. This avoids reopening multi-GB zips repeatedly. Processing order: group by batch → download zip → extract all members → parse each → upsert.

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
       base_comp / 100.0 AS base_salary,
       bonus / 100.0 AS bonus,
       deferred_comp / 100.0 AS deferred,
       nontaxable_benefits / 100.0 AS benefits,
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

Add `990-enrich` as a phase in the pipeline orchestrator `COMMAND_MAP` with parameters:
- `state` — state filter dropdown (required)
- `years` — text input, comma-separated years (default: last 5 years, pre-populated)
- `limit` — integer input (optional, empty = no limit)

The dashboard form does NOT expose `--ein`, `--skip-download`, or `--reparse` — those are operator CLI-only flags for debugging. Default `--cache-dir` is used. Years input is validated server-side (same rules as CLI: comma-separated 4-digit years). Invalid input returns a form error, does not create a job.

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
- AC32: Integration test (manual/CI-optional, not unit): download real TEOS index CSV, verify schema
- AC33: Unit test: person_type derivation from role flags
- AC34: XML parsing rejects DTDs and external entities; zip extraction rejects path-traversing members
- AC35: Unit test: contractor with `BusinessName` vs individual with `PersonNm` both parse correctly
- AC36: Unit test: amended filing for same EIN+tax_period stores both, query picks latest by return_ts
- AC37: Unit test: `is_former=TRUE` with `person_type='officer'` for former officers (not `person_type='former'`)
- AC38: Zip members exceeding 50MB uncompressed size are rejected
- AC39: HTML tags stripped from person_name, title, services_desc before storage
- AC40: Atomic zip download (tmp + rename), truncated downloads detected and cleaned up
- AC41: Retry with exponential backoff on 429/5xx, max 3 attempts
- AC42: CLI validates --ein, --state, --years, --cache-dir inputs

### Schedule J
- AC43: Schedule J compensation breakdown fields (base_comp, bonus, deferred_comp, nontaxable_benefits, other_reportable, total_comp_sch_j) populated when Schedule J present
- AC44: Schedule J entries matched to Part VII rows by person_name — unmatched entries logged as warning, not inserted
- AC45: Filing without Schedule J leaves all Schedule J columns NULL (not zero)
- AC46: Unit test: parse fixture XML with Schedule J → correct breakdown on matching Part VII row
- AC47: Unit test: Schedule J name mismatch → warning logged, no orphan row created
- AC48: Expected XML member (`{OBJECT_ID}_public.xml`) absent from a valid zip → filing_index.status='error', error_message set, pipeline continues
- AC49: Unit test: Part VII entry with no role flags → person_type='listed'
- AC50: Unit test: --skip-download with missing cache file → filing remains `indexed`, warning logged, pipeline continues
- AC51: Unit test: --reparse re-processes `error` rows (clears error_message, re-parses)
- AC52: Unit test: idempotent index insertion (running same year twice doesn't duplicate filing_index rows)
- AC53: Unit test: Part VII entry missing PersonNm → row skipped with WARNING, filing still parsed
- AC54: Filings grouped by (filing_year, xml_batch_id) — each zip opened at most once per run

## Traps to Avoid

1. **IRS XML namespace variations** — Different filing years use different XML namespaces. Parse with namespace-agnostic methods (local name matching, not full namespace URIs).

2. **Compensation already in whole dollars** — IRS XML amounts are integers in dollars, not cents. Multiply by 100 for our cents storage. Don't double-convert.

3. **Contractor names are nested** — `<ContractorName>` can contain either `<BusinessName><BusinessNameLine1Txt>` or `<PersonNm>`. Handle both.

4. **Large zip files** — Monthly TEOS zips can be 1-2GB. Extract individual XML files by name, don't extract the entire archive into memory.

5. **Same person, multiple roles** — A person can be both officer AND director in the same filing. The XML has separate boolean indicators. Store all flags, derive `person_type` by priority.

6. **Filing amendments** — An org may file an amended 990 for the same tax period. The index CSV includes both original and amended. **Store all filings** — each has a unique `object_id`, so the dedup index `(ein, object_id, person_name, person_type)` handles this naturally. Queries that want "current" leadership join to `filing_index` and use `DISTINCT ON (ein, tax_period, person_name) ORDER BY return_ts DESC NULLS LAST` to pick the latest amendment.

7. **EIN format** — IRS uses 9-digit EINs without dash. ProPublica/our seed uses the same format. No conversion needed, but validate.

## Consultation Log

### Round 1: Spec Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

8 findings, all addressed in v2:

1. **"Last 5 years" vs `--years` flag ambiguity** — Clarified: `--years` controls which years to process, default is last 5. No canonical selection rule needed beyond what the operator requests.
2. **Amendment handling unresolved** — Fixed: store all filings (each has unique `object_id`), queries use `DISTINCT ON ... ORDER BY return_ts DESC NULLS LAST` for latest.
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
6. **[MEDIUM] `sub_date` is TEXT but used for ordering** — Investigated: index CSV `SUB_DATE` is just a year (e.g., '2024'), not a full date. Amendment ordering now uses `return_ts` (full timestamp parsed from XML `ReturnTs` header) instead. `sub_date` stays as TEXT.
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

### Round 3: Schedule J Addition + Spec Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

7 findings, all addressed in v5:

1. **`filing_year` column missing** — Fixed: added `filing_year INTEGER NOT NULL` to `filing_index`. Needed to construct zip download URL `{YEAR}_TEOS_XML_{batch}.zip`.
2. **Dedup key vs Schedule J matching ambiguity** — Fixed: clarified that role-priority derivation is deterministic (same XML → same `person_type`), so dedup key is stable. Schedule J matches by `(person_name, object_id)` and updates regardless of `person_type`.
3. **No-flag Part VII entries** — Fixed: entries with no recognized role flags get `person_type='listed'`.
4. **Name normalization boundary** — Fixed: specified 3-step normalization (XML entity decode, HTML strip, whitespace collapse). No case normalization. Schedule J matching uses same normalization.
5. **AC32 live download brittle for CI** — Fixed: marked as integration-only, CI-optional.
6. **Dashboard requirements too thin** — Fixed: expanded dashboard section with validation rules, default years, which flags are CLI-only.
7. **Missing XML member in valid zip** — Fixed: AC48 added. Mark filing as error, continue pipeline.

**Gemini** — Quota exhausted, no response.

### Round 4: Red Team Security Review (2026-04-30)

**Codex** — **Verdict**: REQUEST_CHANGES (HIGH confidence)

8 findings, all addressed in v6:

1. **[HIGH] Current-snapshot queries underspecified for contractors/duplicate names** — Fixed: documented as best-effort analytics, not audit-grade. Consumers must check `person_type` to distinguish natural persons from contractors.
2. **[HIGH] Malformed/partial Part VII row handling undefined** — Fixed: missing `PersonNm` → skip row (WARNING). Missing optional fields → NULL. Individual row skips don't affect filing status.
3. **[MEDIUM] Zip batch grouping not required** — Fixed: Step 2 now mandates grouping by `(filing_year, xml_batch_id)`. Each zip opened once per run. AC54 added.
4. **[MEDIUM] Status transitions for cache reuse and --reparse ambiguous** — Fixed: added explicit status transition rules. Cached zip → skip download. --reparse resets parsed/skipped/error → downloaded.
5. **[MEDIUM] Boolean parsing not normalized** — Fixed: case-insensitive, whitespace-trimmed. "X", "x", "true", "TRUE", "1" all = TRUE.
6. **[MEDIUM] Schedule J mismatch threshold undefined** — Fixed: 100% mismatch logs at ERROR level (possible parser bug). Filing still `parsed`.
7. **[LOW] Mixed party table undocumented** — Fixed: explicit design decision noting `people` is intentionally mixed for v1.
8. **[LOW] Missing state-transition tests** — Fixed: AC50-AC54 added for --skip-download, --reparse, idempotent index, missing PersonNm, batch grouping.

**Gemini** — Quota exhausted, no response.
