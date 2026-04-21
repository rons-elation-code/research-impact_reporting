from __future__ import annotations

import csv

from lavandula.nonprofits.eval.premark import classify_current_url, premark_csv


def test_classify_current_url_marks_obvious_reject():
    outcome, note = classify_current_url("https://www.greatschools.org/texas/houston")
    assert outcome == "reject"
    assert "greatschools.org" in (note or "")


def test_classify_current_url_ignores_plausible_host():
    outcome, note = classify_current_url("https://www.trellisfoundation.org")
    assert outcome is None
    assert note is None


def test_premark_csv_only_fills_blank_gold_outcomes(tmp_path):
    src = tmp_path / "input.csv"
    dst = tmp_path / "output.csv"
    src.write_text(
        "ein,name,website_url_current,gold_outcome,gold_notes,ambiguity_class\n"
        "1,Org One,https://www.greatschools.org,,,\n"
        "2,Org Two,https://www.trellisfoundation.org,,,\n"
        "3,Org Three,https://www.daffy.org,accept,keep existing,\n",
        encoding="utf-8",
    )
    changed = premark_csv(src, dst)
    assert changed == 1

    with dst.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    assert rows[0]["gold_outcome"] == "reject"
    assert "greatschools.org" in rows[0]["gold_notes"]
    assert rows[1]["gold_outcome"] == ""
    assert rows[2]["gold_outcome"] == "accept"
    assert rows[2]["gold_notes"] == "keep existing"


def test_premark_csv_overwrites_default_ambiguous_without_notes(tmp_path):
    src = tmp_path / "input.csv"
    dst = tmp_path / "output.csv"
    src.write_text(
        "ein,name,website_url_current,gold_outcome,gold_notes,ambiguity_class\n"
        "1,Org One,https://www.daffy.org,ambiguous,,needs_review\n",
        encoding="utf-8",
    )
    changed = premark_csv(src, dst)
    assert changed == 1

    with dst.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["gold_outcome"] == "reject"
    assert "daffy.org" in row["gold_notes"]
    assert row["ambiguity_class"] == "obvious_reject"
