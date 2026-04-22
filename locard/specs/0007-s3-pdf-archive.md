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

A single `--archive` CLI flag replaces `--archive-dir`. Value
interpretation:

| Value | Backend | Example |
|-------|---------|---------|
| `s3://BUCKET/PREFIX` | S3 | `s3://lavandula-nonprofit-collaterals/pdfs` |
| absolute filesystem path | local | `/tmp/tx-test/raw` |

Old `--archive-dir` remains accepted as alias for `--archive` for
backward compatibility during the transition. Both flags specifying
different destinations → error at argv parse time.

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

Each uploaded object carries S3 user-metadata for forensic traceability:

- `x-amz-meta-source-url`: the URL the PDF was fetched from (truncated
  to 2048 bytes per S3 limits)
- `x-amz-meta-ein`: the owning org's EIN
- `x-amz-meta-crawl-run-id`: the crawler invocation ID
- `x-amz-meta-fetched-at`: ISO 8601 timestamp

This lets you answer "where did this PDF come from?" from S3 alone,
even if the reports.db is corrupted or offline.

### Configuration

| Env var / flag | Purpose | Default |
|----------------|---------|---------|
| `--archive s3://...` or `/path` | Archive destination | (required) |
| `--s3-region` | AWS region override | from IAM / AWS_REGION env |
| `LAVANDULA_S3_ENDPOINT_URL` (env) | Override for testing (moto, minio) | AWS default |

The EC2 instance role `cloud2_lavandulagroup` provides S3 credentials
via IMDS. No AWS access keys are read from env or file. If credentials
are unavailable at startup, fail with a clear error.

### Failure handling

| Condition | Behavior |
|-----------|----------|
| Bucket does not exist | Fail fast at crawler startup. Don't process any orgs. |
| Bucket exists, wrong region | Fail fast. Logs say `bucket is in us-west-2; expected us-east-1`. |
| IAM missing s3:PutObject | Fail fast at startup after a HEAD probe. |
| Transient S3 5xx | boto3 default retry (3 attempts with exponential backoff). |
| PUT succeeds, DB write fails | Orphaned S3 object is acceptable. Log structured warning so a garbage-collection TICK can clean up later. |
| PUT fails after all retries | Record `fetch_log` row with `outcome='error'`, do NOT insert a `reports` row. The sha256 is retained in fetch_log for debugging. |
| PDF validation fails (not a PDF, bad structure) | Existing behavior — no PUT attempted. |

### Startup probe

At crawler init (once per run), when archive is S3, the crawler
performs a `head_bucket` call. This surfaces permission / region /
existence errors before the first org is processed, so you don't
discover the failure after 3 hours of crawling.

---

## Acceptance Criteria

**AC1** — When `--archive s3://bucket/prefix` is passed, downloaded
PDFs are uploaded to S3 at key `{prefix}/{sha256}.pdf`. No bytes
are written to local disk.

**AC2** — Each S3 object has `ServerSideEncryption=AES256` applied
(SSE-S3). Verified by `head_object` in integration test.

**AC3** — Each S3 object carries user-metadata: `source-url`, `ein`,
`crawl-run-id`, `fetched-at`. Verified via `head_object`.

**AC4** — First-page text extraction happens before the S3 PUT on the
same in-memory bytes. `first_page_text` is populated in the reports
row even if the subsequent S3 PUT fails.

**AC5** — 50 MB size cap from TICK-002 is still enforced. A PDF
exceeding 50 MB is rejected before any S3 PUT attempt.

**AC6** — When `--archive /local/path` is passed, existing local
archive behavior is unchanged (byte-identical output on the same
input).

**AC7** — Crawler startup performs a `head_bucket` probe. If the
bucket does not exist or is inaccessible, the crawler logs a clear
error and exits with non-zero status before touching any org.

**AC8** — Transient S3 5xx errors are retried via boto3 default
retry. Test proves retry happens (mocked 503 → 503 → 200 succeeds).

**AC9** — When all S3 PUT retries fail, a `fetch_log` row is written
with `kind='pdf-get'`, `outcome='error'`, `note` including the S3
error class; no `reports` row is written for that sha256.

**AC10** — Unit tests use `moto` to mock S3; zero real AWS calls in
the test suite. Integration tests against real S3 live behind
`LAVANDULA_LIVE_S3=1` env flag, default off.

**AC11** — Both `--archive-dir` (legacy) and `--archive` (new) work.
If both are specified with different values, argv parse fails with
a clear error. If both specify the same local path, they're treated
as equivalent.

**AC12** — The classifier (`classify_null.py`) runs unchanged against
a reports DB whose PDFs live in S3. No S3 reads occur during
classification.

**AC13** — S3 object key matches `^[a-f0-9]{64}\.pdf$` exactly (sha256
hex + `.pdf`). No sha256 from any crawl has ever produced a
different-length digest, but defensive check stays in place.

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

---

## Files Changed

| File | Change |
|------|--------|
| `lavandula/reports/s3_archive.py` | NEW — `S3Archive` class: `put(sha256, body, metadata)`, `get(sha256)`, `head(sha256)`, `head_bucket()` |
| `lavandula/reports/fetch_pdf.py` | `download()` takes an archive backend (S3 or local); routes PUT accordingly |
| `lavandula/reports/crawler.py` | `--archive` arg; `--archive-dir` alias; backend construction at startup; `head_bucket` probe |
| `lavandula/reports/config.py` | S3 config fields: bucket, prefix, region, endpoint_url |
| `lavandula/reports/tests/unit/test_s3_archive_0007.py` | NEW — AC1–AC5, AC8, AC9, AC10, AC13 with moto |
| `lavandula/reports/tests/integration/test_s3_archive_live.py` | NEW — AC7 (bucket probe), AC11 (CLI args), gated on `LAVANDULA_LIVE_S3=1` |
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
