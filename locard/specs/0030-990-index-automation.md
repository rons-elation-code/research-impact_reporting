# Spec 0030: 990 Filing Index Automation & S3 Archive

**Status**: Draft
**Author**: Architect
**Date**: 2026-05-01

## Problem

The current 990 pipeline requires a human to manually trigger index downloads for specific EINs and years. Each request re-downloads a 70–90 MB IRS index CSV, scans 700K+ rows, and discards the result after extracting one match. This is slow (~12 seconds per year), wasteful, and doesn't scale.

Additionally:
- **2017–2023 indexes are silently ignored** — the IRS added an `XML_BATCH_ID` column in 2024; our indexer requires 10 columns and silently skips all 9-column rows from older years, losing 8 years of filing history.
- **Zip downloads sit on expensive EBS** — the local cache at `~/.lavandula/990-cache/` already holds 1.4 GB from 3 zips. A full backfill across ~120 batch zips would be 50–80 GB on EBS.
- **No automatic maintenance** — when new orgs are added to `nonprofits_seed` or new filings appear in the IRS index, nothing happens until a human notices and clicks buttons.

## Goals

### Goal 1: Bulk Index Load & Maintenance

Pre-load the complete IRS TEOS filing index for all available years (2017–2026) into `filing_index`. Maintain it with a nightly job that refreshes the current year's index to pick up new filings.

**Why all EINs, not just ours?** The full 990 index is ~2.6M rows — trivially small for Postgres. Pre-loading everything means:
- Instant lookup when a new org is added to our seed list
- No per-request CSV downloads
- No filtering logic at index time (filter at query time instead)

### Goal 2: S3-Backed 990 Archive

Replace local EBS zip cache with a dedicated S3 bucket. Download IRS batch zips to S3 once, extract individual per-org XML files to a structured prefix (`990-xml/{ein}/{object_id}.xml`). Parse from S3, not local disk.

### Goal 3: Automatic 990 Processing for Tracked Orgs

A background worker that keeps 990 data current for every org in `nonprofits_seed`:

1. **New org trigger** — When an EIN is added to `nonprofits_seed`, check `filing_index` for matching filings and queue them for download/parse.
2. **New filing trigger** — After the nightly index refresh, identify new `filing_index` rows where the EIN exists in `nonprofits_seed` and queue them for download/parse.
3. **Backfill** — On first run, process all existing `nonprofits_seed` EINs that have indexed but unprocessed filings.

### Goal 4: Manual Controls Remain

The dashboard 990 Index and 990 Parse forms stay available for ad-hoc use (e.g., one-off EINs not in the seed list). But for tracked orgs, the pipeline is fully automatic.

## Non-Goals

- Parsing 990-EZ, 990-PF, or 990-T. Continue filtering to `RETURN_TYPE = '990'` only.
- Real-time streaming from IRS. The IRS updates indexes daily/weekly; nightly refresh is sufficient.
- Deleting the dashboard manual controls. They remain for ad-hoc use.
- Migrating existing parsed data. Already-parsed filings (`status = 'parsed'`) are untouched.

## Data Source Analysis

### IRS TEOS Index CSVs

| Year | Rows | ~990s (50%) | Size | Columns |
|------|------|-------------|------|---------|
| 2017 | 489K | ~245K | 62 MB | 9 (no XML_BATCH_ID) |
| 2018 | 457K | ~229K | 58 MB | 9 |
| 2019 | 417K | ~209K | 53 MB | 9 |
| 2020 | 399K | ~200K | 51 MB | 9 |
| 2021 | 590K | ~295K | 75 MB | 9 |
| 2022 | 657K | ~329K | 72 MB | 9 |
| 2023 | 705K | ~353K | 78 MB | 9 |
| 2024 | 729K | 363K | 91 MB | 10 (has XML_BATCH_ID) |
| 2025 | 749K | ~375K | 93 MB | 10 |
| 2026 | 99K | ~50K | 12 MB | 10 |
| **Total** | **~5.3M** | **~2.6M** | **~645 MB** | |

### Column Format Change

- **2017–2023**: 9 columns — `RETURN_ID, FILING_TYPE, EIN, TAX_PERIOD, SUB_DATE, TAXPAYER_NAME, RETURN_TYPE, DLN, OBJECT_ID`
- **2024–2026**: 10 columns — same + `XML_BATCH_ID`

### IRS Batch Zip Structure

- URL pattern: `https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch_id}.zip`
- Batch IDs: `{year}_TEOS_XML_{01A..12A}` (monthly batches, some months have B/C/D sub-batches)
- 2024 has 12 batches, 2025 has ~16 (with sub-batches), 2022 has only 2
- Zip sizes: 100 MB – 2.5 GB each
- Member paths: nested `{batch_id}/{object_id}_public.xml` (2024) or flat `{object_id}_public.xml` (2025) — already handled by the fix in commit c092ce8
- Total estimated zip storage: 50–80 GB across all years

### Scale for Our Orgs

- `nonprofits_seed` has ~38K EINs
- 2024 index has 334K unique 990-filing EINs
- Our orgs represent ~10-15% of all 990 filers
- Estimated filings to process: 100–200K across 10 years

## Technical Design

### Phase 1: Fix the 9-Column Bug

Change `teos_index.py` to accept 9-column rows. When `XML_BATCH_ID` is missing (2017–2023 indexes), store `NULL` in `filing_index.xml_batch_id`.

### Phase 2: Bulk Index Loader

New management command: `manage.py load_990_index`

```
# Load all available years
python3 manage.py load_990_index

# Load specific years
python3 manage.py load_990_index --years 2024,2025

# Refresh current year only (for nightly cron)
python3 manage.py load_990_index --current-year
```

Behavior:
- Downloads each year's index CSV
- Inserts all `RETURN_TYPE = '990'` rows into `filing_index` via `ON CONFLICT (object_id) DO NOTHING`
- For 9-column years, `xml_batch_id` = NULL
- Reports: rows scanned, matched (990), inserted, skipped per year
- Idempotent — safe to re-run

### Phase 3: Resolve Missing Batch IDs

For 2017–2023 filings where `xml_batch_id IS NULL`, we need to determine which batch zip contains each filing's XML.

Approach: **Batch manifest scan** — for each year, download each batch zip's file listing (not the full zip), build an `object_id → batch_id` mapping, and update `filing_index`.

```sql
UPDATE filing_index SET xml_batch_id = :batch_id
WHERE object_id = :object_id AND xml_batch_id IS NULL;
```

This is a one-time backfill. New filings (2024+) always have `xml_batch_id` in the index.

Implementation note: Python's `zipfile` module can read the central directory from a remote zip via HTTP Range requests without downloading the entire file. If the IRS server supports Range (likely), we can resolve batch membership for ~2M filings by downloading only the central directories (~1-5 MB each vs 100 MB–2.5 GB for the full zip). If Range isn't supported, fall back to downloading full zips.

### Phase 4: S3 Bucket & Archive

New S3 bucket: `lavandula-990-corpus` (us-east-1, SSE-S3, versioned, private)

Prefix structure:
```
s3://lavandula-990-corpus/
  zips/{year}/{batch_id}.zip          # IRS batch zips (cached, Glacier-eligible)
  xml/{ein}/{object_id}.xml           # Extracted per-org XMLs (working set)
```

Download flow:
1. Check if `zips/{year}/{batch_id}.zip` exists in S3
2. If not, download from IRS → stream to S3
3. Extract target filing's XML member → upload to `xml/{ein}/{object_id}.xml`
4. Parse from S3 (stream XML through memory, same as current `teos_download.py` logic)

The `xml/` prefix is organized by EIN for easy per-org access and lifecycle management.

### Phase 5: Auto-Process Worker

New management command: `manage.py process_990_auto`

Behavior:
1. Query `filing_index` for filings where:
   - `ein IN (SELECT ein FROM nonprofits_seed)`
   - `status = 'indexed'` (not yet downloaded/parsed)
   - `xml_batch_id IS NOT NULL` (batch resolved)
2. Group by `(filing_year, xml_batch_id)` for efficient batch zip reuse
3. For each batch: download zip (or use S3 cache), extract matching XMLs, parse, update status
4. Concurrency: single worker, processes one batch at a time (same advisory lock pattern as existing 990 jobs)

Run modes:
- **Backfill**: `manage.py process_990_auto --backfill` — process all unprocessed filings for tracked orgs
- **Incremental**: `manage.py process_990_auto` — process only filings indexed since last run
- **Single EIN**: `manage.py process_990_auto --ein 030440761` — process one org (useful for testing)

### Phase 6: Nightly Cron

A cron job (or systemd timer) that runs:
```bash
# 1. Refresh the current year's index
python3 manage.py load_990_index --current-year

# 2. Process any new filings for tracked orgs
python3 manage.py process_990_auto
```

Schedule: nightly at 03:00 UTC (IRS updates are infrequent, daily is more than enough).

### Phase 7: Dashboard Changes

- **990 Index page**: Add status banner showing last index refresh time and total indexed filings. The "Queue Index Job" form remains for ad-hoc use but is no longer the primary path.
- **Org Detail page**: Filing list is now pre-populated. No manual indexing needed for tracked orgs.
- **New admin view**: Index maintenance status — last refresh per year, total filings by year, unresolved batch IDs count.

## Schema Changes

### filing_index modifications

```sql
-- Allow NULL xml_batch_id for 2017-2023 filings
ALTER TABLE lava_corpus.filing_index
  ALTER COLUMN xml_batch_id DROP NOT NULL;

-- Add column to track when filing was indexed
ALTER TABLE lava_corpus.filing_index
  ADD COLUMN indexed_at TIMESTAMPTZ DEFAULT now();

-- Add column for S3 XML location (set after extraction)
ALTER TABLE lava_corpus.filing_index
  ADD COLUMN s3_xml_key TEXT;
```

### New table: index_refresh_log

```sql
CREATE TABLE lava_corpus.index_refresh_log (
    id              SERIAL PRIMARY KEY,
    filing_year     INTEGER NOT NULL,
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_scanned    INTEGER NOT NULL DEFAULT 0,
    rows_inserted   INTEGER NOT NULL DEFAULT 0,
    rows_skipped    INTEGER NOT NULL DEFAULT 0,
    duration_sec    NUMERIC(8,2)
);
```

## Acceptance Criteria

### Goal 1: Bulk Index
- AC1: `load_990_index` downloads and inserts all 990 rows from years 2017–2026
- AC2: 9-column CSVs (2017–2023) are processed correctly with `xml_batch_id = NULL`
- AC3: 10-column CSVs (2024–2026) are processed with `xml_batch_id` populated
- AC4: Idempotent — re-running inserts 0 new rows
- AC5: `--current-year` flag only fetches the current year
- AC6: `index_refresh_log` records stats for each run
- AC7: Existing `teos_index.py` is updated to accept 9-column rows (fixes current silent-skip bug)

### Goal 2: S3 Archive
- AC8: New S3 bucket `lavandula-990-corpus` created with SSE-S3, versioning, private ACL
- AC9: Batch zips download from IRS to `s3://lavandula-990-corpus/zips/`
- AC10: Per-org XMLs extracted to `s3://lavandula-990-corpus/xml/{ein}/{object_id}.xml`
- AC11: `filing_index.s3_xml_key` populated after extraction
- AC12: Parse reads XML from S3, not local disk
- AC13: Local EBS cache (`~/.lavandula/990-cache/`) no longer used for new downloads

### Goal 3: Auto-Process
- AC14: `process_990_auto` processes all unprocessed filings for `nonprofits_seed` EINs
- AC15: `--backfill` mode processes historical filings
- AC16: Incremental mode processes only newly indexed filings
- AC17: Batch zip reuse — multiple filings from same zip don't re-download
- AC18: Advisory lock prevents concurrent auto-process runs
- AC19: After nightly cron, new IRS filings for tracked orgs are automatically parsed

### Goal 4: Batch ID Resolution
- AC20: Filings from 2017–2023 have `xml_batch_id` resolved via batch manifest scan
- AC21: HTTP Range requests used if supported by IRS server; full download fallback otherwise
- AC22: Resolution is idempotent and only targets `xml_batch_id IS NULL` rows

### Goal 5: Dashboard
- AC23: 990 Index page shows last refresh time and total indexed filings
- AC24: Manual index/parse forms still work for ad-hoc use
- AC25: Org Detail filing list is pre-populated for tracked orgs

## Migration Path

1. Fix 9-column bug in existing `teos_index.py` (immediate, unblocks 2017–2023)
2. Run `load_990_index` to bulk-load all years (~2.6M rows, ~10 minutes)
3. Resolve batch IDs for 2017–2023 filings
4. Create S3 bucket and migrate download path
5. Run `process_990_auto --backfill` for initial processing
6. Enable nightly cron
7. Update dashboard

Steps 1–3 can ship independently. Steps 4–6 are the main body. Step 7 is polish.

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| IRS changes CSV format again | Low | Medium | Column count detection already handles 9 vs 10; header parsing would be more robust |
| IRS rate-limits bulk zip downloads | Medium | Medium | Download one zip at a time with 5s delay; cache in S3 so we only download once |
| HTTP Range not supported for batch manifest scan | Medium | Low | Fall back to full zip download; still one-time cost |
| 50-80 GB S3 storage cost | Low | Low | ~$1-2/month at S3 standard; zips can move to Glacier after extraction |
| Backfill takes too long | Low | Medium | Process in priority order (recent years first); can run over multiple nights |

## Traps to Avoid

1. **Don't filter by EIN at index time** — load all 990s, filter at query time. The full index is tiny for Postgres.
2. **Don't assume 10 columns** — 2017–2023 have 9. Check `len(row) >= 9`, not `< 10`.
3. **Don't download full zips for batch ID resolution** — use HTTP Range to read zip central directories first.
4. **Don't store extracted XMLs on EBS** — S3 only. EBS is for hot data (DB, code, logs).
5. **Zip member paths vary** — 2024 uses nested `{batch}/{oid}_public.xml`, 2025 uses flat `{oid}_public.xml`. Already fixed in commit c092ce8.
