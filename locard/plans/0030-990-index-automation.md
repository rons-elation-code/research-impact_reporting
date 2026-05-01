# Plan 0030: 990 Filing Index Automation & S3 Archive

**Spec**: `locard/specs/0030-990-index-automation.md`
**Date**: 2026-05-01

## Implementation Phases

This plan has 8 phases. Phases 1–3 ship independently (index fixes + bulk loader). Phases 4–6 are the main body (S3 + auto-process). Phases 7–8 are integration + cleanup.

---

### Phase 1: Schema Migration + 9-Column Bug Fix

**Goal:** Unblock 2017–2023 index loading. Add new columns to `filing_index`.

**Files to modify:**
- `lavandula/nonprofits/teos_index.py` — accept 9-column rows
- New: `lavandula/migrations/rds/migration_011_990_index_automation.sql`
- `lavandula/dashboard/pipeline/models.py` — add new fields to unmanaged `FilingIndex` model

**Migration SQL:**
```sql
-- Allow NULL xml_batch_id for 2017-2023 filings
ALTER TABLE lava_corpus.filing_index
  ALTER COLUMN xml_batch_id DROP NOT NULL;

-- New columns for automation
ALTER TABLE lava_corpus.filing_index
  ADD COLUMN IF NOT EXISTS first_indexed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS s3_xml_key TEXT,
  ADD COLUMN IF NOT EXISTS zip_checksum TEXT;

-- Backfill: set first_indexed_at = NULL for existing rows so the first
-- bulk load will set it properly. Don't default to now() on migration —
-- that would make all existing rows look "just indexed" and the incremental
-- window would process them all on the first nightly run.
-- The bulk loader's INSERT...ON CONFLICT will set first_indexed_at = now()
-- for genuinely new rows.
--
-- NOTE: The spec shows DEFAULT now() for these columns. This plan intentionally
-- deviates: columns are added WITHOUT defaults so existing rows get NULL.
-- The loader explicitly sets first_indexed_at = now() on INSERT, and
-- last_seen_at = now() on both INSERT and UPDATE. This is safer for
-- the incremental processing window. Update spec to match if approved.

-- If filing_index.status has a CHECK constraint, extend it:
-- (Check with: SELECT conname, consrc FROM pg_constraint
--  WHERE conrelid = 'lava_corpus.filing_index'::regclass AND contype = 'c')
-- If a CHECK exists, run:
-- ALTER TABLE lava_corpus.filing_index DROP CONSTRAINT <name>;
-- ALTER TABLE lava_corpus.filing_index ADD CONSTRAINT filing_index_status_check
--   CHECK (status IN ('indexed','downloaded','parsed','error','batch_unresolvable'));

CREATE TABLE IF NOT EXISTS lava_corpus.index_refresh_log (
    id              SERIAL PRIMARY KEY,
    filing_year     INTEGER NOT NULL,
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    rows_scanned    INTEGER NOT NULL DEFAULT 0,
    rows_inserted   INTEGER NOT NULL DEFAULT 0,
    rows_skipped    INTEGER NOT NULL DEFAULT 0,
    duration_sec    NUMERIC(8,2)
);
```

**teos_index.py changes:**
- Line 119: Change `if len(row) < 10` to `if len(row) < 9`
- Handle `xml_batch_id`: `row[_COL_XML_BATCH_ID].strip() if len(row) > 9 else None`
- Add CSV field validation before insert: EIN matches `^\d{9}$`, OBJECT_ID matches `^\d+$`

**Django model changes:**
- Add `first_indexed_at`, `last_seen_at`, `s3_xml_key` to `FilingIndex` model (unmanaged)
- Add `batch_unresolvable` to status choices if applicable

**ACs covered:** AC2, AC3, AC7

**Tests:**
- Synthetic 9-column CSV → verify insert with `xml_batch_id=NULL`
- Synthetic 10-column CSV → verify insert with `xml_batch_id` populated
- CSV row with invalid EIN (letters) → verify row rejected

---

### Phase 2: Bulk Index Loader Management Command

**Goal:** `manage.py load_990_index` that downloads IRS index CSVs and bulk-inserts into `filing_index`.

**Files to create:**
- `lavandula/dashboard/pipeline/management/commands/load_990_index.py`

**Implementation:**
- Arguments: `--years` (comma-separated), `--current-year` (flag), `--ein` (single EIN for ad-hoc — still downloads and streams the full CSV, but only inserts/updates rows matching this EIN; this preserves the bulk-load architecture while allowing targeted refreshes from the dashboard)
- Default years: 2017 through current year (detect available years by probing HEAD on index URL)
- For each year:
  1. Download CSV via `requests.get()` with streaming, byte counter capped at 200 MB
  2. Verify hostname is `apps.irs.gov` (reject redirects to other hosts)
  3. Parse with `csv.reader`, detect column count from header row
  4. For each `RETURN_TYPE == '990'` row, validate fields (EIN, OBJECT_ID, XML_BATCH_ID patterns)
  5. Bulk insert via `ON CONFLICT (object_id) DO UPDATE SET last_seen_at = now()`
  6. Record stats in `index_refresh_log`
- Concurrency: acquire session-level `pg_advisory_lock(hashtext('990-family'))` at command start, release at command end. Session-level (not transaction-scoped) because the command uses multiple transactions for batch inserts — a transaction-scoped lock would release between batches and fail to serialize against concurrent runs.

**Key design decisions:**
- Use SQLAlchemy engine (same as `teos_index.py`) for bulk inserts, not Django ORM — batch performance matters for 2.6M rows
- Stream CSV parsing — don't buffer entire 90 MB file in memory
- The existing `download_and_filter_index()` in `teos_index.py` is NOT reused — it filters by EIN set, which we don't want for bulk load. New code path.

**ACs covered:** AC1, AC4, AC5, AC6

**Tests:**
- Mock IRS CSV response with 9 and 10 columns → verify correct inserts
- Run twice → verify 0 new inserts on second run (idempotent)
- `--current-year` → verify only fetches one year
- `index_refresh_log` populated after run
- `--ein` with specific EIN → verify only that EIN's rows inserted (full CSV still streamed)
- Session-level advisory lock: start two concurrent `load_990_index` runs → verify second waits for first to complete

---

### Phase 3: Batch ID Resolution

**Goal:** Resolve `xml_batch_id` for 2017–2023 filings where it's NULL.

**Files to create:**
- `lavandula/dashboard/pipeline/management/commands/resolve_990_batches.py`

**Implementation:**
1. Probe IRS server for HTTP Range support: `HEAD` + `Range: bytes=0-0` on a known 2022 zip
2. If Range supported:
   - For each year 2017–2023, enumerate batch zips (`{year}_TEOS_XML_{01A..12A}`, plus B/C/D sub-batches)
   - For each batch zip, fetch the zip central directory via Range requests:
     a. GET last 64 KB to find EOCD (handle Zip64 EOCD locator if present)
     b. Parse central directory to get member filename list
     c. Extract `object_id` from each `{oid}_public.xml` member name
   - Bulk UPDATE `filing_index SET xml_batch_id = :batch_id WHERE object_id IN (:oids) AND xml_batch_id IS NULL`
3. If Range NOT supported:
   - Download full zips to a local temp directory (`/tmp/990-batch-resolve/`), read central directories, then delete the temp files. This avoids a dependency on Phase 4 (S3) and preserves Phase 3's ability to ship independently of Phase 4. The zips will be re-downloaded to S3 later in Phase 5 — this is acceptable since it's a one-time operation.
4. After scanning all batches for a year, mark remaining NULL `xml_batch_id` rows as `batch_unresolvable`

**Batch zip enumeration:**
- Pattern: `{year}_TEOS_XML_{MM}{suffix}` where MM = 01–12, suffix = A, B, C, D
- Probe each with HEAD request; 200 = exists, 302 = IRS 404 redirect
- Already confirmed: 2022 has 01A–02A only, 2023 has 01A–12A, 2024+ has 01A–12A plus sub-batches

**ACs covered:** AC20, AC21, AC22

**Tests:**
- Mock Range-supported server → verify central directory read + batch mapping
- Mock Range-unsupported server → verify fallback to full download
- Object ID in no batch → verify `batch_unresolvable` status

---

### Phase 4: S3 Bucket + Archive Module

**Goal:** New S3 bucket and a module for uploading/downloading 990 zips and XMLs.

**Infrastructure (manual operator step):**
- Create bucket `lavandula-990-corpus` via AWS console or CLI
- Settings: us-east-1, SSE-S3, versioning enabled, all four BlockPublicAccess enabled, ACLs disabled
- Lifecycle rule: `zips/` prefix → Standard-IA after 30 days
- IAM: add `s3:GetObject`, `s3:PutObject`, `s3:ListBucket`, `s3:HeadObject` for `arn:aws:s3:::lavandula-990-corpus*` to `cloud2_lavandulagroup` role policy

**Files to create:**
- `lavandula/nonprofits/s3_990.py` — S3 client for 990 corpus bucket

**Module API:**
```python
class S3990Archive:
    def __init__(self, bucket: str = "lavandula-990-corpus"):
        ...

    def zip_exists(self, year: int, batch_id: str) -> bool:
        """HEAD check for zips/{year}/{batch_id}.zip"""

    def upload_zip(self, year: int, batch_id: str, stream: IO[bytes]) -> str:
        """Multipart upload IRS zip to S3 with ChecksumAlgorithm='SHA256'. Returns ChecksumSHA256 for integrity storage."""

    def verify_zip_integrity(self, year: int, batch_id: str, expected_checksum: str) -> bool:
        """HEAD request, compare ChecksumSHA256. Mismatch → return False (caller re-downloads)."""

    def open_zip(self, year: int, batch_id: str) -> IO[bytes]:
        """Stream zip from S3 for extraction. For large zips (>500 MB), download to
        a temp file first rather than holding in memory. Use tempfile.SpooledTemporaryFile
        with max_size=500MB — small zips stay in memory, large ones spill to /tmp."""

    def upload_xml(self, ein: str, object_id: str, data: bytes) -> str:
        """PUT xml/{ein}/{object_id}.xml. Returns s3_xml_key."""

    def read_xml(self, s3_key: str) -> bytes:
        """GET XML from S3."""

    def xml_exists(self, ein: str, object_id: str) -> bool:
        """HEAD check for xml/{ein}/{object_id}.xml"""
```

**Integrity storage:**
- Multipart-upload ETags are NOT plain MD5 — they include a part suffix (e.g., `"abc123-5"`). Don't treat them as content checksums. Instead, use **S3 `ChecksumSHA256`**: pass `ChecksumAlgorithm='SHA256'` on `put_object`/`create_multipart_upload`, and S3 computes + stores a SHA-256 checksum. Store this in `filing_index.zip_checksum TEXT` (rename from `zip_checksum`).
- Before reusing a cached zip, call `verify_zip_integrity()` which does a HEAD request and compares the stored `ChecksumSHA256` value. On mismatch, re-download from IRS.
- Flow: `upload_zip()` returns `ChecksumSHA256` → store in DB → `verify_zip_integrity()` reads DB + HEAD → compare

**Large zip handling:**
- Zips up to 500 MB: hold in `BytesIO` (memory)
- Zips > 500 MB (e.g., 2.5 GB 2025 batches): `SpooledTemporaryFile` spills to `/tmp` on EBS. This is transient — deleted after batch processing. The 2.5 GB temp file is acceptable because it's short-lived and EBS has capacity for one zip at a time.

**Security:**
- All S3 keys validated: EIN `^\d{9}$`, object_id `^\d+$`, batch_id `^\d{4}_TEOS_XML_\w+$`
- boto3 client with retry config (same pattern as `s3_archive.py`)
- Multipart upload for zips > 100 MB

**ACs covered:** AC8, AC9, AC10, AC11, AC13

**Tests:**
- Unit tests with moto mock S3
- Upload zip → verify `zips/` key, get ETag
- Upload XML → verify `xml/{ein}/{oid}.xml` key
- Read back uploaded XML → verify round-trip

---

### Phase 5: Auto-Process Worker

**Goal:** `manage.py process_990_auto` — download, extract, parse filings for tracked orgs.

**Files to create:**
- `lavandula/dashboard/pipeline/management/commands/process_990_auto.py`

**Files to modify:**
- `lavandula/nonprofits/teos_download.py` — refactor `_process_single_filing()` to accept XML bytes from S3 instead of local zip. Extract the parsing logic into a reusable function.

**Implementation:**
1. Acquire session-level advisory lock (`pg_advisory_lock(hashtext('990-family'))`) — session-level for the same reason as Phase 2: the command spans multiple transactions.
2. Query filings to process (two passes):
   - **Pass 1 — Download**: `status = 'indexed' AND xml_batch_id IS NOT NULL` (need zip download + extraction)
   - **Pass 2 — Parse**: `status = 'downloaded'` (already extracted to S3, need parsing — catches prior failures)
   - Both passes filter: `ein IN (SELECT ein FROM nonprofits_seed)`
   - `--backfill`: no time filter
   - Default (incremental): `AND first_indexed_at >= now() - interval '7 days'` (Pass 1 only; Pass 2 always runs without time filter to recover stalled `downloaded` rows)
   - `--ein X`: additional `AND ein = :ein`
3. Group results by `(filing_year, xml_batch_id)`
4. For each batch group:
   a. Check S3 for cached zip → if missing, download from IRS to S3 (validate hostname, TLS, size cap 5 GB, ETag integrity)
   b. Open zip from S3 (via `SpooledTemporaryFile` for large zips). Pre-validate:
      - Check member count (`len(zf.namelist()) <= 200_000`)
      - Track cumulative extracted bytes (cap at 10 GB per batch)
      Iterate over target filings in the group:
      - Validate member name against `_MEMBER_NAME_RE`
      - Check compression ratio: `info.file_size / max(info.compress_size, 1) <= 100`
      - Check file size: `info.file_size <= _MAX_MEMBER_SIZE` (50 MB)
      - Extract XML bytes into memory
      - Upload to `s3://lavandula-990-corpus/xml/{ein}/{object_id}.xml`
      - Update `filing_index.status = 'downloaded'`, set `s3_xml_key`
   c. Parse each downloaded filing:
      - Read XML from S3 (or use in-memory bytes from step b if still available)
      - Parse with `defusedxml.ElementTree.fromstring()` which defends against XXE, entity expansion (billion-laughs), and quadratic blowup out of the box. For the 30-second timeout: isolate XML parsing in a `concurrent.futures.ProcessPoolExecutor` worker with `timeout=30`. This avoids `signal.alarm` pitfalls (main-thread-only, Unix-specific, interferes with surrounding code). If the subprocess times out, `TimeoutError` is caught, the worker is killed, and the filing is marked `error` with `XML_PARSE_TIMEOUT`.
      - Extract people/compensation → insert into `people` table with `ON CONFLICT DO NOTHING`
      - Update `filing_index.status = 'parsed'`
   d. On error: mark filing as `error` with structured error code, continue to next filing
5. Log summary: filings processed, parsed, errored, skipped

**Retry logic:**
- Zip download: 3 retries, exponential backoff (2s, 4s, 8s base)
- S3 operations: boto3 built-in retry (3 retries)
- Individual filing errors don't stop the batch

**Reparse flag (`--reparse`):**
- Additionally selects filings with `status = 'error'`
- Resets to `indexed` before processing

**ACs covered:** AC14, AC15, AC16, AC17, AC18, AC26, AC27, AC28, AC29, AC30, AC31

**Tests:**
- Mock S3 + mock IRS zip → full pipeline: indexed → downloaded → parsed
- `--backfill` vs incremental: verify different query scopes
- `--ein`: verify single-EIN processing
- `--reparse`: verify error filings re-processed
- Zip bomb rejection (high ratio)
- XXE rejection
- Missing member → error status, batch continues

---

### Phase 6: Status Reset Command + Nightly Cron

**Goal:** Management command for operator status resets. Systemd timer for nightly automation.

**Files to create:**
- `lavandula/dashboard/pipeline/management/commands/reset_990_status.py`
- `lavandula/systemd/990-nightly.service`
- `lavandula/systemd/990-nightly.timer`

**reset_990_status:**
```
python3 manage.py reset_990_status --ein 030440761          # all filings for EIN
python3 manage.py reset_990_status --object-id 20242319...  # specific filing
python3 manage.py reset_990_status --status error           # all errored filings
```
- **Allowed source states:** `error`, `downloaded`, `parsed`, `batch_unresolvable`. Refuses to reset `indexed` filings (already at target state).
- Resets `status` to `indexed`, clears `error_message`, clears `s3_xml_key`
- **S3 cleanup:** Does NOT delete cached XML from S3. The XML may be valid; the re-process step will re-upload (S3 PUT is idempotent) or reuse the existing object. Deleting would force unnecessary re-extraction from batch zips.
- Requires confirmation prompt showing affected filing count unless `--yes` flag passed

**Systemd timer:**
```ini
# 990-nightly.timer
[Unit]
Description=Nightly 990 index refresh and auto-process

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# 990-nightly.service
[Unit]
Description=990 index refresh and auto-process

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/research/lavandula/dashboard
# Use a shell wrapper to run both commands independently.
# systemd Type=oneshot with multiple ExecStart= lines actually STOPS on first
# failure, which is not what we want. Use a shell script instead.
ExecStart=/bin/bash -c '/usr/bin/python3 manage.py load_990_index --current-year 2>&1 | logger -t 990-index; /usr/bin/python3 manage.py process_990_auto 2>&1 | logger -t 990-auto'
StandardOutput=journal
StandardError=journal
SyslogIdentifier=990-nightly
```

**ACs covered:** AC19, AC32

**Tests:**
- `reset_990_status --ein X` → verify status reset to `indexed`
- `reset_990_status --object-id X` → verify single filing reset
- `reset_990_status` on `indexed` filing → verify refusal (already at target state)
- Timer unit file validates with `systemd-analyze verify`
- Systemd failure semantics: simulate `load_990_index` failure → verify `process_990_auto` still runs (shell `;` separator, not `&&`)

---

### Phase 7: Orchestrator + Dashboard Integration

**Goal:** Wire the new commands into the dashboard. Update orchestrator COMMAND_MAP.

**Files to modify:**
- `lavandula/dashboard/pipeline/orchestrator.py` — update `990-index` and `990-parse` COMMAND_MAP entries to use new commands
- `lavandula/dashboard/pipeline/views.py` — update `EnrichIndexView`, `EnrichParseView`
- `lavandula/dashboard/pipeline/templates/pipeline/990_index.html` — add index status banner
- `lavandula/dashboard/pipeline/templates/pipeline/990_parse.html` — similar
- `lavandula/dashboard/pipeline/templates/pipeline/org_detail.html` — filing list is pre-populated
- `lavandula/dashboard/pipeline/models.py` — add `IndexRefreshLog` unmanaged model

**Orchestrator changes:**
```python
# Replace old COMMAND_MAP entries:
"990-index": {
    "cmd": ["python3", "manage.py", "load_990_index"],
    "params": {
        "state": {"type": "choice", "choices": US_STATES, "flag": "--state"},
        "ein": {"type": "text", "pattern": r"^\d{9}$", "flag": "--ein"},
        "years": {"type": "text", "pattern": r"^\d{4}(\s*,\s*\d{4})*$", "flag": "--years"},
    },
},
"990-parse": {
    "cmd": ["python3", "manage.py", "process_990_auto"],
    "params": {
        "ein": {"type": "text", "pattern": r"^\d{9}$", "flag": "--ein"},
        "reparse": {"type": "bool", "flag": "--reparse"},
        "backfill": {"type": "bool", "flag": "--backfill"},
    },
},
```

**Dashboard 990 Index page:**
- Query `IndexRefreshLog` for latest refresh per year
- Show banner: "Last refreshed: {date} — {total} filings indexed"
- Filing count breakdown by status

**Index Maintenance admin view** (new URL: `/dashboard/990/maintenance/`, `LoginRequiredMixin`):
- Per-year table: year, last refresh date, total filings, filings by status (indexed/downloaded/parsed/error/batch_unresolvable)
- Unresolved batch IDs count (NULL `xml_batch_id` rows)
- Button to trigger manual refresh for a specific year (authenticated operator-only action, consistent with Spec Security Requirement 11)

**ACs covered:** AC23, AC24, AC25

**Tests:**
- Orchestrator builds correct argv for new commands
- Dashboard views render with index status data
- Manual form submission still creates jobs

---

### Phase 8: Retire Old Code Paths + Final Validation

**Goal:** Remove EBS-based code paths once new infrastructure is proven.

**Files to modify:**
- `lavandula/nonprofits/teos_download.py` — remove local cache logic, use S3 exclusively
- `lavandula/nonprofits/tools/enrich_990.py` — update to delegate to new management commands
- `lavandula/dashboard/pipeline/forms.py` — simplify 990 forms (years no longer required for tracked orgs)

**Retirement steps:**
1. Verify all existing `parsed` filings have `s3_xml_key` set (run backfill for any gaps)
2. Remove `--cache-dir` argument from `enrich_990.py`
3. Remove `_log_cache_size()` function
4. Update `process_filings()` to read from S3 instead of local zip
5. Remove local zip download logic (or keep as fallback behind flag)

**Live validation checklist:**
- [ ] `load_990_index` bulk load for 2024 → ~363K 990 filings inserted
- [ ] `resolve_990_batches` for 2022 → batch IDs resolved for both batch zips
- [ ] `process_990_auto --ein 131624241` → UNCF filings across multiple years parsed
- [ ] Nightly cron dry run → `index_refresh_log` populated
- [ ] Dashboard org detail for UNCF → filing list pre-populated, people data visible
- [ ] Manual parse from dashboard → still works via new code path

**ACs covered:** AC12, AC13 (final verification)

---

## Phase Dependencies

```
Phase 1 (schema + bug fix)
    ↓
Phase 2 (bulk loader) ←→ Phase 3 (batch resolution) [can parallelize]
    ↓
Phase 4 (S3 module)
    ↓
Phase 5 (auto-process worker)
    ↓
Phase 6 (reset command + cron) ←→ Phase 7 (dashboard) [can parallelize]
    ↓
Phase 8 (retire old code)
```

## File Summary

**New files (7):**
- `lavandula/migrations/rds/migration_011_990_index_automation.sql`
- `lavandula/nonprofits/s3_990.py`
- `lavandula/dashboard/pipeline/management/commands/load_990_index.py`
- `lavandula/dashboard/pipeline/management/commands/resolve_990_batches.py`
- `lavandula/dashboard/pipeline/management/commands/process_990_auto.py`
- `lavandula/dashboard/pipeline/management/commands/reset_990_status.py`
- `lavandula/systemd/990-nightly.timer` + `990-nightly.service`

**Modified files (8):**
- `lavandula/nonprofits/teos_index.py` — 9-column support + field validation
- `lavandula/nonprofits/teos_download.py` — refactor parse logic for S3 reuse, retire local cache
- `lavandula/nonprofits/tools/enrich_990.py` — delegate to new commands
- `lavandula/dashboard/pipeline/models.py` — new fields on FilingIndex, IndexRefreshLog model
- `lavandula/dashboard/pipeline/orchestrator.py` — update COMMAND_MAP entries
- `lavandula/dashboard/pipeline/views.py` — index status banner, pre-populated filings
- `lavandula/dashboard/pipeline/templates/pipeline/990_index.html` — status banner
- `lavandula/dashboard/pipeline/forms.py` — simplify forms

## Operator Steps (Manual)

These steps require the operator (not the builder):

1. **Before Phase 1**: Run migration 011 via PGAdmin
2. **Before Phase 4**: Create S3 bucket `lavandula-990-corpus` + IAM policy update
3. **After Phase 5**: Run initial bulk load + backfill:
   ```bash
   python3 manage.py load_990_index
   python3 manage.py resolve_990_batches
   python3 manage.py process_990_auto --backfill
   ```
4. **After Phase 6**: Install and enable systemd timer:
   ```bash
   sudo cp lavandula/systemd/990-nightly.* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now 990-nightly.timer
   ```
