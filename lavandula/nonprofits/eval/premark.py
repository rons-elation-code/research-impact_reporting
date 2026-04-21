from __future__ import annotations

import argparse
import csv
from pathlib import Path
from urllib.parse import urlsplit


OBVIOUS_REJECT_HOSTS = frozenset({
    "greatnonprofits.org",
    "theorg.com",
    "govtribe.com",
    "wellness.com",
    "givefreely.com",
    "whereorg.com",
    "influencewatch.org",
    "foundationcenter.org",
    "fconline.foundationcenter.org",
    "intellispect.co",
    "gudsy.org",
    "nursa.com",
    "app.milliegiving.com",
    "milliegiving.com",
    "npidb.org",
    "instrumentl.com",
    "giboo.com",
    "charityfootprints.com",
    "yellowpages.com",
    "houstonchronicle.com",
    "unfcufoundation.org",
    "daffy.org",
    "greatschools.org",
    "opennpi.com",
    "planmygift.org",
    "mydso.planmygift.org",
    "wellmedhealthcare.com",
})


def classify_current_url(url: str | None) -> tuple[str | None, str | None]:
    if not url:
        return None, None
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return None, None
    if host in OBVIOUS_REJECT_HOSTS or any(host.endswith("." + bad) for bad in OBVIOUS_REJECT_HOSTS):
        return "reject", f"premark: obvious non-official host {host}"
    return None, None


def premark_csv(input_csv: Path | str, output_csv: Path | str) -> int:
    input_path = Path(input_csv)
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    changed = 0
    with input_path.open(newline="", encoding="utf-8") as in_handle:
        reader = csv.DictReader(in_handle)
        fieldnames = list(reader.fieldnames or [])
        with output_path.open("w", newline="", encoding="utf-8") as out_handle:
            writer = csv.DictWriter(out_handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in reader:
                if not row:
                    continue
                current_outcome = (row.get("gold_outcome") or "").strip().lower()
                current_notes = (row.get("gold_notes") or "").strip()
                is_unlabeled_default = current_outcome in {"", "ambiguous"} and not current_notes
                if is_unlabeled_default:
                    outcome, note = classify_current_url(row.get("website_url_current"))
                    if outcome is not None:
                        row["gold_outcome"] = outcome
                        row["gold_notes"] = note or ""
                        if not (row.get("ambiguity_class") or "").strip() or row.get("ambiguity_class") == "needs_review":
                            row["ambiguity_class"] = "obvious_reject"
                        changed += 1
                writer.writerow(row)
    return changed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resolver-premark",
        description="Pre-mark obvious junk current URLs as likely rejects in the resolver eval CSV.",
    )
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    changed = premark_csv(args.input_csv, args.output_csv)
    print(f"wrote {args.output_csv} (premarked {changed} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
