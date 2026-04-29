# Plan 0025: Definition-Driven Classifier

**Spec**: `locard/specs/0025-definition-driven-classifier.md`
**Status**: Draft
**Author**: Architect
**Date**: 2026-04-29

## Overview

Replace the hard-coded V1 classifier prompt in `gemma_client.py` and the taxonomy-based V2 prompt in `classify.py` with a unified definition-file-driven system. Both `pipeline_classify` (OpenAI-compatible API) and `classify_null` (subscription CLI) read the same definition file via a shared loader, producing identical prompt structure and output schema.

## Critical Files

| File | Role |
|------|------|
| `lavandula/nonprofits/definitions/corpus_reports.md` | **NEW** — The definition file |
| `lavandula/nonprofits/definition_loader.py` | **NEW** — Loader, parser, schema builder |
| `lavandula/nonprofits/gemma_client.py` | **MODIFY** — `LLMClient.classify()` uses definition |
| `lavandula/nonprofits/pipeline_classify.py` | **MODIFY** — Consumer writes all columns + `classifier_definition` |
| `lavandula/nonprofits/tools/pipeline_classify.py` | **MODIFY** — Add `--definition` flag |
| `lavandula/reports/tools/classify_null.py` | **MODIFY** — Refactor to use `load_definition()` |
| `lavandula/reports/classify.py` | **MODIFY** — V2 functions delegate to definition loader |
| `lavandula/dashboard/pipeline/orchestrator.py` | **MODIFY** — Add `definition` to COMMAND_MAP |
| `lavandula/dashboard/pipeline/forms.py` | **MODIFY** — Add definition dropdown |

## Phased Implementation

### Phase 1: Definition File + Loader (AC1-AC9)

**Goal**: Create the definition file format and the loader module. This is the foundation — everything else depends on it.

#### Step 1.1: Create definitions directory and `corpus_reports.md`

Create `lavandula/nonprofits/definitions/corpus_reports.md` with:

```yaml
---
name: corpus_reports
version: 1
description: Classify nonprofit PDF documents by material type
source_taxonomy: collateral_taxonomy.yaml
output_columns:
  - material_type
  - material_group
  - event_type
---
```

Then write the Markdown body with four sections:

1. **`# System Instructions`** — The system prompt text (prompt injection defense with `<untrusted_document>` tags, tool-call instruction).

2. **`# Categories`** — Structured as `## group_name` / `### category_id` with description, `**Examples**`, and `**Not this**` blocks. Include ALL material types from `collateral_taxonomy.yaml` (70+). Focus detailed examples on high-volume ambiguous categories: `annual_report` vs `other_collateral`, `financial_report` vs `not_relevant`, `impact_report` vs `annual_report`. Rare categories (e.g., `bid_paddle`, `tee_gift_card`) get one-line descriptions only per spec trap #6.

3. **`# Guidelines`** — Classification rules (most-specific-wins, catch-all semantics, confidence guidance, event_type usage rules).

4. **`# Event Types`** — Flat list of `- event_type_id` items matching `collateral_taxonomy.yaml`.

**Source of truth**: All category IDs, group assignments, and event type IDs must match `collateral_taxonomy.yaml` exactly. The definition file adds descriptions, examples, and counter-examples that the taxonomy YAML doesn't have.

#### Step 1.2: Create `definition_loader.py`

Create `lavandula/nonprofits/definition_loader.py` with:

**Data structures:**

```python
@dataclass(frozen=True)
class CategoryDef:
    id: str          # e.g., "annual_report"
    group: str       # e.g., "reports"
    body: str        # Full Markdown body (description + examples + counter-examples)

@dataclass(frozen=True)
class EventTypeDef:
    id: str          # e.g., "gala"

@dataclass(frozen=True)
class ClassifierDefinition:
    name: str
    version: int
    description: str
    source_taxonomy: str | None
    output_columns: list[str]
    system_prompt: str
    categories: list[CategoryDef]
    guidelines: str
    event_types: list[EventTypeDef]
    tool_schema: dict   # Pre-built OpenAI function-calling schema

    def get_category(self, category_id: str) -> CategoryDef | None:
        ...
```

**Functions:**

- `load_definition(name: str) -> ClassifierDefinition` — Main entry point.
  - Validate name: `^[a-z][a-z0-9_]*$`, no `/` or `..`
  - Resolve path: `Path(__file__).parent / "definitions" / f"{name}.md"`
  - File size guard: reject > 100KB
  - Parse YAML frontmatter (everything between `---` delimiters)
  - Validate required frontmatter fields: `name`, `version`, `description`, `output_columns`
  - Extract sections by `#`-level headings: `System Instructions`, `Categories`, `Guidelines`, `Event Types`
  - Reject unrecognized `#`-level headings (raise `DefinitionLoadError`)
  - Parse categories: `## group` → `### category_id` structure, validate IDs with `^[a-z][a-z0-9_]*$`
  - Parse event types: flat `- id` list
  - If `source_taxonomy` is set, validate all IDs against the taxonomy YAML
  - Build tool schema via `build_tool_schema()`
  - Cache at module level (one load per process)

- `build_tool_schema(definition: ClassifierDefinition) -> dict` — Builds OpenAI-compatible function-calling schema with `enum` constraint on `material_type` from definition categories. See spec for exact schema shape.

- `material_type_to_legacy(material_type_id: str) -> str` — Wraps the existing `_MATERIAL_TYPE_TO_LEGACY` mapping from `taxonomy.py`. Import and delegate — don't duplicate the mapping.

**Error handling:**

- `DefinitionLoadError` — Raised for all loader failures (missing file, malformed frontmatter, bad IDs, size limit, unrecognized sections, taxonomy validation failure).

**Implementation notes:**

- Use `yaml.safe_load` for frontmatter parsing.
- For Markdown section parsing: split on lines starting with `# ` (single `#` + space). Within `# Categories`, split on `## ` for groups and `### ` for categories. Lines in `# System Instructions` and `# Guidelines` that happen to have `##` or lower headings pass through as prose (spec parser rule 5).
- Module-level cache: `_cache: dict[str, ClassifierDefinition] = {}`.
- Source taxonomy validation: load the YAML via `taxonomy.load_taxonomy()`, check each category ID is in `taxonomy.material_type_ids` or is `other_collateral`/`not_relevant`, check each event type ID is in `taxonomy.event_type_ids`.

#### Step 1.3: Unit tests for loader

Test file: `tests/test_definition_loader.py`

- Valid file loads correctly (all fields populated, tool schema has correct enum)
- Missing file → `DefinitionLoadError`
- Malformed frontmatter (missing required field) → `DefinitionLoadError`
- Bad category ID (uppercase, special chars) → `DefinitionLoadError`
- Unrecognized `#`-level heading → `DefinitionLoadError`
- File > 100KB → `DefinitionLoadError`
- `source_taxonomy` validation catches unknown category ID
- Tool schema `material_type.enum` matches definition categories
- Tool schema `event_type.enum` includes `None`
- `get_category()` returns correct `CategoryDef` or `None`
- Caching: second call returns same object

**Fixtures**: Create a minimal test definition file in `tests/fixtures/definitions/test_minimal.md` with ~5 categories. Don't test against the full `corpus_reports.md` in unit tests — that's for integration.

---

### Phase 2: `pipeline_classify` Integration (AC10-AC15, AC18, AC22-AC24)

**Goal**: Replace the V1 classifier in `gemma_client.py` with definition-driven classification. Update the consumer to write all columns.

#### Step 2.1: Modify `LLMClient` in `gemma_client.py`

```python
class LLMClient:
    def __init__(self, *, base_url, model, api_key=None, definition_name="corpus_reports"):
        ...
        self._definition = load_definition(definition_name)

    def classify(self, first_page_text: str) -> dict:
        """Definition-driven classification. Returns dict with all fields."""
        messages = [
            {"role": "system", "content": self._definition.system_prompt},
            {"role": "user", "content": (
                "Classify the nonprofit PDF below by calling the "
                "record_classification tool.\n"
                "<untrusted_document>\n"
                f"{first_page_text}\n"
                "</untrusted_document>"
            )},
        ]
        body = self._build_request_body(messages, self._definition.tool_schema)
        resp = self._call(body)
        result = self._parse_tool_response(resp, "record_classification")

        # Derive group + legacy classification
        mt = result.get("material_type")
        if mt:
            cat = self._definition.get_category(mt)
            if cat is None:
                raise LLMParseError(f"Unknown material_type from LLM: {mt}")
            result["material_group"] = cat.group
            result["classification"] = material_type_to_legacy(mt)
        else:
            raise LLMParseError("LLM response missing material_type")

        # Validate event_type if present
        et = result.get("event_type")
        if et is not None:
            valid_ets = {e.id for e in self._definition.event_types}
            if et not in valid_ets:
                raise LLMParseError(f"Unknown event_type from LLM: {et}")

        result["classifier_definition"] = f"{self._definition.name}:v{self._definition.version}"
        return result

    @property
    def definition(self):
        return self._definition
```

**Keep**: `CLASSIFIER_PROMPT_V1` and `CLASSIFIER_TOOL_V1` constants remain for backward reference but are no longer called by `classify()`. Remove them only if no other code imports them — check first.

**Do not change**: `disambiguate()` method, `_build_request_body()`, `_call()`, `_parse_tool_response()` — these are shared with the resolver and work correctly.

#### Step 2.2: Modify consumer in `pipeline_classify.py`

Update the successful-classification DB write to include all columns:

```python
# In classify_consumer, after result = gemma.classify(first_page_text):
with engine.begin() as conn:
    conn.execute(
        text(
            f"UPDATE {_SCHEMA}.corpus SET "
            "classification=:cls, "
            "classification_confidence=:conf, "
            "classifier_model=:model, "
            "material_type=:mt, "
            "material_group=:mg, "
            "event_type=:et, "
            "reasoning=:reasoning, "
            "classifier_definition=:cdef "
            "WHERE content_sha256=:csha"
        ),
        {
            "cls": result.get("classification", "other"),
            "conf": float(result.get("confidence", 0)),
            "model": method,
            "mt": result.get("material_type"),
            "mg": result.get("material_group"),
            "et": result.get("event_type"),
            "reasoning": (result.get("reasoning") or "")[:500],
            "cdef": result.get("classifier_definition"),
            "csha": content_sha256,
        },
    )
```

Also update the `parse_error` and `skipped` paths to write `classifier_definition`:

```python
# parse_error path:
"classification='parse_error', "
"classifier_model=:model, "
"classifier_definition=:cdef "

# skipped path (in producer):
"classification='skipped', "
"classifier_model=:model, "
"classifier_definition=:cdef "
```

**Producer needs access to the definition string**. Pass `classifier_definition` as a parameter to `classify_producer()` alongside `method`. The producer writes it on skipped rows.

#### Step 2.3: Add `--definition` flag to CLI

In `lavandula/nonprofits/tools/pipeline_classify.py`:

```python
p.add_argument("--definition", default="corpus_reports",
               help="Definition file name (default: corpus_reports)")
```

Pass to `LLMClient`:

```python
llm = LLMClient(
    base_url=args.llm_url, model=args.llm_model,
    api_key=api_key_value, definition_name=args.definition,
)
```

Pass `classifier_definition` string to producer:

```python
cdef = f"{llm.definition.name}:v{llm.definition.version}"
```

#### Step 2.4: DB migration

**Single SQL migration** file (not Django migration — this project uses raw SQL migrations applied via PGAdmin per the single-operator pattern):

```sql
-- Migration: Add classifier_definition column + formalize corpus_class_chk
ALTER TABLE lava_corpus.corpus ADD COLUMN IF NOT EXISTS classifier_definition TEXT;

ALTER TABLE lava_corpus.corpus DROP CONSTRAINT IF EXISTS corpus_class_chk;
ALTER TABLE lava_corpus.corpus ADD CONSTRAINT corpus_class_chk
  CHECK (classification IS NULL OR classification IN
         ('annual','impact','hybrid','other','not_a_report','skipped','parse_error'));
```

Place at: `lavandula/migrations/rds/migration_XXX_classifier_definition.sql` (determine next migration number).

**Important**: The CHECK constraint was already applied manually during this session. The migration formalizes it. The `ADD COLUMN IF NOT EXISTS` is safe for re-runs.

---

### Phase 3: `classify_null` Integration (AC16-AC17, AC19)

**Goal**: Refactor `classify_null` to use the shared `load_definition()` path instead of building its own taxonomy prompt.

#### Step 3.1: Refactor classify.py V2 functions

The V2 classifier in `classify.py` currently builds its own prompt from `build_taxonomy_prompt_section()`. Refactor `classify_first_page_v2()` to accept a `ClassifierDefinition` and use its system prompt + tool schema.

Option A (preferred): Add a new function `classify_first_page_v3()` that takes a `ClassifierDefinition` and delegates to the existing Anthropic client pathway. This avoids breaking the V2 pathway while tests are being migrated.

```python
def classify_first_page_v3(
    first_page_text: str,
    *,
    client: _HasMessagesCreate,
    definition: ClassifierDefinition,
    model: str | None = None,
    raise_on_error: bool = True,
) -> ClassificationResult:
    """V3 classifier using definition-driven prompt."""
    system = definition.system_prompt
    user = (
        "Classify the nonprofit PDF below by calling the "
        "record_classification tool.\n"
        "<untrusted_document>\n"
        f"{first_page_text}\n"
        "</untrusted_document>"
    )
    # Build Anthropic tool format from definition's OpenAI format
    tool = _openai_to_anthropic_tool(definition.tool_schema)
    ...
```

**Critical**: The Anthropic tool format uses `input_schema` while OpenAI uses `parameters`. The `build_tool_schema()` function produces OpenAI format (for `pipeline_classify`). We need a converter:

```python
def _openai_to_anthropic_tool(openai_schema: dict) -> dict:
    """Convert OpenAI function-calling schema to Anthropic tool format."""
    fn = openai_schema["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "input_schema": fn["parameters"],
    }
```

Validation of the response uses the definition's category list (same as `gemma_client.py` does):
- Check `material_type` is in `{c.id for c in definition.categories}`
- Check `event_type` is in `{e.id for e in definition.event_types}` or is None
- Derive `material_group` from `definition.get_category(mt).group`
- Derive `classification` from `material_type_to_legacy(mt)`

#### Step 3.2: Refactor `classify_null.py` main()

- Add `--definition` flag (default: `corpus_reports`)
- Load definition at startup: `defn = load_definition(args.definition)`
- Pass `defn` to `classify_first_page_v3()` instead of taxonomy
- Write `classifier_definition` in `_write_result()`:
  ```python
  "classifier_definition=:cdef"
  ...
  "cdef": f"{defn.name}:v{defn.version}",
  ```

#### Step 3.3: Parity test

Test that given the same definition file and the same fixture text, both classifiers produce the same `material_type`, `material_group`, `classification`, and `classifier_definition`. The LLM-dependent fields (confidence, reasoning) may vary since different LLM backends are used, but the enum-constrained fields (material_type, event_type) and derived fields (material_group, classification) must match when the LLM returns the same raw classification.

Test approach: mock both LLM backends to return the same raw `material_type` + `event_type` + `confidence` + `reasoning`, verify the derived fields match exactly.

---

### Phase 4: Dashboard Integration (AC20-AC21)

**Goal**: Add `definition` parameter to the orchestrator and dashboard form.

#### Step 4.1: Update COMMAND_MAP in `orchestrator.py`

Add `definition` to the `classify` phase params:

```python
"classify": {
    "cmd": [...],
    "params": {
        ...existing params...,
        "definition": {"type": "text", "pattern": r"^[a-z][a-z0-9_]*$", "flag": "--definition"},
    },
},
```

#### Step 4.2: Update ClassifierForm in `forms.py`

Add a definition dropdown. Dynamically discover available definitions:

```python
def _get_definition_choices():
    """Scan definitions/ directory for available .md files."""
    defn_dir = Path(__file__).resolve().parents[2] / "nonprofits" / "definitions"
    choices = []
    if defn_dir.is_dir():
        for f in sorted(defn_dir.glob("*.md")):
            choices.append((f.stem, f.stem))
    if not choices:
        choices = [("corpus_reports", "corpus_reports")]
    return choices

class ClassifierForm(forms.Form):
    ...existing fields...
    definition = forms.ChoiceField(
        choices=_get_definition_choices,  # callable for lazy eval
        initial="corpus_reports",
        widget=forms.Select(attrs={"class": _SELECT}),
        label="Definition",
    )
```

---

### Phase 5: Re-classification Targeting (AC25-AC27)

**Goal**: Add `--re-classify-definition` flag for targeted re-classification.

#### Step 5.1: Add flag to `pipeline_classify` CLI

```python
p.add_argument("--re-classify-definition", default=None,
               help="Re-classify rows where classifier_definition != this value "
               "(e.g., corpus_reports:v2). Uses IS DISTINCT FROM to include NULLs.")
```

When set, the producer's WHERE clause changes:
- Instead of `AND classification IS NULL`, use `AND classifier_definition IS DISTINCT FROM :target_def`
- This selects NULL rows (pre-definition) + rows classified under a different definition version

#### Step 5.2: Add flag to `classify_null` CLI

Same flag, same semantics. Modify the SQL query builder in `main()`.

#### Step 5.3: Add to COMMAND_MAP

```python
"re_classify_definition": {
    "type": "text",
    "pattern": r"^[a-z][a-z0-9_]*:v\d+$",
    "flag": "--re-classify-definition",
},
```

---

### Phase 6: Tests (AC28-AC39)

#### Step 6.1: Error handling tests (AC28-AC31)

Mocked tests in `tests/test_definition_classifier_errors.py`:

- Unknown `material_type` from LLM → `parse_error` written with `classifier_definition` set
- Unknown `event_type` from LLM → `parse_error` written
- Unparseable LLM response → `parse_error` written (existing behavior preserved)
- Empty `first_page_text` → `skipped` written with `classifier_definition` set

#### Step 6.2: Mocked response-path tests (AC38)

- Valid tool response → correct derived DB fields (`classification`, `material_group`, `classifier_definition`)
- Invalid enum → `parse_error`
- Missing required fields → `parse_error`
- `classifier_definition` persisted on both success and failure paths

#### Step 6.3: `--re-classify-definition` query test (AC39)

Mock the DB and verify:
- `IS DISTINCT FROM` correctly selects NULL rows
- `IS DISTINCT FROM` correctly selects mismatched version rows
- Does NOT select rows with matching definition version

#### Step 6.4: Integration tests with fixture text (AC35-AC36)

**These require a live LLM**. Mark with `@pytest.mark.integration` or similar skip marker.

- Fixture: "2024 Annual Report to the Community\nDear Friends, As we reflect on another year..." → `material_type='annual_report'`, `material_group='reports'`, `classification='annual'`, `confidence >= 0.7`
- Fixture: "Form 990 Return of Organization Exempt From Income Tax\nDepartment of the Treasury Internal Revenue Service..." → `material_type` in (`financial_report`, `not_relevant`)

#### Step 6.5: `material_type_to_legacy()` coverage test (AC34)

Verify every material type in `corpus_reports.md` maps to a valid legacy classification. Load the definition, iterate all categories, call `material_type_to_legacy()`, assert result is in `('annual', 'impact', 'hybrid', 'other', 'not_a_report')`.

---

## Dependency Graph

```
Phase 1 (Definition + Loader)
    ├── Phase 2 (pipeline_classify)
    │       └── Phase 4 (Dashboard)
    ├── Phase 3 (classify_null)
    └── Phase 5 (Re-classify targeting)
            └── depends on Phase 2 + Phase 3

Phase 6 (Tests) runs alongside Phases 2-5
```

Phases 2 and 3 can be done in parallel after Phase 1. Phase 4 depends on Phase 2 (needs COMMAND_MAP). Phase 5 depends on both Phase 2 and Phase 3 (both CLIs need the flag). Phase 6 tests are written alongside each phase.

## Traps to Avoid

1. **OpenAI vs Anthropic tool schema format** — `pipeline_classify` uses OpenAI format (`parameters`), `classify_null` uses Anthropic format (`input_schema`). The loader produces OpenAI format; the `classify_null` integration needs a converter. Don't build two separate loaders.

2. **Don't forget `classifier_definition` on error paths** — The spec is explicit: ALL attempted rows (including `parse_error` and `skipped`) must write `classifier_definition`. This is critical for `--re-classify-definition` targeting to work correctly.

3. **Producer needs the definition string** — Currently `classify_producer()` only receives `method` (model name). It also needs `classifier_definition` for the skipped-row write. Thread it through as a new parameter.

4. **Don't break the resolver** — `LLMClient` is shared between classifier and resolver. The `definition_name` parameter must default to `"corpus_reports"` and only affect `classify()`, not `disambiguate()`.

5. **Legacy mapping lives in one place** — Use `taxonomy.material_type_to_legacy()` or import the `_MATERIAL_TYPE_TO_LEGACY` dict. Don't duplicate it in `definition_loader.py`.

6. **Category count** — The full taxonomy has 70+ material types. The definition file with descriptions + examples for all of them will be large. Stay under 100KB. Rare categories get one-line descriptions per spec trap #6.

7. **The CHECK constraint on `classification`** — This constrains legacy values only (`annual`, `impact`, `hybrid`, `other`, `not_a_report`, `skipped`, `parse_error`). The `material_type` column has no CHECK constraint — it's constrained by the `enum` in the tool schema and validated in code.

8. **`classify_null` already writes `classifier_version = 2`** — After this spec, `classifier_version` is superseded by `classifier_definition`. Keep writing `classifier_version` for backward compat (set to `3`), but the primary tracking column is `classifier_definition`.

## Acceptance Criteria Mapping

| AC | Phase | Verification |
|----|-------|-------------|
| AC1-AC3 | 1.1 | Definition file exists with correct format |
| AC4-AC8 | 1.2 | Loader unit tests pass |
| AC9 | 1.2 | Tool schema test: enum matches categories |
| AC10-AC11 | 2.1 | `LLMClient` uses definition prompt |
| AC12-AC14 | 2.1, 2.2 | Consumer writes all columns |
| AC15 | 2.2 | DB write includes all 8 columns |
| AC16-AC17 | 3.1-3.3 | classify_null uses loader, parity test passes |
| AC18 | 2.3 | `--definition` flag on pipeline_classify |
| AC19 | 3.2 | `--definition` flag on classify_null |
| AC20 | 4.1 | COMMAND_MAP includes definition |
| AC21 | 4.2 | ClassifierForm has definition dropdown |
| AC22 | 2.4 | Migration adds column |
| AC23 | 2.2, 3.2 | Both classifiers write `classifier_definition` |
| AC24 | 2.4 | Migration formalizes CHECK constraint |
| AC25-AC26 | 5.1 | `--re-classify` works with definition |
| AC27 | 5.1-5.3 | `--re-classify-definition` flag + IS DISTINCT FROM |
| AC28-AC31 | 6.1 | Error handling tests |
| AC32-AC33 | 1.3 | Loader + schema unit tests |
| AC34 | 6.5 | Legacy mapping coverage test |
| AC35-AC36 | 6.4 | Integration tests (LLM required) |
| AC37 | 3.3 | Parity test |
| AC38 | 6.2 | Mocked response-path tests |
| AC39 | 6.3 | `--re-classify-definition` query test |
