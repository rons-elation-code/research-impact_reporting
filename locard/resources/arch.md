# Architecture

High-level architecture documentation for the research workspace. Updated
as new top-level modules are added.

## Overview

This repo hosts Lavandula Design's research crawlers and prospect-data
tooling. Each top-level module is a self-contained project that reuses
common patterns (throttled HTTP client, checkpoint-and-resume) without
currently sharing code.

## Directory Structure

```
research/
├── locard/                          # Development methodology (specs/plans/reviews)
│   ├── specs/                       # Feature specifications
│   ├── plans/                       # Implementation plans
│   ├── tests/                       # TDD acceptance-test scaffolds
│   └── resources/                   # arch.md, workflow docs
├── nptech/                          # nptechforgood content crawler (existing)
└── lavandula/
    └── nonprofits/                  # Nonprofit seed-list crawler (spec 0001)
```

## Key Components

### lavandula/nonprofits/

**Location**: `lavandula/nonprofits/`

**Purpose**: Crawls Charity Navigator's public `/ein/*` profile sitemap
into a queryable SQLite database of ~48K US nonprofits. Produces the
seed list for the future report-harvesting project.

**Key Files**:
- `crawler.py` — CLI entrypoint; wires everything into the main loop.
- `http_client.py` — throttled client with TLS self-test, redirect
  restriction, decompressed size cap, cookie non-persistence.
- `robots.py` — robots.txt parser with most-specific-stanza matching.
- `sitemap.py` — XXE-safe XML parser (defusedxml, lxml fallback).
- `fetcher.py` — profile fetch + challenge detection + atomic archive.
- `archive.py` — symlink-safe writes via `O_NOFOLLOW` + per-PID temp dir.
- `extract.py` — HTML → `ExtractedProfile` (BeautifulSoup + lxml).
- `url_normalize.py` — 10-rule website URL normalization pipeline.
- `db_writer.py` — parameterized SQLite writes.
- `checkpoint.py` — HMAC-integrity resume state.
- `stop_conditions.py` — halt-policy detector.
- `report.py` — coverage_report.md generator.
- `schema.py` — SQLite DDL.
- `HANDOFF.md` — operational doc for downstream consumers.

**Relationship to nptech/**: `nptech/` is the precedent crawler against
`nptechforgood.com`. `lavandula/nonprofits/` reuses the same throttle +
checkpoint _patterns_ but does not share code — each project stays
self-contained. If a second concrete consumer ever wants the same HTTP
client, a TICK can hoist `common/http_client.py`.

### nptech/

**Location**: `nptech/`

**Purpose**: Scrape nptechforgood.com WordPress content (existing).

## Data Flow (lavandula/nonprofits/)

```
Charity Navigator sitemap index
        ↓
  sitemap.py (XXE-safe parse)
        ↓
  sitemap_entries table (EIN enumeration)
        ↓
  crawler loop
        ↓                        ↓
  fetcher.py                 stop_conditions.py
    ↓           ↓                 ↓
  archive.py  extract.py      halt / HALT-*.md
    ↓           ↓
  raw/cn/   db_writer.py
               ↓
          nonprofits table  ← report.py → coverage_report.md
```

## External Dependencies

| Dependency | Purpose | Notes |
|------------|---------|-------|
| Charity Navigator (`www.charitynavigator.org`) | public sitemap + profile pages | HTTPS only; robots.txt permits `/ein/*` |
| `defusedxml` | XXE-safe XML parsing | required for sitemap.py |
| `lxml >= 4.9.1` | BeautifulSoup backend + XML fallback | HTML-mode entity safety |
| `requests >= 2.31.0` | HTTP transport | CVE-2023-32681 fix |
| `cryptography` | in-process TLS self-test harness | generates expired self-signed cert |

## Configuration

- `lavandula/nonprofits/config.py` — throttle, paths, UA, stop-condition
  thresholds.
- Env overrides: `LAVANDULA_UA_EMAIL` to rotate the contact address.

## Conventions

- SQL writes use `?` parameter binding only.
- Remote-sourced strings pass through `logging_utils.sanitize` before
  log/DB writes.
- Archive writes are atomic (`os.open(...O_NOFOLLOW...)` + `os.replace`
  + parent-dir `fsync`).
- Each new module may reuse nptech _patterns_ but must not import
  across top-level modules.
