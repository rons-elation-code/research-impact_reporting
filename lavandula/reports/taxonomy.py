"""Taxonomy loader: YAML → validated Pydantic models → runtime-optimized view.

The taxonomy YAML is the single source of truth for crawler keyword lists,
signal weights, and tier assignments. Uses yaml.safe_load — never yaml.load.

Classifier integration (Spec 0023): also provides the classifier prompt
section builder, legacy mapping, and validator used by the v2 classifier.
"""
from __future__ import annotations

import logging
import re
import warnings
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_log = logging.getLogger(__name__)

_REGEX_META = set(".*+?[](|)\\^$")

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_ALLOWED_GROUPS = frozenset({
    "reports", "campaign", "invitations", "programs_journals", "auction",
    "appeals", "sponsorship", "major_gifts", "planned_giving", "stewardship",
    "periodic", "membership", "day_of_event", "peer_to_peer",
    "program_services", "sector_specific", "other",
})

_MAX_DESCRIPTION_LEN = 200

_MATERIAL_TYPE_TO_LEGACY = {
    "annual_report": "annual",
    "impact_report": "impact",
    "year_in_review": "annual",
    "financial_report": "annual",
    "community_benefit_report": "annual",
    "donor_impact_report": "impact",
    "endowed_fund_report": "impact",
    "not_relevant": "not_a_report",
}


class TaxonomyLoadError(RuntimeError):
    """Raised when collateral_taxonomy.yaml fails validation."""


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True)
    filename_score_accept: float = Field(ge=0.5, le=1.0)
    filename_score_reject: float = Field(ge=0.0, le=0.5)
    filename_score_weak_path_min: float = Field(ge=0.0, le=1.0)
    base_score: Literal[0.5]

    @model_validator(mode="after")
    def _ordering(self):
        if not (
            self.filename_score_accept
            > self.filename_score_weak_path_min
            > self.filename_score_reject
        ):
            raise ValueError(
                "thresholds must satisfy accept > weak_path_min > reject"
            )
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
    version: Literal[1]
    thresholds: Thresholds
    signal_weights: SignalWeights
    material_types: tuple[MaterialType, ...]
    event_types: tuple[EventType, ...] = ()
    path_keywords: PathKeywords
    filename_negative_signals: FilenameNegativeSignals
    tags: tuple[dict, ...] = ()

    @model_validator(mode="after")
    def _global_constraints(self):
        _check_keyword_rules(self)
        _check_no_duplicate_ids(self)
        _check_strong_weak_disjoint(self)
        _check_no_signal_collision(self)
        return self


def _check_keyword_rules(t: TaxonomyRaw) -> None:
    """Min length 3, no regex metachars, lowercase only."""

    def check_list(name: str, kws: tuple[str, ...]) -> None:
        for kw in kws:
            if len(kw) < 3:
                raise ValueError(f"{name}: keyword {kw!r} shorter than 3 chars")
            if any(c in _REGEX_META for c in kw):
                raise ValueError(
                    f"{name}: keyword {kw!r} contains regex metacharacters"
                )
            if kw != kw.lower():
                raise ValueError(
                    f"{name}: keyword {kw!r} contains uppercase — all keywords "
                    f"must be lowercase (matching is case-insensitive)"
                )

    for mt in t.material_types:
        check_list(
            f"material_types[{mt.id}].filename_signals.strong_positive",
            mt.filename_signals.strong_positive,
        )
        check_list(
            f"material_types[{mt.id}].filename_signals.medium_positive",
            mt.filename_signals.medium_positive,
        )
    for et in t.event_types:
        check_list(f"event_types[{et.id}].path_keywords", et.path_keywords)
    check_list("path_keywords.strong", t.path_keywords.strong)
    check_list("path_keywords.weak", t.path_keywords.weak)
    check_list(
        "filename_negative_signals.strong", t.filename_negative_signals.strong
    )
    check_list(
        "filename_negative_signals.medium", t.filename_negative_signals.medium
    )


def _check_no_duplicate_ids(t: TaxonomyRaw) -> None:
    seen: set[str] = set()
    for mt in t.material_types:
        if mt.id in seen:
            raise ValueError(f"duplicate id: {mt.id}")
        seen.add(mt.id)
    for et in t.event_types:
        if et.id in seen:
            raise ValueError(
                f"duplicate id across material_types and event_types: {et.id}"
            )
        seen.add(et.id)


def _check_strong_weak_disjoint(t: TaxonomyRaw) -> None:
    common = set(t.path_keywords.strong) & set(t.path_keywords.weak)
    if common:
        raise ValueError(
            f"path_keywords strong and weak overlap: {sorted(common)}"
        )


def _check_no_signal_collision(t: TaxonomyRaw) -> None:
    """No keyword appears in both positive and negative lists."""
    positives: set[str] = set()
    for mt in t.material_types:
        positives |= set(mt.filename_signals.strong_positive)
        positives |= set(mt.filename_signals.medium_positive)
    negatives = set(t.filename_negative_signals.strong) | set(
        t.filename_negative_signals.medium
    )
    collision = positives & negatives
    if collision:
        raise ValueError(
            f"keywords appear in both positive and negative lists: {sorted(collision)}"
        )


class Taxonomy(BaseModel):
    """Runtime-optimized view derived from TaxonomyRaw."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    raw: TaxonomyRaw
    path_keywords_strong: frozenset[str]
    path_keywords_weak: frozenset[str]
    anchor_keywords: frozenset[str]
    filename_positive: dict[str, float]
    filename_negative: dict[str, float]

    @property
    def thresholds(self) -> Thresholds:
        return self.raw.thresholds

    @property
    def signal_weights(self) -> SignalWeights:
        return self.raw.signal_weights

    @property
    def material_type_ids(self) -> frozenset[str]:
        return frozenset(mt.id for mt in self.raw.material_types)

    @property
    def event_type_ids(self) -> frozenset[str]:
        return frozenset(et.id for et in self.raw.event_types)

    @property
    def groups(self) -> frozenset[str]:
        return frozenset(mt.group for mt in self.raw.material_types)

    @property
    def material_types_by_id(self) -> dict[str, MaterialType]:
        return {mt.id: mt for mt in self.raw.material_types}

    def is_valid_material_type(self, mt: str) -> bool:
        return mt in self.material_type_ids

    def is_valid_event_type(self, et: str | None) -> bool:
        if et is None:
            return True
        return et in self.event_type_ids

    def derive_group(self, material_type_id: str) -> str:
        by_id = self.material_types_by_id
        if material_type_id not in by_id:
            raise KeyError(f"unknown material_type: {material_type_id!r}")
        return by_id[material_type_id].group

    def material_type_to_legacy(self, material_type_id: str) -> str:
        return _MATERIAL_TYPE_TO_LEGACY.get(material_type_id, "other")


def _validate_classifier_constraints(raw: TaxonomyRaw) -> None:
    """Extra validation for classifier use (AC8, AC9)."""
    for mt in raw.material_types:
        if not _ID_RE.match(mt.id):
            raise TaxonomyLoadError(
                f"material_type id {mt.id!r} does not match {_ID_RE.pattern}"
            )
        if mt.group not in _ALLOWED_GROUPS:
            raise TaxonomyLoadError(
                f"material_type {mt.id!r} has unknown group {mt.group!r}; "
                f"allowed: {sorted(_ALLOWED_GROUPS)}"
            )
        desc = mt.description or ""
        if "<untrusted_document>" in desc or "</untrusted_document>" in desc:
            raise TaxonomyLoadError(
                f"material_type {mt.id!r} description contains "
                f"<untrusted_document> tag — forbidden for prompt safety"
            )
    for et in raw.event_types:
        if not _ID_RE.match(et.id):
            raise TaxonomyLoadError(
                f"event_type id {et.id!r} does not match {_ID_RE.pattern}"
            )
    if len(raw.material_types) > 100:
        warnings.warn(
            f"taxonomy has {len(raw.material_types)} material_types "
            f"(>100) — classifier prompt may be too large",
            stacklevel=3,
        )


def load_taxonomy(path: Path) -> Taxonomy:
    """Load, validate, and derive runtime view. Raises TaxonomyLoadError."""
    try:
        with path.open() as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise TaxonomyLoadError(
            f"taxonomy YAML not found: {path}"
        ) from None
    if data is None:
        raise TaxonomyLoadError("taxonomy YAML is empty")
    try:
        raw = TaxonomyRaw.model_validate(data)
    except Exception as exc:
        raise TaxonomyLoadError(f"taxonomy validation failed: {exc}") from exc
    _validate_classifier_constraints(raw)
    return _build_runtime_view(raw)


def _build_runtime_view(raw: TaxonomyRaw) -> Taxonomy:
    strong = {kw.lower() for kw in raw.path_keywords.strong}
    for et in raw.event_types:
        strong |= {kw.lower() for kw in et.path_keywords}

    weak = frozenset(kw.lower() for kw in raw.path_keywords.weak)

    anchors: set[str] = set()
    for mt in raw.material_types:
        anchors |= {s.lower() for s in mt.anchor_signals}
    for et in raw.event_types:
        anchors |= {s.lower() for s in et.anchor_signals}

    filename_positive: dict[str, float] = {}
    for mt in raw.material_types:
        for kw in mt.filename_signals.strong_positive:
            filename_positive[kw.lower()] = raw.signal_weights.strong_positive
        for kw in mt.filename_signals.medium_positive:
            filename_positive.setdefault(
                kw.lower(), raw.signal_weights.medium_positive
            )

    filename_negative: dict[str, float] = {}
    for kw in raw.filename_negative_signals.strong:
        filename_negative[kw.lower()] = raw.signal_weights.strong_negative
    for kw in raw.filename_negative_signals.medium:
        filename_negative.setdefault(
            kw.lower(), raw.signal_weights.medium_negative
        )

    return Taxonomy(
        raw=raw,
        path_keywords_strong=frozenset(strong),
        path_keywords_weak=weak,
        anchor_keywords=frozenset(anchors),
        filename_positive=filename_positive,
        filename_negative=filename_negative,
    )


_current: Taxonomy | None = None


def current() -> Taxonomy:
    if _current is None:
        raise RuntimeError("taxonomy not loaded — call bind() from config.py first")
    return _current


def bind(t: Taxonomy) -> None:
    global _current
    _current = t


def _default_taxonomy_path() -> Path:
    return Path(__file__).parent.parent / "docs" / "collateral_taxonomy.yaml"


def get_taxonomy() -> Taxonomy:
    """Return cached taxonomy, loading on first call."""
    if _current is None:
        bind(load_taxonomy(_default_taxonomy_path()))
    return _current


def ensure_loaded() -> None:
    """Eagerly load taxonomy; raises TaxonomyLoadError on failure."""
    get_taxonomy()


def build_taxonomy_prompt_section(taxonomy: Taxonomy) -> str:
    """Build the material-type reference for the classifier prompt.

    Deterministic ordering: sorted by (group, id). Descriptions truncated
    to _MAX_DESCRIPTION_LEN chars.
    """
    sorted_mts = sorted(
        taxonomy.raw.material_types,
        key=lambda mt: (mt.group, mt.id),
    )
    lines: list[str] = []
    for mt in sorted_mts:
        desc = (mt.description or "").strip()[:_MAX_DESCRIPTION_LEN]
        lines.append(f"- {mt.id} (group: {mt.group}): {desc}")
    lines.append("")
    lines.append(
        "Event types (set event_type if the document is for a specific event):"
    )
    for et in sorted(taxonomy.raw.event_types, key=lambda e: e.id):
        lines.append(f"- {et.id}")
    section = "\n".join(lines)
    if len(section) > 5000:
        warnings.warn(
            f"taxonomy prompt section is {len(section)} chars "
            f"(>5000) — may be too large to inline",
            stacklevel=2,
        )
    return section


def material_type_to_legacy(material_type_id: str) -> str:
    """Map expanded material_type to legacy 5-value classification."""
    return _MATERIAL_TYPE_TO_LEGACY.get(material_type_id, "other")
