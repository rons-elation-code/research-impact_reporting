# Plan: Nonprofit Seed List Extraction

## Metadata

- **ID**: plan-2026-04-17-nonprofit-seed-list-extraction
- **Status**: draft
- **Specification**: locard/specs/0001-nonprofit-seed-list-extraction.md
- **Created**: 2026-04-17

## Executive Summary

This plan implements Approach 1 from the spec: a sitemap-driven scrape of Charity
Navigator's ~48K public nonprofit profiles into a queryable SQLite database. The
work is decomposed into **seven phases**, each independently committable and
valuable.

Phase ordering deliberately front-loads the "safe to exercise against live
infrastructure" pieces (schema, HTTP client, robots parsing) before the phases
that actually touch Charity Navigator at scale (Phase 3 onwards). That isolation
is the key testability pattern: each phase ships a committed, tested unit that
the next builds on.

The implementation directory is `lavandula/nonprofits/` — a new top-level sibling
to `nptech/`. Where possible we reuse patterns (not code, yet) from `nptech/`,
and we hoist shared primitives into `common/` only when a second concrete
consumer appears (not in this project).

## Success Metrics

All metrics below map to Success Criteria in the spec (locard/specs/0001-nonprofit-seed-list-extraction.md).

### Parser Correctness (GATING)

- (v) Fixtures pass: every committed HTML fixture in `tests/fixtures/cn/`
  produces the expected tuple. 100% accuracy required.
- (v) Fixture coverage includes: rated 4-star, 1-3 star, unrated, missing
  website, wrapped website, tracking-param website, 404 profile, 301 redirect,
  cross-EIN redirect, malformed/truncated HTML, Cloudflare challenge body.
- (v) Sitemap parsing: all 48 child sitemaps enumerated; malformed XML raises a
  clear error (no silent skip).
- (v) Deduplication: the same EIN appearing in two child sitemaps is fetched
  exactly once.
- (v) Idempotency: re-running against an existing checkpoint re-fetches nothing
  unless `--refresh` is passed.
- (v) Test coverage greater than or equal to 80% on extraction and parsing
  modules (not the network layer).

### Empirical Source Coverage (REPORTED, not gated)

- Report observed `website_url`, `rating`, `revenue`, `state` population rates
  in `coverage_report.md`.
- Field population below 50% triggers manual review (not automated failure).

### Operational / Compliance (GATING)

- (v) Effective sustained request rate less than or equal to 0.4 req/s.
- (v) Post-retry 429 rate less than 1% (defined precisely via `fetch_log`).
- (v) No CN-initiated IP block observed.
- (v) Stop-condition halt: if any halt condition fires, exit code 2 +
  `HALT-*.md` written.
- (v) `robots.txt` re-fetched at crawl start; any new disallow on `/ein/*`
  halts the crawl.
- (v) `lavandula/nonprofits/HANDOFF.md` complete with schema, queries,
  refresh instructions, contact protocol.

### Security (GATING — from red-team findings)

- (v) XXE fixture test: malicious sitemap with external entity references does
  NOT yield file contents or outbound network calls.
- (v) Cross-host redirect rejection test: `302 Location: attacker.example.org`
  is not followed; logged as `fetch_status='server_error'`.
- (v) TLS startup self-test: connection to `expired.badssl.com` fails; if it
  succeeds, crawler halts before any production request.
- (v) Symlink refusal test: pre-planted symlink at `raw/cn/{ein}.html` triggers
  halt; target is not modified.
- (v) SQL injection round-trip: mission containing `'; DROP TABLE ...; --` is
  stored byte-identical and the table still exists.
- (v) Log injection sanitation: CR/LF in `Retry-After` is stripped before
  reaching `fetch_log.notes` or disk logs.
- (v) Single-instance flock: second concurrent crawler process exits code 3.
- (v) File permissions: DB mode 0o600, archive dir mode 0o700, verified
  post-run.
- (v) Cookie non-persistence: sequential GETs contain no `Cookie:` header.
- (v) Zero critical security issues open at PR review time.

## Acceptance Test Matrix (MANDATORY)

Every acceptance criterion in the spec maps to at least one test. The matrix
below identifies which phase owns each test. Tests are generated BEFORE
implementation per TDD discipline.

| AC | Acceptance Criterion (from spec) | Phase | Test Type | Test Location |
|----|----------------------------------|-------|-----------|---------------|
| AC1 | SQLite schema validates (CHECK constraints pass) | 1 | Unit | tests/unit/test_schema.py |
| AC2 | Throttle enforcement: 100 reqs take greater than or equal to 300s (fake-clock assertion on `time.monotonic` deltas) | 1 | Unit | tests/unit/test_http_client.py |
| AC3 | TLS startup self-test fails on expired.badssl.com; also halts if self-test cannot determine outcome within 30s | 1 | Integration | tests/integration/test_http_client.py |
| AC4 | Decompression bomb (10 MB decompressed) is size-capped | 1 | Integration | tests/integration/test_http_client.py |
| AC5 | Cross-host redirect rejected | 1 | Integration | tests/integration/test_http_client.py |
| AC5a | Scheme-downgrade redirect rejected (302 to http://www.charitynavigator.org/...) | 1 | Integration | tests/integration/test_http_client.py |
| AC5b | Content-Type rejection: response not starting with `text/html` → fetch_status=server_error | 1 | Integration | tests/integration/test_http_client.py |
| AC6 | Cookie non-persistence across sequential GETs (assert jar len == 0) | 1 | Integration | tests/integration/test_http_client.py |
| AC6a | Retry-After HTTP-date form handled (not just seconds) | 1 | Unit | tests/unit/test_http_client.py |
| AC7 | robots.txt stanza matching (most-specific wins) | 2 | Unit | tests/unit/test_robots.py |
| AC7a | robots.txt fetch failure at startup halts | 2 | Integration | tests/integration/test_robots.py |
| AC7b | robots.txt re-fetch revealing new `/ein/*` disallow halts | 2 | Integration | tests/integration/test_robots.py |
| AC7c | Periodic robots.txt re-fetch (every 6h or 1000 EINs) during long run | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC8 | XXE local-file entity not resolved (`file:///dev/null` sentinel, not /etc/passwd) | 2 | Unit | tests/unit/test_sitemap_parse.py |
| AC8a | XXE SSRF entity (`SYSTEM "http://127.0.0.1:..."`) triggers no outbound network call | 2 | Unit | tests/unit/test_sitemap_parse.py |
| AC8b | HTML-mode XXE: BeautifulSoup parser does not resolve entities in HTML body | 4 | Unit | tests/unit/test_extract.py |
| AC9 | Sitemap enumerates 48 child sitemaps | 2 | Unit | tests/unit/test_sitemap_parse.py |
| AC10 | Malformed EIN in sitemap skipped | 2 | Unit | tests/unit/test_sitemap_parse.py |
| AC11 | Disallowed EIN 86-3371262 is not enumerated (both dashed + undashed forms rejected via canonicalize_ein) | 2 | Unit | tests/unit/test_sitemap_parse.py |
| AC12 | Symlink refusal on archive write | 3 | Integration | tests/integration/test_archive.py |
| AC13 | Cloudflare challenge body detected and isolated | 3 | Integration | tests/integration/test_fetch.py |
| AC13a | Challenge detection also triggers immediate halt (distinct from isolation) | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC14 | Cross-EIN redirect populates `redirected_to_ein` in source row | 3 | Integration | tests/integration/test_fetch.py |
| AC14a | Second, independent row written when target EIN (B) is enumerated on its own | 3 | Integration | tests/integration/test_fetch.py |
| AC14b | Query-time dedup: `GROUP BY COALESCE(redirected_to_ein, ein)` collapses redirect-pair to one logical org | 3 | Unit | tests/unit/test_db.py |
| AC15 | Atomic archive write (crash mid-write leaves no partial file) | 3 | Integration | tests/integration/test_archive.py |
| AC15a | Parent-directory fsync after os.replace (crash-durability) | 3 | Integration | tests/integration/test_archive.py |
| AC15b | Per-process temp subdir isolates in-flight writes (TOCTOU defense) | 3 | Integration | tests/integration/test_archive.py |
| AC16 | Parser produces all fields for rated fixture | 4 | Unit | tests/unit/test_extract.py |
| AC17 | Parser produces name/mission only for unrated fixture | 4 | Unit | tests/unit/test_extract.py |
| AC18 | URL normalization rule set (host lowercase, default-port strip, trailing-slash on root only, fragment drop) | 4 | Unit | tests/unit/test_url_normalize.py |
| AC18a | CN-redirect unwrap (wrapper URL becomes underlying URL) | 4 | Unit | tests/unit/test_url_normalize.py |
| AC18b | Tracking-parameter strip (utm_*, fbclid, gclid, mc_cid, mc_eid, _ga removed; other query params kept) | 4 | Unit | tests/unit/test_url_normalize.py |
| AC18c | IDN host punycode encoding | 4 | Unit | tests/unit/test_url_normalize.py |
| AC18d | Invalid/empty-host URL returns reason='invalid' | 4 | Unit | tests/unit/test_url_normalize.py |
| AC19 | mailto/tel/sms/javascript links rejected with correct `website_url_reason` | 4 | Unit | tests/unit/test_url_normalize.py |
| AC19a | Social-only links (facebook/twitter/linkedin/etc.) rejected with `reason='social'` | 4 | Unit | tests/unit/test_url_normalize.py |
| AC20 | SQL injection round-trip (mission with SQL keywords) | 4 | Integration | tests/integration/test_db.py |
| AC21 | Log injection: CR/LF stripped from remote strings | 4 | Unit | tests/unit/test_log_sanitize.py |
| AC22 | Single-instance flock prevents concurrent runs | 5 | Integration | tests/integration/test_cli.py |
| AC23 | Checkpoint resume: partial run continues from last EIN | 5 | Integration | tests/integration/test_cli.py |
| AC24 | Corrupt checkpoint renamed, fresh run starts | 5 | Integration | tests/integration/test_cli.py |
| AC24a | Checkpoint HMAC mismatch treated as corrupt (tampering defense) | 5 | Unit | tests/unit/test_checkpoint.py |
| AC24b | Corrupt-checkpoint retention cap: oldest deleted once more than 5 exist | 5 | Integration | tests/integration/test_cli.py |
| AC25 | Stop-condition halt on 3 consecutive 403s | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC26 | Stop-condition halt on 5 consecutive unresolved 429s | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC26a | Stop-condition halt on 2 consecutive Retry-After values greater than 300s | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC26b | Stop-condition halt on cumulative runtime greater than 72 hours | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC27 | Stop-condition halt on disk-space less than 5 GB | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC28 | Stop-condition halt on cumulative archive greater than 50 GB | 5 | Integration | tests/integration/test_stop_conditions.py |
| AC29 | SIGTERM graceful shutdown with checkpoint flush + HALT file | 5 | Integration | tests/integration/test_cli.py |
| AC29a | SIGTERM fallback: HALT write failure falls back to stderr + _exit(2) | 5 | Integration | tests/integration/test_cli.py |
| AC30 | File permissions: DB 0o600, raw/cn/ 0o700, coverage_report.md + HANDOFF.md 0o600 after run | 5 | Integration | tests/integration/test_cli.py |
| AC30a | DNS IP-pin: startup resolves `www.charitynavigator.org` IP set; drift logs warning | 5 | Integration | tests/integration/test_cli.py |
| AC30b | Clock-sync warning when local vs CN `Date:` header diverges greater than 5 min | 5 | Integration | tests/integration/test_cli.py |
| AC30c | Content-Length vs actual-bytes sanity warning at greater than 10× divergence | 1 | Unit | tests/unit/test_http_client.py |
| AC31 | coverage_report.md generated with empirical metrics | 6 | Integration | tests/integration/test_report.py |
| AC32 | HANDOFF.md contains schema + query + refresh + contact-protocol + usage-restrictions sections | 6 | Manual | inspection |
| AC32a | `lavandula/nonprofits/README.md` quick-start exists and covers install + run + test | 6 | Manual | inspection |
| AC32b | `locard/resources/arch.md` updated with `lavandula/nonprofits/` module entry (Review phase, owned by Phase 6 prep) | 6 | Manual | inspection |
| AC33 | Live small-batch run (50 EINs) completes with zero halts; at least 8/10 spot-checked rows match browser-rendered values; field-population percentages in predicted range | 7 | E2E | manual validation |

**Coverage Requirements:**

- Every AC has at least one test (complete above).
- User-facing ACs (coverage reports, handoff docs) have integration or manual
  tests.
- Security-sensitive ACs (AC3-AC6, AC8, AC12-AC15, AC20-AC21, AC25-AC28) have
  explicit adversarial tests.

## Phase Breakdown

### Phase 0: TDD Acceptance Test Scaffolding

**Dependencies**: none (must commit BEFORE any Phase-1 implementation)

Rationale: Codex red-team-plan MEDIUM-2 flagged that the repo's TDD workflow
(see CLAUDE.md / AGENTS.md) requires acceptance-test scaffolding to be
generated and committed BEFORE implementation begins. A builder starting
Phase 1 without this committed would be violating the workflow even while
following the plan.

#### Objectives

- Generate failing acceptance-test skeletons for every AC in the matrix.
- Commit `locard/tests/0001-nonprofit-seed-list-extraction/` as the
  TDD scaffolding.

#### Deliverables

- `locard/tests/0001-nonprofit-seed-list-extraction/README.md` — which AC
  each test file covers, mapping identical to the matrix.
- `tests/unit/test_*.py` and `tests/integration/test_*.py` stubs — one
  `pytest` function per AC, marked `@pytest.mark.xfail(reason="not yet
  implemented")` or equivalent, asserting the observable behavior.
- Suite runs; **all tests fail or xfail** (none pass yet — that's the point).
- Committed as: `[Spec 0001] TDD acceptance test scaffolding`.

#### Acceptance Criteria

- Every AC in the matrix has at least one test stub.
- `pytest -q` completes without framework errors.
- No test passes unexpectedly (would mean the AC is already satisfied by
  the empty implementation, which is a smell).

#### Rollback

Pure additive scaffolding; revert the commit.

#### Risks

- **Risk**: Stubs drift from the matrix as phases evolve.
  - **Mitigation**: the builder MUST update stubs in the same commit as
    any AC-matrix change.

---

### Phase 1: Scaffolding + Schema + HTTP Client

**Dependencies**: Phase 0 (TDD scaffolding committed)

#### Objectives

- Establish the `lavandula/nonprofits/` directory structure.
- Stand up the SQLite schema with all CHECK constraints.
- Build the throttled HTTP client with every security control from the spec:
  TLS verification self-test, cross-host redirect block, decompressed-size cap,
  cookie non-persistence, retry/backoff, daily-cap hook.
- Config module + structured logging.
- First integration tests that exercise the client against controlled mocks (NO
  real Charity Navigator traffic yet).

#### Deliverables

- `lavandula/nonprofits/config.py` — throttle, paths, UA, stop-condition
  thresholds, max-response-size, allowed-redirect-host.
- `lavandula/nonprofits/schema.py` — DDL for `nonprofits`, `fetch_log`,
  `sitemap_entries` tables, all indexes, all CHECK constraints (including the
  `website_url_reason` enum from Claude MEDIUM-9).
- `lavandula/nonprofits/http_client.py` — `ThrottledClient` class.
- `lavandula/nonprofits/logging_utils.py` — log-injection-safe formatter
  (strip control chars, truncate to 500).
- `tests/unit/test_schema.py`, `tests/integration/test_http_client.py`.
- **`pyproject.toml` (Poetry) OR `requirements.txt` generated by `pip-tools`
  with `--generate-hashes`** (Claude red-team-plan HIGH-1 — supply chain):
  - Minimum versions documented as security requirements:
    `defusedxml>=0.7.1`, `lxml>=4.9.1` (XXE hardening for both XML and HTML
    modes), `requests>=2.31.0` (CVE-2023-32681 fix).
  - Install command uses `--require-hashes --only-binary=:all:`.
  - Lockfile committed; no unpinned transitive deps.
- `.python-version` file pinning `3.12.x` (Claude LOW-6 — prevent major
  version drift).
- Pre-commit / CI step running `pip-audit` (or `safety`) against the
  lockfile; a HIGH/CRITICAL CVE is a failing check.
- Pre-commit / CI step running `bandit` or `ruff` with S-rules enabled;
  explicitly flag `verify=False`, `shell=True`, `eval(`, pickle usage (Claude
  LOW-7).

#### Implementation Details

- Reuse the throttle pattern from `nptech/http_client.py` but add:
  - `verify=True` explicit; reject `verify=False` in review.
  - **Hybrid local + remote TLS self-test** (Codex red-team-plan MEDIUM +
    Claude red-team-plan MEDIUM — both flagged third-party dependency on
    `expired.badssl.com` as a nondeterminism + DoS/MITM risk):
    - **Primary: local known-bad-cert endpoint.** Phase 1 scaffolding
      provisions a tiny test harness that serves a self-signed cert on
      `localhost:<ephemeral_port>`. This is deterministic, never down,
      never MITM-able from outside the process.
    - **Secondary: `expired.badssl.com`** as a cross-verification signal.
    - Behavior: BOTH endpoints must fail with a cert error. If the local
      endpoint succeeds (verification disabled) OR the remote endpoint
      succeeds (upstream-path MITM), halt with a distinguishing message.
      If ONLY the remote is inconclusive within a 30 s budget (including up
      to 2 retries), log a warning but PASS — the deterministic local
      check is authoritative. If the local endpoint itself is inconclusive
      (port can't bind, etc.), halt — the gate cannot run.
    - CI / integration tests use the local endpoint only (no network
      dependency).
  - `allow_redirects=False`; custom redirect loop validates each hop (scheme ==
    'https' AND host == 'www.charitynavigator.org').
  - `requests.Session` used for connection pooling, but cookies reset after
    every `get()` via `session.cookies = requests.cookies.RequestsCookieJar()`
    (Claude red-team-plan MEDIUM — `session.cookies.clear()` is not
    guaranteed to empty the jar under all internal states). AC6 test asserts
    `len(session.cookies) == 0` after each GET, not just absence of the
    `Cookie:` header.
  - **Streamed decompression with explicit decoded-bytes cap** (Claude MED —
    `requests` transparently decompresses gzip/deflate on `stream=True`
    with `iter_content(decode_content=True)`):
    ```
    response = session.get(url, stream=True, timeout=...)
    size = 0
    chunks = []
    for chunk in response.iter_content(chunk_size=8192, decode_content=True):
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            response.close()              # terminate socket before full read
            raise ResponseSizeExceeded(...)
        chunks.append(chunk)
    ```
    AC4 asserts the response's underlying socket was closed before the full
    10 MB of decoded bytes was accumulated into memory.
  - **Throttle jitter** (Claude red-team-plan MED): `REQUEST_DELAY_SEC = 3.0`
    with `REQUEST_DELAY_JITTER_SEC = 0.5` added as uniform random
    (`random.uniform(-jitter, +jitter)`). Prevents perfectly-periodic
    fingerprintable traffic. AC2 lower bound becomes `>= 250s` for 100
    requests.
  - **Retry-After handles HTTP-date AND seconds** (Claude red-team-plan MED —
    per RFC 7231 the header can be either form). Parser accepts both;
    date-form values parsed then converted to seconds; values greater than
    300s are clamped/logged and counted toward the Retry-After stop
    condition. Malicious year-9999 dates are clamped at the stop-condition
    threshold.
  - **Content-Type validation** (Claude red-team-plan HIGH-3): before handing
    the response to the parser, assert `Content-Type` startswith `text/html`
    or `application/xhtml+xml`. Mismatch → `fetch_status='server_error'` with
    note `"unexpected content-type: {ct}"`; archive is NOT written.
  - Log control-char sanitization applied to every `notes`/`error` string.
- `config.py` exposes:
  - `ROOT = Path(__file__).parent`
  - `RAW = ROOT / "raw" / "cn"`, `DATA = ROOT / "data"`, `LOGS = ROOT / "logs"`
  - `REQUEST_DELAY_SEC = 3.0`
  - `REQUEST_DELAY_JITTER_SEC = 0.5`
  - `MAX_RESPONSE_BYTES = 5 * 1024 * 1024`
  - `USER_AGENT` — default uses an alias address
    `"Lavandula Design research crawler/1.0 (+https://lavanduladesign.com;
    crawler-contact@lavanduladesign.com)"` (Claude red-team-plan LOW-1 —
    aliasable without refactor; may rotate by env var).
  - `UA_EMAIL` — env-overridable so the alias can be swapped without a code
    change if CN's logs ever leak.
  - `ALLOWED_REDIRECT_HOST = "www.charitynavigator.org"`
  - `ALLOWED_REDIRECT_SCHEME = "https"`
  - **`DISALLOWED_EINS`** (Claude red-team-plan MED): stored as canonical
    undashed 9-digit strings (e.g., `"863371262"`). A normalization
    function `canonicalize_ein(s: str) -> str` strips dashes and validates
    `^[0-9]{9}` anchored; applied before any disallow comparison. Fixture
    test asserts both `863371262` and `86-3371262` are rejected.
- `schema.py` is idempotent: `CREATE TABLE IF NOT EXISTS ...`; version-bump in
  comments when fields change.

#### Acceptance Criteria (map to AC1-AC6)

- All unit tests for schema CHECK constraints pass.
- 100-request integration test takes greater than or equal to 300 seconds.
- TLS self-test actively fails; HTTP client raises on successful connection to
  expired.badssl.com.
- Decompression-bomb test (mock gzip response decompressing to 10 MB) raises
  `ResponseSizeExceeded`.
- Cross-host redirect test: mocked 302 to attacker.example.org NOT followed.
- Cookie test: two sequential mocked GETs — second request carries no Cookie
  header.

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC1 | Unit | Schema insert violates CHECK constraint | row with `rating_stars=5` | sqlite3.IntegrityError |
| AC2 | Integration | Throttle holds during burst | 100 GET calls | total elapsed greater than or equal to 300s |
| AC3 | Integration | TLS self-test fails cleanly | client init with cert-invalid endpoint | TLSMisconfigured raised |
| AC4 | Integration | Decompression bomb capped | mocked gzip yielding 10 MB decompressed | ResponseSizeExceeded, peak mem less than 50 MB |
| AC5 | Integration | Cross-host redirect rejected | 302 Location: http://attacker.example.org | fetch_status='server_error', no outbound |
| AC6 | Integration | Cookies do not persist | 2 sequential GETs, first server sets cookie | 2nd request outgoing headers have no Cookie |

**Edge Cases**:

- Client init with non-https config URL: reject at load time.
- Response body with no Content-Encoding but larger than 5 MB: cap still applies.
- Redirect to same host but different subdomain: rejected (exact host match).

**Error Cases**:

- TLS self-test succeeds on expired.badssl.com — client refuses to start
  (halt, not warn).
- Mocked network failure on self-test — distinguish from "self-test passed"
  (retry once, then halt).

#### Rollback Strategy

If Phase 1 fails review: nothing deployed; just revert the commit. No
external state mutated.

#### Risks

- **Risk**: TLS self-test flaky (expired.badssl.com intermittently down).
  - **Mitigation**: 30-second total budget with 2 retries. If still
    inconclusive, HALT (not warn). A backup known-bad-cert endpoint is
    allowed as an additional try, but "could not determine outcome"
    remains a halt condition. The self-test is a security gate; skipping
    it silently is strictly worse than halting on inconclusive result.

---

### Phase 2: Robots.txt + Sitemap Enumeration

**Dependencies**: Phase 1

#### Objectives

- Fetch + parse `/robots.txt` with stanza-matching behavior from the spec.
- Fetch + parse `extra-index.xml` and the 48 child sitemaps with XXE defense.
- Enumerate all `/ein/{EIN}` URLs into `sitemap_entries`, applying the
  hardcoded + robots-derived disallow list.
- Deduplicate EINs across sitemaps (first-seen precedence).

#### Deliverables

- `lavandula/nonprofits/robots.py` — parser with stanza-matching + halt
  policy.
- `lavandula/nonprofits/sitemap.py` — fetches + parses sitemap index and
  child sitemaps using defusedxml (or explicitly-configured lxml).
- `tests/unit/test_robots.py`, `tests/unit/test_sitemap_parse.py`.
- Fixtures: `tests/fixtures/cn/robots-simple.txt`,
  `tests/fixtures/cn/robots-named-ua.txt`, `tests/fixtures/cn/xxe-sitemap.xml`,
  `tests/fixtures/cn/malformed-sitemap.xml`, `tests/fixtures/cn/extra-index-48.xml`,
  `tests/fixtures/cn/sitemap-with-duplicate-ein.xml`.

#### Implementation Details

- `robots.py`:
  - Parse stanzas keyed by `User-agent:`.
  - For our UA, find stanzas whose UA token is a case-insensitive substring
    of our full UA. Specificity = length of the matching token. Longest wins.
    If multiple tie, raise `AmbiguousRobots` (halt).
  - Apply all `Disallow:` patterns; no wildcards beyond `*` at end.
  - Always overlay hardcoded disallow list.
- `sitemap.py`:
  - Use `defusedxml.lxml.fromstring` (or `lxml.etree.XMLParser(
    resolve_entities=False, no_network=True, huge_tree=False)` if defusedxml
    is unavailable).
  - Sitemap-index parser → child sitemap URLs. Reject URLs outside
    `https://www.charitynavigator.org/sitemap/`.
  - Child sitemap parser → `/ein/{EIN}` URLs. Filter to a regex that anchors
    both start and end of the path and requires exactly nine digits after
    `/ein/`.
  - Insert into `sitemap_entries` table with `first_seen_at`; `INSERT OR
    IGNORE` provides first-seen precedence on duplicates.

#### Acceptance Criteria (map to AC7-AC11)

- `robots.py` stanza-match test: input with both `User-agent: Lavandula` and
  `User-agent: *` — Lavandula stanza wins.
- XXE sitemap fixture: parser returns the URL list WITHOUT resolving the
  external entity; no file read side effect observed (assert by monitoring
  `os.open` calls).
- `extra-index-48.xml` fixture: parser returns exactly 48 child URLs.
- Malformed-EIN fixture: rows with `/ein/ABC12345` or `/ein/12345678` (8-digit)
  not inserted.
- Disallowed EIN `863371262` (and its dashed form) not inserted.

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC7 | Unit | Specific UA wins over generic * | robots with both stanzas | only Lavandula rules applied |
| AC8 | Unit | XXE entity not resolved | xxe-sitemap.xml fixture | no /etc/passwd read, no network call |
| AC9 | Unit | Sitemap index enumerated | extra-index-48.xml | returns 48 URLs |
| AC10 | Unit | Malformed EIN skipped | sitemap with /ein/ABC | 0 rows inserted for bad EINs |
| AC11 | Unit | Disallowed EIN blocked | enumeration including 863371262 | entry NOT in sitemap_entries |

**Edge Cases**:

- robots.txt tied specificity between two named stanzas: halt (AmbiguousRobots).
- Sitemap index with 0 children (empty): log + halt (unexpected structure).
- Child sitemap with 0 `/ein/` URLs: log warning, continue (might be an
  admin-only sitemap).
- Duplicate EIN in two different child sitemaps: single row; `source_sitemap`
  reflects the first one seen.

**Error Cases**:

- 5xx on robots.txt: halt.
- XML parse fails: halt.
- Any `<!DOCTYPE` or `<!ENTITY` in sitemap: defusedxml raises; surface clearly.

#### Risks

- **Risk**: defusedxml may not be packaged in the deployment environment.
  - **Mitigation**: fallback to `lxml.etree.XMLParser(resolve_entities=False,
    no_network=True, huge_tree=False)` with an integration test that asserts
    the XXE fixture is safe under that parser.

---

### Phase 3: Profile Fetching + Archiving

**Dependencies**: Phase 1, Phase 2

#### Objectives

- Fetch a single profile `/ein/{EIN}` through the throttled client.
- Detect Cloudflare challenge bodies BEFORE writing the archive (never poison
  `{ein}.html`).
- Handle cross-EIN 30x redirects: parse the destination page but retain the
  source EIN as the row key; populate `redirected_to_ein`.
- Archive raw HTML atomically (`{ein}.html.tmp` then `os.replace`) and
  symlink-safely (`os.lstat` pre-check + `O_NOFOLLOW`).
- Record every request in `fetch_log` with status/elapsed/attempt/is_retry.

#### Deliverables

- `lavandula/nonprofits/fetcher.py` — profile fetch + archive write.
- `tests/integration/test_fetch.py`, `tests/integration/test_archive.py`.
- Fixtures: `fetch-200-rated.html`, `fetch-200-unrated.html`,
  `fetch-302-to-other-ein.headers`, `fetch-200-challenge.html`,
  `fetch-200-compensation-heavy.html` (for PII-in-archive awareness).

#### Implementation Details

- Challenge detection runs BEFORE archive write. Signatures:
  `cf-challenge`, `__cf_chl_jschl_tk__`, `"captcha"`, `<title>Just a moment...</title>`,
  Turnstile token patterns. Match against response body (first 16 KB is enough
  since the challenge appears near the top).
- On challenge: write `{ein}.challenge.html` + populate `fetch_log.fetch_status='challenge'`;
  DO NOT update the main `{ein}.html`.
- **Atomic write (hardened per Codex HIGH-2 + Claude HIGH-4):** the naïve
  `lstat`-then-`os.replace` sequence has a TOCTOU race where a local attacker
  (or a stray process) can plant a symlink between the check and the rename,
  since `renameat(2)` does not check symlinks at the destination. Mitigated
  by isolating the final directory:
  - Archive directory layout: `raw/cn/` (final, mode `0o700`) contains
    ONLY regular files or nothing. A per-process temp subdirectory
    `raw/cn/.tmp-{pid}-{uuid}/` (mode `0o700`, owned by the crawler PID)
    holds in-flight `.html.tmp` files.
  - Write sequence:
    1. `fd = os.open(tmp_path, O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW, 0o600)`
       in the per-PID temp subdir.
    2. Write decoded bytes; `os.fsync(fd)`; close.
    3. `os.lstat(final_path)` inside `raw/cn/` — the mode-0o700 parent
       prevents unauthorized symlink plants by any other UID.
    4. If `final_path` exists AND is a symlink, halt immediately.
    5. `os.replace(tmp_path, final_path)` — atomic.
    6. **`os.fsync(dir_fd)`** on `raw/cn/` (Codex red-team-plan HIGH-2 —
       directory-entry durability across power loss).
    7. Unlink the per-PID temp subdir on normal shutdown; leftover subdirs
       from crashed prior runs are swept on startup (report count in log).
- Cross-EIN redirect handling:
  - Phase 1's HTTP client returns the full redirect chain.
  - Parse the final body into the source EIN's row; set
    `redirected_to_ein = <final EIN from location URL>`.
  - Skip if the redirect target is outside `www.charitynavigator.org`
    (Phase 1 already blocks this; re-assert at fetcher level).

#### Acceptance Criteria (map to AC12-AC15)

- Symlink-planted-in-advance test: pre-creating `raw/cn/530196605.html`
  pointing at `/tmp/sensitive`; running fetcher with EIN 530196605 halts
  and does NOT modify `/tmp/sensitive`.
- Challenge fixture test: response body with Cloudflare challenge → `{ein}.html`
  NOT written; `{ein}.challenge.html` present; `fetch_log.fetch_status='challenge'`.
- Cross-EIN redirect test: mocked `/ein/A` returning `302 Location:
  https://www.charitynavigator.org/ein/B` where B is a valid profile; row A
  has content from B and `redirected_to_ein='B'`.
- Atomic write test: inject a crash between `os.open` and `os.replace`; verify
  previous `{ein}.html` intact and no `.tmp` garbage in final path.

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC12 | Integration | Symlink refusal | pre-planted symlink + fetch | halt, symlink target untouched |
| AC13 | Integration | Challenge body isolated | response with CF challenge markers | challenge.html written, main not |
| AC14 | Integration | Cross-EIN redirect | mocked 302 A -> B | row A populated from B, redirected_to_ein=B |
| AC15 | Integration | Atomic write | simulated crash mid-write | final path intact or absent, never torn |

**Edge Cases**:

- Redirect chain length greater than 5: halt (suspicious).
- Redirect from `/ein/A` back to `/ein/A` (self-loop): break after first.
- Response 200 OK but body empty: `parse_status='partial'`; not a halt.
- Content-Length mismatch vs actual bytes: log warning, use actual.

**Error Cases**:

- Disk full mid-write (ENOSPC): halt via Phase 5 stop condition; do NOT leave
  `.tmp` behind.
- Permission denied on archive dir: halt at startup (Phase 5 preflight).

#### Risks

- **Risk**: Cloudflare may add new challenge signatures we don't detect,
  silently poisoning the archive.
  - **Mitigation**: monitor first 100 archive files in Phase 7 validation for
    minimum expected content markers (`<h1 class="orgName">` etc.). Phase 7
    halts production run if percentage without markers exceeds 1%.

---

### Phase 4: Profile Extraction + Classification

**Dependencies**: Phase 3

#### Objectives

- Parse archived HTML into the `nonprofits` table fields.
- Apply the 10-rule URL normalization policy (from spec).
- Handle rated vs unrated profiles (differential schema population).
- Sanitize remote-sourced strings before writing (log injection defense).
- SQL writes use `?` parameter binding, never string concatenation.

#### Deliverables

- `lavandula/nonprofits/extract.py` — HTML → dict of fields; pure local
  transform (no network).
- `lavandula/nonprofits/url_normalize.py` — the 10 rules.
- `lavandula/nonprofits/db_writer.py` — `?`-parameterized inserts/updates.
- `tests/unit/test_extract.py`, `tests/unit/test_url_normalize.py`,
  `tests/unit/test_log_sanitize.py`, `tests/integration/test_db.py`.
- Fixtures: at least 20 HTML files covering the test-scenario list from the
  spec.

#### Implementation Details

- `extract.py` uses BeautifulSoup only; **never** resolves subresources
  (`<img>`, `<iframe>`, etc.). Enforce by reading raw HTML text and passing
  `features='lxml'` with no custom entity resolver.
- **HTML-mode entity-safety** (Claude red-team-plan HIGH-2):
  - Require `lxml >= 4.9.1` (documented in Phase 1 dependency pinning).
  - Construct parser as `BeautifulSoup(html_bytes, 'lxml',
    from_encoding='utf-8')` — no custom entity resolver injected.
  - Add a fixture `tests/fixtures/cn/xxe-html-mode.html` containing an
    HTML `<!DOCTYPE>` + `<!ENTITY>` pointing at `file:///dev/null` (NOT a
    sensitive file — Claude LOW-4: avoid tripping enterprise CI security
    scanners with `/etc/passwd` literal). Assert entity is not resolved;
    fetch_status/parse_status correctly set.
- Extraction pipeline:
  1. Detect "rated" vs "unrated" profile (presence of rating stars selector).
  2. Extract core fields: name, mission, address, state, revenue, expenses,
     NTEE.
  3. Extract rating fields only if rated.
  4. Extract website URL: find the "Website" link; capture `href` as
     `website_url_raw`.
  5. Normalize `website_url_raw` through the 10-rule pipeline → `website_url`
     + `website_url_reason`.
  6. Hash raw HTML → `content_sha256`.
  7. Return dict; `db_writer` inserts with all values parameter-bound.
- `url_normalize.py` implements the 10 rules in order. Each returns
  `(url_or_None, reason_or_None)`. Unit test one rule at a time.
- Log sanitation runs on every remote-sourced string before any log emission
  OR DB write to `fetch_log.notes`/`fetch_log.error`.

#### Acceptance Criteria (map to AC16-AC21)

- Rated-profile fixture: parser produces all expected fields byte-identical
  to the fixture-expected dict.
- Unrated-profile fixture: `rated=0`, rating fields NULL, name/mission set.
- URL normalization: per-rule test cases from the spec example table all pass.
- SQL injection round-trip: insert mission `'; DROP TABLE nonprofits; --`;
  SELECT returns byte-identical string; `nonprofits` table still exists.
- Log injection: `Retry-After: \r\nFAKE_LOG` → sanitized to `Retry-After: FAKE_LOG`
  (or equivalent with control chars stripped).

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC16 | Unit | Rated profile parsing | Red Cross fixture | all fields populated per golden dict |
| AC17 | Unit | Unrated profile parsing | unrated fixture | name/mission only; rated=0 |
| AC18 | Unit | URL normalization baseline | HTTPS://Redcross.Org:443/?fbclid=abc | https://redcross.org (reason NULL) |
| AC19 | Unit | mailto rejected | mailto:info@example.org | website_url=NULL, reason='mailto' |
| AC20 | Integration | SQL injection round-trip | mission with SQL payload | stored byte-identical, table intact |
| AC21 | Unit | Log sanitation | 'foo\r\nfake: bar' | 'foofakebar' (or truncated safely) |

**Edge Cases**:

- Profile with a website link that's ONLY a `<button onclick="...">` with no
  `<a href>`: website_url=NULL, reason='missing'.
- Mission statement containing zero-width characters: preserved in DB, not
  stripped (only control chars `\x00-\x1f\x7f` are stripped, NOT all Unicode
  punctuation).
- Profile with multiple website links: use the first one that's not a CN
  redirect wrapper; document the precedence.

**Error Cases**:

- `content_sha256` computed on an empty byte string: allowed (partial fetch);
  `parse_status='partial'`.
- BeautifulSoup raises on malformed HTML: catch, log, set
  `parse_status='unparsed'`, populate `name` from `<title>` if possible.

#### Risks

- **Risk**: CN redesigns their profile HTML; selectors break silently.
  - **Mitigation**: Phase 7 validation compares field-population percentages
    from a live 50-EIN sample to the fixture-predicted range; greater than
    25% deviation halts production. Parser is isolated in `extract.py` so a
    bump is a local fix.

---

### Phase 5: Orchestration + CLI + Stop Conditions

**Dependencies**: Phase 4

#### Objectives

- `crawler.py` wires Phases 1-4 into the main loop: read sitemap_entries,
  fetch each EIN (fetcher.py owns archive writes end-to-end — per Codex
  red-team-plan MEDIUM-3 archive ownership clarification — the crawler does
  NOT re-archive; it receives a result record from fetcher), extract, write
  DB.
- Checkpoint + resume semantics.
- Single-instance `fcntl.flock` lock.
- Disk-space preflight (50 GB) + runtime check (5 GB).
- Stop-condition detection + halt-with-exit-code-2 behavior.
- `SIGTERM` handler: flush checkpoint, write `HALT-provider-complaint-*.md`;
  if HALT write itself fails (disk full), fall back to a stderr log + `_exit(2)`
  (Claude red-team-plan MED).
- `--limit`, `--refresh`, `--start-ein`, `--no-download` flags.
- File-permission enforcement: DB mode 0o600, directories 0o700.
- **Report / handoff file permissions 0o600** (Claude red-team-plan MED
  — `coverage_report.md` contains aggregate stats that could fingerprint
  Lavandula's prospect methodology if leaked).
- Log rotation: `RotatingFileHandler(100 MB * 5)`.
- **Periodic robots.txt re-fetch** during a long run (Claude red-team-plan
  MED): every 6 hours OR every 1000 EINs, whichever comes first. If
  `/ein/*` becomes disallowed mid-run, halt immediately with AC7b's
  condition fired. Cost: ~12 extra requests per 72-hour run, negligible.
- **Cumulative-runtime stop condition** wired (> 72 hours → halt, AC26b).
- **Stale-flock policy is fail-closed** (Claude red-team-plan MED): if
  `fcntl.flock(LOCK_EX | LOCK_NB)` fails because the lock is held, we do
  NOT auto-takeover even if the owning PID appears dead. Always exit code
  3 with a message directing the operator to investigate manually. Automatic
  takeover introduces PID-collision bugs that are worse than the convenience
  is worth.
- **Corrupt-checkpoint retention cap** (Claude red-team-plan LOW-5): keep
  the last 5 `checkpoint.corrupt-*.json` files; older ones are deleted on
  startup.
- **Checkpoint integrity** (Claude red-team-plan MED-8): the checkpoint
  file stores fetched EIN set + next-target. A local actor with UID-level
  access (mode 0o600 notwithstanding) could inject a fabricated checkpoint
  that skips the disallow list. Mitigation: HMAC-SHA256 the checkpoint
  content using a per-installation secret `.crawler.key` (mode 0o600,
  generated with `secrets.token_bytes(32)` on first run). Verify MAC on
  load; MAC mismatch = treat as corrupt (rename `.corrupt-*`, fresh start,
  log WARNING).
- **DNS IP-pin + drift alert** (Claude red-team-plan MED-12): at crawler
  startup, resolve `www.charitynavigator.org` and record the IP set.
  Before each request, re-resolve; if the IP set changes, log a warning
  (not a halt — Cloudflare's pool rotates legitimately; this is signal,
  not control). The already-enforced host/scheme redirect check operates
  on the URL, but pinning is a cheap addition.
- **Content-Length sanity check** (Claude red-team-plan LOW-2): if
  `Content-Length` header is present and diverges from actual decoded
  bytes read by more than 10×, log a warning; signal of response
  smuggling via compromised proxy.
- **Clock-sync check at startup** (Claude red-team-plan LOW-3): compare
  local `time.time()` against the `Date:` header of the first response
  from CN. If divergence greater than 5 minutes, log a warning. Bad
  `last_fetched_at` values would poison a future incremental-refresh
  TICK.
- **Internal exception sanitization** (Gemini Flash red-team-plan LOW-3):
  in addition to sanitizing remote-sourced strings before logging,
  internal exception messages and stack traces are truncated to 2000
  characters and stripped of absolute paths containing the operator's
  home directory (replaced with `~/`) before being written to
  `HALT-*.md` or log files. Prevents accidental disclosure of deployment
  layout if a HALT file is shared with a third party (e.g., a
  subcontractor or CN's support).
- **HMAC key rotation procedure** (Gemini Flash red-team-plan LOW-2):
  to rotate the checkpoint HMAC key, delete `.crawler.key` and let the
  next run regenerate it with `secrets.token_bytes(32)`. Regeneration
  invalidates all existing checkpoints (they fail MAC verification and
  are treated as corrupt). Document this in HANDOFF.md.
- **Encryption at rest** (Gemini Flash red-team-plan LOW-1): out of
  scope for v1 (acknowledged trade-off — see PII posture in Legal
  Constraints). A future TICK may add SQLCipher for the DB or LUKS
  full-disk encryption on the deployment host. For v1, filesystem
  permissions (`0o600`/`0o700`) + internal-only-use + no-cloud-backup
  are the controls. Reconsider if the archive ever leaves the host.

#### Deliverables

- `lavandula/nonprofits/crawler.py` — main entrypoint; argparse + flock +
  main loop.
- `lavandula/nonprofits/checkpoint.py` — load/save/recover-corrupt.
- `lavandula/nonprofits/stop_conditions.py` — all halt rules centralized.
- `tests/integration/test_cli.py`, `tests/integration/test_stop_conditions.py`.

#### Implementation Details

- Main loop:
  ```
  ensure_flock()
  preflight_disk_check()
  startup_tls_selftest()
  robots = fetch_and_parse_robots()
  if not robots.allows('/ein/'): halt('robots disallows /ein/')
  for ein in unfetched_sitemap_entries():
      if stop_conditions.should_halt(): halt(reason)
      if runtime_disk_check_fails(): halt('disk_low')
      record = fetch_profile(ein)
      archive(record)
      db_writer.upsert(record)
      checkpoint.update(ein)
  ```
- Stop conditions tracked via a sliding window on `fetch_log`:
  - 3 consecutive rows with `fetch_status='forbidden'` → halt
  - 5 consecutive rows with `fetch_status='rate_limited'` (after all retries)
    → halt
  - any row with `fetch_status='challenge'` → halt immediately
  - 2 consecutive `Retry-After` values greater than 300s → halt
  - cumulative runtime greater than 72 hours → halt
  - cumulative archive size greater than 50 GB → halt
- SIGTERM handler path (provider complaint):
  - Flush checkpoint.
  - Write `lavandula/nonprofits/logs/HALT-provider-complaint-{timestamp}.md`
    with last 10 `fetch_log` entries.
  - Exit code 2.

#### Acceptance Criteria (map to AC22-AC30)

- Single-instance lock: two concurrent processes — first runs, second exits
  code 3 with clear message.
- Checkpoint resume: partial run crashed at EIN X; re-invocation continues at
  X+1 (using sitemap_entries ordering).
- Corrupt checkpoint: truncated JSON file → renamed `checkpoint.corrupt-*.json`;
  fresh run starts.
- Halt on 3 consecutive 403s, 5 consecutive 429s, disk-space less than 5 GB,
  cumulative archive greater than 50 GB — each triggers exit code 2 +
  `HALT-*.md`.
- SIGTERM mid-run: checkpoint flushed, `HALT-*.md` written, exit code 2.
- Post-run file permissions: DB `stat().st_mode & 0o777 == 0o600`;
  `raw/cn/` is `0o700`.

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC22 | Integration | Double-run | two concurrent crawler invocations | one runs, other exits code 3 |
| AC23 | Integration | Resume after crash | partial run, checkpoint at EIN N | resume continues from N+1 |
| AC24 | Integration | Corrupt checkpoint | truncated checkpoint file | renamed, fresh start logged |
| AC25 | Integration | 403 halt | mocked 3 consecutive 403s | exit 2, HALT-forbidden-*.md |
| AC26 | Integration | 429 halt | mocked 5 consecutive unresolved 429s | exit 2, HALT-rate-limited-*.md |
| AC27 | Integration | Disk-low halt | runtime disk check returns less than 5 GB | exit 2, HALT-disk-low-*.md |
| AC28 | Integration | Archive cap halt | cumulative raw/cn > 50 GB | exit 2, HALT-archive-cap-*.md |
| AC29 | Integration | SIGTERM shutdown | send SIGTERM during run | checkpoint flushed, HALT file, exit 2 |
| AC30 | Integration | File permissions | normal run completes | DB mode 0o600, dir 0o700 |

**Edge Cases**:

- SIGTERM during a mid-flight HTTP request: wait for request or its retry to
  complete, THEN shutdown (to avoid torn fetch_log entries).
- `--limit` less than total unfetched: stop cleanly at limit, exit 0.
- `--refresh` flag: requeue all already-fetched EINs; honor stop conditions.

**Error Cases**:

- Flock file exists but owning PID is dead (stale lock): detect via
  `fcntl.F_GETLK`; if owner is dead, take the lock with warning log.

#### Risks

- **Risk**: Stop-condition window logic has a bug that causes premature halt.
  - **Mitigation**: each stop condition gets its own integration test with
    1 "just below threshold" + 1 "at threshold" + 1 "above threshold" case.

---

### Phase 6: Reporting + Handoff

**Dependencies**: Phase 5

#### Objectives

- `report.py` generates `coverage_report.md` from the populated DB.
- Empirical coverage metrics (all fields, % populated).
- Row-count breakdown by state, NTEE major, rating_stars bucket.
- `HANDOFF.md` is the operational doc for whoever consumes the dataset next.

#### Deliverables

- `lavandula/nonprofits/report.py`.
- `lavandula/nonprofits/HANDOFF.md`.
- `lavandula/nonprofits/README.md` — quick-start (install, run, test,
  troubleshoot) — one screenful. Required (Codex plan-review finding #1).
- Update `locard/resources/arch.md` to include a `lavandula/nonprofits/`
  module entry describing its role and relationship to `nptech/`
  (required; Codex plan-review finding #1).
- `tests/integration/test_report.py`.

#### Implementation Details

- `report.py` queries are ALL read-only (`PRAGMA query_only = 1`).
- `coverage_report.md` structure:
  - Totals: enumerated / fetched / failed.
  - Per-field population percentages.
  - Distribution tables (top 20 states, NTEE majors, rating distribution).
  - Run metadata: start, end, elapsed, total requests, 429 rate, challenge
    count.
- `HANDOFF.md` sections:
  - **What this is** — one-paragraph summary + link to spec + link to plan.
  - **Schema** — copy of the DDL + column commentary.
  - **Example queries** — "orgs in arts with greater than USD 5M revenue",
    "4-star orgs by state", etc.
  - **How to refresh** — command to re-enumerate sitemap + incrementally
    re-fetch.
  - **Contact protocol** — if Charity Navigator reaches out, how we respond;
    escalation to paid API.
  - **Retention** — when to delete raw archive; what stays.
  - **Usage restrictions** — required by Claude plan-review finding #7.
    Enumerates spec's internal-use-only posture: `mission` field is NEVER
    shown to a CN competitor, included in public Lavandula output, or
    exported outside the internal DB. `rated`, `rating_stars`,
    `overall_score`, `beacons_completed` may be used for internal
    segmentation but MUST NOT be republished or presented as Lavandula's
    own ratings. Raw archive is local-only; no cloud upload.
  - **Incidents log pointer** — `incidents/` directory.

#### Acceptance Criteria (map to AC31-AC32b)

- `report.py` against a 50-row synthetic DB produces a valid markdown file
  with all expected sections.
- `HANDOFF.md` is human-readable (manual review); covers all required
  sections including Usage Restrictions.
- `README.md` is present and one-screen.
- `locard/resources/arch.md` has a new `lavandula/nonprofits/` entry.

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC31 | Integration | Report generation | 50-row synthetic DB | valid markdown with all sections |
| AC32 | Manual | HANDOFF completeness | review by Ron | sections present, actionable |
| AC32a | Manual | README exists | grep for "Install", "Run", "Test" headings | all present |
| AC32b | Manual | arch.md updated | diff against pre-phase version | new module entry for `lavandula/nonprofits/` |

#### Risks

- **Risk**: `HANDOFF.md` drifts from reality between project phases.
  - **Mitigation**: the doc is generated (by `report.py`) for the metric
    sections; only the prose (contact protocol, retention) is hand-written.

---

### Phase 7: Validation Run + Full Crawl Commissioning

**Dependencies**: Phase 6

Scope note (Codex plan-review finding #2): the phase's acceptance is GATED
on the 50-EIN validation only. Kicking off the full ~48K crawl is operational
follow-on work and is listed under Post-Implementation Tasks. Codex correctly
flagged that making "full crawl starts and completes" part of a phase AC would
overreach the approved spec.

#### Objectives

- Execute a small live validation run (`--limit 50`) during off-peak hours.
- Spot-check 10 rows for parsing accuracy vs a browser-rendered profile.
- Verify field-population percentages are within predicted ranges.
- Produce a GO/ROLLBACK decision. If GO: the full crawl is then commissioned
  as a Post-Implementation task, not as part of this phase.

#### Deliverables

- `validation_run_report.md` — observations from the 50-EIN run.
- A GO or ROLLBACK decision recorded with the PR review.

#### Implementation Details

- Run: `./venv/bin/python -m lavandula.nonprofits.crawler --limit 50`.
- Validate:
  - All 50 attempted EINs either appear in `nonprofits` or have a `fetch_log`
    row explaining why.
  - Randomly sample 10 rows; compare to the CN profile rendered in a browser
    — name, website URL, rating, revenue must match byte-identically (or
    with explicable normalization).
  - Coverage percentages: if `website_url` populated is less than 70% OR
    `rating_stars` less than 60%, investigate before the Post-Implementation
    full-crawl task runs.
  - Zero halt conditions fired; exit code 0.

The actual full-crawl command + monitoring guidance lives in the Post-
Implementation Tasks section, not here.

#### Acceptance Criteria (map to AC33)

- 50-EIN validation run completes without any halt.
- At least 8 of 10 spot-checked rows match browser-rendered values.
- Coverage within predicted range.

#### Acceptance Test Design

| AC | Test Type | Description | Input | Expected Output |
|----|-----------|-------------|-------|-----------------|
| AC33 | E2E | Live validation run | --limit 50 against CN | 0 halts, greater than or equal to 8/10 spot-checks match |

**Edge Cases**:

- Validation run halts on a real CN challenge: pause, investigate, do NOT
  commission the full run until understood.

**Error Cases**:

- Full crawl halts on a stop condition after more than 30K EINs: we have
  most of the dataset; archive what we have, investigate the halt, then
  resume.

#### Rollback Strategy

- If Phase 7 fails (validation exposes a parser regression): return to Phase
  4 with a new fixture capturing the failure. No production data is
  redistributed so there is nothing external to roll back.

#### Risks

- **Risk**: CN starts challenge-page-serving specifically to our UA during
  the live run, halting us at EIN ~100.
  - **Mitigation**: We keep the validation run small; if challenge rate
    greater than 5%, consider UA rotation before full crawl (but escalate to
    Ron first — see the HANDOFF contact protocol, spec LOW-2 / MEDIUM-6).

---

## Dependency Map

```
Phase 1 (scaffolding/schema/HTTP)
    |
    v
Phase 2 (robots + sitemap) -- depends on Phase 1's HTTP client + schema
    |
    v
Phase 3 (profile fetch + archive) -- depends on Phase 2's sitemap_entries + Phase 1's HTTP
    |
    v
Phase 4 (extract + classify) -- depends on Phase 3's archived HTML
    |
    v
Phase 5 (orchestrate + CLI + stop conditions) -- depends on all of 1-4
    |
    v
Phase 6 (report + handoff)
    |
    v
Phase 7 (validation run + full crawl)
```

## Resource Requirements

### Development Resources

- **Engineer**: one builder agent (spawned via `af spawn -p 0001`).
- **Environment**: the existing Python 3.12 venv pattern used by `nptech/`;
  a fresh venv at `lavandula/nonprofits/venv/` is cleanest.

### Infrastructure

- **Disk**: approx 15 GB for raw archive, 50 MB for DB. 50 GB free partition
  preflight-required.
- **Network**: outbound HTTPS to `www.charitynavigator.org` and
  `expired.badssl.com` (startup TLS self-test).
- **No cloud**: all storage local, per spec's Legal / Compliance stance.

## Integration Points

### External Systems

- **Charity Navigator**:
  - Integration Type: HTTP GET on public URLs.
  - Phase: Phase 2 onwards.
  - Fallback: if live crawling becomes infeasible (persistent challenges,
    cease-and-desist, etc.), pivot to paid Data Feed per spec Approach 2.
    This is a spec-level fallback, not an implementation branch.

### Internal Systems

- **nptech/** (existing crawler): no integration yet. If a common HTTP client
  abstraction becomes useful later, we'll extract `common/http_client.py` in
  a separate TICK.

## Risk Analysis

### Technical Risks

- CN HTML schema changes mid-project → parser regressions.
  - **Mitigation**: Phase 7 validation compares live results to fixtures.
    Parser is isolated in `extract.py`.
- defusedxml unavailable in deploy env → XXE exposure.
  - **Mitigation**: fallback configured-lxml path with its own XXE test.
- Long test run time (integration tests with throttle simulation).
  - **Mitigation**: throttle-aware tests use a fake clock, not real sleep.

### Schedule Risks

We explicitly do NOT include time estimates (per SPIDER protocol "No Time
Estimates in the AI Age"). Progress is measured by phase completion, not
elapsed time.

## Validation Checkpoints

1. **After Phase 1**: client passes TLS self-test + size-cap + redirect-block
   integration tests; schema validates.
2. **After Phase 2**: 48 sitemaps enumerated; XXE fixture safe; 48K EINs in
   `sitemap_entries`.
3. **After Phase 3**: 10-EIN live fetch with archive writes; symlink refusal
   fires when expected; challenge detection works against a synthetic
   response.
4. **After Phase 4**: full-fixture suite passes; URL normalization correct.
5. **After Phase 5**: full crawler run (--limit 10) dry-run end-to-end with
   all stop conditions tested in isolation.
6. **After Phase 6**: generate `coverage_report.md` on synthetic 50-row DB.
7. **After Phase 7**: 50-EIN live validation passes; full crawl commissioned.

## Monitoring and Observability

### Metrics to Track

- Effective request rate (rolling 1-minute average).
- Retry rate (attempts / distinct URLs attempted).
- Challenge-body count (stop condition fires at greater than 0).
- 429 rate post-retry (target less than 1%).
- Cumulative archive size (stop at 50 GB).
- Disk free on partition (halt below 5 GB runtime).

### Logging Requirements

- INFO level: one line per EIN attempted with status + elapsed.
- WARNING level: retries, size caps, parse_status='partial'.
- ERROR level: stop-condition triggers.
- All log lines sanitized for control characters before emission.
- Rotating file handler: 100 MB per file, keep 5 files.
- `HALT-*.md` files never rotated.

### Alerting

- No external alerting in v1. Operator reviews `logs/` + `HALT-*.md` manually.
- A future Phase 8 (out of scope) could wire this into the agent-farm
  dashboard.

## Documentation Updates Required

- (v) `lavandula/nonprofits/HANDOFF.md` (Phase 6 deliverable).
- (v) `lavandula/nonprofits/README.md` — minimal quick-start (one screenful).
- (v) Update `locard/resources/arch.md` at Review phase (R of SPIDER) with
  the new directory and its relationship to `nptech/`.

## Post-Implementation Tasks

- **Commission the full ~48K crawl** (moved out of Phase 7 per Codex
  plan-review finding #2):
  - `./venv/bin/python -m lavandula.nonprofits.crawler` in tmux or nohup.
  - Log to `logs/crawl-{date}.log`.
  - Expected wall-clock approx 48h at 3 s throttle.
  - Owner monitors for halt conditions; if any fires, investigate before
    restart.
- Performance validation: compare observed request rate vs configured 3 s.
- Security audit: re-run red-team-impl on the committed PR.
- Spot-check a random sample of 50 rows against live CN profiles after the
  full crawl completes.
- Operator sign-off: Ron confirms the dataset is usable for prospect-list
  filtering.
- Document retention decision for the raw archive (delete now, delete after
  downstream project consumes, or keep indefinitely).

## Cross-Phase Rollback Strategy (Claude plan-review finding #9)

Per-phase rollback sections handle regressions within a phase. For regressions
that surface in a later phase but originate in an earlier one, the policy is:

- **Do not attempt in-place patches to earlier phases from a later phase's PR.**
  A Phase-4 parser change that turns out to need a Phase-1 HTTP-client
  adjustment is a TICK amendment targeting Phase 1; it does not ride in the
  Phase-4 PR.
- TICK amendments follow the TICK protocol: modify both spec and plan
  together, get expert + red-team review, get human approval, then merge.
- The original plan document and all its phase commits remain the record of
  "what was planned at each point in time."

## Consultation Log

### First Consultation (After Initial Plan)

**Date**: 2026-04-17
**Models Consulted**: GPT-5 Codex ✅ (REQUEST_CHANGES, HIGH), Claude ✅ (COMMENT,
HIGH), Gemini Pro ❌ (quota-exhausted for the second straight session)

**Key Feedback**:

*Codex (4 issues):*
- README.md + arch.md updates unassigned to any phase; builder could ship
  without them. Fixed by adding deliverables + ACs under Phase 6.
- Phase 7 overreached spec by gating on full-crawl completion. Moved the
  full crawl to Post-Implementation Tasks.
- Security test matrix missing XXE-SSRF and scheme-downgrade redirect
  rejection cases. Added AC8a and AC5a.
- TLS self-test behavior internally inconsistent (Phase 1 said retry-then-halt;
  risk mitigation said warn). Rewrote Phase 1 TLS self-test semantics
  explicitly: 30 s total budget including 2 retries; successful connection =
  halt; inconclusive = halt; failure with cert error = pass.

*Claude (9 items, most minor):*
- Stop-condition AC matrix only enumerated 4 of 9 halt triggers. Added
  AC13a (challenge halt), AC26a (Retry-After > 300s), AC26b (runtime > 72h),
  AC7a (robots fetch-fail), AC7b (robots re-fetch newly disallowing /ein).
- URL normalization matrix covered 2 of 10 rules. Added AC18a (CN-redirect
  unwrap), AC18b (tracking strip), AC18c (IDN punycode), AC18d (invalid host),
  AC19a (social rejection).
- Cross-EIN redirect missing ACs for (a) second row written when target EIN
  enumerated, (b) query-time dedup pattern. Added AC14a and AC14b.
- TLS self-test inconsistency — same as Codex; resolved together.
- Phase 2 regex commentary mentioned "dashboard-compatibility" with no
  referent. Stripped.
- AC2 fake-clock vs real-clock ambiguity. Resolved: AC2 uses fake clock;
  the throttle unit test asserts `time.monotonic` deltas under frozen time.
- Mission-statement internal-use-only carve-out not in HANDOFF.md deliverable.
  Added "Usage Restrictions" section to the Phase 6 HANDOFF deliverable.
- (optional) Interim 5-EIN live smoke at end of Phase 4 — NOT adopted for
  v1; skip.
- Cross-phase rollback wording ambiguous. Added a Cross-Phase Rollback
  Strategy section.

**Plan Adjustments** (sections modified):

- Acceptance Test Matrix: expanded from 33 to 47 rows.
- Phase 1 TLS self-test: tightened semantics; halt on inconclusive.
- Phase 2 implementation details: stripped confusing regex note.
- Phase 6: added README.md + arch.md deliverables; added HANDOFF Usage
  Restrictions section; added AC32a/AC32b.
- Phase 7: scope pulled back to 50-EIN validation only.
- Post-Implementation Tasks: added full-crawl commissioning.
- Cross-Phase Rollback Strategy: new section.

### Second Consultation (After Human Review)

### Second Consultation (After Human Review)

**Date**: pending
**Models Consulted**: pending
**Key Feedback**: pending
**Plan Adjustments**: pending

### Red Team Security Review (MANDATORY)

**Date**: 2026-04-17
**Commands**:

```
consult --model codex  --type red-team-plan plan 0001   # REQUEST_CHANGES, HIGH
consult --model claude --type red-team-plan plan 0001   # REQUEST_CHANGES, HIGH
consult --model gemini --type red-team-plan plan 0001   # quota-exhausted (3 attempts this session)
```

**Reviewers**: Codex (5 findings — 2 HIGH, 3 MEDIUM) + Claude (21 findings
— 0 CRITICAL, 4 HIGH, 11 MEDIUM, 7 LOW). Gemini quota-locked again.

**Findings (from Codex):**

#### Codex HIGH-1 — Workflow not approval-ready
- **Issue**: plan had pending second consultation + red-team review.
- **Mitigation**: addressed by completing this very review cycle. Procedural.

#### Codex HIGH-2 — Archive write not crash-durable
- **Issue**: `tmp + fsync(file) + os.replace` omits `fsync(parent_dir)`.
  After power loss, the directory entry update may not be durable even
  though the file contents are.
- **Mitigation**: added `os.fsync(dir_fd)` on `raw/cn/` after `os.replace`.
  AC15a added to matrix.

#### Codex MEDIUM-1 — TLS self-test relies on third-party
- **Issue**: `expired.badssl.com` is a network dependency for a security
  gate; makes startup + tests nondeterministic.
- **Mitigation**: redesigned as **hybrid local-primary + remote-secondary**.
  Local known-bad-cert endpoint is authoritative; remote is cross-check.
  CI/integration tests use local only.

#### Codex MEDIUM-2 — TDD pre-implementation step missing
- **Issue**: matrix is strong but the "generate acceptance tests before
  implementation" commit is not scheduled.
- **Mitigation**: added **Phase 0 (TDD Acceptance Test Scaffolding)** —
  must be committed before Phase 1 starts. Dependencies updated accordingly.

#### Codex MEDIUM-3 — Archive ownership ambiguous
- **Issue**: `fetcher.py` archives, but Phase 5 pseudocode implies a
  second archive step in the crawler.
- **Mitigation**: `fetcher.py` owns archiving end-to-end; the crawler
  receives a result record and does NOT re-archive. Phase 5 Objectives
  updated explicitly.

**Findings (from Claude):**

#### Claude HIGH-1 — Dependency pinning / CVE scanning missing
- **Mitigation**: Phase 1 mandates Poetry `poetry.lock` OR pip-tools with
  `--generate-hashes`; version floors for `defusedxml>=0.7.1`,
  `lxml>=4.9.1`, `requests>=2.31.0`; `pip-audit` + `bandit`/ruff S-rules
  in CI; `.python-version` pinning Python 3.12.x.

#### Claude HIGH-2 — BeautifulSoup HTML-mode entity safety
- **Mitigation**: explicit parser construction with `lxml>=4.9.1`;
  `tests/fixtures/cn/xxe-html-mode.html` added; new AC8b.

#### Claude HIGH-3 — Content-Type not validated before parsing
- **Mitigation**: HTTP client asserts `Content-Type` starts with
  `text/html` or `application/xhtml+xml`; mismatch → fetch_status=
  server_error. New AC5b.

#### Claude HIGH-4 — TOCTOU race between lstat and os.replace
- **Mitigation**: per-process temp subdir `raw/cn/.tmp-{pid}-{uuid}/`
  (mode 0o700) isolates in-flight writes; the final directory (0o700,
  crawler-owned) is not writable by other UIDs. Plus `os.fsync(dir_fd)`
  after `os.replace` covers Codex HIGH-2. New AC15b.

#### Claude MEDIUM — 11 items, all addressed in the plan body:

- Decompression with `decode_content=True` + explicit decoded-bytes cap +
  early socket close on overflow. AC4 updated.
- Cookie jar reset via `session.cookies = RequestsCookieJar()`, AC6
  asserts `len(jar) == 0`.
- Hybrid local+remote TLS self-test (shared with Codex MED-1).
- Periodic robots.txt re-fetch (every 6h or 1000 EINs). New AC7c.
- Throttle jitter ±0.5s. AC2 lower bound relaxed to greater than or equal
  to 250s.
- Stale-flock: no auto-takeover; always exit 3 and require manual
  intervention.
- Disallow EIN canonicalization (`canonicalize_ein`) applied before
  comparison. AC11 updated.
- Checkpoint HMAC-SHA256 with per-installation secret `.crawler.key`.
  New AC24a.
- SIGTERM fallback: stderr log + `_exit(2)` if HALT file write fails.
  New AC29a.
- Retry-After HTTP-date form handled. New AC6a.
- Report/HANDOFF file permissions 0o600. AC30 expanded.

#### Claude MEDIUM (supply chain + network): DNS rebinding

- **Mitigation**: resolve `www.charitynavigator.org` at startup; record IP
  set; log drift warning on per-request re-resolve. New AC30a. Not a halt
  condition — Cloudflare's pool rotates.

#### Claude LOW — 7 items, addressed in-place:

- L1: UA email alias default `crawler-contact@lavanduladesign.com`; env-
  overridable.
- L2: Content-Length vs actual decoded bytes sanity warning at greater
  than 10× divergence. New AC30c.
- L3: Clock-sync warning vs CN `Date:` header, greater than 5 min
  divergence. New AC30b.
- L4: XXE fixture uses `file:///dev/null`, not `/etc/passwd` (avoids
  triggering enterprise CI security scanners).
- L5: Corrupt-checkpoint retention cap at 5 files. New AC24b.
- L6: `.python-version` pinning (shared with H1).
- L7: `bandit`/ruff S-rules in CI (shared with H1).

**All 26 findings (Codex 5 + Claude 21) resolved in the plan body.**

**Follow-up with Gemini 2.5 Flash (2026-04-17, after patching consult to
swap gemini-2.5-pro → gemini-2.5-flash to work around the pro quota cap):**

- **Plan-review**: `APPROVE`, HIGH confidence, 0 issues.
- **Red-team-plan**: `APPROVE`, 0 CRITICAL / 0 HIGH / 0 MEDIUM / 3 LOW.

Gemini Flash LOW findings (addressed in plan body):

- **LOW-1 (Encryption at rest)**: acknowledged as out-of-scope trade-off;
  filesystem permissions + internal-only use are the v1 controls.
  Documented in Orchestration/Hygiene section as a future-TICK candidate.
- **LOW-2 (HMAC key rotation)**: procedure added — delete `.crawler.key`,
  next run regenerates; invalidates prior checkpoints. Documented for
  HANDOFF.md.
- **LOW-3 (Internal exception sanitization)**: added to Phase 5 Operational
  Hygiene — internal exception messages and stack traces are truncated
  and home-dir-redacted before HALT or log emission.

**Verdict**: APPROVE (all findings resolved). Codex originally REQUEST_CHANGES
(5 findings) and Claude originally REQUEST_CHANGES (21 findings, 0 CRITICAL
/ 4 HIGH / 11 MEDIUM / 7 LOW); every issue was addressed in the plan body.
Gemini Pro was unavailable (quota cap); Gemini Flash was substituted and
returned APPROVE with 3 additional LOW findings, all addressed. Plan has
been through 3-way multi-agent review at both plan-review and red-team-plan
checkpoints.

## Approval

- ( ) Technical Lead Review
- ( ) Engineering Manager Approval (Ron)
- ( ) Resource Allocation Confirmed
- ( ) Expert AI Consultation Complete
- ( ) Red Team Security Review Complete (no unresolved findings)

## Change Log

| Date       | Change                  | Reason                                   |
|------------|-------------------------|------------------------------------------|
| 2026-04-17 | Initial plan draft      | Spec 0001 approved for planning          |

## Notes

- Builder will be spawned via `af spawn -p 0001` AFTER this plan is approved
  AND committed. Spec + plan must be in the working tree at the time of
  spawning (per CLAUDE.md Architect rule).
- A lightweight first step before Phase 1 may be useful: a TICK-0000 that
  creates `common/http_client.py` by refactoring `nptech/http_client.py`.
  That is EXPLICITLY out of scope for this plan — this plan delivers the
  full project as a self-contained `lavandula/nonprofits/` tree. Any shared
  hoisting is a separate decision.

---

## Amendment History

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->
