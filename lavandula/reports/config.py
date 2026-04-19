"""Configuration for spec 0004 site-crawl report catalogue.

Values here are the operator-tunable surface. Defaults are conservative per
the spec; override via direct assignment in tests or via env vars where
explicitly supported.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RAW = ROOT / "raw"
LOGS = ROOT / "logs"
HALT = ROOT / "halt"

DB_PATH = DATA / "reports.db"
LOCK_PATH = ROOT / ".crawler.lock"

# --- HTTP ---------------------------------------------------------------
REQUEST_DELAY_SEC = 3.0
REQUEST_DELAY_JITTER_SEC = 0.5
REQUEST_TIMEOUT_SEC = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SEC = 5.0
MAX_RETRY_AFTER_SEC = 300.0
MAX_REDIRECTS = 5

# User-Agent (non-deceptive, per spec).
UA_EMAIL = os.environ.get("LAVANDULA_UA_EMAIL", "crawler-contact@lavanduladesign.com")
USER_AGENT = (
    f"Lavandula Design report crawler/1.0 "
    f"(+https://lavanduladesign.com; {UA_EMAIL})"
)

# Outbound Accept-Encoding — gzip and identity ONLY.
ACCEPT_ENCODING = "gzip, identity"

# --- Response size caps -------------------------------------------------
# Per AC8 — caps enforced across every encoding + every fetch kind.
MAX_PDF_BYTES = 50 * 1024 * 1024         # 50 MB (decompressed)
MAX_TEXT_BYTES = 5 * 1024 * 1024         # 5 MB for robots / sitemap / HTML

# --- Sitemap + link caps (AC8.1) ----------------------------------------
MAX_SITEMAP_URLS_PER_ORG = 10_000
MAX_SITEMAPS_PER_ORG = 5
MAX_SITEMAP_DEPTH = 1
MAX_PARSED_LINKS_PER_PAGE = 10_000

# --- Candidate / discovery caps -----------------------------------------
CANDIDATE_CAP_PER_ORG = 30
MAX_SUBPAGES_PER_ORG = 5
# TICK-001: When expanding a subpage whose OWN URL/anchor already
# matched a report pattern, accept any PDF-suffix link inside it
# (bypassing the strict anchor/path keyword filter). Capped to
# prevent runaway fan-out on pathological landing pages.
MAX_PDFS_PER_REPORT_SUBPAGE = 20

# --- Classifier ---------------------------------------------------------
# Pinned model ID (per spec "requires a spec amendment to rotate").
CLASSIFIER_MODEL = "claude-haiku-4-5"
CLASSIFIER_TEMPERATURE = 0
# Pricing in cents per million tokens (per-spec: ~$0.25 in, ~$1.25 out).
# We multiply by 1.2 for the pessimistic safety margin referenced in AC18.1.
CLASSIFIER_INPUT_CENTS_PER_MTOK = 25.0
CLASSIFIER_OUTPUT_CENTS_PER_MTOK = 125.0
CLASSIFIER_BUDGET_CENTS = 1000            # default USD 10.00
CLASSIFIER_CONFIDENCE_FOR_PUBLIC = 0.8    # view threshold

# --- Retention ----------------------------------------------------------
RETENTION_DAYS = 365

# --- Filesystem permissions --------------------------------------------
DB_MODE = 0o600
ARCHIVE_DIR_MODE = 0o700
PDF_MODE = 0o600

# --- Robots -------------------------------------------------------------
ROBOTS_CACHE_TTL_SEC = 24 * 3600

# --- Hosting platform allowlist ----------------------------------------
HOSTING_PLATFORMS = frozenset({"issuu.com", "flipsnack.com", "canva.com"})

# --- Candidate keywords (spec) ------------------------------------------
ANCHOR_KEYWORDS = frozenset({
    "annual report",
    "impact report",
    "year in review",
    "results",
    "accountability",
    "financials",
    "transparency",
    "our impact",
    "annual",
    "impact",
})

PATH_KEYWORDS = frozenset({
    "/impact",
    "/annual-report",
    "/annual_report",
    "/annualreport",
    "/transparency",
    "/financials",
    "/about/results",
    "/our-impact",
    "/reports",
    "/publications",
})

# Named cloud-metadata deny list (IPv4 + IPv6 extras on top of RFC classes).
CLOUD_METADATA_DENY = frozenset({
    "169.254.169.254",     # AWS v4
    "168.63.129.16",       # Azure
    "100.100.100.200",     # Alibaba
    "fd00:ec2::254",       # AWS v6
})

# Sensitive query-params / fragment-segments to redact (AC13).
SENSITIVE_URL_PARAMS = frozenset({
    "token", "api_key", "apikey", "api-key",
    "access_token", "access-token",
    "refresh_token", "refresh-token",
    "id_token", "id-token",
    "bearer", "password", "pwd", "secret",
    "credential", "sig", "signature",
    "code", "key", "auth", "session",
})

# Forum / comment / UGC path fragments — exclude from platform-verified.
UGC_PATH_SIGNATURES = ("/forum", "/comments", "/community/", "/discuss/")

# --- SPIDER constants (diagnostic only) --------------------------------
PARSE_VERSION = 1
EXTRACTOR_VERSION = 1
CLASSIFIER_VERSION = 1
