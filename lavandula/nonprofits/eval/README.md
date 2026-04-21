# Resolver Evaluation

This directory contains the offline harness for evaluating nonprofit
website-resolution strategies before they are trusted in the crawl.

## Files

- `schema.py`
  Validates labeled CSV datasets and writes a blank template.
- `runner.py`
  Executes one resolver strategy against a labeled dataset and emits
  JSONL per-row decisions plus a compact summary.

## Dataset shape

The CSV template includes:

- seed identity: `ein`, `name`, `address`, `city`, `state`, `zipcode`
- seed metadata: `ntee_code`, `revenue`, `subsection_code`,
  `activity_codes`, `classification_codes`, `foundation_code`,
  `ruling_date`, `accounting_period`
- current resolver outputs: `website_url_current`,
  `resolver_status_current`, `resolver_confidence_current`,
  `resolver_method_current`
- candidate evidence: `candidate_results_json`
- gold labels: `gold_official_url`, `gold_outcome`, `gold_notes`,
  `ambiguity_class`

`candidate_results_json` must be a JSON list of search-result objects,
typically preserving at least `url`, `title`, and `description`.

`gold_outcome` must be one of:
- `accept`
- `ambiguous`
- `reject`

## Strategies

The scaffold currently supports:

- `current`
  Score the currently stored `website_url_current`.
- `heuristic`
  Re-run the local `_pick_best()` resolver scorer against
  `candidate_results_json`.

Placeholders are also defined for:

- `packet-cheap`
- `two-cheap-consensus`
- `frontier-arbitrated`

These intentionally return `ambiguous` until their model-backed logic
is implemented.

## Usage

Write a blank template:

```bash
python -m lavandula.nonprofits.eval.runner \
  --write-template /tmp/resolver_eval_dataset.csv
```

Run the heuristic baseline:

```bash
python -m lavandula.nonprofits.eval.runner \
  --input-csv /tmp/resolver_eval_dataset.csv \
  --output-jsonl /tmp/resolver_eval_results.jsonl \
  --strategy heuristic
```

