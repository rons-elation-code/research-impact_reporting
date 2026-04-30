# Spec 0026: 990 Leadership & Contractor Intelligence

**Status**: Draft
**Author**: Architect
**Date**: 2026-04-30

## Problem

Lavandula's pre-call briefings lack leadership context. We know the org's name, website, revenue, and what reports they publish — but not who runs the organization, who controls the budget, who their current vendors are, or how long the leadership team has been in place. This information is publicly available in IRS 990 filings but we don't extract it.

## Goals

1. **Extract named individuals** from IRS 990 Part VII Section A (officers, directors, trustees, key employees, highest compensated employees) and Section B (top independent contractors) into a `people` table keyed by EIN + tax period.

2. **Multi-year history** — Store the last 5 years of filings per org to enable tenure tracking and transition detection (new CDO = potential vendor opportunity).

3. **Contractor intelligence** — Capture independent contractor names, service descriptions, and compensation. This reveals existing agency relationships (design firms, fundraising consultants) before the sales call.

4. **Enrichment pipeline step** — Add a `990-enrich` phase to the pipeline that, given a set of EINs from `nonprofits_seed`, downloads and parses the relevant 990 XML filings from IRS TEOS.

5. **Pre-call briefing output** — Produce a structured leadership summary per org: CEO/ED name and tenure, board chair, development officer (if disclosed), board size, top contractors with service descriptions.

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
    person_type     TEXT NOT NULL,           -- 'officer', 'director', 'key_employee', 'highest_compensated', 'former', 'contractor'
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
CREATE UNIQUE INDEX idx_people_dedup ON lava_corpus.people(ein, tax_period, person_name, person_type);
```

**Design decisions:**

- **Compensation in cents** (BIGINT) not dollars — avoids floating-point issues, matches IRS precision.
- **`person_type`** is derived from the role flags for quick filtering. An officer who is also a director gets `person_type='officer'` (highest authority wins: officer > key_employee > highest_compensated > director > former).
- **`object_id`** links back to the specific IRS filing for provenance.
- **Dedup index** on `(ein, tax_period, person_name, person_type)` prevents duplicate entries on re-runs. Same person with same name in same filing = upsert.
- **No FK constraint** to `nonprofits_seed` — we may enrich EINs before they're fully seeded, and the single-operator pattern doesn't need referential integrity enforcement.

### Table: `lava_corpus.filing_index`

Track which filings we've processed to enable incremental runs.

```sql
CREATE TABLE lava_corpus.filing_index (
    object_id       TEXT PRIMARY KEY,       -- IRS OBJECT_ID
    ein             TEXT NOT NULL,
    tax_period      TEXT NOT NULL,
    return_type     TEXT NOT NULL,           -- '990', '990EZ', '990PF'
    sub_date        TEXT,                   -- submission date from index
    taxpayer_name   TEXT,
    status          TEXT DEFAULT 'indexed',  -- 'indexed', 'downloaded', 'parsed', 'error'
    error_message   TEXT,
    parsed_at       TIMESTAMPTZ,
    run_id          TEXT
);

CREATE INDEX idx_filing_ein ON lava_corpus.filing_index(ein);
CREATE INDEX idx_filing_status ON lava_corpus.filing_index(status);
```

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
1. Determine which zip file contains the `OBJECT_ID` (from `XML_BATCH_ID` or by month)
2. Download the zip (cache locally to avoid re-downloading)
3. Extract the specific XML file
4. Parse Part VII Section A and Section B
5. Upsert rows into `people` table
6. Update `filing_index.status` to `'parsed'`

**Rate limiting**: Throttle downloads to 1 request per second against IRS TEOS. Cache zip files locally in a configurable directory (default: `/tmp/lavandula-990/`).

**Error handling**: If XML parsing fails for a filing, set `filing_index.status='error'` with `error_message`, continue to next filing. Don't halt the pipeline for individual parse failures.

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

## Traps to Avoid

1. **IRS XML namespace variations** — Different filing years use different XML namespaces. Parse with namespace-agnostic methods (local name matching, not full namespace URIs).

2. **Compensation already in whole dollars** — IRS XML amounts are integers in dollars, not cents. Multiply by 100 for our cents storage. Don't double-convert.

3. **Contractor names are nested** — `<ContractorName>` can contain either `<BusinessName><BusinessNameLine1Txt>` or `<PersonNm>`. Handle both.

4. **Large zip files** — Monthly TEOS zips can be 1-2GB. Extract individual XML files by name, don't extract the entire archive into memory.

5. **Same person, multiple roles** — A person can be both officer AND director in the same filing. The XML has separate boolean indicators. Store all flags, derive `person_type` by priority.

6. **Filing amendments** — An org may file an amended 990 for the same tax period. The index CSV includes both original and amended. Use the most recent `SUB_DATE` for a given EIN+tax_period, or store both and let the query pick the latest.

7. **EIN format** — IRS uses 9-digit EINs without dash. ProPublica/our seed uses the same format. No conversion needed, but validate.

## Consultation Log

(To be filled during review)
