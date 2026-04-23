"""URL normalization for resolved nonprofit websites (Spec 0018).

Normalizes URLs before persisting to the database:
- Strip tracking query parameters (utm_*, fbclid, gclid, ref)
- Prefer HTTPS when available (probed via ReportsHTTPClient for SSRF safety)
- Trailing slash: include for bare domains, omit for paths
"""
from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

_TRACKING_PARAMS = {"fbclid", "gclid", "ref"}
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking_param(key: str) -> bool:
    key_lower = key.lower()
    if key_lower in _TRACKING_PARAMS:
        return True
    return any(key_lower.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str, *, check_https: bool = True) -> str:
    """Normalize a resolved URL for database storage."""
    parts = urlsplit(url)

    query_params = parse_qs(parts.query, keep_blank_values=True)
    cleaned = {k: v for k, v in query_params.items() if not _is_tracking_param(k)}
    new_query = urlencode(cleaned, doseq=True) if cleaned else ""

    path = parts.path

    is_bare_domain = path in ("", "/")
    if is_bare_domain:
        path = "/"
    elif path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")

    scheme = parts.scheme
    if scheme == "http" and check_https:
        https_url = urlunsplit(("https", parts.netloc, "/", "", ""))
        try:
            from lavandula.reports.http_client import ReportsHTTPClient
            client = ReportsHTTPClient(allow_insecure_cleartext=False)
            result = client.head(https_url, kind="pdf-head")
            if result.http_status == 200:
                scheme = "https"
        except Exception:
            pass

    return urlunsplit((scheme, parts.netloc, path, new_query, ""))


__all__ = ["normalize_url"]
