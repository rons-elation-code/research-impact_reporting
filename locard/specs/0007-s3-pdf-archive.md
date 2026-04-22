# Spec 0007 — S3-Backed PDF Archive

**Status**: draft  
**Protocol**: SPIDER  
**Priority**: high (blocks big-crawl work on >5K orgs)  
**Date**: 2026-04-22  
**Depends on**: Spec 0004 (crawler pipeline)

---

## Problem

The current crawler archives downloaded PDFs to a local directory
(`--archive-dir`). That works at 88-org test scale but fails the two
forcing requirements that matter at production scale:

1. **Disk exhaustion.** A 5000-org east-coast run at ~5 MB avg PDF × 2
   PDFs/org = ~50 GB. Current EBS root is 58 GB with 20 GB used.
   We'd fill the disk before finishing the crawl. 48K-org nationwide
   scale (~500 GB) is impossible.

2. **Data protection.** Local EBS is a single-point-of-failure. No
   automated backup, no replication, no cross-region recovery.
   Accidental deletion, EBS volume failure, or instance termination
   loses the entire corpus.

S3 solves both. It's unlimited, durable (99.999999999%), encrypted at
rest (SSE-S3), versioned, and cross-AZ replicated by default.

---

## Goals

1. Stream PDF downloads directly through memory to S3. Never write
   bytes to local disk during a crawl.
2. Preserve the SHA256 content-addressed key scheme. The S3 object
   key is `{prefix}/{content_sha256}.pdf` — same deterministic,
   dedup-friendly naming the local archive uses today.
3. Keep the classifier hot path unchanged. First-page text extraction
   still happens from the in-memory bytes *before* the S3 upload; it
   remains stored in the `reports.first_page_text` SQLite column.
4. Preserve SSRF / 50 MB size-cap guards. Bytes are validated in
   memory the same way before any S3 PUT.
5. Backward compatibility. `--archive-dir /local/path` still works
   for local development and testing. Production uses
   `--archive s3://bucket/prefix`.
6. Bail fast on missing bucket, misconfigured IAM, or wrong region.

---

## Non-Goals

- **Migration of existing local PDFs** to S3. Separate TICK if needed.
- **Read-through cache** for previously-uploaded PDFs on a fresh crawl.
  Already covered by content-addressing — sha256 check avoids
  re-download.
- **KMS-based encryption.** SSE-S3 (AES-256, S3-managed keys) is
  sufficient for public-report data. See CLAUDE.md decision log.
- **CloudFront distribution** for public-read access to the corpus.
  Future concern; the bucket stays private for now.
- **Replacing SQLite for metadata.** Reports DB stays local SQLite;
  migration to Postgres is Spec 0013+.

---

## Design

### Archive backend selection

A single `--archive` CLI flag is the canonical archive selector.
Value interpretation:

| Value | Backend | Example |
|-------|---------|---------|
| `s3://BUCKET/PREFIX` | S3 | `s3://lavandula-nonprofit-collaterals/pdfs` |
| absolute filesystem path | local | `/tmp/tx-test/raw` |

Rules:
1. Exactly one of `--archive` or `--archive-dir` MUST be specified.
   Neither → argv parse error: `archive destination is required`.
2. `--archive-dir VALUE` is an alias for `--archive VALUE` and is
   accepted only if its value is an absolute filesystem path (not an
   `s3://` URL). This preserves backward compatibility for existing
   runbooks and tests while phasing in the new flag.
3. Both flags specified → argv parse error:
   `use --archive or --archive-dir, not both`, even if they point to
   the same value. Keeps the rule simple for operators.

### Write path (new)

```
HTTP GET → stream bytes into memory (ReportsHTTPClient, 50MB cap) →
    validate PDF magic bytes →
    extract first_page_text + PDF metadata via pypdf (in-memory) →
    compute sha256 →
    PUT to s3://bucket/prefix/{sha256}.pdf (SSE-S3) →
    DBWriter queue: upsert_report(sha256, first_page_text, ...) →
    DBWriter queue: record_fetch(kind='pdf-get', outcome='ok')
```

Key property: the PDF bytes exist only in process memory during this
sequence. They are never persisted to local disk.

### Read path (rare)

The classifier does NOT read the PDF — it reads `first_page_text`
from the SQLite column. The only components that need S3 GET are:

- Future human-review UI / dashboard drill-down
- Re-extraction if the pypdf text extractor improves
- Debug/audit workflows

For now, nothing in the hot path reads from S3. Add a stub
`s3_archive.get(sha256) -> bytes` for future callers.

### S3 object layout

```
s3://lavandula-nonprofit-collaterals/
├── pdfs/
│   ├── 02ed772ae609516c7d83604c346dfb8260ea03d67df38cd0c1fb0f58994e26b0.pdf
│   ├── 04a35eab2c85e2bc8f89fd094fb3a5449ba6897af2f15e4093c741e577f9224d.pdf
│   └── ...
```

Flat `pdfs/` prefix for now. If we exceed S3's request-rate limit
(3500 PUT/s per prefix — we're nowhere close), switch to sha-prefix
sharding: `pdfs/{sha[:2]}/{sha[2:4]}/{sha}.pdf`. Deferred.

### Object metadata

Each uploaded object carries S3 user-metadata for forensic traceability.
S3 lowercases metadata keys on receipt; the canonical stored keys are:

- `source-url`: the URL the PDF was fetched from (see truncation rule below)
- `ein`: the owning org's EIN (9 digits, ASCII)
- `crawl-run-id`: the crawler invocation ID (ASCII)
- `fetched-at`: ISO 8601 UTC timestamp with seconds precision
  (e.g. `2026-04-22T16:30:05Z`)

The spec uses these short canonical names. boto3 prepends `x-amz-meta-`
automatically on the wire; do not include that prefix when setting
metadata in Python code.

**Truncation and encoding rules for `source-url`**:

1. S3 enforces a 2 KB total user-metadata limit per object. To stay
   well under, the URL is truncated to **1024 characters** before
   upload.
2. S3 user-metadata must be US-ASCII. Non-ASCII and control characters
   in the URL are percent-encoded via `urllib.parse.quote(url, safe='')`
   **before** truncation. The strict `safe=''` mode encodes *everything*
   that isn't an unreserved RFC 3986 character, which inherently blocks
   CRLF (`\r\n`) injection into the S3 PUT's HTTP headers.
3. Truncation happens after encoding to guarantee the stored value
   never splits a percent-encoded triplet.
4. If the encoded+truncated URL still fails S3's metadata validator
   (e.g., an exotic edge case), the upload proceeds without the
   `source-url` metadata key; a structured warning is logged. The
   PDF itself still uploads; provenance degrades to EIN + crawl-run-id
   only.

**CRLF / header-injection defense**: the `safe=''` strictness is the
primary defense. A malicious source URL containing raw `\r\n` would
be encoded to `%0D%0A` — literal text, not a header terminator. boto3
additionally validates metadata values and rejects non-ASCII; the
encoding prevents that rejection path from firing on legitimate URLs.
Do **not** relax `safe=''` to include `%` or `/` "for readability" —
those relaxations reopen the injection vector.

This lets you answer "where did this PDF come from?" from S3 alone,
even if the reports.db is corrupted or offline.

### Configuration

| Env var / flag | Purpose | Default |
|----------------|---------|---------|
| `--archive s3://...` or `/path` | Archive destination | (required) |
| `--s3-region` | AWS region override for the bucket | from boto3 resolution chain |
| `LAVANDULA_S3_ENDPOINT_URL` (env) | Override for testing (moto, minio) | AWS default |

### Region handling

1. If `--s3-region` is passed, use it directly.
2. Otherwise, use boto3's default resolution chain (AWS_REGION env,
   `~/.aws/config`, IMDS — whichever resolves first).
3. On startup the crawler calls `head_bucket` to learn the bucket's
   actual region. If the bucket region differs from the configured
   client region, the crawler fails with:
   `bucket {name} is in region {actual}; client configured for {configured}`.
4. If no client region resolves at all (empty chain), fail with:
   `no AWS region configured; pass --s3-region or set AWS_REGION`.

### Credential sources

**Production (EC2)**: IMDS only. The instance role
`cloud2_lavandulagroup` provides S3 credentials. No AWS access keys
are read from env or files. If IMDS returns no credentials at
startup, the crawler fails.

**Development / CI**: boto3's default credential chain (AWS_PROFILE,
SSO, named profile) is acceptable. Inline access keys via
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars are
discouraged but not blocked (boto3 still honors them). The production
stance is enforced by operating environment, not by the code.

### Failure handling

| Condition | Behavior |
|-----------|----------|
| Bucket does not exist | Fail fast at crawler startup. Don't process any orgs. |
| Bucket exists, wrong region | Fail fast. Logs say `bucket is in us-west-2; expected us-east-1`. |
| IAM missing s3:PutObject | Fail fast at startup after a HEAD probe. |
| Transient S3 5xx | boto3 default retry (3 attempts with exponential backoff). |
| PUT succeeds, DB write fails | Orphaned S3 object is acceptable (bounded rate); logged with structured warning. Reconciler detects and repairs. |
| PUT fails after all retries | Record `fetch_log` row with `outcome='error'`, do NOT insert a `reports` row. The sha256 is retained in fetch_log for debugging. |
| PDF validation fails (not a PDF, bad structure) | Existing behavior — no PUT attempted. |

### Startup probe

At crawler init (once per run), when the archive is S3, the crawler
performs a single `head_bucket` call. This surfaces:

- Bucket does not exist → HTTP 404 → fail fast with clear error
- Bucket is in a different region → the boto3 response header
  `x-amz-bucket-region` exposes the actual region; fail fast
- IAM lacks `s3:ListBucket` / `s3:GetBucketLocation` → HTTP 403 →
  fail fast
- Network / IMDS failure → connection error → fail fast

**What `head_bucket` does NOT prove**: `s3:PutObject` permission.
Object-level permissions cannot be validated by a bucket-level HEAD.
We intentionally do not perform a write-probe at startup because:

1. It would require `s3:DeleteObject` in the runtime policy to clean
   up the probe object. We specifically kept that out of least-privilege.
2. A leftover probe object on every startup is noise.
3. A missing `s3:PutObject` will surface immediately on the first real
   upload, with the sha256 and source URL in the error log. That's
   acceptable — the crawler aborts within seconds of starting real
   work if PUT fails.

The startup probe is best-effort for fast failure of common
misconfigurations; it is not a substitute for the first real PUT.

### Orphan reconciliation (addresses async-queue orphan risk)

Because `DBWriter` is async-queued (per TICK-002), a worker's sequence
can be interrupted between a successful S3 PUT and the DB write being
flushed by the writer thread. Interruption sources:

- SIGKILL / `kill -9` on the crawler process
- OOM kill from the OS
- EC2 spot-instance termination
- Writer thread dies with the queue still holding the pending write

All produce the same result: an S3 object that no `reports` row
references. The bytes exist in the bucket but are invisible to the
pipeline. This is a **data-integrity** hazard, not a data-loss one —
the bytes are still there.

**Mitigation**: a new `lavandula/reports/tools/reconcile_s3.py`
utility (ships in this spec). It:

1. Lists all object keys under the archive prefix (`s3:ListBucket`)
2. Queries the reports DB for all `content_sha256` values
3. For each S3 key not in the DB, reads `x-amz-meta-ein` and
   `x-amz-meta-source-url` from the object, HEAD-probes the source URL
   to confirm the PDF is still the same content (optional), and inserts
   a `reports` row using the S3 metadata as the source of truth.
4. For each DB sha256 not in S3, logs a structured warning (this
   would indicate a *different* kind of drift — PUT never happened
   but DB row was written, which the 0007 design explicitly prevents,
   so this should never trigger).

The reconciler runs manually post-crawl via:
```
python -m lavandula.reports.tools.reconcile_s3 \
  --db /path/reports.db \
  --archive s3://bucket/prefix \
  --dry-run
```

`--dry-run` prints differences without writing. Real runs need
`--apply`.

**Recommended operational cadence**: run after each large crawl
(5K+ orgs) and immediately after any abnormal crawler shutdown.
Expected orphan rate is low (< 0.1%) under normal operation; higher
rates indicate infrastructure instability worth investigating.

---

## Acceptance Criteria

**AC1** — When `--archive s3://bucket/prefix` is passed, downloaded
PDFs are uploaded to S3 at key `{prefix}/{sha256}.pdf` (with the
literal `/` separator and no leading slash on the final key). No
bytes are written to local disk during the fetch → extract → upload
sequence. *Offline test: moto.*

**AC2** — Each S3 object has `ServerSideEncryption=AES256` applied
(SSE-S3). Verified by `head_object`. *Offline test: moto.*

**AC3** — Each S3 object carries user-metadata with canonical
lowercase keys: `source-url`, `ein`, `crawl-run-id`, `fetched-at`.
`fetched-at` matches ISO 8601 UTC format. `source-url` is percent-
encoded and truncated to 1024 chars. *Offline test: moto.*

**AC4** — First-page text extraction happens on the in-memory bytes
**before** the S3 PUT. Extraction is a pure in-memory pypdf operation
with no dependency on archive success.

**AC5** — 50 MB size cap from TICK-002 is still enforced. A PDF
exceeding 50 MB is rejected before any S3 PUT attempt. *Offline test.*

**AC6** — When `--archive /local/path` is passed, existing local
archive behavior is unchanged — byte-identical output on the same
input compared to the pre-0007 code path. *Offline regression test.*

**AC7** — Crawler startup performs exactly one `head_bucket` call
before the first org is processed. If the bucket does not exist, is
inaccessible, or is in a different region than the configured client,
the crawler logs a clear error naming the specific cause and exits
with non-zero status. *Offline test: moto + botocore stubber for
4xx / region-mismatch cases.*

**AC8** — Transient S3 5xx errors are retried via boto3 default retry
(3 attempts, exponential backoff). Test proves retry occurs: mocked
`503 → 503 → 200` sequence results in a successful upload and one
`reports` row. *Offline test: moto with fault injection, or botocore
stubber.*

**AC9** — Write ordering and failure semantics:
  - **9a**: Sequence is strictly `extract_text → PUT → DBWriter.put(upsert_report) → DBWriter.put(record_fetch)`.
  - **9b**: When all S3 PUT retries fail, a `fetch_log` row is written
    with `kind='pdf-get'`, `outcome='error'`, `note` naming the S3
    error class; **no `reports` row is written** for that sha256.
  - **9c**: `first_page_text` is therefore NOT persisted when the S3
    PUT fails. It exists only in the worker's memory and is lost when
    the worker moves on. The sha256 in `fetch_log` is sufficient to
    retry that URL on a later crawl.
  *Offline test: moto with permanent PUT failure.*

**AC10** — Unit tests use `moto` to mock S3; zero real AWS calls in
the unit test suite. Integration tests against real S3 live behind
`LAVANDULA_LIVE_S3=1` env flag, default off.

**AC11** — CLI argv parsing enforces the archive-selection rules:
  - Neither `--archive` nor `--archive-dir` → parse error
    `archive destination is required`.
  - Both flags → parse error `use --archive or --archive-dir, not both`.
  - `--archive-dir` with an `s3://` value → parse error
    `--archive-dir accepts only a filesystem path; use --archive for S3`.
  - `--archive` with either form → accepted.
  - `--archive-dir /path` → accepted, treated as equivalent to
    `--archive /path`.
  *Pure offline test: argparse only, no S3.*

**AC12** — The classifier (`classify_null.py`) runs unchanged against
a reports DB whose PDFs live in S3. No S3 reads occur during
classification. *Offline test: grep-based assertion that
classify_null never imports `s3_archive`.*

**AC13** — The S3 object **key basename** (the portion after the
final `/`) matches `^[a-f0-9]{64}\.pdf$`. The key as a whole may
include a prefix: full key `^([^/]+/)*[a-f0-9]{64}\.pdf$`. Defensive
length check rejects any computed sha256 that isn't 64 hex chars.
*Offline test.*

**AC14** — `S3Archive.put()` never sets an `ACL` parameter on the PUT
call. Objects inherit bucket default ACL (private). *Offline test:
botocore stubber asserts no `ACL` key in request params.*

**AC15** — `source-url` metadata encoding uses
`urllib.parse.quote(url, safe='')`. A URL containing CRLF
(`"https://a.example/\r\nx-amz-acl: public-read"`) is encoded to
`https%3A%2F%2Fa.example%2F%0D%0Ax-amz-acl%3A%20public-read`, and the
resulting HTTP request headers contain no injected `x-amz-acl` line.
*Offline test: botocore stubber asserts request headers do not contain
additional `x-amz-*` keys beyond the canonical four and that the
encoded URL is correctly percent-encoded.*

**AC16** — `reconcile_s3.py` tool exists and supports `--dry-run` and
`--apply` modes. Given a reports DB and an S3 prefix with one orphan
object (present in S3, absent from DB), the tool:
  - `--dry-run`: prints the orphan sha256 and its EIN from object
    metadata, exits 0, writes nothing.
  - `--apply`: inserts a `reports` row using the S3 metadata as the
    source of truth, exits 0.
  *Offline test: moto + an in-memory reports DB.*

---

## Traps to Avoid

1. **Do not write the PDF to disk as a "temp file" before S3 upload.**
   The bytes stay in a `BytesIO` buffer. Disk is the problem we're
   solving; touching it defeats the purpose.

2. **Do not skip the 50 MB cap for S3 uploads.** The SSRF/size
   defenses are not "local only." A 200 MB malicious PDF is still
   capped before any PUT.

3. **Do not extract text after the upload.** If the upload fails,
   the retry would re-extract text from the same bytes redundantly,
   and if retries are exhausted we lose the text entirely. Extract
   first, upload second.

4. **Do not make the S3 bucket publicly readable** in a "temporary"
   setup step. The bucket is private; public access is explicitly
   blocked at the bucket level. If a future consumer needs public
   read, use a CloudFront distribution with signed URLs, not a
   public bucket.

5. **Do not delete the local-archive code path.** The local backend
   stays as a first-class option for development, testing against
   moto, and air-gapped environments.

6. **Do not hardcode the bucket name in code.** Pass via CLI.
   Default to `None` (no default) — crawler fails with a clear
   error if no archive is specified, same as today.

7. **Do not embed AWS credentials in config files or env.**
   IMDS-only. If running outside EC2 (developer laptop, CI), use
   `aws sso login` or named profiles via `AWS_PROFILE`, never
   keys on disk.

8. **Do not log PDF bytes or S3 PUT response bodies.** Log only the
   URL, sha256, outcome, and byte count. PDFs can contain sensitive
   PII even in public reports (e.g., staff SSNs occasionally leak
   into 990 schedules).

9. **Do not assume `AWS_REGION` is set.** boto3's region resolution
   has several fallbacks (env, config file, IMDS). Handle `None`
   gracefully. The `head_bucket` error message will name the region
   mismatch clearly.

10. **Do not use `s3:PutObjectAcl`**. Objects inherit bucket-level
    access controls. Uploading with a different ACL is a footgun
    that can unintentionally override the private-by-default stance.
    Explicitly do not set the `ACL` parameter in PUT calls.

11. **Do not relax `urllib.parse.quote(url, safe='')` to
    `safe='/'` or `safe='%'`**. The strict setting is the primary
    defense against CRLF injection into S3 PUT HTTP headers via the
    `source-url` metadata. Any relaxation reintroduces the attack
    vector.

12. **Do not assume DBWriter persistence at the moment `put()`
    returns**. Writes are queued and flushed by the writer thread.
    A worker that considers itself "done" the instant it calls
    `DBWriter.put(upsert_report)` has NOT yet durably recorded the
    upload. On abnormal shutdown, a window exists where S3 has the
    bytes but the DB does not. The `reconcile_s3` tool is how we
    recover, not a pre-commit-style synchronous barrier (which would
    serialize the pipeline and defeat TICK-002's parallelism).

---

## Files Changed

| File | Change |
|------|--------|
| `lavandula/reports/s3_archive.py` | NEW — `S3Archive` class: `put(sha256, body, metadata)`, `get(sha256)`, `head(sha256)`, `head_bucket()` |
| `lavandula/reports/fetch_pdf.py` | `download()` takes an archive backend (S3 or local); routes PUT accordingly |
| `lavandula/reports/crawler.py` | `--archive` arg; `--archive-dir` alias; backend construction at startup; `head_bucket` probe |
| `lavandula/reports/config.py` | S3 config fields: bucket, prefix, region, endpoint_url |
| `lavandula/reports/tests/unit/test_s3_archive_0007.py` | NEW — AC1, AC2, AC3, AC5, AC6, AC7, AC8, AC9, AC11, AC12, AC13, AC14 (moto + botocore stubber; no AWS calls) |
| `lavandula/reports/tests/unit/test_crawler_archive_argv_0007.py` | NEW — AC11 pure argparse tests |
| `lavandula/reports/tests/integration/test_s3_archive_live.py` | NEW — end-to-end smoke test against real S3, gated on `LAVANDULA_LIVE_S3=1` |
| `lavandula/reports/tools/reconcile_s3.py` | NEW — orphan reconciliation tool (AC16) |
| `lavandula/reports/tests/unit/test_reconcile_s3_0007.py` | NEW — AC16 |
| `lavandula/reports/HANDOFF.md` | Update operator runbook — how to configure S3 archive |
| `requirements.in` / `requirements.txt` | Add `boto3`, `moto` (test-only) |

---

## Security Considerations (pre-red-team self-review)

### Threat model

- **Assets**: PDF corpus (non-sensitive public data but expensive to
  re-fetch), S3 bucket credentials (via IMDS, scoped), crawler process
  integrity.
- **Actors**: External attackers via malicious nonprofit websites,
  misconfigured IAM, EBS volume compromise.
- **Attack surface**: S3 PUT path, boto3 dependency, IMDS credential
  fetching.

### Mitigations already in place (pre-0007)

- SSRF guard on outbound HTTP (`url_guard.py`)
- 50 MB stream cap
- Subprocess isolation for PDF structure validation

### New mitigations this spec adds

- SSE-S3 at rest (bucket-level default + explicit per-PUT parameter)
- Private bucket with all public-access-block options enabled
- Least-privilege IAM runtime role: `PutObject`, `GetObject`,
  `ListBucket`, `GetBucketLocation` only — scoped to single bucket
- No ACL parameter on PUT (inherits bucket default: private)
- `head_bucket` probe at startup surfaces misconfiguration before
  sensitive writes occur
- Bucket versioning enabled — accidental overwrite or DELETE from
  a compromised key is recoverable

### Residual risks

- **Compromised EC2 host** → attacker has the IAM role. They can PUT
  and GET from the bucket but cannot change bucket policy, enable
  public access, or access other buckets. Acceptable given the
  scoped runtime policy.
- **S3 API outage** → crawler fails fast; no partial state. Acceptable.
- **Metadata injection via source URL** → source URL is a string we
  got from the seed DB (which we control). It's HTTP-header-encoded
  by boto3 before sending. Not a credential path.

---

## Open Questions

1. **Should we store a manifest in SQLite?** Currently the `reports`
   table has `content_sha256` and the S3 URI is derivable from bucket
   + prefix + sha256. An explicit `archive_uri` column would be more
   self-describing but duplicates derivable data. Recommend: leave
   derivable for now, add column if we ever support multiple archive
   backends per DB.

2. **Should we pre-check if the sha256 already exists in S3 before
   uploading?** Content-addressing means redundant PUTs of the same
   sha256 just overwrite identical bytes (idempotent, cheap). A
   `head_object` check before each PUT adds a round-trip per PDF. On
   balance, skip the check — PUT directly, accept the (rare) redundant
   upload. S3 absorbs these quietly.

3. **Local cache for recently-fetched PDFs?** Useful for dashboard
   drill-down without S3 GET latency. Suggest deferring to a TICK
   after the dashboard (0006) is built and we see what access
   patterns actually matter.

4. **Garbage collection for orphaned S3 objects** (PUT-then-DB-fail
   edge case)? For 2026 the rate is so low we can manually reconcile
   annually. Deferred to an operational TICK if the orphan rate
   turns out nontrivial.
