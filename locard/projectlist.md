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
    status: committed
    priority: high
    files:
      spec: locard/specs/0001-nonprofit-seed-list-extraction.md
      plan: locard/plans/0001-nonprofit-seed-list-extraction.md
      review: locard/reviews/0001-nonprofit-seed-list-extraction.md
    dependencies: []
    tags: [crawler, data-acquisition, nonprofit, lavandula-sales]
    notes: "Spec + plan approved 2026-04-17. Implementation merged to master 2026-04-17 after architect APPROVE (.consult/0001/architect-signoff.md). 96 tests passing + lint.sh clean. TICK-001 (curated-lists pivot) added 2026-04-17 after validation revealed the full-sitemap path was both too large (2.3M vs 48K estimated) and too noisy (50% 404 rate). Project demoted from 'core product' to 'prospect-list helper' once 0002 is approved — 0002 is the actual report-catalogue product."

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
    notes: "Replaces 0002 + 0003. Spec approved 2026-04-19 after 3 review rounds (CRITICAL progression 4 → 0 → 1 → 0). Plan approved 2026-04-19 after 1 review round (0 CRITICAL; all HIGH addressed in 9b752b5). 8 phases, 39 ACs. Phases 0-6 built 2026-04-19 (TDD scaffolding + schema/HTTP/SSRF + discovery + fetch/archive + sandbox + classifier + orchestration). All 138 AC tests green. Phase 7 live-validation pending."
```

## Next Available Number

**0005** - Reserve this number for your next project

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
