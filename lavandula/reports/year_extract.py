from __future__ import annotations

import datetime as _dt
import re
from urllib.parse import unquote, urlsplit

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_DATE_RE = re.compile(r"D:(20\d{2})")


def _valid_year(year: int) -> bool:
    current = _dt.datetime.now(_dt.timezone.utc).year
    return 2000 <= year <= current + 1


def _pick_year(text: str | None) -> int | None:
    if not text:
        return None
    years = [int(match.group(1)) for match in _YEAR_RE.finditer(text)]
    valid = [year for year in years if _valid_year(year)]
    if not valid:
        return None
    return max(valid)


def infer_report_year(
    *,
    source_url: str,
    first_page_text: str | None,
    pdf_creation_date: str | None,
) -> tuple[int | None, str | None]:
    parsed = urlsplit(source_url)
    filename_year = _pick_year(unquote(parsed.path.rsplit("/", 1)[-1]))
    if filename_year is not None:
        return filename_year, "filename"

    url_year = _pick_year(unquote(parsed.path))
    if url_year is not None:
        return url_year, "url"

    text_year = _pick_year(first_page_text)
    if text_year is not None:
        return text_year, "first-page"

    if pdf_creation_date:
        match = _DATE_RE.search(pdf_creation_date)
        if match:
            year = int(match.group(1))
            if _valid_year(year):
                return year, "pdf-creation-date"

    return None, None
