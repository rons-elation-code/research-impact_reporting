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

Edit `lavandula/reports/requirements.in` (runtime) and
`lavandula/reports/tests/requirements.in` (test) if those exist; if
not, the crawler venv already has a single `requirements.in`.

Add to runtime deps:
- `boto3>=1.34` (latest stable as of 2026-04-22)

Add to test deps only:
- `moto[s3]>=5.0`

Regenerate `requirements.txt` via the project's standard `pip-compile`
pipeline. Do not hand-edit the lockfile.

---

## Step 2 — New: `lavandula/reports/archive.py`

Defines the archive backend interface and the two concrete classes.

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

Two concrete implementations:

1. **`LocalArchive`** — wraps the existing local-file write behavior.
   `put()` writes `body` to `{archive_dir}/{sha256}.pdf`; metadata
   written to a sidecar `.json` file (optional — keep simple, no
   sidecar in v1). `startup_probe()` checks the dir exists and is
   writable.

2. **`S3Archive`** — wraps boto3 `put_object`/`head_object`/`get_object`
   /`head_bucket`. Constructor accepts an optional `boto3_client` for
   dependency injection (tests use moto). `startup_probe()` runs
   `head_bucket` and compares the `x-amz-bucket-region` response
   header against the client's configured region.

The `Archive` Protocol is duck-typed — no ABC, no registration. A test
can pass any object with the four methods.

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

def _encode_s3_metadata(raw: dict) -> dict:
    """Encode metadata values to satisfy S3 ASCII rules and prevent
    header injection via CRLF."""
    out = {}
    for k, v in raw.items():
        if v is None:
            continue
        if k == "source-url":
            # safe='' is the CRLF defense — don't relax it.
            encoded = quote(v, safe='')
            encoded = encoded[:_MAX_SOURCE_URL_LEN]
            out[k] = encoded
        else:
            # EIN, run-id, timestamp — already ASCII; pass through.
            out[k] = str(v)
    return out
```

### `S3Archive.startup_probe()`

```python
def startup_probe(self) -> None:
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
        raise

    actual_region = resp["ResponseMetadata"]["HTTPHeaders"].get(
        "x-amz-bucket-region", "unknown"
    )
    configured_region = self._client.meta.region_name
    if configured_region and actual_region != configured_region:
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

`archive` is an `Archive` (duck-typed). Inside `download()`:

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

Logic:

1. Parse args. `--dry-run` default; `--apply` required for writes.
2. Open reports DB (read-only for dry-run, read-write for apply).
3. Load all `content_sha256` values from `reports` into a Python set
   (`db_shas`).
4. Paginate `s3:ListObjectsV2` on `bucket/prefix/`; collect keys
   matching `{prefix}/{sha256}.pdf`. Build `s3_shas` set.
5. Compute `orphans = s3_shas - db_shas` and `missing = db_shas - s3_shas`.
6. For each orphan:
   - `head_object` to read metadata
   - Extract `ein`, `source-url` (percent-decoded for storage)
   - If `--dry-run`: print `ORPHAN sha=... ein=... source=...`
   - If `--apply`: insert a minimal `reports` row using metadata as
     source of truth; leave `first_page_text=NULL`, `classification=NULL`
     (classify_null can fill later)
7. For each missing: print `MISSING sha=... (DB references but S3 has no object)`.
   This should be rare; do not attempt auto-repair.
8. Exit with 0 on success, 2 on any hard error.

Tests: unit test with moto populating a bucket, a tempfile reports.db,
and assertions on exit code + DB state after `--apply`.

---

## Step 7 — Tests

### Unit tests (all with moto, no AWS)

**`tests/unit/test_s3_archive_0007.py`** (covers AC1, AC2, AC3, AC5,
AC7, AC8, AC9, AC13, AC14, AC15):

Use `moto.mock_aws` decorator on each test function.

Representative tests:

```python
@mock_aws
def test_ac1_s3_put_uses_correct_key_format():
    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="testbucket")
    archive = S3Archive("testbucket", prefix="pdfs", client=client)
    archive.put("a"*64, b"%PDF-1.7\n...", {"ein": "123456789",
                "source-url": "https://example.org/r.pdf",
                "crawl-run-id": "run1", "fetched-at": "2026-04-22T00:00:00Z"})
    resp = client.list_objects_v2(Bucket="testbucket")
    assert resp["Contents"][0]["Key"] == "pdfs/" + "a"*64 + ".pdf"
```

```python
@mock_aws
def test_ac2_sse_s3_applied():
    # After put, head_object returns ServerSideEncryption == "AES256"
    ...

@mock_aws
def test_ac3_metadata_present_with_canonical_keys():
    # head_object shows metadata keys source-url, ein, crawl-run-id, fetched-at
    ...

@mock_aws
def test_ac7_head_bucket_404_raises_clear_error():
    client = boto3.client("s3", region_name="us-east-1")
    archive = S3Archive("doesnotexist", client=client)
    with pytest.raises(ArchiveSetupError, match="does not exist"):
        archive.startup_probe()

@mock_aws
def test_ac7_region_mismatch_raises_clear_error():
    # Create bucket in us-west-2 via mocked API; probe with us-east-1 client
    ...
```

For AC8 (5xx retry) and AC14 (no ACL param), use `botocore.stub`
instead of moto so we can inject exact HTTP responses and assert exact
request kwargs.

**`tests/unit/test_crawler_archive_argv_0007.py`** (covers AC11):

Pure argparse tests. Instantiate the parser, call `parse_args` on
various argv combinations, assert `parser.error` raises `SystemExit`
with the expected message.

**`tests/unit/test_archive_encoding_0007.py`** (covers AC15, part of AC3):

Pure-function tests on `_encode_s3_metadata`:

```python
def test_crlf_url_is_encoded_and_does_not_inject_headers():
    result = _encode_s3_metadata({
        "source-url": "https://a.example/\r\nx-amz-acl: public-read",
        "ein": "123456789",
    })
    assert result["source-url"] == (
        "https%3A%2F%2Fa.example%2F%0D%0Ax-amz-acl%3A%20public-read"
    )
    # The literal "\r\n" does not appear anywhere:
    assert "\r" not in result["source-url"]
    assert "\n" not in result["source-url"]

def test_long_url_truncated_to_1024_chars():
    long_url = "https://example.org/" + ("a" * 2000)
    result = _encode_s3_metadata({"source-url": long_url, "ein": "1"})
    assert len(result["source-url"]) == 1024
```

**`tests/unit/test_reconcile_s3_0007.py`** (covers AC16):

```python
@mock_aws
def test_reconcile_dry_run_lists_orphan_but_does_not_write(tmp_path):
    # setup: create bucket, upload one PDF with metadata, create empty reports.db
    # invoke reconcile_s3 main() with --dry-run
    # assert: stdout contains ORPHAN line with sha/ein
    # assert: reports.db has 0 rows

@mock_aws
def test_reconcile_apply_inserts_orphan(tmp_path):
    # same setup
    # invoke with --apply
    # assert: reports.db has 1 row with matching sha/ein
```

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
