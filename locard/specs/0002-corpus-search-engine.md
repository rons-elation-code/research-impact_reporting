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
CREATE TABLE IF NOT EXISTS corpus_items (
  content_sha256   TEXT PRIMARY KEY,
  source_url       TEXT NOT NULL,          -- first URL seen for this content
  canonical_url    TEXT,                    -- post-redirect final URL
  content_type     TEXT NOT NULL,           -- Content-Type after magic-byte verify
  file_size_bytes  INTEGER NOT NULL,
  archived_at      TEXT NOT NULL,           -- ISO-8601 UTC
  topic            TEXT NOT NULL,           -- plugin name, e.g. 'nonprofit-reports'
  topic_version    INTEGER NOT NULL DEFAULT 1,
  CHECK (length(content_sha256) = 64),
  CHECK (file_size_bytes > 0)
);
CREATE INDEX idx_corpus_items_topic ON corpus_items(topic);

-- All URLs that ever resolved to this content (dedup crumbs).
CREATE TABLE IF NOT EXISTS corpus_item_urls (
  content_sha256   TEXT NOT NULL,
  url              TEXT NOT NULL,
  search_query_id  INTEGER,                 -- FK to search_queries_done (if search-originated)
  first_seen_at    TEXT NOT NULL,
  PRIMARY KEY (content_sha256, url),
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

- **AC1** — SSRF guard: integration test exercises these URL shapes
  and asserts `fetch_status='blocked_ssrf'` or `blocked_scheme`:
  `file:///etc/passwd`, `gopher://x`, `ftp://x`, `http://127.0.0.1`,
  `http://10.0.0.1`, `http://169.254.169.254/latest/meta-data/`,
  `http://[::1]/`, `http://[fe80::1]/`, `http://localhost`, and a
  DNS-rebinding server that returns public IP on first resolve then
  `127.0.0.1` on second. No outbound connection made to any of them.
- **AC2** — Scheme allowlist: only `{http, https}` permitted. AC1
  covers this in integration; a unit test pins the allowlist.
- **AC3** — Redirect policy: across a redirect chain, EVERY hop is
  re-validated against AC1/AC2 rules. Cross-host is ALLOWED (search
  results legitimately redirect via `google.com/url?q=...`) but
  scheme must stay HTTPS after the first hop and every target IP
  must pass the allowlist. Max 5 hops.
- **AC4** — Content-type + magic-byte verification: response claiming
  `Content-Type: application/pdf` is only accepted for archive if
  first 1024 bytes contain `%PDF-1.`. Otherwise `blocked_content_type`.
  Mirror rules for each registered `Extractor`'s claimed MIME.
- **AC5** — Parser sandbox: PDF extraction runs in a subprocess with
  `RLIMIT_AS` (800 MB), `RLIMIT_CPU` (30 s wall), `RLIMIT_FSIZE`
  (disallow any write); killed and marked `sandbox_killed` on any
  limit violation. Integration test with a deliberately-expensive
  crafted PDF fixture asserts the parent process stays bounded.
- **AC6** — Content-addressable archive: same bytes fetched via 3
  different URLs produce exactly 1 `corpus_items` row and 3
  `corpus_item_urls` rows. Filename is `{sha256}.{ext}`. SHA256 is
  computed over final verified bytes only.
- **AC7** — Atomic + symlink-safe writes: pre-planted symlink at
  `raw/{sha256}.pdf` triggers halt. Mid-write crash leaves no
  partial file in the final path.
- **AC8** — Budget enforcement: preflight budget check runs
  BEFORE issuing each search query / each fetch that has a marginal
  cost; halts with `exit 2` + `HALT-budget-*.md` before exceeding
  cap. Integration test with simulated cost ledger confirms.
- **AC9** — robots.txt: per-host cache (24 h), fail-closed on fetch
  failure, honor most-specific UA stanza (same policy as 0001).
  Integration test with mocked robots.txt disallowing our UA proves
  `blocked_robots` + no fetch.
- **AC10** — TLS verification: startup self-test against
  `expired.badssl.com` + a locally-served known-bad-cert endpoint
  (belt+suspenders, per 0001 TICK). Halts if verification is
  disabled.
- **AC11** — Flock: two concurrent engine invocations → second exits
  code 3 with clear message.
- **AC12** — SQL parameterization: round-trip of input with
  `'; DROP TABLE corpus_items; --` round-trips byte-identical; table
  still exists; `ruff S608` lint catches raw f-string SQL in CI.
- **AC13** — Log injection sanitation: CR/LF + ANSI escapes stripped
  before any `fetch_log.notes`/`error` write.
- **AC14** — Resume semantics: kill mid-query, resume, assert zero
  double-fetches (content_sha256 dedup) and `search_queries_done`
  advances past interrupted page.
- **AC15** — File permissions: DB mode `0o600`, archive dir `0o700`,
  logs dir `0o700`. Enforced at creation + verified post-run.

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

- **Threat model**: inherited from the abandoned 0002's review and
  now concentrated here. Actors: malicious PDF author, SEO-gaming
  host, network attacker, supply-chain compromise, local attacker
  after catalogue is built. Each vector mapped to a specific AC
  above.
- **SSRF hardening** (AC1–AC3): scheme allowlist, resolved-IP
  allowlist, DNS re-pin post-redirect, cross-host + scheme
  revalidation.
- **Parser isolation** (AC5): subprocess + rlimits + seccomp where
  available. No JavaScript execution. No embedded-file extraction.
  XMP parsed via `defusedxml`.
- **Content-type trust** (AC4): magic-byte verification before any
  disk-land.
- **Supply chain**: hash-pinned lockfile, `pip-audit` in CI, no
  pulls from git URLs or unpinned PyPI names.
- **Secrets**: search API keys live in the env vars
  `CORPUS_SEARCH_GOOGLE_CSE_KEY` and `CORPUS_SEARCH_GOOGLE_CSE_CX`;
  never in argv; never in DB; never in logs.
  `.env` gitignored; startup asserts permissions `0o600` or halts.
  Logger has a redaction regex for any `key=` / `cx=` URL substring
  as belt-and-suspenders.
- **Log injection** (AC13): sanitizer strips control chars + ANSI +
  truncates to 500 chars.
- **PII**: the engine stores full response bytes and a small
  configurable metadata subset. It does NOT parse content for PII —
  that's topic-plugin responsibility. The engine contract says
  "plugins must declare any PII fields they populate."
- **Encryption at rest**: the engine's DB + archive directories are
  deployed on an encrypted volume (operator responsibility). An
  AC-level assertion is out of scope for this spec but documented
  in HANDOFF.md.

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
