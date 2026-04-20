"""Shared config for nptech crawler/extractor."""
from pathlib import Path

SITE = "https://www.nptechforgood.com"
API = f"{SITE}/wp-json/wp/v2"

ROOT = Path(__file__).parent
RAW_POSTS = ROOT / "raw" / "posts"
RAW_MEDIA = ROOT / "raw" / "media"
RAW_IMAGES = ROOT / "raw" / "images"
CLEAN_POSTS = ROOT / "clean" / "posts"
STATE = ROOT / "state"
LOGS = ROOT / "logs"

# Courtesy throttle. robots.txt asks for 3s; we use 10s because this is a
# competitor in a small industry and slow = invisible.
REQUEST_DELAY_SEC = 10.0

# Generic, non-deceptive UA. Not identifying, not impersonating a browser.
USER_AGENT = "Mozilla/5.0 (compatible; research-indexer/1.0)"

# REST page size. 100 is the WP maximum.
PER_PAGE = 100

# Retry policy
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 5.0  # seconds, doubled each retry

# Download inline images? Set False for a metadata-only first pass.
DOWNLOAD_IMAGES = True

# Hard daily cap on HTTP requests. None = no cap.
DAILY_REQUEST_CAP = None

# Fields to fetch from the posts endpoint. _embed pulls featured media inline.
POST_FIELDS = (
    "id,slug,link,date,date_gmt,modified,modified_gmt,"
    "title,content,excerpt,categories,tags,author,featured_media"
)
