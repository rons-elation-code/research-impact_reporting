# Project List

Centralized tracking of all projects with status, priority, and dependencies.

> **Quick Reference**: See `locard/resources/workflow-reference.md` for stage diagrams and common commands.

## Project Lifecycle

Every project goes through stages. Not all projects reach completion:

**Active Lifecycle:**
1. **conceived** - Initial idea captured. Spec file may exist but is not yet approved. **AI agents must stop here after writing a spec.**
2. **specified** - Specification approved by human. **ONLY the human can mark a project as specified.**
3. **planned** - Implementation plan created (locard/plans/NNNN-name.md exists)
4. **implementing** - Actively being worked on (one or more phases in progress)
5. **implemented** - Code complete, tests passing, PR created and awaiting review
6. **committed** - PR merged to main branch
7. **integrated** - Merged to main, deployed to production, validated, reviewed (locard/reviews/NNNN-name.md exists), and **explicitly approved by project owner**. **ONLY the human can mark a project as integrated** - AI agents must never transition to this status on their own.

**Terminal States:**
- **abandoned** - Project canceled/rejected, will not be implemented (explain reason in notes)
- **on-hold** - Temporarily paused, may resume later (explain reason in notes)

## Format

```yaml
projects:
  - id: "NNNN"              # Four-digit project number
    title: "Brief title"
    summary: "One-sentence description of what this project does"
    status: conceived|specified|planned|implementing|implemented|committed|integrated|abandoned|on-hold
    priority: high|medium|low
    files:
      spec: locard/specs/NNNN-name.md       # Required after "specified"
      plan: locard/plans/NNNN-name.md       # Required after "planned"
      review: locard/reviews/NNNN-name.md   # Required after "integrated"
    dependencies: []         # List of project IDs this depends on
    tags: []                # Categories (e.g., auth, billing, ui)
    notes: ""               # Optional notes about status or decisions
```

## Numbering Rules

1. **Sequential**: Use next available number (0001-9999)
2. **Reservation**: Add entry to this file FIRST before creating spec
3. **Renumbering**: If collision detected, newer project gets renumbered
4. **Gaps OK**: Deleted projects leave gaps (don't reuse numbers)

## Archiving Completed Projects

Once projects are `integrated` or `abandoned` for 3+ days, move them to `projectlist-archive.md`:

```
locard/
  projectlist.md          # Active projects (conceived → committed)
  projectlist-archive.md  # Completed projects (integrated, abandoned)
```

**Why archive?**
- Keeps daily work file small and fast
- Full history still versioned in git
- Can grep across both files when needed

**Archive format**: Same YAML format, sorted by ID (historical record).

## Usage Guidelines

### When to Add a Project

Add a project entry when:
- You have a concrete idea worth tracking
- The work is non-trivial (not just a bug fix or typo)
- You want to reserve a number before writing a spec

### Status Transitions

```
conceived → [HUMAN] → specified → planned → implementing → implemented → committed → [HUMAN] → integrated
     ↑                                                                                   ↑
Human approves                                                                    Human approves
   the spec                                                                      production deploy

Any status can transition to: abandoned, on-hold
```

**Human approval gates:**
- `conceived` → `specified`: Human must approve the specification
- `committed` → `integrated`: Human must validate production deployment

### Priority Guidelines

- **high**: Critical path, blocking other work, or significant business value
- **medium**: Important but not urgent, can wait for high-priority work
- **low**: Nice to have, polish, or speculative features

### Tags

Use consistent tags across projects for filtering:
- `auth`, `security` - Authentication and security features
- `ui`, `ux` - User interface and experience
- `api`, `architecture` - Backend and system design
- `testing`, `infrastructure` - Development and deployment
- `billing`, `credits` - Payment and monetization
- `features` - New user-facing functionality

---

## Projects

```yaml
projects:
  - id: "0001"
    title: "Nonprofit Seed List Extraction"
    summary: "Extract ~48K US nonprofit profiles (EIN, name, website URL, rating, revenue, sector, state) from Charity Navigator's public sitemap into a queryable SQLite database, to serve as the seed list for a future report-harvesting bot."
    status: implementing
    priority: high
    files:
      spec: locard/specs/0001-nonprofit-seed-list-extraction.md
      plan: locard/plans/0001-nonprofit-seed-list-extraction.md
      review: locard/reviews/0001-nonprofit-seed-list-extraction.md
    dependencies: []
    tags: [crawler, data-acquisition, nonprofit, lavandula-sales]
    notes: "Spec + plan approved 2026-04-17. Implementation merged to master 2026-04-17 after architect APPROVE (.consult/0001/architect-signoff.md). 96 tests passing + lint.sh clean. TICK-001 (curated-lists pivot) added 2026-04-17. TICK-005 + TICK-006 merged 2026-04-20. TICK-008 (IRS fields from ProPublica) spec+plan approved 2026-04-20 — spawning builder."

  - id: "0002"
    title: "Corpus Search Engine (abandoned)"
    summary: "Generic search-to-catalogue pipeline with Google Custom Search provider. Abandoned 2026-04-19 in favor of a site-crawl approach (see 0004) after external developer input + two rounds of multi-agent review showed the search-first architecture kept producing CRITICAL findings that the site-crawl approach sidesteps entirely."
    status: abandoned
    priority: high
    files:
      spec: locard/specs/0002-corpus-search-engine.abandoned.md
      plan: null
      review: null
    dependencies: []
    tags: [infrastructure, search, crawler, shared-engine, abandoned]
    notes: "Abandoned 2026-04-19. Superseded by 0004. Spec and review artifacts preserved at .consult/0002/, .consult/0002-v2/, .consult/0002-v3/."

  - id: "0003"
    title: "Nonprofit Report Catalogue — search-first (abandoned)"
    summary: "Topic plugin on top of 0002's search-first engine. Abandoned 2026-04-19 alongside 0002; the site-crawl approach in 0004 replaces the functional goal and uses LLM-based classification instead of a hand-rolled design-score rubric."
    status: abandoned
    priority: high
    files:
      spec: locard/specs/0003-nonprofit-report-catalogue.abandoned.md
      plan: null
      review: null
    dependencies: ["0002"]
    tags: [lavandula-core, reports, catalogue, classifier, abandoned]
    notes: "Abandoned 2026-04-19 alongside 0002. Superseded by 0004. Review artifacts preserved at .consult/0003/, .consult/0003-v2/."

  - id: "0004"
    title: "Site-Crawl Report Catalogue"
    summary: "Crawl known nonprofit websites directly for annual/impact reports. Uses 0001's curated nonprofit list as the seed, robots.txt + sitemap + homepage-link extraction with anchor-text + URL-path + hosting-platform filters to find candidate PDFs, HEAD/fetch with throttle, and Haiku-class LLM classification of first-page text to decide whether each PDF is actually a real report. Produces the design inspiration library + prospect signal Lavandula needs."
    status: committed
    priority: high
    files:
      spec: locard/specs/0004-site-crawl-report-catalogue.md
      plan: locard/plans/0004-site-crawl-report-catalogue.md
      review: null
    dependencies: ["0001"]
    tags: [lavandula-core, reports, catalogue, crawler]
    notes: "Replaces 0002 + 0003. Spec approved 2026-04-19 after 3 review rounds (CRITICAL progression 4 → 0 → 1 → 0). Plan approved 2026-04-19 after 1 review round (0 CRITICAL; all HIGH addressed in 9b752b5). 8 phases, 39 ACs. Phases 0-6 built 2026-04-19 (TDD scaffolding + schema/HTTP/SSRF + discovery + fetch/archive + sandbox + classifier + orchestration). All 138 AC tests green. Phase 7 live-validation pending. TICK-001 v3 approved 2026-04-19 after 3 revisions (Codex REQUEST_CHANGES → v2 → v3) + Gemini APPROVE. Relaxed PDF filter on report-anchor subpages. Implementation in progress."

  - id: "0005"
    title: "DeepSeek-Backed Nonprofit Website Resolver"
    summary: "Replace Brave Search + heuristic resolver with a model-backed three-phase pipeline: DeepSeek/Qwen generates 2 candidate URLs, SSRF-hardened HTTP verifies them, model confirms identity from homepage content. Supports DeepSeek-V3 and Qwen via OpenAI-compatible client, selected by RESOLVER_LLM env var."
    status: committed
    priority: high
    files:
      spec: locard/specs/0005-deepseek-resolver.md
      plan: locard/plans/0005-deepseek-resolver.md
      review: null
    dependencies: ["0001"]
    tags: [lavandula-core, resolver, llm, deepseek, qwen]
    notes: "Spec approved 2026-04-21 after 2 review rounds + red-team (2 CRITICAL fixed: SSRF + indirect prompt injection). Plan approved 2026-04-21 after 1 review round + red-team (tag breakout HIGH fixed). Builder spawned 2026-04-21. PR #1 merged 2026-04-21 after 8 review rounds. Precision gate (≥80% on TX 100-org dataset) deferred — must run before --resolver llm used on production seeds."

  - id: "0006"
    title: "Pipeline Status Dashboard"
    summary: "Read-only web dashboard exposing live state of seeds, resolver, crawl, classify across all SQLite DBs. Shows counts by state/NTEE/revenue band, resolver status breakdown, crawl progress, classification mix, and any running background jobs. Auto-refresh every 10s. Port 4350, FastAPI + SQLAlchemy for forward compatibility with RDS migration."
    status: conceived
    priority: high
    files:
      spec: locard/specs/0006-pipeline-status-dashboard.md
      plan: null
      review: null
    dependencies: []
    tags: [dashboard, observability, infrastructure]
    notes: "Prioritized 2026-04-22 to eliminate architect pings for status. Not yet specified."

  - id: "0007"
    title: "S3-Backed PDF Archive"
    summary: "Replace local-disk PDF archive with direct-to-S3 streaming upload. Addresses two concerns: (1) disk exhaustion at scale (5K+ orgs × ~5MB × 2 PDFs = 50GB+), (2) data protection (EBS is single-point-of-failure). PDF bytes stream through memory for first-page text extraction, then PUT to S3 by SHA256 key. SQLite metadata unchanged — classifier hot path unaffected."
    status: conceived
    priority: high
    files:
      spec: locard/specs/0007-s3-pdf-archive.md
      plan: null
      review: null
    dependencies: ["0004"]
    tags: [infrastructure, storage, s3, crawler]
    notes: "Blocks big-crawl work on >5K orgs. Bucket: lavandula-nonprofit-collaterals (us-east-1, SSE-S3, versioned, private). Not yet specified."

  - id: "0008"
    title: "Agent Batch Runner"
    summary: "Orchestrate Claude Code WebSearch sub-agents for URL resolution at scale. Takes an EIN list → splits into N-org chunks → spawns K agents in parallel → waits → ingests results back into seeds.db. Resumable (agents append to results file as they go), skip-already-resolved by default."
    status: conceived
    priority: high
    files:
      spec: locard/specs/0008-agent-batch-runner.md
      plan: null
      review: null
    dependencies: ["0001"]
    tags: [resolver, orchestration, agents, haiku]
    notes: "Replaces manual JSON-file shuffling observed in 2026-04-21 TX 100 validation. Critical for weekly batch workflow at 1K-5K orgs per week."

  - id: "0009"
    title: "Address Verification Pass"
    summary: "Second-pass agent fetches the chosen homepage (and about/contact pages) for each resolved org and confirms the street address matches. Detects wrong-state same-name collisions (Columbus TX hospital vs Columbus NE) that the URL-discovery pass misses."
    status: conceived
    priority: high
    files:
      spec: locard/specs/0009-address-verification.md
      plan: null
      review: null
    dependencies: ["0008"]
    tags: [resolver, verification, agents, data-quality]
    notes: "Gap identified 2026-04-21 during Haiku agent eval — search snippets alone can't disambiguate same-name orgs in different states."

  - id: "0010"
    title: "Tiered Model Strategy"
    summary: "Default URL-resolution agents to Haiku for the easy 80%. Route only low-confidence / null / ambiguous results to Opus for a second look. Optional cross-model voting (Haiku + Qwen API) flags disagreements for manual review. Keeps per-org cost low while preserving accuracy."
    status: conceived
    priority: medium
    files:
      spec: locard/specs/0010-tiered-model-strategy.md
      plan: null
      review: null
    dependencies: ["0008"]
    tags: [resolver, cost-optimization, routing]
    notes: "Depends on 0008 (batch runner). Quantitative data from 2026-04-21 run: Haiku matched Opus on 90/100 TX orgs — strong evidence Haiku-first is viable."

  - id: "0011"
    title: "Operational Controls"
    summary: "Hard budget cap per batch run (orgs + tokens). EIN cache so re-runs use cached agent results unless explicitly invalidated. Runtime warnings when approaching quota limits."
    status: conceived
    priority: medium
    files:
      spec: locard/specs/0011-operational-controls.md
      plan: null
      review: null
    dependencies: ["0008"]
    tags: [operations, cost-control, caching]
    notes: "Protects Claude subscription budget across weekly batches."

  - id: "0012"
    title: "Agent Runner Abstraction"
    summary: "Pluggable interface so Claude Code agents, OpenAI Swarm, or local models can be swapped as the URL-resolution backend without rewriting the pipeline. Stable input/output contract; backends register via entry-point or config."
    status: conceived
    priority: low
    files:
      spec: locard/specs/0012-agent-runner-abstraction.md
      plan: null
      review: null
    dependencies: ["0008", "0010"]
    tags: [architecture, future-proofing]
    notes: "Do after 0008+0010 give concrete contracts to abstract. Premature now."

  - id: "0013"
    title: "SQLite → PostgreSQL (RDS) Dual-Write Migration"
    summary: "Staged migration to managed Postgres RDS. Deploys RDS alongside existing SQLite with clean Alembic-managed schema, adds dual-write mode to db_writer so every write hits both backends, one-time backfills existing SQLite rows to RDS, then flips reads to RDS once dual-write is proven stable. Avoids a clean-cutoff pause because corpus build is continuous. Uses SQLAlchemy (already committed for 0006/0008) so the code change is minimal."
    status: conceived
    priority: high
    files:
      spec: locard/specs/0013-rds-postgres-migration.md
      plan: null
      review: null
    dependencies: ["0004", "0007"]
    tags: [infrastructure, database, rds, migration, dual-write]
    notes: "Timing: start in parallel with 0006 (dashboard). Option A (dual-write) chosen 2026-04-22 over clean-cutover and sync-job alternatives because corpus build is continuous (no natural cutoff) and extraction-app (0014) needs to begin before corpus is 'complete.' Crawler writes stay fast on SQLite; RDS receives every write via async queue. After 2-4 weeks of proven dual-write stability, reads flip to RDS and SQLite writes eventually retire. All new specs (0006+) must use SQLAlchemy so dual-write is a config change, not a rewrite."

  - id: "0014"
    title: "PDF Full-Page Text Extraction for Training"
    summary: "Reads PDFs from s3://bucket/pdfs/, runs full-document text extraction (pypdf + OCR fallback via Tesseract or equivalent for scanned docs), produces structured JSON per PDF at s3://bucket/extractions/v1/. Output feeds the interview-training model pipeline. Versioned prefix (v1/) so future extractions don't clobber prior runs."
    status: conceived
    priority: medium
    files:
      spec: locard/specs/0014-pdf-extraction-training.md
      plan: null
      review: null
    dependencies: ["0007", "0013"]
    tags: [extraction, training-data, future-app]
    notes: "Follow-on app. Depends on 0007 for S3 PDF archive and 0013 for RDS. Structured extraction output must preserve sha256 lineage from source PDF."

  - id: "0015"
    title: "Report Gallery UI"
    summary: "Web gallery app for browsing the corpus. Reads metadata from RDS (org name, year, state, NTEE, classification), displays thumbnails from s3://bucket/thumbnails/, full PDFs via presigned S3 URLs. Supports search/filter. Multi-user, read-only, private (auth required)."
    status: conceived
    priority: medium
    files:
      spec: locard/specs/0015-report-gallery.md
      plan: null
      review: null
    dependencies: ["0007", "0013", "0016"]
    tags: [gallery, ui, future-app]
    notes: "Follow-on app. Depends on 0013 (RDS for multi-user reads), 0007 (S3 pdfs), 0016 (thumbnails)."

  - id: "0016"
    title: "PDF Thumbnail Generator"
    summary: "Batch job that reads PDFs from s3://bucket/pdfs/, renders the first page as a JPEG, uploads to s3://bucket/thumbnails/{sha256}.jpg. Runs either post-crawl or on-demand via lambda. Feeds the gallery UI (0015)."
    status: conceived
    priority: low
    files:
      spec: locard/specs/0016-pdf-thumbnail-generator.md
      plan: null
      review: null
    dependencies: ["0007"]
    tags: [rendering, gallery-support, future-app]
    notes: "Needed by 0015 but independent of it. Could run as a Lambda triggered by S3 PUT events."
```

## Next Available Number

**0017** - Reserve this number for your next project

---

## Quick Reference

### View by Status
To see all projects at a specific status, search for `status: <status>` in this file.

### View by Priority
To see high-priority work, search for `priority: high`.

### Check Dependencies
Before starting a project, verify its dependencies are at least `implemented`.

### Protocol Selection
- **SPIDER**: Most projects (formal spec → plan → implement → review)
- **TICK**: Small, well-defined tasks (< 300 lines) or amendments to existing specs
- **EXPERIMENT**: Research/prototyping before committing to a project
