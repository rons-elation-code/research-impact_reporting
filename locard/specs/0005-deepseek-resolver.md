# Spec 0005 — DeepSeek-Backed Nonprofit Website Resolver

**Status**: draft  
**Protocol**: SPIDER  
**Priority**: high  
**Date**: 2026-04-21  

---

## Problem

The current website resolver (`resolve_websites.py`) uses Brave Search API
plus local heuristics. It has been observed to mis-attribute seeds to wrong
organizations — the canonical failure case being EIN `741394418`
(Columbus Community Hospital, Columbus TX) resolved to a Nebraska hospital's
domain. Brave search results steer heuristic scoring in the wrong direction
and the system has no reasoning capability to catch mismatches.

When Claude Code agents with websearch were used manually, accuracy was
significantly higher. The differentiator was not the search provider — it
was the reasoning layer that could evaluate whether a candidate website
actually belonged to the queried organization.

---

## Goals

1. Replace heuristic-only resolver with a model-backed resolver using
   DeepSeek-V3 (`deepseek-chat`) via the DeepSeek OpenAI-compatible API.
2. Eliminate dependency on Brave Search for the resolution step.
3. Use the richer seed data now available: street address, zipcode, NTEE
   code, subsection code, plus name, city, state, EIN.
4. Achieve **≥ 80% precision** on the TX 100-org eval dataset, measured
   as: (correctly resolved orgs) / (orgs where `resolver_status=resolved`).
   The current heuristic baseline precision on this dataset is the reference
   point; the DeepSeek resolver must exceed it.
5. Integrate into the existing resolver-eval framework so accuracy can be
   validated before production deployment.

---

## Non-Goals

- This spec does not replace the PDF classifier backend.
- This spec does not add a second model for consensus (that is a future
  `two-cheap-consensus` strategy).
- This spec does not change the seed enumeration or crawl stages.

---

## Design

### Core approach: generate → fetch → confirm

The resolver uses a three-phase pipeline per org:

**Phase 1 — Generate candidates**  
Ask DeepSeek to propose up to 5 likely official website URLs for the org,
given: name, EIN, street address, city, state, zipcode, NTEE code.
The model draws on training knowledge of US nonprofits and reasons about
likely domain patterns. Brave Search is explicitly excluded.

If Phase 1 produces zero HTTP-live candidates (all proposed URLs fail
verification), the resolver marks the org `unresolved` with reason
`no_live_candidates`. A future spec may add a non-Brave search fallback
for this long-tail case; that is out of scope here.

**Phase 2 — HTTP verification**  
For each candidate URL (in ranked order), send an HTTP GET with a
3-second connect timeout and 8-second read timeout. Follow up to 3
redirects; record the final resolved URL (after redirects) as the
candidate. Discard candidates that return non-200 responses or exceed
timeouts. Capture up to 2000 characters of homepage text for live
candidates.

**Phase 3 — Identity confirmation**  
Pass the homepage text of each live candidate back to DeepSeek with the
original org identity (name, address, city, state). Ask the model to
confirm which URL belongs to the organization, or return `null` if no
candidate is a confident match. The model must provide a confidence score
(0.0–1.0) and a short reasoning string.

**`ambiguous` status**: If two or more candidates each receive model
confidence ≥ 0.6 and are within 0.1 of each other, the resolver sets
`resolver_status=ambiguous` and stores both URLs in
`website_candidates_json`. The `website_url` field is set to the
highest-confidence candidate but should not be used for production crawls
without human review.

### Output fields (added to `nonprofits_seed`)

| Column | Type | Meaning |
|--------|------|---------|
| `website_url` | TEXT | Chosen URL or NULL |
| `resolver_status` | TEXT | `resolved` / `unresolved` / `ambiguous` |
| `resolver_confidence` | REAL | 0.0–1.0 from model |
| `resolver_method` | TEXT | `deepseek-v1` |
| `resolver_reason` | TEXT | Model's reasoning string |
| `website_candidates_json` | TEXT | JSON array of all candidates tried |

These columns already exist in the schema. No migration needed.

### API key sourcing

The DeepSeek API key is read from SSM at path:

```
/cloud2.lavandulagroup.com/lavandula/deepseek/api_key
```

The key is fetched once at client construction time using `boto3.ssm`.
It is never written to env vars, logs, or error messages.

### Env var override for testing

If `DEEPSEEK_API_KEY` is set in the environment it is used directly,
bypassing SSM. This allows unit tests to inject a fake key without
needing AWS access.

### Sandbox isolation

The DeepSeek client is instantiated with only the API key — no ambient
credential scanning. The `openai.OpenAI` constructor receives
`api_key=key` and `base_url="https://api.deepseek.com"` explicitly.
No other env vars are passed to the HTTP client.

---

## Technical Implementation

### New file: `lavandula/nonprofits/resolver_clients.py`

```python
class DeepSeekResolverClient:
    def __init__(self, *, api_key: str | None = None): ...
    def resolve(self, org: OrgIdentity) -> ResolverResult: ...
```

`OrgIdentity` is a dataclass holding:
`ein, name, address, city, state, zipcode, ntee_code`

`ResolverResult` is a dataclass holding:
`url, status, confidence, method, reason, candidates`

### Updated file: `lavandula/nonprofits/tools/resolve_websites.py`

Add `--resolver deepseek` CLI flag (default: `heuristic` for backwards
compatibility). When `deepseek` is selected, instantiate
`DeepSeekResolverClient` and call `resolve()` per org instead of the
existing Brave + scoring path.

### Updated file: `lavandula/nonprofits/eval/runner.py`

Add `deepseek` strategy that calls `DeepSeekResolverClient.resolve()`
so accuracy can be measured against the labeled eval dataset before
production use.

### Prompt design

**Phase 1 prompt** (generate):

```
You are identifying the official website of a US nonprofit organization.

Organization:
  Name: {name}
  EIN: {ein}
  Address: {address}, {city}, {state} {zipcode}
  NTEE code: {ntee_code}

List up to 5 URLs that are most likely to be this organization's official
website. Return ONLY a JSON array of URL strings, most likely first.
Example: ["https://example.org", "https://www.example.com"]
```

**Phase 3 prompt** (confirm):

```
You are verifying which website belongs to a specific US nonprofit.

Organization:
  Name: {name}
  EIN: {ein}
  Address: {address}, {city}, {state} {zipcode}

Candidate websites and their homepage excerpts:
{candidates_block}

Which URL is the official website of this exact organization?
Return JSON: {"url": "<chosen url or null>", "confidence": 0.0-1.0, "reason": "<short reasoning>"}
If none match, return {"url": null, "confidence": 0.0, "reason": "<why>"}
```

---

## Acceptance Criteria

**AC1** — `DeepSeekResolverClient` is importable and instantiates without
error when `DEEPSEEK_API_KEY` env var is set.

**AC2** — `resolve()` returns a `ResolverResult` with `status=resolved`
and a non-null URL for a well-known TX nonprofit (e.g., EIN `750808774`,
United Way of Metropolitan Dallas).

**AC3** — When the model returns a URL that does not respond to HTTP,
the resolver marks it `unresolved` rather than returning the dead URL.

**AC4** — When `DEEPSEEK_API_KEY` is not set and SSM is unavailable,
`DeepSeekResolverClient()` raises a clear `ConfigError` with a message
naming the missing credential path.

**AC5** — API key is never present in log output, error messages, or
`resolver_reason` strings.

**AC6** — `resolve_websites.py --resolver deepseek` runs end-to-end on
a single org without error.

**AC6b** — `resolve_websites.py` without `--resolver` flag continues to
use the existing heuristic resolver (no regression).

**AC7** — The `deepseek` strategy is registered in `eval/runner.py` and
`runner.evaluate_row(row, strategy="deepseek")` returns a decision object.

**AC8** — All unit tests pass without making real HTTP or DeepSeek API
calls (fully mocked).

**AC9** — The DeepSeek `openai.OpenAI` client is constructed with only
`api_key` and `base_url` — no ambient env var scanning.

**AC10** — Phase 1 and Phase 3 prompts include the org's street address
and zipcode (not just name/city/state).

---

## Traps to Avoid

1. **Do not re-use Brave search results as candidates.** The model
   generates its own candidates from org identity. Brave results are
   explicitly excluded from this flow.

2. **Do not log the API key.** SSM fetch errors should say "failed to
   fetch DeepSeek API key from SSM" — not include the key or the raw
   boto3 exception chain.

3. **Do not block on HTTP verification for dead domains.** Use a short
   connect timeout (3s) with a read timeout (8s). Skip to next candidate
   on any connection error.

4. **Do not trust the model's Phase 1 URL verbatim.** Always verify via
   HTTP before passing to Phase 3.

5. **Backwards compatibility.** The existing heuristic resolver must
   remain the default. `--resolver deepseek` is opt-in.

---

## Files Changed

| File | Change |
|------|--------|
| `lavandula/nonprofits/resolver_clients.py` | NEW |
| `lavandula/nonprofits/tools/resolve_websites.py` | Add `--resolver` flag |
| `lavandula/nonprofits/eval/runner.py` | Add `deepseek` strategy |
| `lavandula/nonprofits/tests/unit/test_resolver_0005.py` | NEW — AC1–AC10 |

---

## Open Questions

1. Should Phase 1 use `deepseek-reasoner` (R1) for better domain
   generation, or is `deepseek-chat` (V3) sufficient? V3 is ~4x cheaper.
   Recommend starting with V3 and upgrading only if accuracy is
   insufficient on the eval dataset.

2. For the recall gap on obscure/newer orgs where Phase 1 produces zero
   live candidates: a future spec should add a non-Brave search fallback
   (e.g., Google Custom Search or DuckDuckGo) as Phase 1.5. Not in scope
   for this spec.
