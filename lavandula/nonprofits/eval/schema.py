from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path


DATASET_COLUMNS = (
    "ein",
    "name",
    "address",
    "city",
    "state",
    "zipcode",
    "ntee_code",
    "revenue",
    "subsection_code",
    "activity_codes",
    "classification_codes",
    "foundation_code",
    "ruling_date",
    "accounting_period",
    "website_url_current",
    "resolver_status_current",
    "resolver_confidence_current",
    "resolver_method_current",
    "candidate_results_json",
    "gold_official_url",
    "gold_outcome",
    "gold_notes",
    "ambiguity_class",
)

REQUIRED_COLUMNS = {
    "ein",
    "name",
    "city",
    "state",
    "candidate_results_json",
    "gold_official_url",
    "gold_outcome",
}

VALID_GOLD_OUTCOMES = {"accept", "ambiguous", "reject"}


@dataclass
class EvalRow:
    raw: dict[str, str]

    @property
    def ein(self) -> str:
        return self.raw["ein"]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def city(self) -> str | None:
        return self.raw.get("city") or None

    @property
    def state(self) -> str | None:
        return self.raw.get("state") or None

    @property
    def candidate_results(self) -> list[dict]:
        blob = (self.raw.get("candidate_results_json") or "").strip()
        if not blob:
            return []
        data = json.loads(blob)
        if isinstance(data, dict):
            web = data.get("web") or {}
            data = web.get("results") or []
        if not isinstance(data, list):
            raise ValueError(
                "candidate_results_json must decode to a JSON list or Brave response object"
            )
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("candidate_results_json entries must be JSON objects")
        return data

    @property
    def gold_official_url(self) -> str | None:
        return self.raw.get("gold_official_url") or None

    @property
    def gold_outcome(self) -> str:
        return self.raw["gold_outcome"]


def load_dataset(path: Path | str) -> list[EvalRow]:
    rows: list[EvalRow] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = REQUIRED_COLUMNS - set(fieldnames)
        if missing:
            raise ValueError(f"dataset missing required columns: {sorted(missing)}")
        for row in reader:
            assert row is not None
            if not any((value or "").strip() for value in row.values()):
                continue
            gold_outcome = (row.get("gold_outcome") or "").strip().lower()
            if gold_outcome not in VALID_GOLD_OUTCOMES:
                raise ValueError(
                    f"row ein={row.get('ein')!r} has invalid gold_outcome={gold_outcome!r}"
                )
            row["gold_outcome"] = gold_outcome
            rows.append(EvalRow(raw=row))
    return rows


def write_template(path: Path | str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(DATASET_COLUMNS))
        writer.writeheader()
