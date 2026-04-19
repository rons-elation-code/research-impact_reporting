# Specification: Corpus Search Engine (Generic Pipeline)

## Metadata

- **ID**: spec-2026-04-17-corpus-search-engine
- **Status**: draft
- **Created**: 2026-04-17
- **Supersedes**: 0002-report-search-agent.abandoned.md (split into 0002
  pipeline + 0003 topic plugin after multi-agent review)

## Clarifying Questions Asked

- **Q: Why a generic engine rather than a one-off report crawler?**
  A: The multi-agent review of the abandoned 0002 returned 3 CRITICAL
  + 10 HIGH findings. Almost all were pipeline concerns (SSRF, parser
  isolation, budget caps, content-type trust, redirect policy, supply
  chain). Those concerns are identical whether we're harvesting
  nonprofit reports, marketing collateral, brochures, event programs,
  or case statements. Concentrating the security hardening in one
  reusable engine avoids re-auditing each topic and re-implementing
  defenses each time. Lavandula already has a foreseeable second
  consumer (marketing-materials catalogue); more will follow.

- **Q: What is the engine explicitly NOT doing?**
  A: Not classifying. Not extracting domain-specific fields. Not
  scoring "quality." Not deriving attribution. Not deciding what's a
  "real" artifact. All of that is topic-plugin work (0003 and
  beyond). The engine delivers **bytes + provenance + dedup**.

- **Q: Scope?**
  A: v1 supports (a) Google Custom Search as the reference provider,
  (b) PDF as the reference artifact type, (c) one topic plugin
  wired in (0003). The `SearchProvider` and `Extractor` interfaces
  are designed so adding Bing, SerpAPI, or HTML/image extractors is
  a plugin-level change, not a rewrite.

- **Q: What did we inherit from 0001?**
  A: `ThrottledClient`, atomic archive writes, SQL parameterization
  discipline, sanitize-logging helper, `flock` single-instance,
  preflight disk check, TLS self-test, lint gate. These lift into
  `corpus_search/` verbatim. 0001 itself continues to use the local
  copy until it retires; no forced migration in this spec.

## Problem Statement

Every topic we want to catalogue (nonprofit reports, marketing
materials, event programs, etc.) re-implements the same pipeline:
issue search queries → fetch URLs → validate content → archive →
feed an extractor → classify → write a DB row → report. The
security and operational concerns of that pipeline (SSRF, parser
CVEs, budget, robots, redirects, dedup, resume, rate limiting) are
topic-agnostic. Without a shared engine, each topic spec rediscovers
them.

This spec defines that shared engine so topic plugins stay small
and focused.

## Current State

- Spec 0001 (`lavandula/nonprofits/`) implements a crawler with all
  the relevant primitives already, but coupled to its schema and
  domain.
- No shared `corpus_search/` package exists.
- Second-topic demand exists in conversation (marketing materials,
  case statements) but is not specced.

## Desired State

At the end of this project:

1. **`corpus_search/` package** at repo root holding the engine
   modules (see Module Layout).
2. **Base SQLite schema** (tables `fetch_log`, `search_queries_done`,
   `corpus_items`) that every topic plugin extends with its own
   columns via a companion table keyed by `content_sha256`.
3. **Hardened defaults** for SSRF, parser isolation, budget caps,
   robots compliance, redirects, content-type validation, and
   dedup — all exercised by engine-level tests.
4. **Provider abstraction**: a `SearchProvider` interface with one
   reference implementation (Google Custom Search).
5. **Extractor abstraction**: an `Extractor` interface with one
   reference implementation for `application/pdf` (subprocess
   sandbox runner using `pypdf`).
6. **CI + lint**: pip-audit, bandit, ruff S-rules, `verify=False`
   banned, hash-pinned deps.
7. **HANDOFF.md + README.md** documenting how a topic plugin
   consumes the engine.

### Module Layout

```
corpus_search/
  config.py                # throttles, caps, paths, UA, allowlists
  http_client.py           # ThrottledClient (hoisted, hardened)
  search/
    __init__.py
    provider.py            # SearchProvider ABC
    google_cse.py          # Google Custom Search impl
  fetch/
    __init__.py
    url_guard.py           # SSRF scheme/IP allowlist + DNS re-check
    redirect_policy.py     # per-call redirect rules (cross-host allow, scheme fixed)
    downloader.py          # streamed download + content-type + magic-byte + size cap
  archive.py               # content-addressable storage; atomic+symlink-safe writes
  budget.py                # spend tracking + preflight halt
  robots.py                # per-host robots.txt cache (from 0001) with fail-closed
  sandbox/
    __init__.py
    runner.py              # subprocess isolation: RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE, seccomp-bpf where available
    pdf_extractor.py       # reference pypdf wrapper, invoked inside sandbox
  schema.py                # base tables + migration helpers
  logging_utils.py         # sanitizer (hoisted from 0001)
  cli.py                   # `python -m corpus_search ...` reference runner for topic plugins
  tests/
    unit/
    integration/           # end-to-end with a local mock search server + local cert+PDF fixtures
    fixtures/
  HANDOFF.md
  README.md
  requirements.txt         # hash-pinned
  requirements-dev.txt
  .python-version
  lint.sh
```

### Base Schema

```sql
-- One row per successfully archived artifact.
-- Topic-agnostic: a single PDF can legitimately be relevant to
-- multiple topics (e.g., a hospital's annual report matters to
-- both nonprofit-reports and healthcare-marketing topic plugins).
-- Topic association lives in topic_corpus_items, not here (fix
-- for Codex red-team HIGH: corpus_items key conflict with multi-
-- topic).
CREATE TABLE IF NOT EXISTS corpus_items (
  content_sha256   TEXT PRIMARY KEY,
  source_url_redacted TEXT NOT NULL,        -- URL with sensitive query params redacted; see URL redaction policy
  canonical_url_redacted TEXT,              -- post-redirect final URL, redacted
  content_type     TEXT NOT NULL,           -- Content-Type after magic-byte verify
  file_size_bytes  INTEGER NOT NULL,
  archived_at      TEXT NOT NULL,           -- ISO-8601 UTC
  CHECK (length(content_sha256) = 64),
  CHECK (file_size_bytes > 0)
);

-- Topic association: many-to-many so a single artifact can belong
-- to N topics without conflicts.
CREATE TABLE IF NOT EXISTS topic_corpus_items (
  topic            TEXT NOT NULL,           -- plugin name, e.g. 'nonprofit-reports'
  content_sha256   TEXT NOT NULL,
  topic_version    INTEGER NOT NULL DEFAULT 1,
  first_seen_at    TEXT NOT NULL,
  PRIMARY KEY (topic, content_sha256),
  FOREIGN KEY (content_sha256) REFERENCES corpus_items(content_sha256)
);
CREATE INDEX idx_topic_corpus_items_topic ON topic_corpus_items(topic);

-- All URLs that ever resolved to this content (dedup crumbs).
-- URLs are stored with redaction policy applied.
CREATE TABLE IF NOT EXISTS corpus_item_urls (
  content_sha256   TEXT NOT NULL,
  url_redacted     TEXT NOT NULL,           -- per URL redaction policy; see Security Considerations
  search_query_id  INTEGER,                 -- FK to search_queries_done (if search-originated)
  first_seen_at    TEXT NOT NULL,
  PRIMARY KEY (content_sha256, url_redacted),
  FOREIGN KEY (content_sha256) REFERENCES corpus_items(content_sha256)
);

-- Audit: every search-API call + every fetch attempt.
CREATE TABLE IF NOT EXISTS fetch_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,               -- 'search' | 'fetch' | 'robots'
  target       TEXT NOT NULL,               -- query text (search) or URL (fetch/robots)
  status_code  INTEGER,                     -- null on network error
  fetch_status TEXT NOT NULL,               -- enum: ok, not_found, rate_limited, forbidden,
                                            -- server_error, network_error, size_capped, blocked_ssrf,
                                            -- blocked_scheme, blocked_robots, blocked_content_type
  attempt      INTEGER NOT NULL DEFAULT 1,
  fetched_at   TEXT NOT NULL,
  elapsed_ms   INTEGER,
  bytes_read   INTEGER,
  notes        TEXT,                        -- sanitized
  error        TEXT,                        -- sanitized, truncated
  CHECK (kind IN ('search','fetch','robots')),
  CHECK (fetch_status IN ('ok','not_found','rate_limited','forbidden','server_error',
                          'network_error','size_capped','blocked_ssrf','blocked_scheme',
                          'blocked_robots','blocked_content_type'))
);

-- Resume support: queries we've already run, with cursor state.
CREATE TABLE IF NOT EXISTS search_queries_done (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  topic          TEXT NOT NULL,
  provider       TEXT NOT NULL,
  query_text     TEXT NOT NULL,
  last_page      INTEGER NOT NULL DEFAULT 0,
  results_count  INTEGER NOT NULL DEFAULT 0,
  completed_at   TEXT,                      -- NULL while in progress
  UNIQUE (topic, provider, query_text)
);

-- Budget ledger (one row per spend event; sum <= cap).
CREATE TABLE IF NOT EXISTS budget_ledger (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  topic        TEXT NOT NULL,
  provider     TEXT NOT NULL,
  cents_spent  INTEGER NOT NULL,
  occurred_at  TEXT NOT NULL,
  ref          TEXT                        -- ref to fetch_log.id or search_queries_done.id
);
```

Topic plugins create their own companion table keyed by
`content_sha256` (e.g., `nonprofit_reports(content_sha256, org_name,
page_count, design_score, ...)`) and own the schema evolution within
that table.

## Stakeholders

- **Primary Users**: topic-plugin authors (Ron + AI agents).
- **Secondary Users**: Lavandula operators running catalogue refreshes.
- **External**: search-API providers (Google CSE for v1), host sites
  serving artifacts, the crawler's outbound-network policy.

## Success Criteria

### Engine Correctness (GATING)

- **AC1** — SSRF guard (RFC-class + named metadata, per Claude
  red-team HIGH): `url_guard` resolves target hostname and rejects
  any IP satisfying `ipaddress.ip_address(x).is_private |
  is_loopback | is_link_local | is_multicast | is_reserved |
  is_unspecified`. Additionally rejects an explicit named
  cloud-metadata list: `169.254.169.254` (AWS), `168.63.129.16`
  (Azure), `100.100.100.200` (Alibaba), `fd00:ec2::254` (AWS IPv6),
  plus CGNAT `100.64.0.0/10`. IPv6-mapped IPv4 addresses are
  normalized before the check. Integration test exercises each
  class as a distinct case.
- **AC2** — Scheme allowlist: only `{http, https}` permitted.
- **AC3** — Redirect policy: across a redirect chain, EVERY hop is
  re-resolved AND re-validated against AC1/AC2. Cross-host is
  ALLOWED (search results legitimately redirect via
  `google.com/url?q=...`) but scheme must stay HTTPS after the first
  hop and every target IP must pass the allowlist. Max 5 hops.
- **AC3.1** — IP pinning at connect time (per Codex red-team HIGH):
  the resolved IP validated in AC1/AC3 is the IP the socket actually
  connects to. Use an HTTP adapter that accepts a pre-resolved IP and
  sets `Host`/SNI headers to the original hostname. Without this,
  DNS rebinding between validation and `connect()` defeats AC1.
- **AC4** — Content-type + magic-byte verification: response claiming
  `Content-Type: application/pdf` is only accepted for archive if
  first 1024 bytes contain `%PDF-1.`. Otherwise `blocked_content_type`.
  Mirror rules for each registered `Extractor`'s claimed MIME.
- **AC4.1** — Streaming decompressed-size cap (per Claude red-team
  CRITICAL): `Content-Encoding: gzip|deflate|br` responses are
  decompressed as a streaming operation with a running decompressed
  byte counter. Exceeding `config.MAX_RESPONSE_BYTES` aborts the
  stream, closes the socket, deletes any partial temp file, and
  records `fetch_status='size_capped'`. Integration test: a 1 KB
  gzip bomb that decompresses to 2 GB is aborted; peak memory
  delta < 50 MB during the test.
- **AC5** — Parser sandbox — resource bounds: PDF extraction runs
  in a subprocess with `RLIMIT_AS` (800 MB), `RLIMIT_CPU` (30 s
  wall), `RLIMIT_FSIZE` (disallow any write outside `/tmp/sandbox-
  {pid}/` scratch). Killed and marked `sandbox_killed` on any limit
  violation. Integration test with a crafted deliberately-expensive
  PDF fixture asserts parent process stays bounded.
- **AC5.1** — Parser sandbox — network denial (per Claude red-team
  CRITICAL): the sandbox child MUST be unable to perform any
  outbound network syscall. Implementation:
  1. **Linux**: `unshare(CLONE_NEWNET)` creates an isolated network
     namespace with no interfaces other than loopback. Extractor
     code inside cannot reach any external IP.
  2. **Linux + seccomp-bpf**: additionally install a seccomp filter
     returning `EACCES` for `socket`, `socketpair`, `connect`,
     `sendto`, `sendmsg`, `bind`. Belt-and-suspenders with the
     namespace; kills the syscall even if namespace setup partially
     fails.
  3. **macOS** (dev only): if namespace and seccomp aren't available,
     engine hard-fails startup with exit code 4 unless
     `CORPUS_SEARCH_ALLOW_UNSANDBOXED=1` is explicitly set AND it's
     a test run. Production must run on Linux.
- **AC5.2** — Parser sandbox — environment scrubbing (per Codex
  red-team HIGH + Claude red-team CRITICAL #2 fallout): the sandbox
  child receives an empty environment; no `CORPUS_SEARCH_*` env vars,
  no `PATH`, no `HOME`, no provider API keys, no parent `os.environ`.
  argv is explicitly set; no shell expansion. Integration test
  dumps the child's `os.environ` via a fixture extractor and asserts
  it is empty save for sandbox-provided paths.
- **AC5.3** — Seccomp required on Linux (per Claude red-team HIGH):
  on Linux hosts, missing `pyseccomp` (or equivalent) at startup →
  exit code 4 with a clear message. The dependency is pinned in
  `requirements.txt` with a hash. Not optional.
- **AC5.4** — Sandbox output validation (per Claude red-team
  CRITICAL — belongs to topic plugins but enforced by engine
  contract): the engine's `sandbox.runner` validates extractor
  output against a schema declared by the topic plugin. Out-of-bounds
  values (strings > declared max, non-printable bytes, types
  mismatching the schema) are rejected; plugin is given a
  `sandbox_validation_error` signal, no DB write occurs.
- **AC6** — Content-addressable archive: same bytes fetched via 3
  different URLs produce exactly 1 `corpus_items` row and 3
  `corpus_item_urls` rows.
- **AC6.1** — Archive filename extension mapping (per Claude red-team
  HIGH): filename is `{sha256}.{ext}` where `ext` comes from a
  hardcoded MIME → extension mapping in `config.py` (e.g.,
  `application/pdf → pdf`, `text/html → html`). NEVER from URL or
  raw `Content-Type` header. Unknown MIME → archive is rejected,
  fetch_status `blocked_content_type`.
- **AC7** — Atomic + symlink-safe writes: pre-planted symlink at
  `raw/{sha256}.pdf` triggers halt. Mid-write crash leaves no
  partial file in the final path.
- **AC8** — Budget enforcement: preflight budget check runs BEFORE
  issuing each search query or each fetch with a marginal cost;
  halts with `exit 2` + `HALT-budget-*.md` before exceeding cap.
- **AC8.1** — Budget-ledger atomicity (per Claude red-team HIGH):
  preflight check + spend insert is a single `BEGIN IMMEDIATE`
  transaction on `budget_ledger` (`SELECT SUM(cents_spent)` +
  `INSERT`). Multi-threaded engines that both preflight concurrently
  cannot both see under-cap and both over-spend.
- **AC8.2** — Per-topic cumulative archive cap (per Claude red-team
  HIGH): each topic declares `max_topic_archive_bytes`; engine tracks
  cumulative bytes written for that topic (via
  `topic_corpus_items`) and halts with `HALT-disk-topic-*.md` when
  reached.
- **AC8.3** — Failure-rate circuit breaker (per Claude red-team
  HIGH): if successful fetch rate over a sliding window of 50
  attempts drops below 10%, engine halts with
  `HALT-failure-rate-*.md`. Prevents burning the budget on a
  query set that produces only 404s / guard-blocks.
- **AC9** — robots.txt: per-host cache (24 h), fail-closed on fetch
  failure, honor most-specific UA stanza.
- **AC9.1** — robots.txt transport + guard (per Claude red-team
  HIGH): robots.txt fetches MUST use HTTPS. The robots fetch itself
  is subject to `url_guard` + redirect policy (AC1–AC3.1). Integration
  test with attacker-controlled robots returning pathological rules
  + an http→https downgrade confirms failure.
- **AC10** — TLS verification: startup self-test against
  `expired.badssl.com` + a locally-served known-bad-cert endpoint
  (belt+suspenders, per 0001 TICK). Halts if verification is
  disabled.
- **AC11** — Flock: two concurrent engine invocations → second exits
  code 3 with clear message.
- **AC12** — SQL parameterization: round-trip of input with
  `'; DROP TABLE corpus_items; --` round-trips byte-identical; table
  still exists. `ruff S608` lint catches raw f-string SQL in CI.
- **AC13** — Log injection sanitation: CR/LF + ANSI escapes stripped
  before any `fetch_log.notes`/`error` write.
- **AC14** — Resume semantics: kill mid-query, resume, assert zero
  double-fetches and `search_queries_done` advances past interrupted
  page.
- **AC15** — File permissions: DB mode `0o600`, archive dir `0o700`,
  logs dir `0o700`.
- **AC16** — URL redaction policy (per Claude red-team HIGH): before
  any URL is stored in `corpus_items`, `corpus_item_urls`, or
  `fetch_log.target`, query-string parameters with names matching
  a case-insensitive sensitive-key set (`token`, `key`,
  `api_key` / `api-key`, `password`, `pass`, `secret`, `session`,
  `auth`, `bearer`, `sig`, `signature`, `access_token` /
  `access-token`, `id_token` / `id-token`) are replaced with
  `REDACTED`. Test: a URL with
  `?session=abc123&normal=ok` round-trips as
  `?session=REDACTED&normal=ok`.
- **AC17** — Encryption-at-rest verification (per Gemini red-team
  HIGH): engine startup checks that `data/` and `raw/` are on a
  filesystem marked encrypted. Check attempts, in order:
  (a) `/proc/mounts` flag inspection for LUKS / fscrypt,
  (b) macOS APFS `diskutil apfs list` encryption flag,
  (c) presence of a `.encrypted-volume` operator-signed marker file
  in each dir (escape hatch for unusual setups).
  Failure mode: WARN at startup (not halt) in v1; promote to halt
  in a future TICK. Operators get explicit signal.
- **AC18** — CLI input validation (per Gemini red-team HIGH): every
  CLI argument (`--topic`, `--budget-cents`, `--max-pages`, paths)
  is validated at argparse boundary:
  - `--topic`: lowercase alphanumeric + hyphen, starts with a
    letter or digit, 1-32 chars total, anchored to string bounds
  - numeric args: integer in declared range
  - path args: resolved with `Path.resolve(strict=True)` and must
    be within the declared project root
  Invalid input → exit 2, no side effects.
- **AC19** — Plugin trust boundary (per Claude red-team CRITICAL
  #2): v1 declares plugins **trusted code**. Topic plugins are
  authored by Lavandula, reviewed, and imported in-process. The
  spec documents this explicitly in the threat model; plugins that
  import `os.environ` or `socket` are considered "authored-in-bad-
  faith" and not defended against at the engine boundary. A
  subprocess plugin contract is deferred to a future TICK if
  untrusted plugins become a real use case. This resolves the
  "worst of both" critique from Claude by picking (a) explicitly.
  Engine still enforces the data-plane boundary (AC5.4 validates
  sandbox output before DB writes) regardless of plugin trust.
- **AC20** — Enforce fetch.downloader usage (per Gemini red-team
  HIGH): `ruff` custom rule (or grep-based lint check in `lint.sh`)
  rejects any import of `requests`, `urllib.request`, `httpx`,
  `aiohttp`, `socket`, `http.client` from any module under
  `corpus_search/plugins/*` or any registered topic-plugin package
  (e.g., `lavandula/reports/`). Plugins that need HTTP call
  `corpus_search.fetch.downloader`. This is a complement to AC19's
  trust declaration, not a substitute.

### Reported (not gated)

- Coverage report showing per-topic row counts, bytes, fetch_status
  distribution, budget spent.
- Throttle-adherence metrics (actual vs configured).

## Constraints

### Technical

- **Python 3.12+**. `.python-version` pins `3.12`.
- **Dependencies hash-pinned** via `pip-compile --generate-hashes`.
  Install with `--require-hashes --only-binary=:all:`.
- `defusedxml>=0.7.1`, `lxml>=4.9.1`, `requests>=2.31.0`,
  `pypdf>=4.0`. Full lockfile committed.
- **No `verify=False` anywhere**. CI + `lint.sh` enforce.
- **No shell execution of untrusted content.** The sandbox runner
  uses `subprocess.Popen` with `shell=False`, argv only, no string
  interpolation.
- **Per-host throttle**: 3 s ± 0.5 s jitter (from 0001). Engine
  tracks last-fetch-time per host.

### Compliance

- Search provider ToS honored; query rates stay within their
  documented caps.
- robots.txt honored per-host for non-search fetches.
- No redistribution of archived content outside the host filesystem.

## Assumptions

- The PDF-heavy reference plugin (0003) validates the engine against
  realistic load. Other plugins (HTML, images) will need their own
  `Extractor` implementations but not engine changes.
- Topic plugins NEVER issue raw `requests.get()` — they MUST go
  through `corpus_search.fetch.downloader`. Enforced by code review
  + absence of `requests` import in plugin modules.

## Solution Approaches

### Approach 1: Single shared engine package + reference provider + reference extractor (RECOMMENDED)

**Description**: as drafted above.

**Pros**:
- One security audit, many topics.
- Plugin authors stay small and focused on domain logic.
- Future extractor types (HTML, image) drop in.

**Cons**:
- Up-front design cost.
- Tight coupling between engine schema and plugin companion tables
  (mitigated by foreign-key discipline).

### Approach 2: Copy-paste engine into each topic

**Description**: no shared package; each topic plugin owns its full
pipeline.

**Pros**:
- Zero shared-library friction.

**Cons**:
- N-fold security maintenance. A CVE in `pypdf` means N patches.
- Drift between plugins over time.

**Rejected.**

### Approach 3: Engine as a single-file utility module, no package

**Description**: one `corpus_search.py` module, not a package.

**Pros**:
- Lower ceremony.

**Cons**:
- Doesn't fit: too many responsibilities (search, fetch, sandbox,
  schema) for one file. Forces future refactor.

**Rejected.**

### Recommendation

**Approach 1.**

## Open Questions

### Critical (blocks progress)
- none.

### Important (affects design)
- **Provider selection for v1.** Google CSE is the default target;
  reasons: (a) largest index, (b) stable paid API, (c) well-documented
  pagination. The provider abstraction keeps Bing / SerpAPI as a
  one-file addition. AC-level behavior is identical across providers.
- **Sandbox stack.** On Linux we get `resource.setrlimit` + optional
  `prctl(PR_SET_NO_NEW_PRIVS)` + `seccomp-bpf` via `pyseccomp` if
  present. On macOS developer boxes, rlimit alone; seccomp skipped
  with a warning. The plan pins specific limits.
- **Topic-plugin interface shape.** Proposed: each plugin exports
  `TopicPlugin(name, queries, extractor, schema_ext, classifier,
  attribution)` and the engine's `cli.py` imports it. Alternative
  is a pure-subprocess contract. Decide in plan.

### Nice-to-know (optimization)
- Should we bundle a minimal CLI for running a topic plugin
  (`python -m corpus_search run --topic=nonprofit-reports`), or
  leave CLI ownership to each plugin?
- Can we expose the engine as an installable wheel so Lavandula
  could vendor it into unrelated projects? (Not v1.)

## Performance Requirements

- Effective search rate ≤ 1 query/sec per provider (Google CSE
  default quota).
- Effective fetch rate ≤ 0.33 req/sec per host (matches 0001).
- Sandbox overhead ≤ 200 ms per extractor invocation.
- Peak resident memory in the main process < 300 MB.
- Disk budget: engine itself adds < 50 MB; topic plugins define
  their own archive caps.

## Security Considerations

- **Threat model** — explicit actors:
  1. Malicious PDF / content authors (primary — arbitrary attacker-
     controlled input at the extractor boundary)
  2. SEO-gaming hosts that influence search rankings to inject
     attacker-chosen URLs
  3. Network attackers on outbound traffic
  4. Supply-chain actors (pypdf / requests / transitive CVEs)
  5. Local filesystem attackers after catalogue is built
  6. **In scope** for v1: plugins that import things they shouldn't
     (defended via AC20 lint + AC19 trust declaration)
  7. **Out of scope** for v1: truly adversarial plugin authors —
     see AC19
- **SSRF hardening** (AC1, AC3, AC3.1): RFC-class IP rejection +
  named cloud-metadata deny list + IP pinning at connect time.
- **Parser isolation** (AC5, AC5.1, AC5.2): subprocess + rlimits +
  network namespace + seccomp-bpf + empty child environment. No
  JavaScript execution. No embedded-file extraction. XMP parsed via
  `defusedxml`. Seccomp required on Linux (AC5.3).
- **Content-type trust** (AC4, AC6.1): magic-byte verification +
  hardcoded MIME→extension whitelist; attacker-controlled `ext`
  never reaches the filesystem.
- **Decompression bomb** (AC4.1): streaming decompressed-byte cap.
- **Plugin trust boundary** (AC19, AC20): v1 treats plugins as
  trusted code. Lint rule prevents accidental import of raw HTTP
  libraries. The engine validates data crossing the sandbox-output
  boundary (AC5.4) regardless.
- **Supply chain**: hash-pinned lockfile, `pip-audit` in CI, no
  pulls from git URLs or unpinned PyPI names. `pyseccomp` is a
  required dep on Linux (AC5.3).
- **Secrets**: search API keys live in env vars
  `CORPUS_SEARCH_GOOGLE_CSE_KEY` and `CORPUS_SEARCH_GOOGLE_CSE_CX`;
  never in argv; never in DB; never in logs; never in the sandbox
  child's environment (AC5.2). `.env` gitignored; startup asserts
  permissions `0o600` or halts. Logger has a redaction regex for
  any `key=` / `cx=` URL substring as belt-and-suspenders.
- **URL redaction** (AC16): query-string params with credential-
  shaped names are replaced with `REDACTED` before any DB/log
  write.
- **Log injection** (AC13): sanitizer strips control chars + ANSI +
  truncates to 500 chars.
- **CLI input** (AC18): all CLI args validated at argparse boundary.
- **PII**: the engine stores full response bytes and a small
  metadata subset. It does NOT parse content for PII — that's
  topic-plugin responsibility. The engine contract REQUIRES each
  plugin to declare its PII columns so a future deletion routine
  can sweep them.
- **Encryption at rest** (AC17): engine startup verifies the
  storage volumes are encrypted. v1 warns; future TICK promotes to
  halt. Operators see explicit signal.
- **Concurrency** (AC8.1, AC11): single-instance flock; budget
  preflight is a single atomic transaction.

## Test Scenarios (summary)

- Integration suite with a local mock search API server
  (returning canned results) and a local mock origin server
  (serving fixtures + misbehaving responses: slow, oversize,
  redirect-loop, bad-content-type, expired cert, gzip bomb).
- Unit tests for url_guard, redirect_policy, budget, sandbox
  runner, schema round-trip.
- Fuzz-style test: random-byte responses through the full pipeline;
  no crash, always ends in a sensible `fetch_status`.
- SSRF grid: the 10 URLs from AC1, each a separate test.
- Sandbox breakout: a PDF fixture containing nested-object-graph
  bomb, a billion-laughs analogue, and a compressed stream that
  expands to 2 GB. Each must hit rlimit and kill cleanly.

## Dependencies

- Google Custom Search API (paid; Lavandula-held credentials).
- `pypdf >= 4.0`.
- `requests >= 2.31`, `defusedxml >= 0.7.1`, `lxml >= 4.9.1`.
- Standard library `resource`, `subprocess`, `sqlite3`, `fcntl`,
  `hashlib`, `ipaddress`, `socket`.
- Dev: `pytest`, `pytest-mock`, `pip-audit`, `bandit`, `ruff`.

## References

- Abandoned spec: `locard/specs/0002-report-search-agent.abandoned.md`
- 0001 review findings: `.consult/0001/*-red-team-*.md`
- 0002 review findings: `.consult/0002/*-red-team-spec.md`
- OWASP SSRF defense cheat sheet.
- pypdf known CVEs referenced in the 0002 review.

## Risks and Mitigation

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Sandbox escapes via an unknown pypdf CVE | Low | High | rlimits bound blast radius; subprocess isolation; pip-audit monitors |
| SSRF allowlist drift over time (new metadata IPs) | Low | High | Dedicated module with its own tests; review on every new cloud deploy |
| Provider API deprecation / pricing change | Medium | Medium | Provider abstraction; swap to alternative; budget cap prevents surprise spend |
| Topic plugins call raw `requests.get` bypassing fetch guard | Medium | High | Code review; absence-of-import lint check; engine-provided `fetch` is the only sanctioned path |
| DB/archive accidentally backed up to cloud with raw PDFs | Medium | Medium | HANDOFF.md documents exclusion from sync surfaces; operator check |
| `expired.badssl.com` dependency in tests flaky | Low | Low | Local mock cert server is the primary gate (0001 TICK approach) |

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: pending
**Models Consulted**: Codex, Claude, Gemini Flash
**Key Feedback**: pending

### Red Team Security Review (MANDATORY)
**Date**: pending
**Command**: `consult --model gemini --type red-team-spec spec 0002`
**Findings**: pending
**Verdict**: pending

## Approval

- Technical Lead Review
- Product Owner Review (Ron)
- Stakeholder Sign-off
- Expert AI Consultation Complete
- Red Team Security Review Complete (no unresolved findings)

## Notes

- This spec replaces the abandoned 0002. Planning starts only after
  human approval.
- 0003 (nonprofit report catalogue) is drafted alongside this spec
  and depends on this one landing first.

---

## Amendments

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->
