# Plan 0020: Data-Driven Crawler Taxonomy & Precision Improvements

**Status**: Review
**Author**: Architect
**Date**: 2026-04-24
**Spec**: [locard/specs/0020-data-driven-crawler-taxonomy.md](../specs/0020-data-driven-crawler-taxonomy.md)

## Approach

Eight sequential sub-phases, each with a shippable checkpoint and explicit tests. The work is ordered so early phases can be validated in isolation (YAML infra, validator), later phases change behavior in small steps, and a final integration/regression gate proves the measurable outcomes from the spec.

- Sub-phases 1.1–1.3 are **structure-only**: no crawler behavior change. These can be reviewed and merged independently.
- Sub-phases 1.4–1.6 change behavior. Each has unit tests that pin the new behavior.
- Sub-phases 1.7–1.8 are validation gates against the spec's AC12/AC13 targets.

Single builder. Single PR expected per the SPIDER protocol, but the commits within the PR should mirror the sub-phase structure so reviewers can walk through the diff in logical chunks.

## Preconditions

- The approved taxonomy document exists at `lavandula/docs/collateral_taxonomy.md` (committed in `c79b23a`).
- `pydantic` is already a direct dependency (used by FastAPI in older specs). Verify with `grep pydantic pyproject.toml` before starting.
- `pyyaml` may or may not be a direct dependency. If not already present, add it in sub-phase 1.1.
- The 378-doc baseline CSV (`lavandula/review_uploads/session_filenames_graded.csv`) exists and is readable — used as fixture source in sub-phase 1.4.

## Sub-Phase 1.1 — YAML taxonomy + Pydantic validator

**Goal**: a validated, loadable taxonomy YAML file with unit tests for the validator. No crawler changes.

### Files created

- `lavandula/docs/collateral_taxonomy.yaml` — the taxonomy, translated from the approved `.md` by hand. Includes: `version: 1`, `thresholds`, `signal_weights`, full `material_types` list with tier marks, `event_types`, `path_keywords` (strong + weak), `filename_negative_signals`, and all positive signals distributed across material_types.
- `lavandula/reports/taxonomy.py` — Pydantic models (`Thresholds`, `SignalWeights`, `MaterialType`, `EventType`, `Taxonomy`) and a loader function `load_taxonomy(path: Path) -> Taxonomy`.
- `lavandula/reports/tests/unit/test_taxonomy_loader.py` — validator tests.

### Pydantic model (`taxonomy.py`)

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pathlib import Path
import re
import yaml

_REGEX_META = set(".*+?[](|)\\^$")

class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True)
    filename_score_accept: float = Field(ge=0.5, le=1.0)
    filename_score_reject: float = Field(ge=0.0, le=0.5)
    filename_score_weak_path_min: float = Field(ge=0.0, le=1.0)
    base_score: float = Field(ge=0.5, le=0.5)  # pinned; structural assumption

    @model_validator(mode="after")
    def _ordering(self):
        if not (self.filename_score_accept > self.filename_score_weak_path_min > self.filename_score_reject):
            raise ValueError("thresholds must satisfy accept > weak_path_min > reject")
        return self

class SignalWeights(BaseModel):
    model_config = ConfigDict(frozen=True)
    strong_positive: float = Field(ge=0.0, le=1.0)
    medium_positive: float = Field(ge=0.0, le=1.0)
    strong_negative: float = Field(le=0.0, ge=-1.0)
    medium_negative: float = Field(le=0.0, ge=-1.0)
    year_bonus: float = Field(ge=0.0, le=1.0)

class FilenameSignals(BaseModel):
    model_config = ConfigDict(frozen=True)
    strong_positive: tuple[str, ...] = ()
    medium_positive: tuple[str, ...] = ()

class MaterialType(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    group: str
    tier: str = Field(pattern=r"^(web|mixed|internal)$")
    filename_signals: FilenameSignals = FilenameSignals()
    anchor_signals: tuple[str, ...] = ()
    description: str | None = None

class EventType(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    path_keywords: tuple[str, ...] = ()
    anchor_signals: tuple[str, ...] = ()

class PathKeywords(BaseModel):
    model_config = ConfigDict(frozen=True)
    strong: tuple[str, ...]
    weak: tuple[str, ...]

class FilenameNegativeSignals(BaseModel):
    model_config = ConfigDict(frozen=True)
    strong: tuple[str, ...] = ()
    medium: tuple[str, ...] = ()

class TaxonomyRaw(BaseModel):
    """Raw YAML shape — validated, but not yet the optimized runtime view."""
    model_config = ConfigDict(frozen=True)
    version: int = Field(eq=1)  # bump for schema changes
    thresholds: Thresholds
    signal_weights: SignalWeights
    material_types: tuple[MaterialType, ...]
    event_types: tuple[EventType, ...] = ()
    path_keywords: PathKeywords
    filename_negative_signals: FilenameNegativeSignals
    tags: tuple[dict, ...] = ()  # loose for now

    @model_validator(mode="after")
    def _global_constraints(self):
        _check_keyword_rules(self)
        _check_no_duplicate_ids(self)
        _check_strong_weak_disjoint(self)
        _check_no_signal_collision(self)
        return self

def _check_keyword_rules(t: "TaxonomyRaw") -> None:
    """Min length 3, no regex metachars, lowercase only, case-insensitive-friendly."""
    def check_list(name: str, kws: tuple[str, ...]) -> None:
        for kw in kws:
            if len(kw) < 3:
                raise ValueError(f"{name}: keyword {kw!r} shorter than 3 chars")
            if any(c in _REGEX_META for c in kw):
                raise ValueError(f"{name}: keyword {kw!r} contains regex metacharacters")
            if kw != kw.lower():
                raise ValueError(
                    f"{name}: keyword {kw!r} contains uppercase — all keywords must be "
                    f"lowercase (matching is case-insensitive, so mixed case is ambiguous)"
                )

    for mt in t.material_types:
        check_list(f"material_types[{mt.id}].filename_signals.strong_positive", mt.filename_signals.strong_positive)
        check_list(f"material_types[{mt.id}].filename_signals.medium_positive", mt.filename_signals.medium_positive)
    for et in t.event_types:
        check_list(f"event_types[{et.id}].path_keywords", et.path_keywords)
    check_list("path_keywords.strong", t.path_keywords.strong)
    check_list("path_keywords.weak", t.path_keywords.weak)
    check_list("filename_negative_signals.strong", t.filename_negative_signals.strong)
    check_list("filename_negative_signals.medium", t.filename_negative_signals.medium)

def _check_no_duplicate_ids(t: "TaxonomyRaw") -> None:
    seen: set[str] = set()
    for mt in t.material_types:
        if mt.id in seen:
            raise ValueError(f"duplicate id: {mt.id}")
        seen.add(mt.id)
    for et in t.event_types:
        if et.id in seen:
            raise ValueError(f"duplicate id across material_types and event_types: {et.id}")
        seen.add(et.id)

def _check_strong_weak_disjoint(t: "TaxonomyRaw") -> None:
    common = set(t.path_keywords.strong) & set(t.path_keywords.weak)
    if common:
        raise ValueError(f"path_keywords strong and weak overlap: {sorted(common)}")

def _check_no_signal_collision(t: "TaxonomyRaw") -> None:
    """No keyword appears in both positive and negative lists."""
    positives: set[str] = set()
    for mt in t.material_types:
        positives |= set(mt.filename_signals.strong_positive)
        positives |= set(mt.filename_signals.medium_positive)
    negatives = set(t.filename_negative_signals.strong) | set(t.filename_negative_signals.medium)
    collision = positives & negatives
    if collision:
        raise ValueError(f"keywords appear in both positive and negative lists: {sorted(collision)}")

class Taxonomy(BaseModel):
    """Runtime-optimized view derived from TaxonomyRaw."""
    model_config = ConfigDict(frozen=True)
    raw: TaxonomyRaw
    path_keywords_strong: frozenset[str]
    path_keywords_weak: frozenset[str]
    anchor_keywords: frozenset[str]
    filename_positive: dict[str, float]  # keyword -> weight
    filename_negative: dict[str, float]

    @property
    def thresholds(self) -> Thresholds:
        return self.raw.thresholds

    @property
    def signal_weights(self) -> SignalWeights:
        return self.raw.signal_weights

def load_taxonomy(path: Path) -> Taxonomy:
    """Load, validate, and derive runtime view. Uses yaml.safe_load — never yaml.load."""
    with path.open() as f:
        data = yaml.safe_load(f)  # CRITICAL: never yaml.load()
    raw = TaxonomyRaw.model_validate(data)
    return _build_runtime_view(raw)

def _build_runtime_view(raw: TaxonomyRaw) -> Taxonomy:
    # INVARIANT: every keyword in the runtime view is lowercased. The validator
    # rejects uppercase, but anchor_signals (human-readable phrases) are allowed
    # to contain display casing, so we lowercase on aggregation here as a belt-
    # and-suspenders measure.

    # Auto-aggregate event_type.path_keywords into strong set; lowercase all
    strong = {kw.lower() for kw in raw.path_keywords.strong}
    for et in raw.event_types:
        strong |= {kw.lower() for kw in et.path_keywords}
    weak = frozenset(kw.lower() for kw in raw.path_keywords.weak)

    # Anchor keywords drawn from both material_types and event_types
    anchors: set[str] = set()
    for mt in raw.material_types:
        anchors |= {s.lower() for s in mt.anchor_signals}
    for et in raw.event_types:
        anchors |= {s.lower() for s in et.anchor_signals}

    # Filename positive/negative maps — all keywords lowercased for case-insensitive
    # matching (basenames are lowercased in filename_grader.normalize too).
    filename_positive: dict[str, float] = {}
    for mt in raw.material_types:
        for kw in mt.filename_signals.strong_positive:
            filename_positive[kw.lower()] = raw.signal_weights.strong_positive
        for kw in mt.filename_signals.medium_positive:
            filename_positive.setdefault(kw.lower(), raw.signal_weights.medium_positive)

    filename_negative: dict[str, float] = {}
    for kw in raw.filename_negative_signals.strong:
        filename_negative[kw.lower()] = raw.signal_weights.strong_negative
    for kw in raw.filename_negative_signals.medium:
        filename_negative.setdefault(kw.lower(), raw.signal_weights.medium_negative)

    return Taxonomy(
        raw=raw,
        path_keywords_strong=frozenset(strong),
        path_keywords_weak=weak,
        anchor_keywords=frozenset(anchors),
        filename_positive=filename_positive,
        filename_negative=filename_negative,
    )

# Module-level singleton — populated at import by config.py
_current: Taxonomy | None = None

def current() -> Taxonomy:
    if _current is None:
        raise RuntimeError("taxonomy not loaded — call bind() from config.py first")
    return _current

def bind(t: Taxonomy) -> None:
    global _current
    _current = t
```

### Tests (`test_taxonomy_loader.py`)

- `test_loads_committed_yaml_file` — load the real YAML and assert non-empty material_types, strong path set, etc.
- `test_rejects_yaml_load_unsafe_payload` — attempt to load a string containing `!!python/object/apply:os.system [['echo pwned']]` via `load_taxonomy` through a tmp_path file; confirm `yaml.YAMLError` or validation failure, and crucially confirm no side effect.
- `test_rejects_unknown_version` — YAML with `version: 99` raises validation error.
- `test_rejects_threshold_out_of_order` — `accept = 0.3, reject = 0.5` raises.
- `test_rejects_threshold_accept_below_half` — `accept = 0.4` raises.
- `test_rejects_threshold_reject_above_half` — `reject = 0.6` raises.
- `test_rejects_short_keyword` — keyword `"ar"` in positive list raises.
- `test_rejects_regex_metachar_keyword` — keyword `".*report"` raises.
- `test_rejects_uppercase_keyword` — keyword `"Annual-Report"` (filename_signals strong_positive) raises with a clear "keywords must be lowercase" error.
- `test_runtime_view_lowercases_anchor_signals` — anchor_signals in the YAML may contain display-cased phrases like `"Annual Report"`; verify `Taxonomy.anchor_keywords` contains only lowercased entries.
- `test_rejects_strong_weak_path_overlap` — `"/media"` in both strong and weak raises.
- `test_rejects_positive_negative_collision` — same keyword in both lists raises.
- `test_rejects_duplicate_ids` — two material_types with id `"annual_report"` raises.
- `test_event_path_keywords_aggregate_into_strong` — given event_type with `path_keywords: ['/gala']` and global strong missing it, the resolved Taxonomy has `/gala` in `path_keywords_strong`.

### Acceptance check

- `pytest lavandula/reports/tests/unit/test_taxonomy_loader.py` passes.
- `python -c "from lavandula.reports.taxonomy import load_taxonomy; from pathlib import Path; print(load_taxonomy(Path('lavandula/docs/collateral_taxonomy.yaml')))"` prints a non-empty Taxonomy.
- No behavior change to the crawler (config.py not yet modified).

## Sub-Phase 1.2 — Config refactor (no behavior change)

**Goal**: `reports/config.py` derives `ANCHOR_KEYWORDS` and `PATH_KEYWORDS` from the taxonomy. Existing crawler tests must pass unchanged. This is the "swap source, preserve behavior" checkpoint.

### Files modified

- `lavandula/reports/config.py`:
  - Import `taxonomy.load_taxonomy` and bind the singleton at module load.
  - Replace the hardcoded frozenset definitions of `ANCHOR_KEYWORDS` and `PATH_KEYWORDS` with:
    ```python
    _TAXONOMY_PATH = ROOT / "docs" / "collateral_taxonomy.yaml"
    _taxonomy = load_taxonomy(_TAXONOMY_PATH)
    taxonomy.bind(_taxonomy)

    # Legacy aliases kept for backward compat; new code uses taxonomy.current()
    ANCHOR_KEYWORDS = _taxonomy.anchor_keywords
    PATH_KEYWORDS = _taxonomy.path_keywords_strong  # weak handled separately in 1.5
    ```
  - Leave `HOSTING_PLATFORMS`, `LOCALE_PREFIXES`, and other constants unchanged.

### Guardrails

- The initial committed YAML must reproduce the existing keyword sets exactly. Before starting 1.2, verify:
  ```
  set(ANCHOR_KEYWORDS_before) == _taxonomy.anchor_keywords
  set(PATH_KEYWORDS_before) ⊆ _taxonomy.path_keywords_strong  # weak keywords may have been promoted
  ```
- This is the one place a hand-transcribed YAML could silently break the existing crawler. Sub-phase 1.2 ends by running the **existing** crawler test suite — no new tests, just proving nothing regressed.

### Acceptance check

- `pytest lavandula/reports/tests/` passes unchanged (same set of tests pass as pre-change).
- `git grep ANCHOR_KEYWORDS lavandula/` shows only references, no new definitions outside `config.py`.

## Sub-Phase 1.3 — Anchor extraction (alt/title/aria-label)

**Goal**: pick up image-link and attribute-based anchor text currently dropped.

### Files modified

- `lavandula/reports/candidate_filter.py`:
  - Add `_effective_anchor_text(a: Tag) -> str` near the top of the module:
    ```python
    def _effective_anchor_text(a: Tag) -> str:
        visible = a.get_text(" ", strip=True) or ""
        title = (a.get("title") or "").strip()
        aria = (a.get("aria-label") or "").strip()
        alts = " ".join((img.get("alt") or "").strip() for img in a.find_all("img"))
        parts = [p for p in (visible, title, aria, alts.strip()) if p]
        return " ".join(parts).strip()
    ```
  - Replace `anchor_text = a.get_text(" ", strip=True) or ""` at line 276 with `anchor_text = _effective_anchor_text(a)`.

### Tests added

- `test_anchor_text_image_only_with_alt` — `<a href="r.pdf"><img alt="2024 Annual Report"></a>` → effective text is `"2024 Annual Report"`, candidate passes anchor-keyword filter.
- `test_anchor_text_title_attribute` — `<a href="x.pdf" title="Our Impact">...</a>` → title contributes.
- `test_anchor_text_aria_label` — `<a href="x.pdf" aria-label="Gala Invitation">...</a>` → aria-label contributes.
- `test_anchor_text_visible_overrides_nothing` — visible text is concatenated with title/alt, not replaced.
- `test_anchor_text_empty_when_all_missing` — returns `""` cleanly.

### Acceptance check

- New tests pass; existing tests pass unchanged.
- This is a pure additive change — any previously accepted candidate remains accepted.

## Sub-Phase 1.4 — Filename heuristic `grade_filename`

**Goal**: add the grading function with full unit coverage against the 378-doc baseline. Not yet wired into the fetch path — purely a function that returns a score.

### Files created

- `lavandula/reports/filename_grader.py`:
  ```python
  import re
  from .taxonomy import Taxonomy

  _YEAR_RE = re.compile(r"(?:^|[^0-9])((?:19|20)\d{2})(?:[^0-9]|$)")
  _FY_RE = re.compile(r"\bfy-?\d{2}\b")

  def normalize(basename: str) -> str:
      return re.sub(r"[\s_]+", "-", basename.lower().removesuffix(".pdf"))

  def grade_filename(basename: str, tax: Taxonomy) -> float:
      b = normalize(basename)
      score = tax.thresholds.base_score
      for kw, weight in tax.filename_positive.items():
          if kw in b:
              score += weight
      for kw, weight in tax.filename_negative.items():
          if kw in b:
              score += weight  # weight is negative
      if _YEAR_RE.search(b):
          score += tax.signal_weights.year_bonus
      if _FY_RE.search(b):
          score += tax.signal_weights.year_bonus
      return max(0.0, min(1.0, score))
  ```

### Tests created

- `lavandula/reports/tests/unit/test_filename_grader.py`:
  - Known-good fixtures from the 378-doc baseline:
    - `grade_filename("UHS-Foundation-Annual-Report-2018.pdf", tax) >= 0.8`
    - `grade_filename("HSS-Annual-Report-2024.pdf", tax) >= 0.8`
    - `grade_filename("Carnegie-Council-Annual-Report-2021-300dpi.pdf", tax) >= 0.8`
  - Known-junk fixtures:
    - `grade_filename("Ram_Coloring_Page.pdf", tax) <= 0.2`
    - `grade_filename("25-26-Estimated-Yearly-Income-for-Dependent-Students.pdf", tax) <= 0.2`
    - `grade_filename("WFH-Permission_guidelines.pdf", tax) <= 0.2`
    - `grade_filename("Waiver-Substitution-Form.pdf", tax) <= 0.2`
  - Neutral fixtures:
    - `grade_filename("download", tax) == 0.5`
    - `grade_filename("document-1234.pdf", tax) == 0.5`
  - Year bonus:
    - `grade_filename("report-2024.pdf", tax) > grade_filename("report.pdf", tax)`
    - `grade_filename("fy24-results.pdf", tax)` includes FY bonus.
  - Pre-2000 year:
    - `grade_filename("report-1998.pdf", tax)` includes year bonus (validates the widened regex).

### Bulk offline validation

- One-shot script `lavandula/reports/tools/grade_baseline.py`:
  - Read `lavandula/review_uploads/session_filenames_graded.csv`.
  - Apply new `grade_filename` to every row using the committed YAML taxonomy.
  - Produce a CSV with columns `filename, heuristic_score, heuristic_triage, prior_graded_score, prior_triage, agreement`.
  - Print summary: accept/middle/reject counts, agreement-on-tails percentage.
  - Commit the output to `lavandula/review_uploads/session_filenames_retGraded.csv` for reviewer inspection.
- Satisfies AC12 once the agreement metric is ≥ 90% on the tails.

### Acceptance check

- `pytest lavandula/reports/tests/unit/test_filename_grader.py` passes.
- `python -m lavandula.reports.tools.grade_baseline` reports ≥ 90% agreement on tails; output CSV is readable.
- `filename_grader` not yet imported by crawler runtime code.

## Sub-Phase 1.5 — Three-tier triage + path tiering in filter

**Goal**: integrate the grader into the candidate filter with the three-tier triage and weak-path gating. Behavior change.

### Files modified

- `lavandula/reports/candidate_filter.py`:
  - Import `grade_filename` and `Taxonomy`.
  - Add helper `_basename_from_url(url: str) -> str` (use `urlparse` + `unquote` on the last path segment; return `""` if path has no segments).
  - In `_classify_link`, compute:
    - `basename = _basename_from_url(href)` — returns unquoted, lowercased last path segment (via `unquote(url.split("/")[-1]).lower()`); empty string if path is missing
    - `filename_score = grade_filename(basename, tax)` (only when `basename` is non-empty and looks like a filename; otherwise default to `tax.thresholds.base_score`)
    - `path_lower = url.path.lower()` — **case-insensitive path matching** per spec
    - `strong_path_hit = any(kw in path_lower for kw in tax.path_keywords_strong)` — keywords already lowercased at load time (invariant)
    - `weak_path_hit = any(kw in path_lower for kw in tax.path_keywords_weak)`

**Normalization invariant**: both the keywords in `tax.path_keywords_*` and the comparison target `path_lower` are lowercase. Same invariant holds for filename signals (`grade_filename.normalize` lowercases the basename; `tax.filename_positive`/`filename_negative` are lowercased at load time). Matching code must never add a `.lower()` on the keyword side — keywords are already normalized and an extra `.lower()` is a mistake that masks YAML problems.
  - Triage sequence:
    1. If `filename_score <= tax.thresholds.filename_score_reject`: return `Decision.DROP_FILENAME_REJECT`.
    2. If `filename_score >= tax.thresholds.filename_score_accept`: return `Decision.ACCEPT_FILENAME_STRONG`.
    3. Otherwise apply the tiered path/anchor filter:
       ```python
       pass_ = (
           anchor_hit
           or strong_path_hit
           or (weak_path_hit and (anchor_hit or filename_score >= tax.thresholds.filename_score_weak_path_min))
           or pdf_with_anchor
           or pdf_on_report_subpage
       )
       ```
       Return `Decision.ACCEPT_MIDDLE` or `Decision.DROP_NO_SIGNAL`.
  - Add a `Decision` enum in the same module. Enum values match the strings written to the instrumentation log (see 1.6).

### Tests

- `test_triage_accept_filename_strong` — filename `"annual-report-2024.pdf"`, no anchor, no path signal → decision = ACCEPT_FILENAME_STRONG.
- `test_triage_drop_filename_reject` — filename `"coloring-page.pdf"`, path `/media/...` → decision = DROP_FILENAME_REJECT, no fetch.
- `test_triage_weak_path_alone_rejected` — path `/media/x.pdf`, empty anchor, neutral filename → decision = DROP_NO_SIGNAL.
- `test_triage_weak_path_with_anchor` — path `/media/x.pdf`, anchor text `"annual report"` → ACCEPT_MIDDLE.
- `test_triage_weak_path_with_filename_support` — path `/media/...`, filename_score = 0.65 (medium-positive + base) → ACCEPT_MIDDLE.
- `test_triage_strong_path_alone` — path `/annual-report/x.pdf`, empty anchor, neutral filename → ACCEPT_MIDDLE.
- `test_triage_case_insensitive_path` — path `/Annual-Report/X.pdf` accepts same as `/annual-report/x.pdf`.
- `test_triage_fordham_coloring_page_dropped` — full URL from the 2026-04-23 Fordham run → decision = DROP_FILENAME_REJECT.

### Acceptance check

- New and existing tests pass.
- Manual spot-check: replay one page of HTML from a prior crawl (captured via `extract_candidates(html, ...)`) and verify the list of accepted candidates shrinks vs. pre-change.

## Sub-Phase 1.6 — Instrumentation (decisions log)

**Goal**: every candidate evaluation writes a JSON line to a rotating file.

### Files created / modified

- `lavandula/reports/decisions_log.py`:
  ```python
  import json
  import logging
  from logging.handlers import TimedRotatingFileHandler
  from pathlib import Path
  from datetime import datetime, timezone
  from .url_redact import redact_url

  _logger: logging.Logger | None = None

  # Fields whose values are URLs that must be redacted before logging.
  # Consistent with the existing DB-side pattern where only
  # source_url_redacted / referring_page_url_redacted are stored.
  _URL_FIELDS = frozenset({"url", "referring_page"})

  # Fields allowed raw into the log. Anything else is dropped to avoid
  # accidental leakage of future unredacted context added by callers.
  _ALLOWED_FIELDS = frozenset({
      "ts", "ein", "url_redacted", "referring_page_redacted",
      "basename", "filename_score", "triage",
      "strong_path_hit", "weak_path_hit",
      "anchor_text", "anchor_hit",
      "decision", "reason",
  })

  def _init() -> logging.Logger:
      logger = logging.getLogger("lavandula.crawler.decisions")
      logger.setLevel(logging.INFO)
      logger.propagate = False  # do not pollute root logger
      log_dir = Path("logs")
      log_dir.mkdir(exist_ok=True)
      handler = TimedRotatingFileHandler(
          filename=log_dir / "crawler_decisions.jsonl",
          when="midnight",
          backupCount=90,
          encoding="utf-8",
          utc=True,
      )
      handler.setFormatter(logging.Formatter("%(message)s"))  # raw JSON, no prefix
      if not logger.handlers:
          logger.addHandler(handler)
      return logger

  def log_decision(record: dict) -> None:
      """Emit a JSONL decision record. Redacts URL fields and allowlists keys."""
      global _logger
      if _logger is None:
          _logger = _init()
      safe: dict = {}
      for k, v in record.items():
          if k in _URL_FIELDS and isinstance(v, str) and v:
              safe[f"{k}_redacted"] = redact_url(v)
          elif k in _ALLOWED_FIELDS:
              safe[k] = v
          # else: silently dropped
      safe.setdefault("ts", datetime.now(timezone.utc).isoformat())
      _logger.info(json.dumps(safe, default=str))
  ```

**Logging safety**: every URL passed to the logger is redacted via `lavandula.reports.url_redact.redact_url` (the same function that produces `source_url_redacted` and `referring_page_url_redacted` in the `reports` table — consistent with the existing codebase pattern). The logger uses an **allowlist** for non-URL fields: anything not explicitly permitted is silently dropped. This prevents future callers from accidentally logging sensitive context by adding new keys to the record dict.

**Anchor text caveat**: `anchor_text` is allowed through unredacted because it *is* the primary signal we are measuring. In practice anchor text is rarely sensitive (it's public page content), but callers should not put PII-bearing data into that field.

- `lavandula/reports/candidate_filter.py`: call `decisions_log.log_decision({...})` inside `_classify_link` at each terminal branch (accept/drop). Pass raw `url` and `referring_page` — the logger handles redaction.
- `lavandula/reports/discover.py`: pass enough context (ein, referring page) through to the logger. Add fields to the record dict at the `_classify_link` call site.

### Tests

- `test_decisions_log_writes_jsonl` — redirect the logger's file path to `tmp_path`, call `log_decision`, read file, assert valid JSON per line with the expected fields.
- `test_decisions_log_redacts_url_fields` — call `log_decision({"url": "https://example.org/x?token=secret", ...})`; confirm output contains `"url_redacted"` key and the token query param is stripped. Confirm no raw `url` key appears.
- `test_decisions_log_drops_unknown_fields` — call `log_decision({"password": "hunter2", "triage": "accept"})`; confirm `password` is not present in the output.
- `test_decisions_log_rotates_daily` — harder; can be skipped if the logging-handler behavior is hard to simulate. Cover via integration test in 1.7 instead.
- `test_log_emitted_on_drop_filename_reject` — stub a candidate, run the filter, confirm one log record with `decision == "drop"` and `reason == "filename_score<=reject"`.

### Acceptance check

- Running any crawler test that exercises `_classify_link` writes the expected number of records to `logs/crawler_decisions.jsonl`.
- File is not written to if `_classify_link` is never invoked (sanity).

## Sub-Phase 1.7 — Integration test & Fordham regression gate

**Goal**: end-to-end proof that the spec's AC13 target holds on a real baseline.

### Setup

- A fixture HTML corpus captured once from Fordham's site: `lavandula/reports/tests/fixtures/fordham_2026_04_23/` with the pages the original run crawled (homepage, media index pages, subpages found via `/events/`, `/campaign/`, etc.). Capture via a one-shot script committed alongside that runs `requests.get` with polite headers and saves the raw HTML under filename-hashed names. Capturing once is acceptable because we're proving the filter change, not proving the network path works.

### Integration test

- `lavandula/reports/tests/integration/test_fordham_baseline_regression.py`:
  - Load each captured HTML page, call `extract_candidates` with the current taxonomy.
  - Collect the full set of candidate URLs that would be fetched after the new triage.
  - Assert `len(candidates) <= 15`.
  - Assert that each of the 41 items from the 2026-04-23 run with `filename_score >= 0.8` is either in the candidate set OR not present in the fixture HTML (if the sub-page it came from wasn't captured).

### Acceptance check

- Test passes.
- If the test fails, the failure message prints the candidate basenames it would have fetched — easy to diff against expectation.

## Sub-Phase 1.8 — Final regression & documentation

**Goal**: close out the PR with a clean test run and brief operator documentation.

### Tasks

- Full `pytest lavandula/reports/tests/` run in CI mode.
- Update `lavandula/reports/README.md` (or create one) with:
  - "Taxonomy lives in `lavandula/docs/collateral_taxonomy.yaml`. To tune keyword lists or thresholds, edit that file. Changes take effect on next crawler start."
  - Sample YAML editing workflow (edit → PR → merge → restart crawler).
  - Pointer to `logs/crawler_decisions.jsonl` for inspecting per-decision data.
- Confirm CHANGELOG or release notes mention the behavior change.

### Acceptance check

- All tests green.
- Manual smoke test: start the crawler against a dev/test seed pool, confirm decisions log fills with expected records, spot-check 10 random entries.

---

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Hand-transcribed YAML loses a keyword vs. current config | Medium | Sub-phase 1.2's parity check. CI test comparing legacy aliases to current sets. |
| Filename grader over-fits to the 378-doc baseline, hurts recall elsewhere | Medium | Don't tune weights to pass AC12; tune keyword lists. Accept some missed edge cases. |
| Fordham fixture drift (site changes between capture and review) | Low | Fixture is captured once, versioned in git. Recapture only if the target site's structure changes meaningfully. |
| `TimedRotatingFileHandler` writes break under multiprocessing crawler run | Medium | Current crawler uses a `ThreadPoolExecutor` (single process), so the default file handler is safe. Flag this in docs. For future multi-process crawlers, switch to `QueueHandler`. |
| PM-level YAML edit introduces a regression we don't catch | Medium | Validator + integration tests form the safety net. The decisions log makes post-hoc diagnosis easy. |
| Decisions log disk usage exceeds estimate | Low | `backupCount=90` plus explicit size estimate in spec. If observed usage balloons, switch to gzipped rotation. |

## Out of scope (deferred)

- PDF `/Title` metadata as a second-pass filename signal
- `Content-Disposition` header as a filename source
- Multi-process crawler log handling
- Taxonomy hot-reload without crawler restart
- Dashboard UI for YAML editing (that's spec 0019 Iteration 2+)
- Per-event-type subtypes on classifier output (spec 0021, Phase 2)
- Renaming `reports` table → `collaterals` (Phase 3)
- Automated dependency vulnerability scanning (`pip-audit`, Dependabot, etc.) — org-wide concern, not scoped to this spec. Should be addressed by a separate DevOps/CI spec covering the whole `lavandula/` tree.

## Estimated effort

Single builder, focused work:

- 1.1: YAML + validator + loader + tests — 4–6 hours
- 1.2: Config refactor + parity check — 1–2 hours
- 1.3: Anchor extraction — 1 hour
- 1.4: Filename grader + baseline validation — 3–4 hours (keyword-list tuning iteration)
- 1.5: Triage integration + tests — 3 hours
- 1.6: Instrumentation — 1–2 hours
- 1.7: Fordham fixture capture + integration test — 2–3 hours
- 1.8: Cleanup + docs — 1 hour

**Total estimate: 16–22 hours of focused builder work.** Likely one PR spanning 1–2 days of wall-clock time.

## Consultation Log

### First Consultation (After Initial Draft)
**Date**: 2026-04-24
**Models Consulted**: Gemini ✅
**Commands**:
```
consult --model gemini --type plan-review plan 0020
```

Round 1 — Gemini plan review

**Verdict**: APPROVE (HIGH confidence)

Single finding, addressed:

1. **Keyword normalization at load time**. The runtime Taxonomy view must lowercase all keywords so mixed-case YAML entries match lowercased URL basenames and paths. → **Applied in two places**:
   - Validator now **rejects** uppercase in keyword lists (`filename_signals`, `path_keywords`, `filename_negative_signals` — all the machine-matched lists). `anchor_signals` (human-readable phrases) may contain display casing in the YAML and are lowercased at runtime-view build time as belt-and-suspenders.
   - `_build_runtime_view` lowercases all aggregations so the invariant holds even if validator rules loosen in future.

Additional refinements from the plan re-read:

2. **Explicit normalization invariant in sub-phase 1.5**: both keyword and comparison target are lowercased; matching code should never add an extra `.lower()` on the keyword side (that masks YAML problems). Made the invariant a named rule so reviewers catch violations.
3. **Basename extraction spec**: explicitly unquote + lowercase in `_basename_from_url` so case-differences in URL paths don't cause spurious mismatches between the filename signal and the path signal.
4. Added two tests: `test_rejects_uppercase_keyword` and `test_runtime_view_lowercases_anchor_signals`.

### Red Team Security Review (MANDATORY)
**Date**: 2026-04-24
**Model**: Gemini 2.5 Flash (fallback from pro and flash-preview due to quota exhaustion)
**Commands**:
```
# consult --model gemini --type red-team-plan plan 0020 (hit quota; reran via direct gemini CLI)
gemini --yolo --model gemini-2.5-flash -o text -p "<red-team-plan template + plan>"
```

Round 2 — Gemini red-team plan review

**Verdict**: REQUEST_CHANGES → addressed (HIGH confidence — CRITICAL 0, HIGH 0, MEDIUM 1, LOW 1; all findings applied)

Both findings addressed:

**MEDIUM #1 — Sensitive data leakage in `decisions_log.py`**. Original sketch logged raw `url` and `referring_page`, which can carry query-parameter tokens, fragment-embedded auth material, or internal hostnames. → **Applied** URL redaction via the existing `url_redact.redact_url` function (same function used for the DB `source_url_redacted` / `referring_page_url_redacted` columns — established codebase pattern). Added an **allowlist** for non-URL fields so future callers can't accidentally leak new context by adding keys to the record dict. Added two unit tests: `test_decisions_log_redacts_url_fields` and `test_decisions_log_drops_unknown_fields`.

**LOW #1 — No dependency vulnerability management strategy**. → **Documented** as out-of-scope with rationale: this is an org-wide DevOps concern (affects the whole `lavandula/` tree, not just the crawler), and Phase 1's Pydantic + PyYAML usage is already hardened against the specific CVEs that matter here (safe_load, version pinning). Added explicitly to the Out-of-Scope list with a pointer to a future DevOps spec.

**Self-review catches from the red-team re-read:**

5. Anchor text is not redacted — it is the primary signal we are measuring. Noted explicitly in the plan: callers must not put PII-bearing data into the `anchor_text` field (belongs to page content, not user data).
