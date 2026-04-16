---
spec_id: "0001"
title: "Nonprofit Seed List Extraction"
version: 2
features: []
---

# Specification: Nonprofit Seed List Extraction

## Metadata
- **ID**: spec-2026-04-16-nonprofit-seed-list-extraction
- **Status**: draft
- **Created**: 2026-04-16

## Clarifying Questions Asked

The following exchange shaped this spec:

- **Q: What is the business goal?**
  A: Lavandula Design wants to offer nonprofit impact-report / annual-report design as a service. We first need a catalogue of existing reports to study design trends, build an inspiration library, and generate prospect lists. Harvesting reports requires a seed list of nonprofit websites; this spec produces that seed list.

- **Q: Where should the nonprofit list come from?**
  A: Ruled out: IRS BMF/990 XML (too administrative, no website URLs / too much enrichment needed), agency portfolios (too curated / too sliced), `nonprofitlocator.org` (verified: no website URLs on org pages). Selected: **Charity Navigator**, which exposes all ~48K rated-org profile URLs via a public sitemap index, and each profile page includes the org's website URL plus rating/revenue/sector metadata.

- **Q: API or scrape?**
  A: Scrape. Charity Navigator's commercial data API has non-public enterprise pricing and is overkill for a one-time extraction. Their public sitemap + `robots.txt` explicitly permit crawling `/ein/*` paths. Respectful throttle + identifying UA makes this an ethically acceptable one-time research extraction.

- **Q: What throttle and duration?**
  A: 3 seconds per request, single worker, no parallelism. ~40 hours wall-clock (weekend). This is polite by research-crawler standards and invisible to a Cloudflare-fronted site of this scale.

- **Q: What fields matter?**
  A: At minimum: `ein, name, website_url, rating, revenue, state`. Nice-to-have: `expenses, program_expense_ratio, sector (NTEE), city, address, mission_statement, year_founded`. Everything that helps filter for "orgs that commission designed reports."

- **Q: Scope boundary?**
  A: One-time extraction of the current sitemap snapshot. No ongoing monitoring in this project. A future project may refresh via API if needed.

## Problem Statement

Lavandula Design needs to build a catalogue of nonprofit annual / impact reports as the foundation of a new service offering. That catalogue requires a seed list of nonprofit websites to harvest PDFs from. No single public source provides `(nonprofit_name, website_url, quality_signal)` tuples at sufficient scale and fidelity — IRS data lacks website URLs, agency portfolios only show a sliver, and consumer-facing locator sites don't expose URL fields. **Charity Navigator's public sitemap is the only source that provides all three, at enumerable scale (~48K orgs), for free.**

Without this seed list, the downstream report-harvesting bot has no targets. This spec unblocks that entire product line.

## Current State

- We have zero structured data on nonprofit web presences.
- Existing infrastructure (`nptech/`) proves out the throttled-crawler pattern against a small WordPress target (~1,068 items), with checkpointing, logging, and off-peak operation.
- Charity Navigator's discovery surface was verified manually (see References):
  - `https://www.charitynavigator.org/extra-index.xml` → 48 child sitemaps (`Sitemap1.xml` … `Sitemap47.xml`)
  - Each child sitemap contains ~1,000 `<loc>` entries of the form `https://www.charitynavigator.org/ein/{EIN}`
  - `robots.txt` disallows `/search/`, `/profile/`, `/basket/`, and two specific EINs, but **explicitly permits `/ein/*`**
  - Individual profile pages (verified against EIN 530196605 = American Red Cross) render the organization's website URL, star rating, score, name, mission, and address
- No workaround exists today. Manually curating a list of 10K+ org URLs is not viable.

## Desired State

At the end of this project:

1. A SQLite database at `lavandula/data/nonprofits.db` (new directory sibling to `nptech/`) holding one row per extracted nonprofit profile.
2. A fully-versioned raw archive of every fetched HTML page at `lavandula/raw/cn/{ein}.html`, so re-extraction against new parsers is a pure local transform (mirrors nptech's "raw/ is append-only ground truth" invariant).
3. A small CLI (`lavandula/nonprofits/crawl.py`, `extract.py`, `report.py`) following the nptech conventions so the codebase has a coherent pattern.
4. The database is queryable for segments such as:
   - `rating >= 4 AND revenue >= 5_000_000` → prime prospects for designed reports
   - `sector = 'arts' AND state = 'NY'` → sector-targeted sales lists
   - `website_url IS NOT NULL` → ~90% coverage expected based on sample
5. Clear handoff documentation so the future report-harvesting project can consume this dataset without interpretation guesswork.

## Stakeholders

- **Primary User**: Ron (Lavandula Design) — queries the DB for prospect research, feeds filtered URL lists into the report-harvesting bot.
- **Secondary Users**: Future Builder(s) implementing the report-harvesting project; Lavandula sales team (eventual consumers of filtered lists).
- **Technical Team**: Ron + AI agents in this repo.
- **Business Owner**: Ron (Lavandula Design).
- **External**: Charity Navigator is the data provider. We are not obligated to notify them, but we owe them respectful technical behavior.

## Success Criteria

- [ ] Sitemap discovery: all 48 child sitemaps fetched and parsed; total enumerated EIN count is within 5% of the sitemap-index total.
- [ ] Coverage: ≥ 95% of enumerated profile URLs successfully fetched and parsed. Unreachable / 404 / blocked profiles are logged and retriable.
- [ ] Website-URL extraction: ≥ 85% of fetched profiles have a non-empty `website_url` populated. (Some orgs don't publish one. A spot sample of 20 confirms extractor accuracy.)
- [ ] Core fields populated: `ein, name` are 100%; `rating, revenue, state` are ≥ 90%.
- [ ] Idempotency: re-running the crawl against an existing checkpoint re-fetches nothing unless `--refresh` is passed.
- [ ] Respectful operation: effective request rate ≤ 0.4 req/s (i.e., ≥ 2.5 s average between requests); zero reports of 429/abuse from Charity Navigator during or after the run.
- [ ] Documentation: a `lavandula/nonprofits/HANDOFF.md` exists describing schema, how to query, how to refresh.
- [ ] All tests pass; test coverage ≥ 80% on extraction / parsing logic (not on the network layer).

## Constraints

### Technical Constraints

- **Must reuse the throttled-client pattern** from `nptech/http_client.py` (or abstract it into a shared module). Do not introduce a second pattern.
- **Must honor `robots.txt`**. Explicitly check `/search/`, `/profile/`, `/basket/`, and the two disallowed EINs; skip them at the crawler level.
- **Must cache raw HTML** to disk before parsing, so the parser is a pure local transform.
- **Must be resumable**: a SIGINT / power loss / retry-exhaustion mid-run must not require restarting from zero.
- **Must not require secrets / auth** — pure public-data crawl.
- **Python 3.12+** (consistent with `nptech/`).

### Business Constraints

- No paid API subscription this quarter. If an ongoing refresh is needed later, that's a future spec.
- Output must be usable by a non-technical stakeholder (Ron) — i.e., SQL or a thin CLI, not a programmatic-only interface.

### Legal / Compliance Constraints

- We rely on: (a) the site's `robots.txt` permitting `/ein/*`, (b) no authentication gate, (c) the research use being internal (no redistribution of scraped data), and (d) the data being factual and not creative-work protected by copyright.
- The raw HTML archive is for **internal parsing only** and is not redistributed. If we ever want to publish derivative data we must re-verify terms.
- The Lavandula Design product is to use the extracted facts (org name, URL, rating) — none of which are copyrightable — to inform outreach. We do not republish Charity Navigator's editorial content (reviews, rankings prose, etc.).

## Assumptions

- Charity Navigator's sitemap reflects the complete set of profiles they intend to be publicly crawlable. Anything not in the sitemap is out of scope.
- Profile page HTML structure is stable enough that a BeautifulSoup + CSS-selector parser can handle it with occasional maintenance.
- The website URL on the profile is the org's canonical domain (not a UTM-tagged redirect).
- Charity Navigator does not aggressively rate-limit well-behaved crawlers at 0.3 req/s. (If they do, we detect via 429 and extend the throttle.)
- Network reliability on our host is adequate for a ~2-day continuous crawl (same host that runs `nptech/` nightly successfully).

## Solution Approaches

### Approach 1: Sitemap-driven scrape (RECOMMENDED)

**Description**: Fetch the sitemap index, enumerate the 48 child sitemaps, extract all `/ein/*` URLs, then fetch each profile at a 3-second throttle and parse with BeautifulSoup. Raw HTML archived to disk; parsed fields written to SQLite.

**Pros**:
- Uses only public, sitemap-advertised URLs (explicit crawl permission)
- No auth, no API key, no paid tier
- Reuses nptech's crawler pattern — minimal new architecture
- Raw archive decouples fetching from parsing (re-parse is free)
- Checkpoint + resume is well-understood from nptech

**Cons**:
- ~40 hours wall-clock
- HTML parser is fragile to site redesigns (mitigated by raw archive)
- No way to detect newly-added profiles without re-pulling the sitemap

**Estimated Complexity**: Low-Medium
**Risk Level**: Low

### Approach 2: Paid Charity Navigator Data Feed

**Description**: License the commercial API; hit a clean JSON endpoint per EIN; skip HTML parsing entirely.

**Pros**:
- Structured JSON, no fragile parsing
- Explicitly sanctioned access pattern
- Supports ongoing refresh as part of the product

**Cons**:
- Enterprise pricing (typical industry range $3K–$25K/yr) is prohibitive for a single one-time extraction
- Procurement overhead delays delivery by weeks
- We don't yet know if the downstream product will need ongoing refresh

**Estimated Complexity**: Low (but blocked on procurement)
**Risk Level**: Medium (vendor dependency, pricing surprise)

### Approach 3: Hybrid — scrape for seed, API for refresh

**Description**: Do Approach 1 now; defer API purchase to a future spec if ongoing freshness is needed.

**Pros**:
- Minimizes commitment
- Delivers usable seed list this weekend
- Learns whether refresh is actually needed before paying

**Cons**:
- Requires two migrations (scrape → API) if API comes later
- Slight risk of API schema differing from scraped fields

**Estimated Complexity**: Low
**Risk Level**: Low

### Approach 4: Manual curation

**Description**: Hand-pick 200–500 nonprofits from Chronicle of Philanthropy 400, Forbes Top 100, Charity Navigator "Best Of" lists.

**Pros**:
- No crawl risk
- Curated for relevance

**Cons**:
- Order of magnitude smaller sample
- Biased toward the top tier — misses the mid-size orgs who are actually Lavandula's sweet-spot prospects
- Manual labor

**Estimated Complexity**: Low
**Risk Level**: Low (but low value)

### Recommendation

**Approach 3** — execute Approach 1 now, document the path to Approach 2 in case it's needed later. That is what this spec scopes.

## Open Questions

### Critical (Blocks Progress)
- [ ] **User-Agent identity**: nptech uses a neutral "research-indexer" UA. Should the Charity Navigator crawler identify Lavandula Design by name (more transparent — may invite a cease-and-desist or just a conversation) or stay neutral (less friction, but less honest)? **Leaning neutral, matching nptech precedent, but want explicit sign-off.**
- [ ] **Where to put the code**: nptech lives at `nptech/`; should this live at `lavandula/nonprofits/`, `charitynav/`, or be integrated into a shared `crawlers/` tree? **Leaning `lavandula/nonprofits/`.**

### Important (Affects Design)
- [ ] **Rating & revenue parsing**: Charity Navigator uses a scoring system (0–100) plus a star rating (1–4) plus "Beacons Completed" (0/4). Which of these do we persist? **Recommend: all three, plus `overall_score`.**
- [ ] **Sector taxonomy**: Charity Navigator uses their own cause categorization AND NTEE codes. NTEE is more standard and joins to IRS data cleanly; CN cause categories are marketing-friendly. **Recommend: store both.**
- [ ] **Handling non-rated profiles**: some `/ein/` pages are for orgs that haven't been evaluated — they show name/mission but no rating or financials. Do we keep them? **Yes — still useful as a URL source, just flagged `rated=False`.**
- [ ] **Deduplication**: an org can have multiple historical EINs (mergers, renames). Do we deduplicate by canonical website domain? **Post-extraction pass, out of scope for v1.**

### Nice-to-Know (Optimization)
- [ ] Can we extract `year_founded` from the profile? (Occasionally present.)
- [ ] Does Charity Navigator expose board size, employee count, or other structural signals? (Would help segment by org complexity.)
- [ ] Is there a 990-filing link on the profile we can preserve for cross-referencing with ProPublica Nonprofit Explorer?

## Performance Requirements

- **Effective request rate**: ≤ 0.4 req/s sustained (equivalent to ≥ 2.5 s average interval, respecting the 3 s configured throttle with retry slack).
- **Total runtime**: ≤ 60 hours (allowing 50% padding over the 40-hour happy-path estimate for retries, backoff, and network variability).
- **Disk footprint**: ~15 GB for raw HTML archive (~48K × ~300 KB median), ~50 MB for SQLite DB.
- **Memory**: < 300 MB resident. We stream-parse sitemaps and process one profile at a time.
- **Resumability**: after SIGINT or crash, re-invoking the crawler must pick up within 1 profile of the last successful fetch.

## Security Considerations

- **Authentication**: none. All data is public.
- **Authorization**: we honor `robots.txt`, including the two explicitly-disallowed EINs.
- **Data privacy**: the extracted data is exclusively organizational, not personal. No PII is harvested. Staff names (if they appear on profiles) are public officer/director listings; we do not persist them.
- **Secrets management**: no secrets. Nothing to leak.
- **Audit**: every HTTP request is logged with timestamp, URL, status code; raw responses are on disk.
- **Reputation**: our crawler's behavior reflects on Lavandula Design. A poorly-behaved crawler creates reputational and legal risk. Mitigations:
  - Identifiable but non-deceptive User-Agent
  - Respectful throttle (3s; auto-extend on 429)
  - `robots.txt` compliance
  - Off-peak operation ideal but not required (CN's scale tolerates any-time low-rate crawling)
  - Graceful shutdown on sustained 4xx/5xx signaling blocking
- **Storage**: raw HTML archive stays local. No cloud upload, no redistribution. If we ever want to share derivative data, we re-evaluate terms first.

## Test Scenarios

### Functional Tests
1. **Happy path**: given a known `/ein/530196605` (Red Cross) HTML fixture, the parser produces `{ein, name, website, rating, score, revenue, state}` matching expected values.
2. **Non-rated org**: given a fixture for an unrated profile, the parser produces name/mission but sets `rating=None, rated=False`.
3. **Missing website**: given a fixture with no website URL visible, the parser records `website=None` without raising.
4. **Sitemap index parse**: given the `extra-index.xml` fixture, the parser enumerates exactly 48 child sitemap URLs.
5. **Child sitemap parse**: given a `Sitemap1.xml` fixture, the parser enumerates ~1,000 EIN URLs with valid `/ein/{9-digit-numeric}` format.
6. **Disallowed EIN filter**: given the two `robots.txt`-disallowed EINs, the crawler skips them at enumeration time.
7. **Checkpoint resume**: given a partial run with 5,000 EINs in checkpoint, a second invocation processes only the remaining ~43K.

### Non-Functional Tests
1. **Throttle enforcement**: 100 consecutive requests complete in ≥ 300 seconds (3 s × 100).
2. **Retry behavior**: injected 429 response triggers backoff; injected 503 triggers retry; permanent 404 logs and skips.
3. **Schema validation**: every row in `nonprofits.db` satisfies the declared schema (EIN is 9-digit string, rating is 1-4 or NULL, etc.).

### Integration Tests
1. **Dry-run mode** (`--limit 10 --no-archive`): completes in < 45 s, exits cleanly, produces a parseable DB of 10 rows without mutating the real archive.
2. **Full pipeline against staging fixtures**: end-to-end with 20 real-site HTML fixtures covering rated / unrated / missing-website / redirected cases.

## Dependencies

- **External Services**: Charity Navigator (`www.charitynavigator.org`) — HTTPS only, public.
- **Internal Systems**: none required. Optional: factor the throttled-client from `nptech/http_client.py` into a shared `common/http_client.py`.
- **Libraries**:
  - `requests` (already used by nptech)
  - `beautifulsoup4` + `lxml` (already used by nptech)
  - Python stdlib `sqlite3`
  - `tenacity` or in-house retry (in-house preferred for consistency with nptech)
- **Dev-time**: `pytest` + fixtures directory.

## References

- Interactive site-verification conversation (2026-04-16): confirmed sitemap structure, robots.txt, profile-page fields.
- Charity Navigator sitemap index: `https://www.charitynavigator.org/extra-index.xml`
- Charity Navigator robots.txt: `https://www.charitynavigator.org/robots.txt`
- Red Cross reference profile: `https://www.charitynavigator.org/ein/530196605`
- Existing crawler precedent: `nptech/crawler.py`, `nptech/http_client.py`, `nptech/HANDOFF.md`
- Downstream consumer (future spec): report-harvesting bot that consumes `nonprofits.db` as its seed list.

## Risks and Mitigation

| Risk | Probability | Impact | Mitigation Strategy |
|------|------------|--------|---------------------|
| Charity Navigator adds anti-bot challenges (Cloudflare Turnstile, JS challenge) mid-run | Low | High | Throttle is already so low that we shouldn't trigger it. If we do, pause, switch UA, add delay, or escalate to API conversation. |
| Site redesigns the profile page structure, breaking the parser | Medium | Medium | Raw HTML archive lets us re-parse without re-crawling. Parser isolated behind a single module so fixes are local. |
| `lastmod` dates across the sitemap reveal we're catching profiles mid-update | Low | Low | One-shot snapshot is acceptable; a future refresh pass can reconcile. |
| Scope creep: user asks "while we're at it, also grab…" during the run | Medium | Medium | This spec is explicitly one-shot and narrow. Additional sources are separate specs. |
| Legal / ToS complaint from Charity Navigator | Low | Medium | robots.txt compliance is our strongest defense. If a takedown request arrives, we respect it immediately and pivot to API approach. |
| Host disk fills during 15GB archive write | Low | High | Pre-flight disk-space check; archive target on a partition with ≥ 50 GB free. |
| Crawl runs into tonight's nptech cron window (04:00 UTC) and competes for resources | Low | Low | Both are I/O-bound with tiny CPU. Bandwidth is non-overlapping (different hosts). Run anyway. |

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: _pending_
**Models Consulted**: Gemini Pro, GPT-5 Codex
**Key Feedback**: _to be populated_
**Sections Updated**: _to be populated_

### Second Consultation (After Human Review)
**Date**: _pending_
**Models Consulted**: Gemini Pro, GPT-5 Codex
**Key Feedback**: _to be populated_
**Sections Updated**: _to be populated_

### Red Team Security Review (MANDATORY)
**Date**: _pending_
**Command**: `consult --model gemini --type red-team-spec spec 0001`
**Findings**: _to be populated_

| Severity | Issue | Attack Vector | Mitigation |
|----------|-------|---------------|------------|
| CRITICAL | _pending_ | _pending_ | _pending_ |
| HIGH | _pending_ | _pending_ | _pending_ |
| MEDIUM | _pending_ | _pending_ | _pending_ |
| LOW | _pending_ | _pending_ | _pending_ |

**Verdict**: _pending_

## Approval
- [ ] Technical Lead Review
- [ ] Product Owner Review (Ron)
- [ ] Stakeholder Sign-off
- [ ] Expert AI Consultation Complete
- [ ] Red Team Security Review Complete (no unresolved findings)

## Notes

- This spec is deliberately **narrowly scoped** to a one-shot extraction. The natural next specs are:
  1. **Report-harvesting bot** (pulls PDFs from each org website)
  2. **Report classification + cataloguing** (structures the catalogue)
  3. **Design-idea catalogue** (expands scope to general marketing materials)
- The project will re-use and generalize the nptech crawler pattern. Some refactoring of nptech is possible as a side effect (factor out the throttled client). That is out of scope here; if it proves useful, file a separate TICK.
- **The human must approve this spec before planning begins.** AI agents must not self-promote the status from `conceived` to `specified`.

---

## Amendments

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->
