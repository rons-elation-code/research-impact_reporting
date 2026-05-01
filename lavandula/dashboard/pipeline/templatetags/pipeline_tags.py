from datetime import timedelta
from urllib.parse import urlparse

from django import template
from django.utils import timezone

register = template.Library()

HEARTBEAT_STALE_LOCAL = 120
HEARTBEAT_STALE_REMOTE = 300


@register.filter
def duration(td):
    """Convert timedelta or seconds to human-readable duration."""
    if td is None:
        return ""
    if isinstance(td, (int, float)):
        td = timedelta(seconds=td)
    total = int(td.total_seconds())
    if total < 0:
        return "0s"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


@register.filter
def percentage(current, total):
    """Calculate percentage from current/total."""
    if not total:
        return ""
    try:
        return f"{int(current / total * 100)}%"
    except (TypeError, ZeroDivisionError):
        return ""


@register.filter
def url_basename(url):
    """Extract filename from a URL path."""
    if not url:
        return ""
    try:
        path = urlparse(str(url)).path
        return path.rsplit("/", 1)[-1] or path
    except Exception:
        return str(url)


@register.filter
def stale_badge(last_heartbeat, is_local=True):
    """Return CSS class if heartbeat is stale."""
    if not last_heartbeat:
        return "text-yellow-600"
    threshold = HEARTBEAT_STALE_LOCAL if is_local else HEARTBEAT_STALE_REMOTE
    age = (timezone.now() - last_heartbeat).total_seconds()
    if age > threshold:
        return "text-yellow-600"
    return ""


@register.filter
def elapsed_since(start_time):
    """Calculate elapsed time since a datetime."""
    if not start_time:
        return ""
    td = timezone.now() - start_time
    return duration(td)


@register.filter
def currency(value):
    if value is None:
        return "—"
    return f"${value:,}"


PERSON_TYPE_BADGES = {
    "officer": "bg-blue-100 text-blue-800",
    "director": "bg-gray-100 text-gray-800",
    "key_employee": "bg-green-100 text-green-800",
    "highest_compensated": "bg-amber-100 text-amber-800",
    "listed": "bg-purple-100 text-purple-800",
}

FILING_STATUS_BADGES = {
    "parsed": "bg-green-100 text-green-800",
    "error": "bg-red-100 text-red-800",
    "downloaded": "bg-blue-100 text-blue-800",
    "indexed": "bg-gray-100 text-gray-800",
}


@register.filter
def person_badge(person_type):
    return PERSON_TYPE_BADGES.get(person_type, "bg-gray-100 text-gray-800")


@register.filter
def filing_badge(status):
    return FILING_STATUS_BADGES.get(status, "bg-gray-100 text-gray-800")


@register.filter
def dictget(d, key):
    if not isinstance(d, dict):
        return None
    return d.get(key)
