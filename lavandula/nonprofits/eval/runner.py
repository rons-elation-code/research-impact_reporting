from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

from lavandula.nonprofits.eval.schema import EvalRow, load_dataset, write_template
from lavandula.nonprofits.tools.resolve_websites import pick_best


@dataclass
class EvalDecision:
    strategy: str
    ein: str
    predicted_url: str | None
    predicted_outcome: str
    confidence: float | None
    reason: str
    gold_url: str | None
    gold_outcome: str
    accepted_correct: bool
    accepted_incorrect: bool
    outcome_match: bool


def _decide_current(row: EvalRow) -> tuple[str | None, float | None, str, str]:
    """Replay what the production resolver already decided — no HTTP re-verification.

    Returns the stored `website_url_current` as an `accept` decision only when
    `resolver_status_current` is a resolved/accepted state (or absent, which
    means the old resolver wrote the URL without a status field). Rows where
    the resolver stored a URL but also recorded `ambiguous` or `rejected` status
    are forwarded with their original status so the eval comparison is not
    inflated by double-counting ambiguous results as accepted.

    NOTE: This behaviour was tightened in Spec 0005 (Round 2). Earlier eval
    runs treated any non-null URL as `accept`, inflating the baseline precision.
    """
    url = row.raw.get("website_url_current") or None
    confidence = row.raw.get("resolver_confidence_current") or None
    parsed_conf = float(confidence) if confidence not in (None, "") else None
    status_current = (row.raw.get("resolver_status_current") or "").strip().lower()

    if url and status_current not in {"ambiguous", "rejected", "reject", "error"}:
        return url, parsed_conf, "accept", "current_seed_resolution"

    # URL is absent, or the stored status indicates a non-accepted result.
    if status_current not in {"accept", "ambiguous", "reject"}:
        status_current = "reject"
    return None, parsed_conf, status_current, "current_seed_resolution"


def _decide_heuristic(row: EvalRow) -> tuple[str | None, float | None, str, str]:
    chosen, confidence, status, reason = pick_best(
        row.candidate_results,
        name=row.name,
        city=row.city,
    )
    outcome = "accept" if status == "accepted" else status
    if outcome not in {"accept", "ambiguous", "reject"}:
        outcome = "reject"
    return chosen, confidence, outcome, reason


def _decide_unimplemented(row: EvalRow, *, label: str) -> tuple[str | None, float | None, str, str]:
    raise NotImplementedError(f"strategy {label!r} is not yet implemented")


def evaluate_row(
    row: EvalRow,
    *,
    strategy: str,
) -> EvalDecision:
    if strategy == "current":
        predicted_url, confidence, predicted_outcome, reason = _decide_current(row)
    elif strategy == "heuristic":
        predicted_url, confidence, predicted_outcome, reason = _decide_heuristic(row)
    elif strategy == "packet-cheap":
        predicted_url, confidence, predicted_outcome, reason = _decide_unimplemented(
            row, label="packet-cheap"
        )
    elif strategy == "two-cheap-consensus":
        predicted_url, confidence, predicted_outcome, reason = _decide_unimplemented(
            row, label="two-cheap-consensus"
        )
    elif strategy == "frontier-arbitrated":
        predicted_url, confidence, predicted_outcome, reason = _decide_unimplemented(
            row, label="frontier-arbitrated"
        )
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    gold_url = row.gold_official_url
    accepted_correct = (
        predicted_outcome == "accept"
        and row.gold_outcome == "accept"
        and predicted_url == gold_url
    )
    accepted_incorrect = predicted_outcome == "accept" and not accepted_correct
    outcome_match = predicted_outcome == row.gold_outcome
    return EvalDecision(
        strategy=strategy,
        ein=row.ein,
        predicted_url=predicted_url,
        predicted_outcome=predicted_outcome,
        confidence=confidence,
        reason=reason,
        gold_url=gold_url,
        gold_outcome=row.gold_outcome,
        accepted_correct=accepted_correct,
        accepted_incorrect=accepted_incorrect,
        outcome_match=outcome_match,
    )


def summarize(decisions: list[EvalDecision]) -> dict[str, object]:
    accepted = [d for d in decisions if d.predicted_outcome == "accept"]
    accepted_correct = sum(1 for d in accepted if d.accepted_correct)
    accepted_incorrect = sum(1 for d in accepted if d.accepted_incorrect)
    precision = (
        accepted_correct / len(accepted)
        if accepted
        else None
    )
    return {
        "rows": len(decisions),
        "accepted": len(accepted),
        "ambiguous": sum(1 for d in decisions if d.predicted_outcome == "ambiguous"),
        "rejected": sum(1 for d in decisions if d.predicted_outcome == "reject"),
        "accepted_correct": accepted_correct,
        "accepted_incorrect": accepted_incorrect,
        "accept_precision": precision,
        "outcome_matches": sum(1 for d in decisions if d.outcome_match),
        "reason_counts": dict(Counter(d.reason for d in decisions)),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resolver-eval",
        description="Evaluate website-resolution strategies against a labeled gold set.",
    )
    parser.add_argument("--input-csv", type=Path, help="Labeled evaluation dataset CSV.")
    parser.add_argument("--output-jsonl", type=Path, help="Write per-row decisions here.")
    parser.add_argument(
        "--strategy",
        choices=("current", "heuristic", "packet-cheap", "two-cheap-consensus", "frontier-arbitrated"),
        default="heuristic",
    )
    parser.add_argument(
        "--write-template",
        type=Path,
        help="Write a blank dataset template CSV and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.write_template:
        write_template(args.write_template)
        print(f"wrote template: {args.write_template}")
        return 0

    if not args.input_csv or not args.output_jsonl:
        parser.error("--input-csv and --output-jsonl are required unless --write-template is used")

    rows = load_dataset(args.input_csv)
    decisions = [
        evaluate_row(row, strategy=args.strategy)
        for row in rows
    ]

    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with args.output_jsonl.open("w", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(json.dumps(asdict(decision), sort_keys=True) + "\n")

    print(json.dumps(summarize(decisions), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

