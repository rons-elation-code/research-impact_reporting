# Review: Nonprofit Seed List Extraction

## Metadata
- **Spec**: `locard/specs/0001-nonprofit-seed-list-extraction.md`
- **Plan**: `locard/plans/0001-nonprofit-seed-list-extraction.md`
- **Branch**: `builder/0001-nonprofit-seed-list-extraction`
- **Reviewer**: Builder 0001 (self-review; SPIDER R phase)
- **Date**: 2026-04-17

## Summary

The crawler is implemented end-to-end across the plan's seven phases in
`lavandula/nonprofits/`. Every module from the plan's deliverable list
is present, the SQLite schema matches the spec DDL byte-for-byte, and the
acceptance-test suite is 96 tests passing covering each AC that can be
exercised hermetically. AC33 (50-EIN live run) is the sole remaining
gate and is explicitly an operator task per the plan.

## What landed

### Code (15 modules in `lavandula/nonprofits/`)

- `config.py` — paths, throttle, stop-condition thresholds, UA (with
  `LAVANDULA_UA_EMAIL` override), disallow-EIN floor, tracking-param and
  social-host lists, challenge signatures.
- `logging_utils.py` — control-char stripping + home-dir redaction +
  rotating file handler.
- `schema.py` — full DDL with every `CHECK` constraint from the spec,
  WAL+NORMAL pragmas, 0o600 on DB file.
- `http_client.py` — `ThrottledClient` + `FetchResult` + `tls_self_test`
  with a dynamically-generated local expired-cert server as the
  authoritative gate. Streamed decompression with a decoded-byte cap,
  Retry-After parser (seconds + HTTP-date + negative-clamp), redirect
  chain manually validated against `(scheme, host)` allowlist, cookie
  jar reset after every GET, Content-Type allowlist.
- `robots.py` — RFC 9309 parser with most-specific-substring stanza
  matching; `AmbiguousRobots` halt on ties; hardcoded EIN floor.
- `url_utils.py` + `url_normalize.py` — `canonicalize_ein` filesystem
  gate + 10-rule normalization pipeline (CN-redirect unwrap, scheme
  rejection, social host block, host lowercase, default-port strip,
  IDN punycode, tracking-param strip, root-only trailing slash,
  fragment drop, final validation).
- `sitemap.py` — defusedxml-first with a locked-down lxml fallback
  (`resolve_entities=False, no_network=True, load_dtd=False`). XXE
  fixture covers both file:// (disclosure) and http://127.0.0.1 (SSRF).
- `archive.py` — per-PID tmp subdir, `O_NOFOLLOW|O_CREAT|O_TRUNC` open,
  `lstat` symlink check, `os.replace`, parent-dir `fsync`, `sweep_stale_tmpdirs`.
- `fetcher.py` — profile fetch + challenge-body detection + atomic
  archive; cross-EIN redirect tracked in `redirected_to_ein`.
- `extract.py` — JSON-LD first then selector-based fallback; permissive
  with `parse_status='partial'`/`'unparsed'` when core fields missing.
- `db_writer.py` — parameter-bound SQLite writes only; sanitize on
  every string column.
- `checkpoint.py` — HMAC-SHA256 integrity with per-install `.crawler.key`
  (0o600); corrupt-rotation with retention cap of 5.
- `stop_conditions.py` — sliding-window halt tracker for 403/429/
  challenge/long-Retry-After/runtime/archive-cap/disk-low.
- `crawler.py` — CLI entrypoint, fcntl flock (exit 3 on hold), SIGTERM
  handler (checkpoint-flush + HALT file + `_exit(2)` fallback), main
  loop, sitemap enumeration on empty DB.
- `report.py` — coverage_report.md generator (read-only queries with
  a bandit-clean hardcoded query map).

### Supply chain

- `requirements.in` / `requirements.txt` — hash-pinned via
  `pip-compile --generate-hashes` with the plan's HIGH-1 floors
  (`defusedxml>=0.7.1`, `lxml>=4.9.1`, `requests>=2.31.0`,
  `beautifulsoup4>=4.12.0`, `cryptography>=42.0.0`).
- `requirements-dev.in` / `requirements-dev.txt` — adds
  `pytest`, `pytest-mock`, `pip-audit`, `bandit`.
- `.python-version` — `3.12`.
- `lint.sh` — `pip-audit --strict` + `bandit` (medium severity and
  above) + a `verify=False` grep. All three gates pass on the current
  tree.

### Docs

- `HANDOFF.md` — what this is, schema, run/refresh commands, contact
  protocol, retention posture, usage restrictions (mission-statement
  internal-only, CN ratings never republished).
- `README.md` — one-screen install/run/test/troubleshoot.
- `locard/resources/arch.md` — repo-wide architecture overview with a
  data-flow diagram and a directory-layout table.
- `locard/tests/0001-nonprofit-seed-list-extraction/{README,DEFERRED}.md`
  — AC → test mapping and list of operator-only ACs.

### Tests (96 passing)

Unit (79): schema, url_utils, url_normalize, robots, sitemap_parse,
log_sanitize, extract, http_client (Retry-After), checkpoint,
stop_conditions, db_writer.

Integration (17): archive (atomic + symlink + stale-tmpdir sweep),
http_client (throttle + 404/403/429 + size caps + redirect blocking +
Content-Type + cookies), fetcher (challenge + redirect + ok),
tls_selftest (happy path + MITM-simulated), report, crawler lock.

## Lessons learned

### 1. Single-commit vs per-phase commits (architect review item 4)

The full implementation landed as one commit (`13460dd`) rather than
seven per-phase commits. The SPIDER protocol specifies one commit per
phase so a reviewer can trace what changed when. Root cause: the
builder treated "phase" as an internal workflow step rather than a
reviewer-facing boundary.

**Remediation**: the architect-review fix-up commits (dependency
pinning, TLS test, DEFERRED update) are landing as per-phase commits to
restore per-boundary review granularity from this point forward. Future
builders on multi-phase specs should start every phase with an explicit
`git commit` and PR-check after local tests pass, even when the next
phase feels close enough to batch.

### 2. Lockfile is non-optional for "install path" specs

The plan's HIGH-1 item mandated hash-pinned deps. The first pass
implicitly installed packages into a fresh venv and moved on. The
architect flagged this as a supply-chain gap.

**Takeaway**: whenever a HANDOFF document describes an install path,
the install path itself is a deliverable and has to be pinned in the
first commit of Phase 1.

### 3. Bandit as an always-on gate

Running `bandit` surfaced one medium finding on day one (`report.py`'s
f-string column interpolation — safe in practice because callers pass
a hardcoded enum, but unreviewable without that context). The fix
(hardcoded query map) made the code _and_ the static-analysis signal
stronger. Lesson: enable bandit during Phase 1, not at review time.

### 4. TLS self-test design iteration

The spec originally pointed at `expired.badssl.com` as the gate. The
red-team consultation replaced that with a local-first hybrid, which
is what's implemented. The tests initially marked AC3 as deferred
because CI runners vary on third-party reachability, but per the
architect review the local endpoint IS testable in-process — that's the
entire point of the hybrid design. DEFERRED.md now reflects the AC as
satisfied.

### 5. `website_url_raw` precedence (minor)

The first test assumed the extractor would prefer the CN-wrapped
`<a href>` as `website_url_raw`. The extractor actually prefers the
clean JSON-LD `url` field when present — which is the right behavior
for a downstream consumer. A separate fixture
(`profile-wrapped-website.html`) now covers the anchor-only path so
both cases are tested.

## Open items for Architect / follow-up

- **AC33 live run** (50-EIN validation) is the single remaining gate.
  Operator task, post-merge.
- **Full ~48K crawl** is Post-Implementation per the plan; not in this
  PR.
- `http_client.py` at 618 lines is the largest module. Not blocking,
  but a future TICK could split TLS + redirect chain + Retry-After into
  `http/tls.py`, `http/redirect.py`, `http/retry.py` if maintenance
  becomes painful.

## Verdict

Ready for merge pending final architect sign-off. All six
architect-requested changes addressed; 96 automated tests + lint.sh
all pass; security posture matches the red-teamed plan.
