"""Taxonomy loader: YAML → validated Pydantic models → runtime-optimized view.

The taxonomy YAML is the single source of truth for crawler keyword lists,
signal weights, and tier assignments. Uses yaml.safe_load — never yaml.load.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_REGEX_META = set(".*+?[](|)\\^$")


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


def load_taxonomy(path: Path) -> Taxonomy:
    """Load, validate, and derive runtime view. Uses yaml.safe_load."""
    with path.open() as f:
        data = yaml.safe_load(f)
    if data is None:
        raise ValueError("taxonomy YAML is empty")
    raw = TaxonomyRaw.model_validate(data)
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
