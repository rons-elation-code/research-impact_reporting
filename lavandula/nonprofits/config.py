"""Configuration for the Charity Navigator crawler.

All throttle, path, UA, size, and stop-condition settings live here so that
tests and operators can override via env vars or direct attribute assignment.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RAW = ROOT / "raw" / "cn"
LOGS = ROOT / "logs"
INCIDENTS = ROOT / "incidents"

DB_PATH = DATA / "nonprofits.db"
CHECKPOINT_PATH = DATA / "checkpoint.json"
CRAWLER_KEY_PATH = DATA / ".crawler.key"
LOCK_PATH = ROOT / ".crawler.lock"

SITE_HOST = "www.charitynavigator.org"
SITE_BASE = f"https://{SITE_HOST}"
SITEMAP_INDEX_URL = f"{SITE_BASE}/extra-index.xml"
ROBOTS_URL = f"{SITE_BASE}/robots.txt"
PROFILE_URL_TEMPLATE = f"{SITE_BASE}/ein/{{ein}}"

# --- Throttle -----------------------------------------------------------
REQUEST_DELAY_SEC = 3.0
REQUEST_DELAY_JITTER_SEC = 0.5
REQUEST_TIMEOUT_SEC = 30.0

# --- Retry --------------------------------------------------------------
MAX_RETRIES = 5
RETRY_BACKOFF_BASE_SEC = 5.0

# --- Response limits ----------------------------------------------------
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB decompressed
MAX_REDIRECTS = 5

# --- User-Agent ---------------------------------------------------------
UA_EMAIL = os.environ.get("LAVANDULA_UA_EMAIL", "crawler-contact@lavanduladesign.com")
USER_AGENT = (
    f"Lavandula Design research crawler/1.0 "
    f"(+https://lavanduladesign.com; {UA_EMAIL})"
)

# --- Redirect policy ----------------------------------------------------
ALLOWED_REDIRECT_HOST = SITE_HOST
ALLOWED_REDIRECT_SCHEME = "https"

# --- Disallowed EINs (robots-derived, canonical undashed form) ----------
DISALLOWED_EINS = frozenset({"863371262"})

# --- Stop-condition thresholds ------------------------------------------
MAX_CONSECUTIVE_403 = 3
MAX_CONSECUTIVE_429 = 5
MAX_RETRY_AFTER_SEC = 300.0
MAX_CONSECUTIVE_LONG_RETRY_AFTER = 2
MAX_RUNTIME_HOURS = 72.0

# --- Disk-space thresholds ----------------------------------------------
# PREFLIGHT was sized for the abandoned full-sitemap path (2.3M pages).
# Curated-lists scope (~3-7K orgs) needs an order of magnitude less.
PREFLIGHT_FREE_GB = 8
RUNTIME_FREE_GB = 5
MAX_ARCHIVE_GB = 50

# --- Robots re-fetch cadence --------------------------------------------
ROBOTS_REFETCH_EVERY_SEC = 6 * 3600
ROBOTS_REFETCH_EVERY_EINS = 1000

# --- Content-Type allowlist ---------------------------------------------
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
)

# --- Tracking parameters stripped in URL normalization ------------------
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "_ga",
})

# --- Social-only hosts rejected during URL normalization ----------------
SOCIAL_HOSTS = frozenset({
    "facebook.com", "www.facebook.com",
    "twitter.com", "www.twitter.com",
    "x.com", "www.x.com",
    "instagram.com", "www.instagram.com",
    "linkedin.com", "www.linkedin.com",
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "tiktok.com", "www.tiktok.com",
    "threads.net", "www.threads.net",
})

# --- Cloudflare challenge signatures ------------------------------------
CHALLENGE_SIGNATURES = (
    "cf-challenge",
    "__cf_chl_jschl_tk__",
    "<title>Just a moment",
    "/cdn-cgi/challenge-platform/",
    "cf_chl_opt",
    "turnstile",
)

PARSE_VERSION = 1
