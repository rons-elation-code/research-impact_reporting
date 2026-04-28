"""Coverage report generator (Phase 6 deliverable).

Reads `lava_corpus.corpus_public` + `fetch_log` aggregates and emits
a Markdown summary for operator review.
"""
from __future__ import annotations

import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine


_SCHEMA = "lava_corpus"


def _table(rows: list[tuple]) -> str:
    if not rows:
        return "_(none)_\n"
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for r in rows:
        lines.append(
            "| " + " | ".join(str(v).ljust(w) for v, w in zip(r, widths)) + " |"
        )
    return "\n".join(lines) + "\n"


def generate(engine: Engine, out: Path) -> Path:
    """Write a coverage_report.md file. Returns its path."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

    with engine.connect() as conn:
        total_public = int(conn.execute(
            text(f"SELECT COUNT(*) FROM {_SCHEMA}.corpus_public")
        ).scalar() or 0)
        by_class = [
            tuple(r) for r in conn.execute(text(
                f"SELECT classification, COUNT(*) FROM {_SCHEMA}.corpus_public "
                "GROUP BY classification ORDER BY 2 DESC"
            ))
        ]
        by_platform = [
            tuple(r) for r in conn.execute(text(
                f"SELECT hosting_platform, COUNT(*) FROM {_SCHEMA}.corpus_public "
                "GROUP BY hosting_platform ORDER BY 2 DESC"
            ))
        ]
        by_year = [
            tuple(r) for r in conn.execute(text(
                f"SELECT report_year, COUNT(*) FROM {_SCHEMA}.corpus_public "
                "GROUP BY report_year ORDER BY 1 DESC"
            ))
        ]
        fetch_outcomes = [
            tuple(r) for r in conn.execute(text(
                f"SELECT fetch_status, COUNT(*) FROM {_SCHEMA}.fetch_log "
                "GROUP BY fetch_status ORDER BY 2 DESC"
            ))
        ]
        crawled = int(conn.execute(
            text(f"SELECT COUNT(*) FROM {_SCHEMA}.crawled_orgs")
        ).scalar() or 0)

    body = f"""# Spec 0004 — Coverage Report

Generated: `{now}`

## Totals

- Orgs processed: **{crawled}**
- Reports in `corpus_public` (attribution + confidence + active-content clean): **{total_public}**

## By classification

{_table(by_class)}

## By hosting platform

{_table(by_platform)}

## By year

{_table(by_year)}

## Fetch outcomes

{_table(fetch_outcomes)}

---
_`corpus_public` excludes `platform_unverified`, low-confidence,
and active-content rows per AC12.3 / AC16.2 / AC23.1._
"""
    out.write_text(body)
    return out


__all__ = ["generate"]
