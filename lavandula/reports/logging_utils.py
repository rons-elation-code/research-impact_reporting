"""Log-injection-safe sanitation helpers for spec 0004.

Thin re-export of 0001's helpers so every consumer in `lavandula/reports/`
can `from .logging_utils import sanitize, sanitize_exception, setup_logging`
without reaching across packages. Hoisting these to a shared `common/`
package is deferred to a follow-up TICK once 0004 stabilizes.
"""
from __future__ import annotations

from lavandula.nonprofits.logging_utils import (
    DEFAULT_MAX_LEN,
    sanitize,
    sanitize_exception,
    setup_logging,
)

__all__ = ["DEFAULT_MAX_LEN", "sanitize", "sanitize_exception", "setup_logging"]
