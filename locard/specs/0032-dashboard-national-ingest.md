# Spec 0032: Dashboard & Phase Pages for National Ingest Tracking

## Problem

The current dashboard shows aggregate pipeline stats (total resolved, total classified, etc.) with no per-state breakdown. As we scale from 15 states to a full 50-state national ingest, there's no way to see at a glance which states have completed which pipeline stages, which are in progress, and which haven't started.

The resolver and classifier phase pages lack the operational context that the seeder page already provides: recent job history, running job visibility, and per-state stats.

## Goals

1. **National ingest tracker** — replace the current dashboard with a state × pipeline-stage progress grid so operators can see the full ingest status at a glance
2. **Phase page parity** — bring all state-oriented phase pages (resolver, classifier, crawler, phone enrich) up to the seeder's standard: recent jobs table, running job with config, per-state stats. For non-state pages (990 index, 990 parse), add recent jobs table and enhanced running job display only (no state grouping — these are organized by filing year).
3. **Running job visibility** — show job config (engines, QPS, LLM, limits) on running jobs so operators know what's executing without checking logs

## Non-Goals

- Real-time log streaming (use job detail page for that)
- Job editing/cancellation from the dashboard (use job detail page)
- Historical trend charts or time-series data
- Changing the seeder page (it's already the model)
- Removing existing page-specific features (crawler's recent orgs/reports tables, 990 index's refresh history, etc.) — we ADD the standard sections alongside them

## Design

### 1. Main Dashboard — National Ingest Progress

Replace the current six summary cards with a **state progress table** — one row per state, columns for each pipeline stage.

#### State Progress Table

| State | Seeded | Resolved | Crawled | Classified | Reports |
|-------|--------|----------|---------|------------|---------|
| CA    | 6,916  | 0 / 6,916 (0%) | 0 | 0 | 0 |
| CT    | 1,832  | 1,356 / 1,832 (74%) | 1,200 | 950 | 2,100 |
| TX    | 109    | 85 / 109 (78%) | 50 | 30 | 75 |
| ...   |        |          |         |            |         |

**Columns:**
- **State**: two-letter code
- **Seeded**: total orgs in nonprofits_seed for this state
- **Resolved**: `resolved_count / seeded_count (pct%)` — color-coded: 0% gray, 1-79% yellow, 80%+ green
- **Crawled**: count of orgs in crawled_orgs for this state
- **Classified**: count of reports with non-null classification for orgs in this state
- **Reports**: total reports archived for orgs in this state

**Row ordering**: states with `running` or `pending` jobs (per `Job.status` where `Job.state_code` matches the row's state) first, highlighted with a left border accent (e.g. blue-500 for running, yellow-500 for pending). Then remaining states by seeded count descending. Global jobs (no `state_code`, e.g. multi-state seed jobs) appear in the Running Jobs section above the table but do not reorder any state row.

**States not yet seeded**: do NOT show. Only states with rows in nonprofits_seed appear.

**Zero values**: show `0` for raw counts, `0 / N (0%)` for ratio columns. If denominator is also 0, show `—`.

#### Running & Recent Jobs Section

Above the state table, show:

**Running Jobs** (if any):
```
🔵 Job #142 — resolve TX | deepseek-v4-flash | brave engine | 50 QPS | 4 threads | Started 12m ago
🔵 Job #143 — classify FL | deepseek-v4-flash | Started 5m ago
```

Show key config params inline so operators know what's executing. Pull from `job.config_json`.

**Recent Jobs** (last 10 across all phases, all statuses including running/pending):
Standard job table: ID, Phase, State, Status, Exit Code, Duration, Created. Jobs without a state_code show `—` in the State column.

### 2. Resolver Page Enhancement

Keep the existing form and status card. Add:

#### Resolver Stats by State
Same pattern as seeder's "Seeds by State" grid — compact pills showing:
```
TX: 85/109 (78%)  |  FL: 4180/5971 (70%)  |  CT: 1356/1832 (74%)  |  ...
```

Each pill shows `resolved / total (pct)`. Color: gray if 0%, yellow if <80%, green if ≥80%.

#### Recent Resolve Jobs
Last 20 resolve jobs. Standard job table columns: ID, State, Status, Exit Code, Progress (current/total), Duration, Created.

### 3. Classifier Page Enhancement

Keep the existing forms (queue + ad-hoc) and status card. Add:

#### Classifier Stats by State
Compact pills showing classified report counts per state:
```
TX: 30 classified  |  FL: 950 classified  |  CT: 800 classified  |  ...
```

Include unclassified count if meaningful: `TX: 30/75 (40%)` where 75 is total reports for TX orgs.

#### Recent Classify Jobs
Job table matching the seeder pattern. Last 20 classify jobs.

### 4. Phone Enrich Page Enhancement

Keep existing status card with phone/resolved counts. Add:

#### Phone Stats by State
Compact pills: `TX: 45/85 (53%)  |  FL: 320/4180 (8%)  |  ...`
Each pill shows `phones_found / resolved_orgs (pct)`.

#### Recent Phone Enrich Jobs
Job table matching the seeder pattern. Last 20 enrich-phone jobs.

### 5. Crawler Page Enhancement

Keep existing forms (queue + ad-hoc), status card, and recent orgs/reports tables. Add:

#### Crawled Orgs by State
Compact pills: `TX: 50/85 (59%)  |  FL: 3200/4180 (77%)  |  ...`
Each pill shows `crawled_orgs / resolved_orgs (pct)`. Only states with resolved orgs shown.

#### Recent Crawl Jobs
Job table matching the seeder pattern. Last 20 crawl jobs.

### 6. 990 Index Page Enhancement

Keep existing refresh history, status counts, and form. Add:

#### Recent 990 Index Jobs
Job table matching the seeder pattern. Last 20 990-index jobs.

Running job display enhanced with config params (filing year, etc.).

### 7. 990 Parse Page Enhancement

Keep existing filing/people counts and form. Add:

#### Recent 990 Parse Jobs
Job table matching the seeder pattern. Last 20 990-parse jobs.

Running job display enhanced with config params (filing year, limit, etc.).

## Technical Implementation

### Database Queries

**Dashboard state progress** — single query with conditional aggregation:
```sql
SELECT
    s.state,
    COUNT(*) as seeded,
    SUM(CASE WHEN s.resolver_status = 'resolved' THEN 1 ELSE 0 END) as resolved,
    SUM(CASE WHEN s.resolver_status = 'unresolved' THEN 1 ELSE 0 END) as unresolved,
    SUM(CASE WHEN s.resolver_status IS NULL THEN 1 ELSE 0 END) as pending,
    COUNT(DISTINCT co.ein) as crawled,
    SUM(CASE WHEN s.phone IS NOT NULL AND s.phone != '' THEN 1 ELSE 0 END) as has_phone
FROM lava_corpus.nonprofits_seed s
LEFT JOIN lava_corpus.crawled_orgs co ON s.ein = co.ein
GROUP BY s.state
ORDER BY COUNT(*) DESC
```

Report/classification counts per state require joining through corpus. Use subquery to avoid double-counting from the seed→corpus join:
```sql
SELECT
    s.state,
    COUNT(DISTINCT c.content_sha256) as total_reports,
    COUNT(DISTINCT CASE WHEN c.classification IS NOT NULL THEN c.content_sha256 END) as classified
FROM lava_corpus.nonprofits_seed s
JOIN lava_corpus.corpus c ON s.ein = c.source_org_ein
GROUP BY s.state
```
Both total_reports and classified use `COUNT(DISTINCT content_sha256)` to prevent inflation from duplicate join rows.

**Database connection**: Execute raw SQL against `connections["pipeline"]` (search_path=lava_corpus). Both `default` (lava_dashboard) and `pipeline` (lava_corpus) point to the same RDS instance — schema-qualified table names (`lava_corpus.nonprofits_seed`) work on either, but using `pipeline` is consistent with existing view patterns. Job queries use Django ORM on `default` as they already do.

**Performance**: These are aggregation queries over the full dataset. With 50 states × ~200K orgs, they should complete in <2s on RDS. Required indexes: `crawled_orgs(ein)` (already exists as PK), `corpus(source_org_ein)` (verify exists — add if not). The view merges the two result sets in Python (dict keyed by state).

**HTMX polling**: Only the main dashboard auto-refreshes (every 5s, existing pattern). Phase pages load on navigation and do NOT auto-poll — their stats and recent jobs are current as of page load. This avoids multiplying DB pressure across 7+ open tabs. If the dashboard aggregation queries exceed 2s under load, add a 30s Django cache on the state progress data (the running/recent jobs section stays uncached).

### Files Changed

| File | Change |
|------|--------|
| `views.py` | Update `DashboardView`, `ResolverView`, `ClassifierView`, `PhoneEnrichView`, `CrawlerView`, `EnrichIndexView`, `EnrichParseView` with new context queries |
| `dashboard_stats.html` | Rewrite: state progress table + running/recent jobs |
| `resolver.html` | Add stats-by-state grid + recent jobs table |
| `classifier.html` | Add stats-by-state grid + recent jobs table |
| `phone_enrich.html` | Add stats-by-state grid + recent jobs table |
| `crawler.html` | Add stats-by-state grid + recent jobs table (keep existing orgs/reports tables) |
| `990_index.html` | Add recent jobs table (keep existing refresh history + status counts) |
| `990_parse.html` | Add recent jobs table (keep existing filing/people counts) |
| `models.py` | No changes (unmanaged models, raw SQL for cross-table aggregation) |

### Job Config Display

Extract display-worthy keys from `job.config_json` for the running job display:
- **resolve**: state, search_engines, llm_model, brave_qps/search_qps, consumer_threads, limit
- **classify**: state, llm_model, definition, limit, re_classify
- **enrich-phone**: state, search_engines, limit
- **crawl**: state, limit
- **seed**: states, target, ntee_majors
- **990-index**: filing_year
- **990-parse**: filing_year, limit

Format as compact inline text, not a raw JSON dump.

**Security**: Only render keys from the per-phase allowlist above. Never surface `api_key`, `ssm`, `password`, or unknown keys. Django templates auto-escape by default — do not use `|safe` on config values. Handle missing keys gracefully: skip absent keys, show `—` for null values. Old jobs may have incomplete config_json.

## Acceptance Criteria

### Dashboard
1. State progress table shows one row per seeded state
2. Columns: State, Seeded, Resolved (count + pct), Crawled, Classified, Reports
3. Resolved column color-coded: gray (0%), yellow (<80%), green (≥80%)
4. States with running jobs appear first, highlighted
5. Running jobs section shows phase, state, key config params, elapsed time
6. Recent jobs section shows last 10 jobs across all phases
7. HTMX auto-refresh preserved (5s interval)

### Resolver Page
8. Stats-by-state grid showing resolved/total (pct) per state, color-coded
9. Recent resolve jobs table (last 20): ID, State, Status, Exit, Duration, Created
10. Running job shows state and config params
11. Existing form and functionality unchanged

### Classifier Page
12. Stats-by-state grid showing classified/total reports per state
13. Recent classify jobs table (last 20)
14. Running job shows state and config params
15. Existing forms (queue + ad-hoc) and functionality unchanged

### Phone Enrich Page
16. Stats-by-state grid showing phones found / resolved per state
17. Recent phone enrich jobs table (last 20)

### Crawler Page
18. Stats-by-state grid showing crawled / resolved per state
19. Recent crawl jobs table (last 20)
20. Existing recent orgs and recent reports tables preserved

### 990 Index Page
21. Recent 990-index jobs table (last 20)
22. Existing refresh history and status counts preserved

### 990 Parse Page
23. Recent 990-parse jobs table (last 20)
24. Existing filing/people counts preserved

### General
25. No new database migrations required
26. Pages load in <3s even with 50 states of data
27. Mobile-responsive: dashboard state table uses horizontal scroll (`overflow-x-auto`) on narrow screens; phase page pill grids wrap naturally via flexbox
28. All running job displays show config params (not raw JSON)

## Traps to Avoid

1. **Don't use Django ORM for cross-schema joins** — nonprofits_seed and corpus are in lava_corpus schema, jobs are in lava_dashboard. Use raw SQL for the aggregation queries, Django ORM for Job queries.
2. **Don't N+1 the state queries** — one query per section, not one per state. The conditional aggregation pattern above handles this.
3. **Don't break the HTMX refresh** — the dashboard partial must remain a standalone includable template for the polling endpoint.
4. **Job config display** — don't dump raw JSON. Extract human-readable keys and format them. Handle missing keys gracefully (old jobs may not have search_engines).
5. **SQL parameterization** — raw SQL queries here are static aggregations with no user input, but if future filters add state/phase parameters, always use parameterized queries (`cursor.execute(sql, [param])`) — never string interpolation.
6. **Multiple running jobs** — the dashboard may show 0, 1, or several running jobs simultaneously (e.g. resolver on TX + classifier on FL). Display all of them, not just the first.
7. **Phone empty-string vs NULL** — use `phone IS NOT NULL AND phone != ''` consistently. The existing PhoneEnrichView already excludes both.

## Testing

Manual verification against the live dashboard with current production data. No SQLite test path — these views use raw SQL against Postgres. Verification checklist:
- Each page loads without error and renders new sections alongside existing ones
- State progress table counts match per-page counts (cross-check resolver stats vs dashboard Resolved column)
- Running job config display works for each phase type
- Empty states (zero crawled, zero reports) display correctly
- Dashboard HTMX auto-refresh still works (stats partial loads independently)
