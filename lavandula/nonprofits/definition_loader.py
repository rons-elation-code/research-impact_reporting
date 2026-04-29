"""Definition-driven classifier loader (Spec 0025).

Loads Markdown+YAML definition files that describe how to classify
documents. Both pipeline_classify (OpenAI API) and classify_null
(Anthropic CLI) consume the same ClassifierDefinition object.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_RE = _ID_RE
_MAX_FILE_SIZE = 100_000
_MAX_PROMPT_CHARS = 50_000
_YAML_ANCHOR_RE = re.compile(r"[&*]\w")

_KNOWN_SECTIONS = frozenset({
    "System Instructions",
    "Categories",
    "Guidelines",
    "Event Types",
})

_UNTRUSTED_OPEN_RE = re.compile(r"<untrusted_document", re.IGNORECASE)
_UNTRUSTED_CLOSE_RE = re.compile(r"</untrusted_document", re.IGNORECASE)


class DefinitionLoadError(RuntimeError):
    """Raised for all definition loader failures."""


@dataclass(frozen=True)
class CategoryDef:
    id: str
    group: str
    body: str


@dataclass(frozen=True)
class EventTypeDef:
    id: str


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
    tool_schema: dict
    _categories_by_id: dict[str, CategoryDef] = field(
        default_factory=dict, repr=False, compare=False,
    )

    def __post_init__(self):
        by_id = {c.id: c for c in self.categories}
        object.__setattr__(self, "_categories_by_id", by_id)

    def get_category(self, category_id: str) -> CategoryDef | None:
        return self._categories_by_id.get(category_id)


def sanitize_document_text(text: str) -> str:
    """Strip untrusted_document tags from document text before wrapping."""
    text = _UNTRUSTED_OPEN_RE.sub("[TAG_STRIPPED]", text)
    text = _UNTRUSTED_CLOSE_RE.sub("[TAG_STRIPPED]", text)
    return text


def resolve_definition_name(cli_value: str | None = None) -> str:
    """Resolve definition name: CLI flag > env var > default."""
    if cli_value:
        return cli_value
    return os.environ.get("LAVANDULA_CLASSIFIER_DEFINITION", "corpus_reports")


_cache: dict[str, ClassifierDefinition] = {}


def _clear_cache() -> None:
    """Clear the definition cache (for test isolation)."""
    _cache.clear()


def load_definition(name: str) -> ClassifierDefinition:
    """Load a definition file by name. Caches at module level."""
    if name in _cache:
        return _cache[name]

    if not _NAME_RE.match(name):
        raise DefinitionLoadError(
            f"Invalid definition name {name!r}: must match {_NAME_RE.pattern}"
        )
    if "/" in name or ".." in name:
        raise DefinitionLoadError(
            f"Path traversal rejected in definition name: {name!r}"
        )

    path = Path(__file__).parent / "definitions" / f"{name}.md"
    if not path.is_file():
        raise DefinitionLoadError(f"Definition file not found: {path}")

    size = path.stat().st_size
    if size > _MAX_FILE_SIZE:
        raise DefinitionLoadError(
            f"Definition file too large: {size} bytes (max {_MAX_FILE_SIZE})"
        )

    raw = path.read_text(encoding="utf-8")
    defn = _parse_definition(raw, name, path)
    _cache[name] = defn
    return defn


def _parse_definition(raw: str, name: str, path: Path) -> ClassifierDefinition:
    """Parse a definition file from raw text."""
    frontmatter, body = _split_frontmatter(raw)
    meta = _parse_frontmatter(frontmatter, name)
    sections = _split_sections(body)
    categories = _parse_categories(sections.get("Categories", ""))
    event_types = _parse_event_types(sections.get("Event Types", ""))
    guidelines = sections.get("Guidelines", "").strip()
    system_instructions = sections.get("System Instructions", "").strip()

    if not system_instructions:
        raise DefinitionLoadError("Missing '# System Instructions' section")
    if not sections.get("Categories", "").strip():
        raise DefinitionLoadError("Missing '# Categories' section")

    if meta["source_taxonomy"]:
        _validate_against_taxonomy(
            categories, event_types, meta["source_taxonomy"], path
        )

    system_prompt = _assemble_system_prompt(
        system_instructions, categories, guidelines, event_types,
    )
    if len(system_prompt) > _MAX_PROMPT_CHARS:
        raise DefinitionLoadError(
            f"Assembled system prompt too large: {len(system_prompt)} chars "
            f"(max {_MAX_PROMPT_CHARS})"
        )
    log.info(
        "Definition %s:v%d loaded — %d categories, %d event types, "
        "prompt %d chars",
        meta["name"], meta["version"], len(categories), len(event_types),
        len(system_prompt),
    )

    output_columns = meta["output_columns"]
    tool_schema = _build_tool_schema(categories, event_types, output_columns)

    return ClassifierDefinition(
        name=meta["name"],
        version=meta["version"],
        description=meta["description"],
        source_taxonomy=meta["source_taxonomy"],
        output_columns=output_columns,
        system_prompt=system_prompt,
        categories=categories,
        guidelines=guidelines,
        event_types=event_types,
        tool_schema=tool_schema,
    )


def _split_frontmatter(raw: str) -> tuple[str, str]:
    """Split YAML frontmatter from Markdown body."""
    if not raw.startswith("---"):
        raise DefinitionLoadError("Definition file must start with --- frontmatter")
    end = raw.find("\n---", 3)
    if end == -1:
        raise DefinitionLoadError("Unterminated frontmatter (no closing ---)")
    fm = raw[3:end].strip()
    body = raw[end + 4:]
    return fm, body


def _parse_frontmatter(fm_text: str, expected_name: str) -> dict:
    """Parse and validate YAML frontmatter."""
    if _YAML_ANCHOR_RE.search(fm_text):
        raise DefinitionLoadError("YAML anchors/aliases not allowed in frontmatter")

    try:
        meta = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise DefinitionLoadError(f"Invalid YAML frontmatter: {exc}") from exc

    if not isinstance(meta, dict):
        raise DefinitionLoadError("Frontmatter must be a YAML mapping")

    for field_name in ("name", "version", "description", "output_columns"):
        if field_name not in meta:
            raise DefinitionLoadError(f"Missing required frontmatter field: {field_name}")

    if not isinstance(meta["version"], int) or meta["version"] < 1:
        raise DefinitionLoadError(
            f"version must be a positive integer, got {meta['version']!r}"
        )
    if not isinstance(meta["output_columns"], list) or not meta["output_columns"]:
        raise DefinitionLoadError("output_columns must be a non-empty list")

    return {
        "name": meta["name"],
        "version": meta["version"],
        "description": meta["description"],
        "source_taxonomy": meta.get("source_taxonomy"),
        "output_columns": meta["output_columns"],
    }


def _split_sections(body: str) -> dict[str, str]:
    """Split Markdown body into sections by # heading."""
    sections: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    for line in body.split("\n"):
        if line.startswith("# ") and not line.startswith("## "):
            if current_name is not None:
                sections[current_name] = "\n".join(current_lines)
            heading = line[2:].strip()
            if heading not in _KNOWN_SECTIONS:
                raise DefinitionLoadError(
                    f"Unrecognized top-level section: '# {heading}'. "
                    f"Allowed: {sorted(_KNOWN_SECTIONS)}"
                )
            if heading in sections:
                raise DefinitionLoadError(f"Duplicate section: '# {heading}'")
            current_name = heading
            current_lines = []
        else:
            current_lines.append(line)

    if current_name is not None:
        sections[current_name] = "\n".join(current_lines)

    return sections


def _parse_categories(section: str) -> list[CategoryDef]:
    """Parse ## group / ### category_id structure from Categories section."""
    categories: list[CategoryDef] = []
    current_group: str | None = None
    current_cat_id: str | None = None
    current_cat_lines: list[str] = []

    def _flush_category():
        nonlocal current_cat_id, current_cat_lines
        if current_cat_id and current_group:
            body = "\n".join(current_cat_lines).strip()
            categories.append(CategoryDef(
                id=current_cat_id, group=current_group, body=body,
            ))
        current_cat_id = None
        current_cat_lines = []

    for line in section.split("\n"):
        if line.startswith("### "):
            _flush_category()
            cat_id = line[4:].strip()
            if not _ID_RE.match(cat_id):
                raise DefinitionLoadError(
                    f"Invalid category ID {cat_id!r}: must match {_ID_RE.pattern}"
                )
            if current_group is None:
                raise DefinitionLoadError(
                    f"Category '{cat_id}' appears before any ## group heading"
                )
            current_cat_id = cat_id
            current_cat_lines = []
        elif line.startswith("## "):
            _flush_category()
            group = line[3:].strip()
            if not _ID_RE.match(group):
                raise DefinitionLoadError(
                    f"Invalid group ID {group!r}: must match {_ID_RE.pattern}"
                )
            current_group = group
        else:
            if current_cat_id is not None:
                current_cat_lines.append(line)

    _flush_category()
    return categories


def _parse_event_types(section: str) -> list[EventTypeDef]:
    """Parse flat list of - event_type_id items."""
    event_types: list[EventTypeDef] = []
    for line in section.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            et_id = line[2:].strip()
            if not _ID_RE.match(et_id):
                raise DefinitionLoadError(
                    f"Invalid event type ID {et_id!r}: must match {_ID_RE.pattern}"
                )
            event_types.append(EventTypeDef(id=et_id))
    return event_types


def _assemble_system_prompt(
    system_instructions: str,
    categories: list[CategoryDef],
    guidelines: str,
    event_types: list[EventTypeDef],
) -> str:
    """Assemble the full system prompt from all definition sections."""
    parts = [system_instructions]

    cat_lines: list[str] = []
    current_group = None
    for cat in categories:
        if cat.group != current_group:
            if current_group is not None:
                cat_lines.append("")
            cat_lines.append(f"## {cat.group}")
            current_group = cat.group
        cat_lines.append(f"### {cat.id}")
        if cat.body:
            cat_lines.append(cat.body)

    if cat_lines:
        parts.append("\n".join(cat_lines))

    if guidelines:
        parts.append(guidelines)

    if event_types:
        et_lines = ["Event types (set event_type if the document is for a specific event):"]
        for et in event_types:
            et_lines.append(f"- {et.id}")
        parts.append("\n".join(et_lines))

    return "\n\n".join(parts)


def _validate_against_taxonomy(
    categories: list[CategoryDef],
    event_types: list[EventTypeDef],
    source_taxonomy: str,
    definition_path: Path,
) -> None:
    """Validate category and event type IDs against the taxonomy YAML."""
    from lavandula.reports.taxonomy import load_taxonomy

    taxonomy_path = Path(__file__).parent.parent / "docs" / source_taxonomy
    if not taxonomy_path.is_file():
        raise DefinitionLoadError(
            f"source_taxonomy file not found: {taxonomy_path}"
        )

    taxonomy = load_taxonomy(taxonomy_path)
    valid_mt_ids = taxonomy.material_type_ids | {"other_collateral", "not_relevant"}
    valid_et_ids = taxonomy.event_type_ids

    for cat in categories:
        if cat.id not in valid_mt_ids:
            raise DefinitionLoadError(
                f"Category '{cat.id}' not found in taxonomy {source_taxonomy}"
            )

    for et in event_types:
        if et.id not in valid_et_ids:
            raise DefinitionLoadError(
                f"Event type '{et.id}' not found in taxonomy {source_taxonomy}"
            )


def _build_tool_schema(
    categories: list[CategoryDef],
    event_types: list[EventTypeDef],
    output_columns: list[str],
) -> dict:
    """Build OpenAI-compatible function-calling schema from definition."""
    properties: dict = {}
    required = ["confidence", "reasoning"]

    if "material_type" in output_columns:
        properties["material_type"] = {
            "type": "string",
            "enum": [c.id for c in categories],
            "description": "Material type from the taxonomy.",
        }
        required.append("material_type")

    if "event_type" in output_columns:
        properties["event_type"] = {
            "type": ["string", "null"],
            "enum": [et.id for et in event_types] + [None],
            "description": "Event type if event-related, else null.",
        }

    properties["confidence"] = {
        "type": "number",
        "minimum": 0,
        "maximum": 1,
    }
    properties["reasoning"] = {
        "type": "string",
        "description": "Short rationale (<=300 chars).",
    }

    return {
        "type": "function",
        "function": {
            "name": "record_classification",
            "description": "Record the classification decision. Call exactly once.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def openai_to_anthropic_tool(openai_schema: dict) -> dict:
    """Convert OpenAI function-calling schema to Anthropic tool format."""
    fn = openai_schema["function"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "input_schema": fn["parameters"],
    }


__all__ = [
    "CategoryDef",
    "ClassifierDefinition",
    "DefinitionLoadError",
    "EventTypeDef",
    "load_definition",
    "openai_to_anthropic_tool",
    "resolve_definition_name",
    "sanitize_document_text",
]
