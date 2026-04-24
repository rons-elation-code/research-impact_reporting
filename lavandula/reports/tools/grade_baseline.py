#!/usr/bin/env python3
"""Offline AC12 validation: grade session_filenames_graded.csv against taxonomy.

Reads the human-graded CSV, applies grade_filename() from the current taxonomy,
compares computed triage (accept/middle/reject) with the human label, and prints
precision/recall/accuracy metrics.

Usage:
    python -m lavandula.reports.tools.grade_baseline [--csv PATH] [--yaml PATH]
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from lavandula.reports.taxonomy import load_taxonomy
from lavandula.reports.filename_grader import grade_filename, normalize


def main() -> None:
    parser = argparse.ArgumentParser(description="AC12 baseline grader")
    parser.add_argument(
        "--csv",
        type=Path,
        default=ROOT / "lavandula" / "review_uploads" / "session_filenames_graded.csv",
    )
    parser.add_argument(
        "--yaml",
        type=Path,
        default=ROOT / "lavandula" / "docs" / "collateral_taxonomy.yaml",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--mismatches", action="store_true", help="Show only mismatches")
    args = parser.parse_args()

    tax = load_taxonomy(args.yaml)

    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    tp = fp = tn = fn = 0
    mismatches: list[dict] = []
    details: list[dict] = []

    for row in rows:
        human_triage = row["triage"].strip().lower()
        filename = row["filename"].strip()
        score = grade_filename(filename, tax)

        if score >= tax.thresholds.filename_score_accept:
            computed = "accept"
        elif score <= tax.thresholds.filename_score_reject:
            computed = "reject"
        else:
            computed = "middle"

        human_positive = human_triage == "accept"
        computed_positive = computed == "accept"

        if human_positive and computed_positive:
            tp += 1
        elif not human_positive and computed_positive:
            fp += 1
        elif not human_positive and not computed_positive:
            tn += 1
        else:
            fn += 1

        match = human_triage == computed
        rec = {
            "filename": filename,
            "human": human_triage,
            "computed": computed,
            "score": round(score, 3),
            "match": match,
            "org": row.get("org", ""),
        }
        details.append(rec)
        if not match:
            mismatches.append(rec)

    total = len(rows)
    exact_match = sum(1 for d in details if d["match"])
    accuracy = exact_match / total if total else 0

    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    print(f"=== AC12 Baseline Grading ===")
    print(f"Total rows:      {total}")
    print(f"Exact match:     {exact_match} / {total}  ({accuracy:.1%})")
    print(f"Mismatches:      {len(mismatches)}")
    print()
    print(f"--- Accept-class metrics (human=accept vs computed=accept) ---")
    print(f"True positives:  {tp}")
    print(f"False positives: {fp}")
    print(f"True negatives:  {tn}")
    print(f"False negatives: {fn}")
    print(f"Precision:       {precision:.3f}")
    print(f"Recall:          {recall:.3f}")
    print(f"F1:              {f1:.3f}")
    print()

    if args.verbose or args.mismatches:
        print("--- Mismatches ---")
        for m in mismatches:
            print(f"  {m['human']:>8} → {m['computed']:>8}  score={m['score']:.3f}  {m['filename']}")
            if args.verbose:
                print(f"         normalized: {normalize(m['filename'])}")
                print(f"         org: {m['org']}")
        print()

    ac12_pass = accuracy >= 0.90
    print(f"AC12 target (≥90%): {'PASS' if ac12_pass else 'FAIL'}  ({accuracy:.1%})")
    sys.exit(0 if ac12_pass else 1)


if __name__ == "__main__":
    main()
