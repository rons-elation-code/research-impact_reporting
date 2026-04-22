# Plan 0007 — S3-Backed PDF Archive

**Spec**: `locard/specs/0007-s3-pdf-archive.md`  
**Protocol**: SPIDER  
**Date**: 2026-04-22

---

## Overview

Deliver S3-backed PDF archiving in a single PR. Eight code files
change; three are new. All unit tests run offline with `moto` +
`botocore.stub`. One optional live-S3 integration test is gated
behind `LAVANDULA_LIVE_S3=1`.

Architecture is a thin abstraction over two archive backends
(`LocalArchive`, `S3Archive`) sharing a small interface. The crawler
instantiates one backend at startup based on the `--archive` argument
and threads it through `process_org` → `fetch_pdf.download`.

---

## Existing code to read first

Read these files top-to-bottom before writing any code:

1. `lavandula/reports/fetch_pdf.py` — current local-file archive write
   happens inside `download()` around the hash + write step. That's
   the single call site we're replacing.
2. `lavandula/reports/crawler.py` — look at `run()` arg parsing,
   `process_org()` signature (accepts `archive_dir: Path`), and the
   per-worker HTTP client construction added by TICK-002.
3. `lavandula/reports/db_writer.py` — `record_fetch` and `upsert_report`
   signatures; we need to preserve them (nothing changes here).
4. `lavandula/reports/http_client.py` — `ReportsHTTPClient` already
   streams with a 50 MB cap (TICK-002 round 5). We consume its
   response body as `bytes`; don't re-engineer streaming inside
   `fetch_pdf`.
5. `lavandula/reports/config.py` — where `MAX_PDF_BYTES` and other
   constants live. Add S3 constants here.

---

## Step 1 — Dependencies

The reports package has two requirements files (verified 2026-04-22):

- `lavandula/reports/requirements.in` — runtime
- `lavandula/reports/requirements-dev.in` — dev/test

Add to `requirements.in`:
```
boto3>=1.34
```

Add to `requirements-dev.in`:
```
moto[s3]>=5.0
```

Regenerate both `requirements.txt` files:
```
cd lavandula/reports
pip-compile requirements.in
pip-compile requirements-dev.in
```

Do not hand-edit the `.txt` lockfiles. If `pip-compile` is not
installed in the dev venv, install it first: `pip install pip-tools`.

---

## Step 2a — New: `lavandula/reports/archive.py`

Defines the archive backend Protocol + the `LocalArchive`
implementation. Keeping the S3 class in a separate module
(`s3_archive.py`) keeps boto3 out of the import chain for
local-only workflows and matches the spec's naming.

```python
from typing import Protocol

class Archive(Protocol):
    """Archive backend interface. Two implementations ship in this spec."""
    scheme: str  # "local" or "s3"

    def put(self, sha256: str, body: bytes, metadata: dict) -> None:
        """Persist `body` keyed by sha256. metadata is best-effort."""
        ...

    def get(self, sha256: str) -> bytes:
        """Retrieve bytes. Raises FileNotFoundError-equivalent if absent."""
        ...

    def head(self, sha256: str) -> dict | None:
        """Return object metadata if present; None if absent."""
        ...

    def startup_probe(self) -> None:
        """Fail fast if backend is misconfigured. Idempotent."""
        ...
```

The `Archive` Protocol is duck-typed — no ABC, no registration. A test
can pass any object with the four methods.

`LocalArchive` goes here:
- `put()` writes `body` to `{archive_dir}/{sha256}.pdf`
- Metadata sidecar is NOT implemented in v1 (kept simple; local mode
  is for dev/test, metadata lives in reports.db anyway)
- `startup_probe()` checks the dir exists and is writable

## Step 2b — New: `lavandula/reports/s3_archive.py`

This is the filename named in the spec's "Files Changed" section.
AC12's grep assertion will look for imports of this module in
`classify_null.py`.

Module contents: the `S3Archive` class, `_encode_s3_metadata` helper,
`ArchiveSetupError` exception.

### `S3Archive` — construction and responsibilities

```python
class S3Archive:
    scheme = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "pdfs",
        *,
        region: str | None = None,
        endpoint_url: str | None = None,  # for moto/localstack
        client=None,  # for dependency injection in tests
    ):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._region = region
        self._client = client or self._make_client(region, endpoint_url)

    def _key(self, sha256: str) -> str:
        return f"{self.prefix}/{sha256}.pdf" if self.prefix else f"{sha256}.pdf"
```

`_make_client` uses `boto3.client("s3", region_name=region,
endpoint_url=endpoint_url)`. No access keys are read from env
explicitly; boto3's default credential chain handles it.

### `S3Archive.put()` — exact body

```python
def put(self, sha256: str, body: bytes, metadata: dict) -> None:
    encoded_metadata = _encode_s3_metadata(metadata)
    kwargs = {
        "Bucket": self.bucket,
        "Key": self._key(sha256),
        "Body": body,
        "ContentType": "application/pdf",
        "ServerSideEncryption": "AES256",  # AC2
        "Metadata": encoded_metadata,
    }
    # Do NOT set "ACL" (AC14)
    self._client.put_object(**kwargs)
```

### `_encode_s3_metadata` helper (critical for AC15)

```python
from urllib.parse import quote

_MAX_SOURCE_URL_LEN = 1024

_ASCII_SAFE = re.compile(r'^[\x21-\x7e]*$')  # printable ASCII, no controls

def _encode_s3_metadata(raw: dict) -> dict:
    """Encode metadata values to satisfy S3 ASCII rules and prevent
    header injection via CRLF.

    Fallback: if even after percent-encoding a value is rejected by
    S3's metadata validator (exotic edge case), drop the offending
    key and log a structured warning. Never block the PUT on
    metadata encoding failure.
    """
    out = {}
    for k, v in raw.items():
        if v is None:
            continue
        if k == "source-url":
            encoded = quote(str(v), safe='')
            encoded = encoded[:_MAX_SOURCE_URL_LEN]
            if not _ASCII_SAFE.match(encoded):
                log.warning(
                    "s3_metadata_encoding_failed key=%s",
                    k,  # do not log the value
                )
                continue
            out[k] = encoded
        else:
            # EIN, run-id, timestamp — already ASCII; pass through.
            str_v = str(v)
            if not _ASCII_SAFE.match(str_v):
                log.warning("s3_metadata_encoding_failed key=%s", k)
                continue
            out[k] = str_v
    return out
```

The ASCII-only regex is defense-in-depth — S3's metadata validation
already rejects non-printable bytes, but we prefer to drop bad values
silently-with-warning rather than fail the whole PUT.

### `S3Archive.startup_probe()`

Handles all the startup validation required by the spec, in this
order:

1. No region configured at all (neither `--s3-region`, AWS_REGION,
   config file, nor IMDS resolved) → fail with
   `no AWS region configured; pass --s3-region or set AWS_REGION`.
2. `head_bucket` against the bucket:
   - 404 / NoSuchBucket → `bucket X does not exist`
   - 403 / AccessDenied → `no permission to access bucket X`
   - Other ClientError → re-raise with full context
   - Connection error (IMDS down, network partition) → propagate
     as `ArchiveSetupError` with a clear cause
3. Compare `x-amz-bucket-region` response header to the client's
   configured region. Mismatch →
   `bucket X is in region Y; client configured for Z`.

```python
def startup_probe(self) -> None:
    configured_region = self._client.meta.region_name
    if not configured_region:
        raise ArchiveSetupError(
            "no AWS region configured; pass --s3-region or set AWS_REGION"
        )

    try:
        resp = self._client.head_bucket(Bucket=self.bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket"):
            raise ArchiveSetupError(
                f"bucket {self.bucket} does not exist"
            ) from exc
        if code in ("403", "AccessDenied"):
            raise ArchiveSetupError(
                f"no permission to access bucket {self.bucket}"
            ) from exc
        raise ArchiveSetupError(
            f"head_bucket failed: {code or exc}"
        ) from exc
    except (EndpointConnectionError, NoCredentialsError) as exc:
        raise ArchiveSetupError(
            f"could not connect to S3 or load credentials: {exc}"
        ) from exc

    actual_region = resp["ResponseMetadata"]["HTTPHeaders"].get(
        "x-amz-bucket-region", "unknown"
    )
    if actual_region != configured_region and actual_region != "unknown":
        raise ArchiveSetupError(
            f"bucket {self.bucket} is in region {actual_region}; "
            f"client configured for {configured_region}"
        )
```

---

## Step 3 — Update `lavandula/reports/fetch_pdf.py`

Current `download()` signature:
```python
def download(client, url, *, archive_dir, run_id, ein, ...) -> DownloadOutcome
```

New signature:
```python
def download(client, url, *, archive, run_id, ein, ...) -> DownloadOutcome
```

`archive` is an `Archive` (duck-typed). **Thread-safety**: boto3
documents that low-level clients (what we're using) are thread-safe
for most operations — safe to share one `S3Archive` instance across
worker threads. We do so to avoid per-thread TLS handshake overhead.
The `boto3-thread-safety` test below proves this holds under
concurrent PUT load. If a future boto3 release changes this
guarantee, the plan should switch to per-thread clients matching the
crawler's existing HTTP-client pattern.

Inside `download()`:

1. Stream the HTTP response into `bytes` (existing code, unchanged).
2. Run the existing PDF magic + structure validation subprocess.
3. Extract first-page text + metadata via pypdf (existing code).
4. Compute sha256 (existing).
5. Build metadata dict:
   ```python
   metadata = {
       "source-url": url,
       "ein": ein,
       "crawl-run-id": run_id,
       "fetched-at": _iso_utc_now(),
   }
   ```
6. Call `archive.put(sha256, body, metadata)`.
7. On success, return DownloadOutcome as before (includes
   first_page_text).
8. On `archive.put()` exception after boto3 retries, catch, log
   structured error with sha256 + URL + exception class, return
   `DownloadOutcome(outcome="error", ...)`. The caller
   (`process_org`) writes only `fetch_log` — not `reports` — per AC9.

**Do not add per-call boto3 retry logic.** boto3's default retry
(3 attempts, exponential) handles 5xx transients. Additional retry
here would amplify delays.

---

## Step 4 — Update `lavandula/reports/crawler.py`

### CLI argument changes

Current: `--archive-dir DIR`. Keep it as an alias.

Add: `--archive VALUE` — canonical.

**Production-prefix invariant**: spec says prefix must be `pdfs/` in
production. Enforcement: the crawler emits a WARNING log line when
the resolved S3 prefix is anything other than `pdfs`. Not a hard
block — tests and dev environments legitimately use alternate
prefixes. The log message reads:
`non-standard S3 prefix "{prefix}"; production convention is "pdfs"`.
Production runbooks (HANDOFF.md) state the convention explicitly.

Argparse setup:

```python
ap.add_argument(
    "--archive",
    help="Archive destination: s3://bucket/prefix or /absolute/path",
)
ap.add_argument(
    "--archive-dir",
    help="[legacy] alias for --archive; accepts only filesystem paths",
)
ap.add_argument("--s3-region", help="Override S3 region for the archive")
```

Post-parse validation function `_resolve_archive(args) -> Archive`:

1. If both `--archive` and `--archive-dir` set → `parser.error(
   "use --archive or --archive-dir, not both")`
2. If neither set → `parser.error("archive destination is required")`
3. If `--archive-dir` is set and value starts with `s3://` →
   `parser.error("--archive-dir accepts only a filesystem path; "
   "use --archive for S3")`
4. Resolved value → parse:
   - `s3://bucket/prefix` → `S3Archive(bucket, prefix, region=args.s3_region)`
   - absolute path → `LocalArchive(Path(value))`
   - anything else → `parser.error("archive value must be s3://... or an absolute path")`

### Startup probe

Right after `_resolve_archive(args)` returns the archive, call
`archive.startup_probe()`. Wrap in try/except; on `ArchiveSetupError`
log the error and exit with code 2. This runs **before** any
`fetch_seeds_from_0001()` call.

### Worker thread wiring

Pass the `archive` object into `process_org()` instead of
`archive_dir`. No per-thread archive state — `S3Archive` is
thread-safe because boto3 clients are thread-safe under normal use.

---

## Step 5 — Update `lavandula/reports/config.py`

Add constants:

```python
DEFAULT_S3_PREFIX = "pdfs"
MAX_S3_METADATA_URL_LEN = 1024  # matches _encode_s3_metadata
```

No environment-variable reading here — crawler args are the single
source of truth.

---

## Step 6 — New: `lavandula/reports/tools/reconcile_s3.py`

Standalone CLI tool implementing AC16.

```
usage: python -m lavandula.reports.tools.reconcile_s3 \
    --db PATH/reports.db \
    --archive s3://bucket/prefix \
    [--dry-run | --apply]
```

**Prerequisite — schema audit**: before building the insert
statement, the builder must read the actual `CREATE TABLE reports`
from `lavandula/reports/schema.py` and identify:

- Which columns are NOT NULL
- Which have DEFAULT values
- Which are required for `reports_public` view membership

The minimal orphan-reinsert row must satisfy NOT NULL constraints.
Based on the current schema (audited 2026-04-22), required columns
for a row to exist are: `content_sha256` (PK). All other columns
including `ein`, `source_url`, `first_page_text`, `classification`
etc. are nullable. The reconciler populates `content_sha256`, `ein`,
`source_url`, `archived_at` (from `fetched-at` metadata); leaves
everything else NULL for classify_null.py and future runs to fill.

If a future schema change adds NOT NULL columns without defaults,
the reconciler's insert will fail and this plan's test will catch
it. That's acceptable — schema change implies the reconciler needs
re-audit too.

Logic:

1. Parse args. Require exactly one of `--dry-run` / `--apply`.
2. Open reports DB (read-only for dry-run, read-write for apply).
3. Load all `content_sha256` values from `reports` into a Python set
   (`db_shas`).
4. Paginate `s3:ListObjectsV2` on `bucket/prefix/`; collect keys
   matching `{prefix}/{sha256}.pdf`. Build `s3_shas` set.
5. Compute `orphans = s3_shas - db_shas` and `missing = db_shas - s3_shas`.
6. For each orphan:
   - `head_object` to read metadata
   - Extract `ein`, `source-url` (percent-decoded for storage),
     `fetched-at`
   - **Optional source-URL HEAD probe** (spec's note): if
     `--verify-source` flag is set, do an HTTP HEAD on the
     decoded source URL and skip re-inserting if the server returns
     non-200. Default: do not probe (simpler; the S3 bytes are the
     source of truth regardless of whether the origin URL still
     serves them).
   - If `--dry-run`: print `ORPHAN sha=... ein=... source=...`
   - If `--apply`: insert a minimal `reports` row with the columns
     audited above.
7. For each missing: print `MISSING sha=... (DB references but S3 has no object)`.
   This should be rare; do not attempt auto-repair.
8. Exit with 0 on success, 2 on any hard error.

Tests: unit test with moto populating a bucket, a tempfile reports.db,
and assertions on exit code + DB state after `--apply`. Plus a
schema-audit test that loads the current `CREATE TABLE reports` and
verifies the reconciler's column-set subset.

---

## Step 7 — Tests

### AC → test mapping (every AC has a concrete test)

| AC | Test file | Test name | Mock strategy |
|----|-----------|-----------|---------------|
| AC1 | `test_s3_archive_0007.py` | `test_ac1_key_format` | moto |
| AC2 | `test_s3_archive_0007.py` | `test_ac2_sse_aes256_applied` | moto |
| AC3 | `test_s3_archive_0007.py` | `test_ac3_canonical_metadata_keys` | moto |
| AC4 | `test_fetch_pdf_s3_0007.py` | `test_ac4_text_extracted_before_put` | MagicMock archive that asserts order |
| AC5 | `test_fetch_pdf_s3_0007.py` | `test_ac5_oversize_not_put` | MagicMock archive; assert `put` never called when body > 50MB |
| AC6 | `test_fetch_pdf_local_regression_0007.py` | `test_ac6_local_archive_byte_identical` | golden-file comparison against pre-0007 snapshot |
| AC7a | `test_s3_archive_0007.py` | `test_ac7_no_region_raises` | client with `region_name=None` |
| AC7b | `test_s3_archive_0007.py` | `test_ac7_bucket_not_found_raises` | moto (bucket not created) |
| AC7c | `test_s3_archive_0007.py` | `test_ac7_access_denied_raises` | botocore stubber injecting 403 |
| AC7d | `test_s3_archive_0007.py` | `test_ac7_region_mismatch_raises` | botocore stubber with header override |
| AC8 | `test_s3_archive_retry_0007.py` | `test_ac8_503_then_success_retries` | custom boto3 event hook (see below) |
| AC9 | `test_crawler_s3_integration_0007.py` | `test_ac9_put_failure_writes_fetch_log_no_reports` | end-to-end via real DBWriter + moto S3 + fault injection |
| AC10 | n/a | `grep` gate in CI — zero `aws s3` / `boto3.client("s3")` in unit test source files without moto decorator | ci check |
| AC11 | `test_crawler_archive_argv_0007.py` | 5 sub-tests, one per argv rule | pure argparse |
| AC12 | `test_classify_null_no_s3_import_0007.py` | `test_ac12_classify_null_does_not_import_s3_archive` | Python AST walk of classify_null.py source |
| AC13 | `test_s3_archive_0007.py` | `test_ac13_key_basename_matches_regex` | moto |
| AC14 | `test_s3_archive_0007.py` | `test_ac14_put_never_sets_acl` | botocore stubber asserts no `ACL` key |
| AC15 | `test_archive_encoding_0007.py` | 4 sub-tests (CRLF, truncation, non-ASCII, fallback) | pure-function |
| AC16 | `test_reconcile_s3_0007.py` | 2 sub-tests (dry-run, apply) | moto + tempfile reports.db |

### How to test AC8 retry without Stubber

`botocore.stub.Stubber` intercepts *above* the retry layer, so it
can't prove retries happened. Two workable approaches — pick one:

**Option A (preferred)**: register a `before-send` event handler on
the S3 client. The handler counts invocations and returns a fake 503
response for the first two, passes through on the third. boto3's
`RetryHandler` sees the 503s, applies its default retry config (3
attempts), and eventually succeeds.

```python
call_count = [0]
def fake_503_then_success(request, **kwargs):
    call_count[0] += 1
    if call_count[0] < 3:
        return botocore.awsrequest.AWSResponse(
            url=request.url, status_code=503, headers={}, raw=None
        )
    return None  # let it through to moto

client.meta.events.register(
    "before-send.s3.PutObject", fake_503_then_success
)
```

**Option A** exercises boto3's real retry stack.

**Option B (fallback)**: scale back AC8 to verify the client's retry
config is set correctly (3 attempts, standard mode), without proving
end-to-end retry. Add a note that a live-S3 integration test covers
actual retries.

Pick Option A; if it proves flaky in CI, fall back to Option B.

### How to test AC9 end-to-end

Create a fixture `fake_crawler` that:
1. Spins up a real `DBWriter` against a tempfile reports.db
2. Uses moto for the S3 backend
3. Configures moto to make `put_object` raise permanently (5
   retries fail)
4. Calls `process_org` with one org on one worker thread
5. Waits for DBWriter to drain, then asserts:
   - `reports` table has 0 rows for the test EIN
   - `fetch_log` table has 1 row with `kind='pdf-get'`,
     `outcome='error'`

Place this test in
`tests/unit/test_crawler_s3_integration_0007.py`. Despite the name
and directory, it's a unit test in the sense that all external
systems are mocked — no real AWS or network.

### Boto3 thread-safety test

**`test_s3_archive_thread_safety_0007.py`**:
Spawn 8 threads, each doing 20 PUTs against a moto-backed `S3Archive`
instance (shared). Assert all 160 objects land with correct content.

This is cheap insurance that a future boto3 release doesn't break
our shared-client assumption silently.

### Integration test (live S3, gated)

**`tests/integration/test_s3_archive_live.py`**:

Skipped unless `LAVANDULA_LIVE_S3=1`. Uses a real bucket (from
`LAVANDULA_LIVE_S3_BUCKET` env). Performs one PUT, one HEAD, one GET,
and one DELETE (cleanup). Validates SSE-S3 is present on the returned
object.

Not part of the default CI run.

---

## Step 8 — Documentation

Update `lavandula/reports/HANDOFF.md`:
- Add a section `## Configuring the PDF archive`
- Document `--archive s3://bucket/prefix` as the production pattern
- Document `--archive /local/path` for dev/test
- Reference `reconcile_s3.py` for operational use
- Note the bucket is `lavandula-nonprofit-collaterals` in us-east-1

Update `lavandula/reports/HANDOFF.md`'s "Observability / Debugging"
section to note that PDFs are no longer on local disk in prod —
fetch via S3 or the reconciler tool.

---

## Acceptance Criteria Checklist

- [ ] AC1 — S3 upload at key `{prefix}/{sha256}.pdf`, no local disk write
- [ ] AC2 — SSE-S3 applied
- [ ] AC3 — Canonical metadata keys present
- [ ] AC4 — Text extraction before PUT (sequence verified in test)
- [ ] AC5 — 50 MB cap enforced before PUT
- [ ] AC6 — Local archive byte-identical regression
- [ ] AC7 — Startup probe fails fast on missing/403/region mismatch
- [ ] AC8 — 5xx retry via boto3 default
- [ ] AC9 — Write ordering; no reports row on PUT failure; fetch_log row written
- [ ] AC10 — No real AWS calls in default unit tests
- [ ] AC11 — CLI argv rules
- [ ] AC12 — classify_null does not import s3_archive
- [ ] AC13 — Key basename regex
- [ ] AC14 — No ACL parameter
- [ ] AC15 — `safe=''` encoding, CRLF-safe
- [ ] AC16 — `reconcile_s3.py` with `--dry-run` / `--apply`

---

## Traps to Avoid

1. **Don't write the PDF to a tempfile before uploading.** Bytes stay
   in memory. The whole point of 0007 is to escape disk.
2. **Don't add per-call retry.** boto3's default retry handles 5xx.
3. **Don't set the `ACL` parameter on PUT.** Inherit bucket private default.
4. **Don't relax `quote(url, safe='')`.** It's the CRLF defense.
5. **Don't extract text after the PUT.** AC4 is strict ordering.
6. **Don't read AWS credentials from env vars in code.** boto3's
   credential chain handles it.
7. **Don't assume `AWS_REGION` is set.** The startup probe's error
   message handles `None` cleanly.
8. **Don't bypass the startup probe in tests.** Call it explicitly
   so AC7 regressions are caught offline.
9. **Don't hardcode the bucket name.** Always CLI-provided. The spec's
   `lavandula-nonprofit-collaterals` is documentation, not config.
10. **Don't make the Protocol an ABC.** Duck-typed is simpler and
    matches existing codebase style.
11. **Don't skip the `head_bucket` test for region mismatch.** It's
    the most common real-world misconfiguration and must be caught
    offline.

---

## Post-merge work (architect's job, not builder's)

1. Create the S3 bucket + configure versioning/encryption/public-access
   block (admin policy work in progress per 2026-04-22 conversation).
2. Attach runtime IAM policy to `cloud2_lavandulagroup`.
3. Run a test crawl with `--archive s3://lavandula-nonprofit-collaterals/pdfs`
   on a small seed set (TX 88 orgs, re-run) and validate:
   - PDFs appear in S3
   - No local disk growth
   - Classifier still works end-to-end
   - Reconciler reports zero orphans
4. Cut over `seeds-eastcoast.db` crawl to S3 archive.
