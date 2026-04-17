"""Website URL normalization per spec 0001 § Website URL Normalization Policy.

The 10 rules, applied in order:
  1. Unwrap CN redirect wrappers (`/redirect?to=...`) up to 3 levels
  2. Reject non-HTTP schemes (mailto/tel/sms/javascript/etc.)
  3. Reject social-only hosts
  4. Lowercase host
  5. Remove default ports
  6. Punycode IDN hosts
  7. Strip tracking params
  8. Strip trailing slash on root path only
  9. Drop fragment
 10. Validate as well-formed http(s) with non-empty host

Returns (url, reason) — exactly one is non-None.
"""
from __future__ import annotations

from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urlparse,
    urlunparse,
)

from . import config


ValidReason = str | None


def normalize(raw: str | None) -> tuple[str | None, ValidReason]:
    """Normalize `raw` per the 10 rules. Returns (normalized_url, reason).

    Exactly one of the return values is non-None:
      - (url, None) → normalized URL usable in DB
      - (None, reason) → reason is an enum from config.WEBSITE_URL_REASONS
    """
    if raw is None:
        return None, "missing"
    s = raw.strip()
    if not s:
        return None, "missing"

    # Rule 1: unwrap CN redirect wrappers up to 3 times.
    unwrapped, ok = _unwrap_cn_redirect(s)
    if not ok:
        return None, "unwrap_failed"
    s = unwrapped

    # Rule 2: reject non-http(s) schemes.
    lower = s.lower()
    if lower.startswith("mailto:"):
        return None, "mailto"
    if lower.startswith("tel:") or lower.startswith("sms:"):
        return None, "tel"
    if ":" in s and not (lower.startswith("http://") or lower.startswith("https://")):
        scheme_guess = s.split(":", 1)[0].lower()
        if scheme_guess in ("javascript", "data", "file", "ftp", "about", "chrome"):
            return None, "invalid"
        # If it has no scheme at all (e.g. "example.org/foo"), try to
        # repair as https.
        if "/" not in scheme_guess and scheme_guess.isalpha():
            return None, "invalid"
    if not (lower.startswith("http://") or lower.startswith("https://")):
        # If it looks like a bare domain, prepend https://.
        if "." in s and " " not in s and "/" not in s.split(":", 1)[0]:
            s = "https://" + s
        else:
            return None, "invalid"

    parsed = urlparse(s)
    if not parsed.hostname:
        return None, "invalid"

    # Rule 3: reject social-only hosts.
    host_lower = parsed.hostname.lower()
    if host_lower in config.SOCIAL_HOSTS:
        return None, "social"

    # Rule 4+5: lowercase host, strip default ports.
    port = parsed.port
    if parsed.scheme == "http" and port == 80:
        port = None
    if parsed.scheme == "https" and port == 443:
        port = None

    # Rule 6: punycode IDN.
    try:
        ascii_host = host_lower.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None, "invalid"

    netloc = ascii_host
    if parsed.username:
        userinfo = quote(parsed.username, safe="")
        if parsed.password is not None:
            userinfo += ":" + quote(parsed.password, safe="")
        netloc = f"{userinfo}@{netloc}"
    if port is not None:
        netloc = f"{netloc}:{port}"

    # Rule 7: strip tracking params, keep others.
    query = urlencode(
        [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k not in config.TRACKING_PARAMS
        ]
    )

    # Rule 8: strip trailing slash on root path only.
    path = parsed.path
    if path in ("/", ""):
        path = ""

    # Rule 9: drop fragment (handled by passing empty string to urlunparse).
    normalized = urlunparse((
        parsed.scheme,
        netloc,
        path,
        parsed.params,
        query,
        "",
    ))

    # Rule 10: validate.
    check = urlparse(normalized)
    if not check.hostname:
        return None, "invalid"
    if check.scheme not in ("http", "https"):
        return None, "invalid"
    return normalized, None


def _unwrap_cn_redirect(url: str, max_depth: int = 3) -> tuple[str, bool]:
    """Peel off `.../redirect?to=<encoded>` wrappers up to max_depth times.

    Returns (final_url, ok). ok=False iff the URL still looks wrapped
    after max_depth attempts.
    """
    current = url
    for _ in range(max_depth):
        p = urlparse(current)
        if p.hostname and p.hostname.lower() == config.SITE_HOST and "/redirect" in p.path.lower():
            params = dict(parse_qsl(p.query, keep_blank_values=True))
            candidate = (
                params.get("to")
                or params.get("url")
                or params.get("destination")
                or params.get("u")
            )
            if not candidate:
                return current, False
            current = unquote(candidate)
            continue
        return current, True
    # Still wrapped after max_depth.
    p = urlparse(current)
    if p.hostname and p.hostname.lower() == config.SITE_HOST and "/redirect" in p.path.lower():
        return current, False
    return current, True
