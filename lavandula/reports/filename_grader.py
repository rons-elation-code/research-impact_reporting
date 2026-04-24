"""Filename heuristic grading for crawler candidate triage.

Scores a URL basename on [0.0, 1.0] using taxonomy keyword signals.
Three-tier triage: accept (>= threshold), reject (<= threshold), middle.
"""
from __future__ import annotations

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
            score += weight
    if _YEAR_RE.search(b):
        score += tax.signal_weights.year_bonus
    if _FY_RE.search(b):
        score += tax.signal_weights.year_bonus
    return max(0.0, min(1.0, score))
