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
  filename_score_accept: 0.8
  filename_score_reject: 0.2
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

A Pydantic model validates the YAML at load time. Validation failures are fatal — the crawler refuses to start rather than silently running with partial config. Validator enforces:

- Every `material_type` has `id`, `group`, `tier` (one of `web` / `mixed` / `internal`)
- No duplicate IDs
- `path_keywords.weak` items cannot appear in `path_keywords.strong`
- Thresholds are in `[0.0, 1.0]` and `accept > reject`
- Signal weight magnitudes within `[0.0, 1.0]`

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
    if re.search(r'(?:^|[^0-9])(20[12]\d)(?:[^0-9]|$)', b):
        score += tax.signal_weights.year_bonus
    if re.search(r'\bfy-?\d{2}\b', b):
        score += tax.signal_weights.year_bonus
    return max(0.0, min(1.0, score))
```

Three-tier triage inside `classify_link` / `extract_candidates`:

- `score >= thresholds.filename_score_accept` → accept without further checks; fetch
- `score <= thresholds.filename_score_reject` → drop pre-fetch; do not archive, do not classify
- `reject < score < accept` → fall through to the tier/anchor/path logic (current behavior)

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
    or (weak_path_hit and (anchor_hit or filename_score >= middle_threshold))
    or pdf_with_anchor
    or pdf_on_report_subpage
)
```

Weak path keywords (`/media`, `/press`, etc.) stop being solo acceptance signals. This is what closes the Fordham class of junk.

### Instrumentation

Every fetch decision writes a log record (JSON line in a separate log or a new DB table, design TBD):

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

This is the measurement substrate for the next iteration — we can see which signals drove each decision and compare against classifier verdicts after the fact.

## Acceptance Criteria

### YAML and loader
- AC01: `lavandula/docs/collateral_taxonomy.yaml` exists, validates against the Pydantic schema, and covers every material type in the approved `.md` reference (excluding the 4 explicitly out-of-scope categories).
- AC02: Crawler fails fast with a clear error message if the YAML is malformed or fails validation.
- AC03: `lavandula/reports/config.py` no longer hardcodes `ANCHOR_KEYWORDS` or `PATH_KEYWORDS` — both are derived from `taxonomy.current()`.

### Anchor extraction
- AC04: Image-link reports with alt text (`<a href="x.pdf"><img alt="2024 Annual Report"/></a>`) are retained as candidates even when visible anchor text is empty. Unit test covers this case.
- AC05: Title-attribute and aria-label contribute to effective anchor text.

### Filename heuristic
- AC06: `grade_filename` produces expected scores on a set of test fixtures drawn from the 378-doc baseline (accept tier: `UHS-Foundation-Annual-Report-2018.pdf` ≥ 0.8; reject tier: `Ram_Coloring_Page.pdf` ≤ 0.2).
- AC07: Fetch is skipped entirely when `filename_score <= reject_threshold`. Unit test confirms no HTTP call and no DB write for rejected candidates.
- AC08: Fetch proceeds and classifier is skipped when `filename_score >= accept_threshold`. (The existing full-download + archive path still runs; only the classifier call is elided.)

### Path keyword tiering
- AC09: Weak path keyword (`/media`) alone does not cause candidate acceptance — requires anchor match or filename_score in middle tier.
- AC10: Strong path keyword (`/annual-report`) alone still causes acceptance.

### Instrumentation
- AC11: Every fetch decision writes a log record with `basename`, `filename_score`, `triage`, `decision`, and signal-hit flags.

### Measurable outcome
- AC12: Re-run against the same 24 orgs from the 2026-04-23 session archives **≤ 25%** of the original 378 PDFs (i.e., ≤95 docs), while retaining all previously archived docs with `filename_score ≥ 0.8` (i.e., all 41 items in the graded accept tier).
- AC13: Baseline Fordham run of 207 PDFs drops to **≤ 15** PDFs (strategic_plan and any surviving genuine annual/impact reports).

### No regressions
- AC14: Existing crawler unit tests pass unchanged.
- AC15: Existing integration tests pass, possibly with test fixtures updated to reflect tiered behavior.

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

*(To be filled after consultation)*
