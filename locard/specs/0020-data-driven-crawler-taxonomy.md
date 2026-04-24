# Spec 0020: Data-Driven Crawler Taxonomy & Precision Improvements

**Status**: Draft
**Author**: Architect
**Date**: 2026-04-24
**Depends on**: 0004 (report crawler), 0018 (Gemma pipeline), `lavandula/docs/collateral_taxonomy.md` (approved 2026-04-24)

## Problem

The crawler currently hard-codes keyword lists (`ANCHOR_KEYWORDS`, `PATH_KEYWORDS`) in `lavandula/reports/config.py`. Every taxonomy change requires a code edit, a PR, and a deploy. Precision is poor: a 2026-04-23 crawl of 24 NY orgs archived 378 PDFs, of which 207 (55%) were junk from a single org (Fordham University) — all matched via the over-broad `/media` path keyword. Filenames in the junk set are decisive rejection signals (`Ram_Coloring_Page.pdf`, `Waiver-Substitution-Form.pdf`, `25-26-Estimated-Yearly-Income-for-Dependent-Students.pdf`) but the crawler never looks at filenames.

The approved collateral taxonomy (`lavandula/docs/collateral_taxonomy.md`) expands scope beyond annual/impact reports to include event collateral, capital campaign materials, planned giving materials, and more. Applying that taxonomy by hand-editing Python files is untenable — the taxonomy is long-lived domain reference material that product-manager-level stakeholders should be able to edit without developer involvement.

## Goals

Make the crawler both more accurate and editable by non-developers. Specifically:

1. Convert the approved taxonomy into machine-readable YAML that is the single source of truth for crawler keyword lists, signal weights, and tier assignments.
2. Refactor the crawler to read all keyword/signal configuration from that YAML at startup.
3. Add filename-based heuristic grading — a three-tier triage (`accept` / `middle` / `reject`) that bypasses the classifier on high-confidence rejections and acceptances.
4. Add alt-text, title-attribute, and aria-label sources to anchor extraction so image-based report links are no longer dropped.
5. Tier path keywords into strong-signal (pass alone) and weak-signal (must combine with anchor or filename match) groups.
6. Expand the keyword universe to cover the taxonomy's Tier 1 scope (events, campaigns, planned giving, membership) that is currently missing.

Measurable outcome: re-running against the same 24 orgs should cut total archived PDFs to a fraction of the 378 baseline while preserving or improving recall of actual reports.

## Non-Goals

- **Classifier expansion** (new document_type / event_type / tags columns) — that is Phase 2 / spec 0021.
- **DB rename** `reports` → `collaterals` — Phase 3 / later spec.
- **Dashboard UI for taxonomy editing** — belongs to spec 0019 Iteration 2+.
- **Linearized-PDF range-fetch pre-classify** — explicitly out; real-world linearization rate (~43% in our archive) makes the payoff weaker than keyword precision fixes. Can revisit later.
- **Re-classifying existing archive** — defer until classifier expansion (Phase 2) produces new labels.
- **Per-org parallelism within `process_org`** — separate optimization, not part of this spec.

## Architecture

### Taxonomy as YAML

The approved taxonomy is converted to `lavandula/docs/collateral_taxonomy.yaml`. The existing `.md` stays as the human-readable reference document (edited alongside the YAML when taxonomy changes). The YAML is the source of truth for code.

```yaml
version: 1
thresholds:
  filename_score_accept: 0.8            # score at or above → accept tier
  filename_score_reject: 0.2            # score at or below → reject tier (drop pre-fetch)
  filename_score_weak_path_min: 0.5     # floor required when only a weak-path keyword matches
  base_score: 0.5

signal_weights:
  strong_positive: 0.3
  medium_positive: 0.15
  strong_negative: -0.4
  medium_negative: -0.1
  year_bonus: 0.1

material_types:
  - id: annual_report
    group: reports
    tier: web
    filename_signals:
      strong_positive: [annual-report, AR-20XX, yearly-report]
      medium_positive: [annual]
    anchor_signals: [annual report, yearly report]
    description: "Org-wide annual report"

  - id: tribute_journal
    group: programs_journals
    tier: mixed
    filename_signals:
      strong_positive: [tribute, journal, ad-book, commemorative]
    anchor_signals: [tribute journal, ad book]
    event_types_common: [gala, ball]

  # ... ~70 more material types

event_types:
  - id: gala
    path_keywords: [/gala]
    anchor_signals: [gala, annual gala]
  # ...

path_keywords:
  strong:
    - /annual-report
    - /impact
    - /gala
    - /golf
    # ... all Tier 1 strong paths
  weak:  # require anchor or filename match
    - /media
    - /press
    - /resources
    - /downloads
    # ...

filename_negative_signals:
  strong:
    - form
    - application
    - waiver
    - coloring
    - campus-map
    - handbook
    - guidelines
    # ...
  medium:
    - notes
    - letter
    - memo
    # ...
```

### YAML Validator

A Pydantic model (with `model_config = ConfigDict(frozen=True)` to prevent runtime mutation) validates the YAML at load time. Validation failures are fatal — the crawler refuses to start rather than silently running with partial config. Validator enforces:

- Every `material_type` has `id`, `group`, `tier` (one of `web` / `mixed` / `internal`)
- No duplicate IDs across material_types, event_types, or tags
- `path_keywords.weak` items cannot appear in `path_keywords.strong`
- Thresholds are in `[0.0, 1.0]` and satisfy `accept > weak_path_min > reject`
- **Defensive bounds** to prevent accidental PM-level misconfiguration:
  - `filename_score_accept >= 0.5` (can't set an "accept-everything" threshold)
  - `filename_score_reject <= 0.5` (can't set a "reject-everything" threshold)
  - `base_score == 0.5` (not tunable — structural assumption)
- Signal weight magnitudes within `[0.0, 1.0]`
- Every `event_type.path_keywords` entry is automatically aggregated into the global strong-path set at load time — YAML editors do not duplicate them in both places

### Crawler refactor: taxonomy loader

A new module `lavandula/reports/taxonomy.py` owns the loaded taxonomy:

```python
class Taxonomy:
    thresholds: Thresholds
    signal_weights: SignalWeights
    material_types: list[MaterialType]
    event_types: list[EventType]
    path_keywords_strong: frozenset[str]
    path_keywords_weak: frozenset[str]
    anchor_keywords: frozenset[str]  # derived from material_types + event_types
    filename_positive: dict[str, float]  # keyword -> weight
    filename_negative: dict[str, float]
```

Singleton loaded at crawler startup. `lavandula/reports/config.py` becomes thin, referencing `taxonomy.current()` instead of hardcoded frozensets. Old `ANCHOR_KEYWORDS` / `PATH_KEYWORDS` names kept as aliases initially for minimal diff.

### Anchor extraction: alt / title / aria-label

In `lavandula/reports/candidate_filter.py:276`, replace:

```python
anchor_text = a.get_text(" ", strip=True) or ""
```

with a prioritized combiner:

```python
def effective_anchor_text(a: Tag) -> str:
    visible = a.get_text(" ", strip=True) or ""
    title = a.get("title") or ""
    aria = a.get("aria-label") or ""
    alts = " ".join(img.get("alt", "") for img in a.find_all("img"))
    parts = [p for p in (visible, title, aria, alts) if p]
    return " ".join(parts).strip()
```

This is a recall win on image-based report links (common design pattern where the report cover thumbnail is the CTA) and a precision win because alt/title text is author-written and higher-signal than keyword matches on URL paths.

### Filename heuristic grading

New function `grade_filename(basename: str, taxonomy: Taxonomy) -> float`:

```python
def grade_filename(basename: str, tax: Taxonomy) -> float:
    b = re.sub(r'[\s_]+', '-', basename.lower().removesuffix('.pdf'))
    score = tax.thresholds.base_score
    for kw, weight in tax.filename_positive.items():
        if kw in b:
            score += weight
    for kw, weight in tax.filename_negative.items():
        if kw in b:
            score += weight  # weight is negative
    if re.search(r'(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)', b):
        score += tax.signal_weights.year_bonus
    if re.search(r'\bfy-?\d{2}\b', b):
        score += tax.signal_weights.year_bonus
    return max(0.0, min(1.0, score))
```

**Edge case — non-filename basenames.** For URLs like `/download?file=report2024.pdf` or `/get-document/12345`, the URL path basename is `download` or `get-document` — no dotted extension, no domain-word content. `grade_filename` naturally returns the base score (0.5, neutral) because no positive or negative keywords match. This is correct: no filename signal means the heuristic stays out of the way and downstream path/anchor logic decides. Post-fetch, the `Content-Disposition` header filename becomes a stronger second-pass signal; capturing and re-grading with that is **not in this spec** — deferred to a later spec since it requires touching the fetch pipeline.

Three-tier triage inside `classify_link` / `extract_candidates`:

- `score >= thresholds.filename_score_accept` → accept the candidate; fetch proceeds. **Classifier still runs post-fetch in Phase 1** — we do not skip classification on accept-tier until the heuristic is empirically validated against classifier verdicts (deferred optimization for a later spec).
- `score <= thresholds.filename_score_reject` → drop pre-fetch; do not archive, do not classify.
- `reject < score < accept` → fall through to the tier/anchor/path logic (current behavior).

Keeping the classifier on the full fetch path in Phase 1 is deliberate defense-in-depth: if the filename heuristic misgrades a candidate into the accept tier (e.g., a document coincidentally named `annual-report-template.pdf` that is not actually a report), the classifier still catches it. Skipping classification is a defensible optimization only after we have data showing heuristic ≥ N% agreement with classifier on accept-tier.

### Path keyword tiering

Current filter (`candidate_filter.py:216`):

```python
if not (anchor_hit or path_hit or pdf_with_anchor or pdf_on_report_subpage):
    reject
```

New filter:

```python
strong_path_hit = any(kw in path for kw in tax.path_keywords_strong)
weak_path_hit = any(kw in path for kw in tax.path_keywords_weak)
pass_ = (
    anchor_hit
    or strong_path_hit
    or (weak_path_hit and (anchor_hit or filename_score >= tax.thresholds.filename_score_weak_path_min))
    or pdf_with_anchor
    or pdf_on_report_subpage
)
```

Weak path keywords (`/media`, `/press`, etc.) stop being solo acceptance signals — they must be backed by either an anchor-text match or a filename score at or above the weak-path floor (`0.5` by default, i.e., the filename must be at least neutrally informative, not actively junk). This is what closes the Fordham class of junk.

### Instrumentation

Every candidate-evaluation decision writes a JSON line to a rotating file at `logs/crawler_decisions.jsonl` (not a DB table — per-candidate decisions run into the thousands per crawl and would bloat `lava_impact`). Format:

```json
{
  "ts": "2026-04-24T18:30:00Z",
  "ein": "131740451",
  "url": "https://www.fordham.edu/media/.../Ram_Coloring_Page.pdf",
  "basename": "Ram_Coloring_Page.pdf",
  "filename_score": 0.10,
  "triage": "reject",
  "strong_path_hit": false,
  "weak_path_hit": true,
  "anchor_text": "",
  "anchor_hit": false,
  "decision": "drop",
  "reason": "filename_score <= reject_threshold"
}
```

Log rotation: daily file per run date (`logs/crawler_decisions-2026-04-24.jsonl`). Retention policy out of scope for this spec — set to 90 days manually for now.

This log is the measurement substrate for the next iteration. We can grep for specific orgs, compute the rate at which the heuristic agrees with the eventual classifier verdict on fetched items, and tune weights empirically.

## Acceptance Criteria

### YAML and loader
- AC01: `lavandula/docs/collateral_taxonomy.yaml` exists, validates against the Pydantic schema, and covers every material type in the approved `.md` reference (excluding the 4 explicitly out-of-scope categories).
- AC02: Crawler fails fast with a clear error message if the YAML is malformed or fails validation.
- AC03: `lavandula/reports/config.py` no longer hardcodes `ANCHOR_KEYWORDS` or `PATH_KEYWORDS` — both are derived from `taxonomy.current()`.

### Anchor extraction
- AC04: Image-link reports with alt text (`<a href="x.pdf"><img alt="2024 Annual Report"/></a>`) are retained as candidates even when visible anchor text is empty. Unit test covers this case.
- AC05: Title-attribute and aria-label contribute to effective anchor text.

### Filename heuristic
- AC06: `grade_filename` produces expected scores on a set of test fixtures drawn from the 378-doc baseline (accept tier: `UHS-Foundation-Annual-Report-2018.pdf` ≥ 0.8; reject tier: `Ram_Coloring_Page.pdf` ≤ 0.2; neutral: URL with `/download` basename = 0.5).
- AC07: Fetch is skipped entirely when `filename_score <= reject_threshold`. Unit test confirms no HTTP call and no DB write for rejected candidates.
- AC08: Fetch proceeds normally when `filename_score >= accept_threshold`. Classifier continues to run on all fetched PDFs in Phase 1. (Skipping classification on accept-tier is an explicit non-goal of this spec.)

### Path keyword tiering
- AC09: Weak path keyword (`/media`) alone does not cause candidate acceptance — requires anchor match or filename_score in middle tier.
- AC10: Strong path keyword (`/annual-report`) alone still causes acceptance.

### Instrumentation
- AC11: Every fetch decision writes a log record with `basename`, `filename_score`, `triage`, `decision`, and signal-hit flags.

### Measurable outcome

Two measurements, each distinct:

- AC12 (**offline heuristic validation**): Running `grade_filename` against the 378 archived-doc basenames from 2026-04-23 produces ≥ 90% agreement with a held-aside manual or classifier label: every file in the `accept` tier (filename_score ≥ 0.8) is a genuine report or event collateral; every file in the `reject` tier (filename_score ≤ 0.2) is genuine junk. Single contested case is allowed; more requires re-tuning keyword weights.
- AC13 (**live-crawl regression check**): Re-running the crawler against the same 24 orgs archives **≤ 25%** of the original 378 PDFs (i.e., ≤ 95 docs). Fordham specifically drops from 207 to **≤ 15**. All previously archived docs with `filename_score ≥ 0.8` from the 2026-04-23 run are retained.

### No regressions
- AC14: Existing crawler unit tests pass unchanged.
- AC15: Existing integration tests pass, possibly with test fixtures updated to reflect tiered behavior.

### Rollback

This spec introduces no DB schema changes. Rollback is a pure code revert: `git revert` the implementing commits, restart the crawler, original keyword behavior resumes. The taxonomy YAML file stays committed for reference. The decisions log file is discardable. No data migration is required either forward or backward.

## Traps to Avoid

1. **Don't let YAML loading fail silently.** A bad YAML that parses but loses keywords could silently degrade recall. Validator catches this; crawler refuses to start on validation failure.
2. **Don't bake the taxonomy into classifier prompts yet.** That's Phase 2. This spec touches only the crawler.
3. **Don't rename the `reports` table or any DB objects.** That's Phase 3.
4. **Don't hardcode the accept/reject thresholds.** They're in YAML specifically so product-level tuning doesn't need a code change.
5. **Don't over-tune filename keywords to the 378-doc baseline.** Over-fitting to one org (Fordham) would hurt recall elsewhere. Keep the keyword lists aligned with the approved taxonomy, not with the observed junk.
6. **Don't skip the instrumentation.** Without the decision log, we can't measure whether filename triage agrees with classifier verdicts — which is how we justify further tuning later.
7. **Don't couple the taxonomy YAML to the crawler's internal types.** The YAML is domain-facing (PMs read it). Crawler-internal structures (frozensets, Pydantic models) are derived from it.

## Implementation Notes

- **Dependency**: Pydantic is already in the project (used by FastAPI in older specs). Add `pyyaml` if not already present.
- **Hot-reload**: out of scope for this spec. Taxonomy reloads on crawler restart. Phase 3 (dashboard) can add hot-reload when the UI editor exists.
- **Migration**: the existing `BLOCKLIST_DOMAINS` in `brave_search.py` and any hand-edited keyword lists stay as-is for now; they're orthogonal to the crawler's candidate filter.
- **Testing strategy**: unit tests for `grade_filename` and the anchor-text combiner. Integration test re-runs one known org (Fordham) and asserts ≤15 fetches. Golden-file test for YAML-to-Taxonomy conversion.

## Consultation Log

### Round 1 — Gemini spec review (2026-04-24)

**Verdict: APPROVE, HIGH confidence.**

Key findings and resolutions:

1. `middle_threshold` was referenced in the filter logic but never defined → **Added** explicit `filename_score_weak_path_min: 0.5` threshold to YAML schema and validator.
2. Year regex `(20[12]\d)` expires in 2030 and misses pre-2000 reports → **Changed** to `((?:19|20)\d{2})` in the grading function and spec.
3. Per-decision logging to DB would bloat `lava_impact` → **Committed** to JSONL at `logs/crawler_decisions.jsonl` with daily rotation.
4. Pydantic Taxonomy model should be immutable → **Added** `model_config = ConfigDict(frozen=True)`.
5. `event_type.path_keywords` could be duplicated in the global strong-path list → **Spec'd** automatic aggregation at load time so YAML editors never duplicate.
6. Query-string URLs like `/download?file=x.pdf` have uninformative basenames → **Documented** that these produce neutral (0.5) scores naturally; Content-Disposition as a second-pass signal is deferred to a later spec.

Additional self-review refinements (not raised by Gemini):

7. Originally AC08 claimed the classifier would be skipped on accept-tier → **Reversed**: Phase 1 keeps the classifier on the full fetch path as defense-in-depth. Classifier-skip optimization deferred until heuristic precision is empirically validated.
8. Thresholds were tunable without bounds → **Added** defensive validator rules (`accept >= 0.5`, `reject <= 0.5`) to prevent "accept-everything" / "reject-everything" misconfigurations from PM-level YAML edits.
9. AC12 conflated offline heuristic validation with live-crawl regression → **Split** into AC12 (offline fixture grading) and AC13 (live re-crawl with Fordham-specific target ≤ 15).
10. **Added** explicit Rollback section — pure code revert, no DB migration, taxonomy YAML stays committed.
