from __future__ import annotations

import json

from lavandula.nonprofits.eval import schema
from lavandula.nonprofits.eval.runner import evaluate_row, summarize


def test_write_template_has_expected_columns(tmp_path):
    out = tmp_path / "resolver_eval_dataset.csv"
    schema.write_template(out)
    text = out.read_text(encoding="utf-8").strip()
    assert text.split(",") == list(schema.DATASET_COLUMNS)


def test_load_dataset_rejects_bad_gold_outcome(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text(
        "ein,name,city,state,candidate_results_json,gold_official_url,gold_outcome\n"
        "123456789,Org,Boston,MA,[],https://example.org,maybe\n",
        encoding="utf-8",
    )
    try:
        schema.load_dataset(path)
    except ValueError as exc:
        assert "invalid gold_outcome" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_heuristic_strategy_accepts_best_candidate():
    row = schema.EvalRow(
        raw={
            "ein": "123456789",
            "name": "Brookwood Community",
            "address": "",
            "city": "Brookshire",
            "state": "TX",
            "zipcode": "",
            "ntee_code": "",
            "revenue": "",
            "subsection_code": "",
            "activity_codes": "",
            "classification_codes": "",
            "foundation_code": "",
            "ruling_date": "",
            "accounting_period": "",
            "website_url_current": "",
            "resolver_status_current": "",
            "resolver_confidence_current": "",
            "resolver_method_current": "",
                "candidate_results_json": json.dumps(
                    [
                        {
                            "url": "https://www.yellowpages.com/brookshire-tx/community-services",
                            "title": "Yellow Pages listing",
                            "description": "Directory listing for community services",
                        },
                        {
                            "url": "https://brookwoodcommunity.org",
                            "title": "Brookwood Community | Brookshire, TX",
                            "description": "Official website for Brookwood Community in Brookshire, Texas",
                        },
                    ]
                ),
            "gold_official_url": "https://brookwoodcommunity.org",
            "gold_outcome": "accept",
            "gold_notes": "",
            "ambiguity_class": "easy",
        }
    )
    decision = evaluate_row(row, strategy="heuristic")
    assert decision.predicted_outcome == "accept"
    assert decision.predicted_url == "https://brookwoodcommunity.org"
    assert decision.accepted_correct is True


def test_summary_reports_accept_precision():
    row_good = schema.EvalRow(
        raw={
            "ein": "1",
            "name": "One",
            "address": "",
            "city": "Austin",
            "state": "TX",
            "zipcode": "",
            "ntee_code": "",
            "revenue": "",
            "subsection_code": "",
            "activity_codes": "",
            "classification_codes": "",
            "foundation_code": "",
            "ruling_date": "",
            "accounting_period": "",
            "website_url_current": "https://one.org",
            "resolver_status_current": "accept",
            "resolver_confidence_current": "0.9",
            "resolver_method_current": "seed",
            "candidate_results_json": "[]",
            "gold_official_url": "https://one.org",
            "gold_outcome": "accept",
            "gold_notes": "",
            "ambiguity_class": "easy",
        }
    )
    row_bad = schema.EvalRow(
        raw={
            "ein": "2",
            "name": "Two",
            "address": "",
            "city": "Dallas",
            "state": "TX",
            "zipcode": "",
            "ntee_code": "",
            "revenue": "",
            "subsection_code": "",
            "activity_codes": "",
            "classification_codes": "",
            "foundation_code": "",
            "ruling_date": "",
            "accounting_period": "",
            "website_url_current": "https://wrong.org",
            "resolver_status_current": "accept",
            "resolver_confidence_current": "0.9",
            "resolver_method_current": "seed",
            "candidate_results_json": "[]",
            "gold_official_url": "https://right.org",
            "gold_outcome": "accept",
            "gold_notes": "",
            "ambiguity_class": "hard",
        }
    )
    decisions = [
        evaluate_row(row_good, strategy="current"),
        evaluate_row(row_bad, strategy="current"),
    ]
    summary = summarize(decisions)
    assert summary["accepted"] == 2
    assert summary["accepted_correct"] == 1
    assert summary["accepted_incorrect"] == 1
    assert summary["accept_precision"] == 0.5
