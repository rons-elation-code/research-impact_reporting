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

1. **Reconciliation-based trigger** — `process_990_auto` runs a JOIN query: `filing_index.ein IN (SELECT ein FROM nonprofits_seed) AND status = 'indexed' AND xml_batch_id IS NOT NULL`. This catches both new orgs and new filings in one pass — no DB triggers or app-layer hooks needed.
2. **Nightly schedule** — Runs after index refresh. The reconciliation query naturally picks up: (a) new filings added by the index refresh, (b) filings for orgs added to the seed list since the last run.
3. **Backfill** — On first run with `--backfill`, processes all existing `nonprofits_seed` EINs that have indexed but unprocessed filings. Without `--backfill`, limits to filings with `first_indexed_at` within the last 7 days (recent index additions only).

### Goal 4: Unified Codebase — Manual Controls Reuse New Infrastructure

The dashboard 990 Index and 990 Parse forms stay available for ad-hoc use (e.g., one-off EINs not in the seed list). But they are **refactored to call the same new code** — the orchestrator invokes `load_990_index --ein X` and `process_990_auto --ein X` instead of the old `teos_index.py` / `teos_download.py` EBS-based paths. One codebase, two entry points (nightly cron and dashboard button). The old EBS cache code paths (`~/.lavandula/990-cache/`, per-request CSV download) are retired after the new infrastructure is proven.

## Non-Goals

- Parsing 990-EZ, 990-PF, or 990-T. Continue filtering to `RETURN_TYPE = '990'` only.
- Real-time streaming from IRS. The IRS updates indexes daily/weekly; nightly refresh is sufficient.
- Deleting the dashboard manual controls. They remain for ad-hoc use.
- Migrating existing parsed data. Already-parsed filings (`status = 'parsed'`) are untouched.
- SSE-KMS encryption. SSE-S3 (AES-256) is sufficient — this is public IRS data, not PII. KMS adds cost and complexity for no security benefit here.
- Pre-2017 filing data. IRS TEOS indexes return 404 for years before 2017. If older e-file data becomes available in the future, add those years to the loader.

## Filing Status Lifecycle

`filing_index.status` tracks each filing through a linear pipeline. The `status` column already exists (Spec 0026, migration 010).

```
indexed → downloaded → parsed
    ↘        ↘          ↘
    error    error      error

indexed → batch_unresolvable  (terminal: no batch zip found for 2017–2023 filing)
```

**States:**
- `indexed` — Filing appears in the IRS TEOS index. Metadata stored. No XML yet.
- `downloaded` — XML extracted from batch zip and uploaded to S3. Ready for parsing.
- `parsed` — XML parsed, people/compensation data written to `people` table.
- `error` — Processing failed at any stage. `error_message` column has details.
- `batch_unresolvable` — Filing from 2017–2023 where no batch zip contains this `object_id`. Terminal state — visible in dashboard as "known but unavailable."

**Transition rules:**
- `indexed → downloaded`: Batch zip downloaded (or cached in S3), XML member extracted and uploaded to `s3://lavandula-990-corpus/xml/{ein}/{object_id}.xml`.
- `downloaded → parsed`: XML parsed successfully, people rows inserted.
- `Any → error`: Failure at any stage. `error_message` records the cause (structured error code, not raw exception string, to avoid leaking environment details).
- `error → indexed`: Manual reset (via Django admin or management command) to retry from scratch.
- `indexed → batch_unresolvable`: Batch resolution scanned all batch zips for the filing year and this `object_id` was not found in any.

**Idempotency:** Each transition checks current status before acting. A filing already at `parsed` is skipped. A filing at `error` is skipped unless `--reparse` is passed. Re-running `process_990_auto` on an already-processed corpus inserts 0 rows.

**Parsed-data idempotency:** The `people` table uses `(ein, object_id, person_name, person_type)` as the natural key. Re-parsing the same filing uses `ON CONFLICT DO NOTHING` — no duplicate people rows.

**S3/DB consistency:** The download step uploads XML to S3 first, then updates `filing_index.status` to `downloaded` and sets `s3_xml_key`. If the DB update fails, the filing stays at `indexed`; the next run re-uploads (S3 PUT is idempotent) and retries the DB update. If a filing is at `downloaded` but the S3 object is missing (corruption/manual deletion), the parse step detects the missing object and transitions to `error` with message "S3 object missing".

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
- Inserts all `RETURN_TYPE = '990'` rows into `filing_index` via `ON CONFLICT (object_id) DO UPDATE SET last_seen_at = now()` — `first_indexed_at` is immutable (set on insert only), `last_seen_at` refreshes on every re-run. The incremental window in Phase 5 uses `first_indexed_at` so re-runs don't re-trigger processing of old filings.
- For 9-column years, `xml_batch_id` = NULL
- Reports: rows scanned, matched (990), inserted, skipped per year
- Idempotent — safe to re-run (data columns unchanged, only `last_seen_at` refreshed)
- **Concurrency:** Acquires `pg_advisory_xact_lock` (same lock family as 990 jobs) to prevent concurrent loader runs. If a manual index job is running, the loader waits.

### Phase 3: Resolve Missing Batch IDs

For 2017–2023 filings where `xml_batch_id IS NULL`, we need to determine which batch zip contains each filing's XML.

Approach: **Batch manifest scan** — for each year, download each batch zip's file listing (not the full zip), build an `object_id → batch_id` mapping, and update `filing_index`.

```sql
UPDATE filing_index SET xml_batch_id = :batch_id
WHERE object_id = :object_id AND xml_batch_id IS NULL;
```

This is a one-time backfill. New filings (2024+) always have `xml_batch_id` in the index.

Implementation note: Python's `zipfile` module can read the central directory from a remote zip via HTTP Range requests without downloading the entire file. If the IRS server supports Range, we can resolve batch membership for ~2M filings by downloading only the central directories (~1-5 MB each vs 100 MB–2.5 GB for the full zip).

**Feasibility strategy:**
1. First, issue a HEAD + Range probe on a single 2022 batch zip.
2. If Range is supported: read central directories only (~50 MB total across all old batches). **Zip64 handling required** — the 2025 batches (2.5 GB) almost certainly use Zip64 format, which has a different End-of-Central-Directory (EOCD64) structure. The implementation must: (a) fetch the last 64 KB to find the EOCD, (b) detect the Zip64 EOCD locator if present, (c) fetch the Zip64 EOCD to get the true central directory offset. Python's `zipfile` module handles Zip64 natively when reading from a file-like object.
3. If Range is NOT supported: download full zips to S3 and read manifests locally. This costs 10-20 GB of one-time transfer for 2017–2023 (the zips we'd download eventually anyway for parsing). Since S3 caches them, this is not wasted work.
4. **Stop condition**: If any year has zero resolvable batch zips (404s for all batch patterns), log a warning and mark those year's filings as `batch_unresolvable`. They won't block the rest of the pipeline.

### Phase 4: S3 Bucket & Archive

New S3 bucket: `lavandula-990-corpus` (us-east-1, SSE-S3, versioned, private)

**Bucket security:**
- `BlockPublicAccess`: all four settings enabled (BlockPublicAcls, IgnorePublicAcls, BlockPublicPolicy, RestrictPublicBuckets)
- IAM: same `cloud2_lavandulagroup` role used by the existing `lavandula-nonprofit-collaterals` bucket. Scoped to `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:HeadObject` on this bucket only.
- Encryption: SSE-S3 (AES-256). Not SSE-KMS — this is public IRS data, KMS adds cost for no benefit.
- Lifecycle: `zips/` prefix transitions to Standard-IA after 30 days (cheaper than Standard, but no retrieval delay — Glacier would block new-org backfills for hours). `xml/` prefix stays Standard (active working set).
- Object keys contain EINs — these are public identifiers (IRS publishes them), not PII.

Prefix structure:
```
s3://lavandula-990-corpus/
  zips/{year}/{batch_id}.zip          # IRS batch zips (cached, Glacier-eligible)
  xml/{ein}/{object_id}.xml           # Extracted per-org XMLs (working set)
```

Download flow:
1. Check if `zips/{year}/{batch_id}.zip` exists in S3 (HEAD request)
2. If not, download from IRS → stream to S3 (multipart upload for large zips)
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
- **Backfill**: `manage.py process_990_auto --backfill` — process all unprocessed filings for tracked orgs (no time filter)
- **Incremental** (default): `manage.py process_990_auto` — process filings with `first_indexed_at` within the last 7 days AND `status = 'indexed'`. Uses `first_indexed_at` (immutable, set on initial insert) so nightly index refreshes don't re-trigger old filings. The 7-day window provides overlap to catch filings that were indexed but not yet batch-resolved during the previous run. Safe to run repeatedly — already-processed filings are skipped.
- **Single EIN**: `manage.py process_990_auto --ein 030440761` — process one org (useful for testing)

**Failure handling:**
- **Missing XML member in zip**: Mark filing as `error` with message "XML member not found in batch zip". Do not stop the batch — continue processing remaining filings.
- **Zip download interrupted**: Retry up to 3 times with exponential backoff. On final failure, log error and skip that batch. Filings remain `indexed` for the next run.
- **Malformed XML**: Mark filing as `error` with parse exception message. Continue processing.
- **S3 upload succeeds but DB update fails**: Filing stays at `indexed`. Next run re-extracts the XML (S3 PUT is idempotent) and retries the DB update.
- **DB update succeeds but parse fails**: Filing is at `downloaded`. Next run picks it up for parsing. Already-uploaded S3 XML is reused.

### Phase 6: Nightly Cron

A systemd timer (preferred over cron for logging and failure visibility) that runs:
```bash
#!/bin/bash
set -euo pipefail
cd /home/ubuntu/research/lavandula/dashboard

# 1. Refresh the current year's index
python3 manage.py load_990_index --current-year 2>&1 | logger -t 990-index

# 2. Process any new filings for tracked orgs
python3 manage.py process_990_auto 2>&1 | logger -t 990-auto
```

Schedule: nightly at 03:00 UTC. Logs to journald via `logger` (queryable with `journalctl -t 990-index`).

**Locking:** `process_990_auto` acquires the same `pg_advisory_xact_lock` used by the existing 990 job family. If a manual job is running, the auto worker waits (advisory lock is blocking). This prevents the auto worker and manual dashboard jobs from colliding — they share the same lock family, same status columns, same `filing_index` table. No deduplication needed beyond the status check (`WHERE status = 'indexed'`).

**"Current year" semantics:** `--current-year` means `datetime.date.today().year` at runtime. When the calendar year rolls over on Jan 1, the cron automatically starts fetching the new year's index. The previous year's index is already fully loaded from the bulk import and doesn't need refresh (IRS rarely adds to old years).

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

-- Track when filing was first indexed (immutable) and last seen in IRS index
ALTER TABLE lava_corpus.filing_index
  ADD COLUMN first_indexed_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN last_seen_at TIMESTAMPTZ DEFAULT now();

-- Add column for S3 XML location (set after extraction)
ALTER TABLE lava_corpus.filing_index
  ADD COLUMN s3_xml_key TEXT;

-- error_message column already exists (migration 010, Spec 0026)
-- Verify: SELECT column_name FROM information_schema.columns
--         WHERE table_name = 'filing_index' AND column_name = 'error_message';
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
- AC20: Filings from 2017–2023 have `xml_batch_id` resolved OR status set to `batch_unresolvable`
- AC21: HTTP Range requests used if supported by IRS server; full download fallback otherwise. Zip64 central directories handled for large (>2 GB) archives.
- AC22: Resolution is idempotent and only targets `xml_batch_id IS NULL` rows

### Goal 5: Dashboard
- AC23: 990 Index page shows last refresh time and total indexed filings
- AC24: Manual index/parse forms still work for ad-hoc use
- AC25: Org Detail filing list is pre-populated for tracked orgs

### Goal 6: Security
- AC26: CSV fields validated against allowlist patterns before use in URLs or S3 keys
- AC27: XML parser defends against XXE, billion-laughs, quadratic blowup, and depth attacks
- AC28: Zip extraction checks compression ratio (reject >100:1) and total extracted size (cap 10 GB)
- AC29: All IRS downloads use HTTPS with TLS verification; hostname restricted to `apps.irs.gov`
- AC30: `--ein` flag processes a single EIN (for testing and ad-hoc use)
- AC31: `--reparse` flag re-processes filings in `error` state
- AC32: `manage.py reset_990_status` command resets `error` → `indexed` for specified EINs or object_ids

## Testing Strategy

### Unit Tests
- **9-column vs 10-column CSV parsing**: Synthetic CSV fixtures with 9 and 10 columns. Verify both produce correct `filing_index` rows (9-col has `xml_batch_id=NULL`).
- **Idempotent re-run**: Insert rows, re-run loader, assert 0 new inserts.
- **Batch ID resolution logic**: Mock zip central directory responses, verify `object_id → batch_id` mapping.
- **Status transitions**: Verify each transition (`indexed → downloaded → parsed`, `any → error`, `error → indexed` reset).
- **Incremental selection**: Verify 7-day window filter, verify `--backfill` ignores the window.

### Integration Tests
- **S3 round-trip**: Upload XML to localstack/moto S3, parse from S3, verify output matches local parse.
- **Advisory lock**: Start two `process_990_auto` processes concurrently, verify only one runs at a time.
- **Failure recovery**: Simulate interrupted zip download, verify filing stays at `indexed` and is picked up on retry.
- **Manual + auto interaction**: Queue a manual parse job, run auto worker concurrently, verify advisory lock prevents collision.

### Security Tests
- **XXE prevention**: Feed an XML with external entity declarations, verify parser rejects it.
- **Billion-laughs defense**: Feed an XML with exponentially expanding entities, verify parser rejects or times out.
- **Oversized XML member**: Zip with a 60 MB member, verify extraction is blocked by `_MAX_MEMBER_SIZE`.
- **Zip bomb**: Zip with 1000:1 compression ratio, verify rejected before full decompression.
- **Malicious zip member name**: Zip with path traversal (`../../etc/passwd`), verify `_MEMBER_NAME_RE` rejects it.
- **CSV field injection**: CSV row with EIN containing path-traversal characters (`../`), verify row rejected by validation.
- **TLS verification**: Mock a non-IRS redirect, verify download aborted.

### Edge Case Tests
- **Parsed-data idempotency**: Parse the same filing twice, verify no duplicate `people` rows.
- **Missing S3 object at parse time**: Filing at `downloaded` but S3 object deleted — verify transition to `error`.
- **Batch resolution with absent member**: Batch zip exists but `object_id` not in any zip — verify `batch_unresolvable` status.
- **Manual + auto collision**: Queue a manual parse job while auto worker is running — verify advisory lock serializes them.
- **IRS metadata correction**: Re-run index loader after IRS updates a filing's metadata — verify `indexed_at` refreshed, data columns unchanged.

### Live Validation
- **Bulk load smoke test**: Run `load_990_index --years 2024` on a single year, verify row count matches expected ~363K 990 filings.
- **End-to-end for one EIN**: Run full pipeline for a known EIN (e.g., 131624241 UNCF), verify filings from 2017–2025 are indexed, downloaded, and parsed.
- **Nightly cron dry run**: Run the cron script manually, verify `index_refresh_log` is populated and new filings are processed.

## Migration Path

1. Fix 9-column bug in existing `teos_index.py` (immediate, unblocks 2017–2023)
2. Run `load_990_index` to bulk-load all years (~2.6M rows, ~10 minutes)
3. Resolve batch IDs for 2017–2023 filings
4. Create S3 bucket and migrate download path
5. Run `process_990_auto --backfill` for initial processing
6. Enable nightly cron
7. Update dashboard

Steps 1–3 can ship independently. Steps 4–6 are the main body. Step 7 is polish.

**Backfill time estimate:** ~100–200K filings across ~120 batch zips. Each batch zip download takes 1–5 minutes (100 MB–2.5 GB). XML extraction + parse is ~10ms per filing. Bottleneck is zip downloads. With 5-second delay between downloads: ~120 zips × 3 min avg = ~6 hours. Can run overnight; if split across 2 nights, process recent years (2024–2026) first for immediate value.

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| IRS changes CSV format again | Low | Medium | Column count detection already handles 9 vs 10; header parsing would be more robust |
| IRS rate-limits bulk zip downloads | Medium | Medium | Download one zip at a time with 5s delay; cache in S3 so we only download once |
| HTTP Range not supported for batch manifest scan | Medium | Low | Fall back to full zip download; still one-time cost |
| 50-80 GB S3 storage cost | Low | Low | ~$1-2/month at Standard-IA; PUT/GET for 200K objects ~$1 one-time |
| Backfill takes too long | Low | Medium | Process in priority order (recent years first); can run over multiple nights |

## Security Requirements

1. **Safe XML parsing**: Use `defusedxml` or `lxml` with `resolve_entities=False`, `no_network=True`. Defend against XXE, billion-laughs, quadratic blowup, and excessive depth. Set parse timeout of 30 seconds per filing. No DTD loading.
2. **Zip extraction by exact name only**: Only extract members matching `{object_id}_public.xml` pattern. Never extract arbitrary members or trust archive paths. Validate member name against `_MEMBER_NAME_RE` before extraction (already implemented in `teos_download.py`).
3. **Zip-bomb protection**: Check `compress_size` vs `file_size` ratio (reject if ratio > 100:1). Cap total extracted size per batch at 10 GB. Cap member count at 200K per zip. These bounds are well above normal IRS batches (~75K members, ~2.5 GB compressed).
4. **Size limits**: Max 50 MB per extracted XML member (already enforced by `_MAX_MEMBER_SIZE`). Max 5 GB per batch zip download (2025 batches are already 2.5 GB; allows headroom). Max 200 MB per index CSV download. Reject oversized responses via streaming with byte counter, not Content-Length alone.
5. **CSV field validation**: Before interpolating CSV-derived values into URLs or S3 keys, validate: `EIN` matches `^\d{9}$`, `OBJECT_ID` matches `^\d+$`, `XML_BATCH_ID` matches `^\d{4}_TEOS_XML_\w+$`. Reject rows with non-conforming values. This prevents SSRF via crafted URLs and S3 key traversal via crafted batch IDs.
6. **TLS and hostname allowlist**: All IRS downloads use HTTPS with TLS verification enabled (default `requests` behavior — never set `verify=False`). Hostname allowlist: `apps.irs.gov` only. Reject redirects to other hosts.
7. **S3 integrity**: After uploading a zip to S3, store the `ETag` (MD5) returned by PutObject. Before reading from the cached zip, compare the stored ETag with a HeadObject call. Mismatch → re-download from IRS.
8. **S3 bucket**: All four `BlockPublicAccess` settings enabled. ACLs disabled (bucket-owner-enforced). IAM scoped to this bucket only.
9. **Error messages**: Use structured error codes in `error_message` (e.g., `ZIP_MEMBER_MISSING`, `XML_PARSE_FAILED`, `S3_UPLOAD_FAILED`), not raw exception strings that could leak environment details.
10. **Dashboard auto-escape**: All CSV-sourced fields (`taxpayer_name`, `ein`, etc.) rendered in templates via Django's default auto-escape. No `|safe` filter on IRS-derived data. Already enforced by Spec 0027 convention.
11. **Authorization**: The `--reparse` flag and `error → indexed` status reset are operator-only actions. Dashboard exposes them only to authenticated (`LoginRequiredMixin`) users. No public API for status manipulation.

## Traps to Avoid

1. **Don't filter by EIN at index time** — load all 990s, filter at query time. The full index is tiny for Postgres.
2. **Don't assume 10 columns** — 2017–2023 have 9. Check `len(row) >= 9`, not `< 10`.
3. **Don't download full zips for batch ID resolution** — use HTTP Range to read zip central directories first.
4. **Don't store extracted XMLs on EBS** — S3 only. EBS is for hot data (DB, code, logs).
5. **Zip member paths vary** — 2024 uses nested `{batch}/{oid}_public.xml`, 2025 uses flat `{oid}_public.xml`. Already fixed in commit c092ce8.
6. **Don't trust zip member paths blindly** — validate against expected pattern before extraction. Never extract to filesystem; read into memory only.
