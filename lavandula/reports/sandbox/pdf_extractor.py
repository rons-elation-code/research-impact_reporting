"""Untrusted PDF parsing payload — RUNS INSIDE THE SANDBOX.

Imported by `sandbox.runner.extract_pdf_fields`. Emits a single JSON
object on stdout with the declared schema. Never import anything that
touches the network or disk outside the input PDF path.
"""
from __future__ import annotations

import json
from pathlib import Path

from lavandula.reports.pdf_extract import (
    sanitize_metadata_field,
    scan_active_content,
)


SCHEMA = {
    "first_page_text",
    "page_count",
    "pdf_creator",
    "pdf_producer",
    "pdf_creation_date",
    "pdf_has_javascript",
    "pdf_has_launch",
    "pdf_has_embedded",
    "pdf_has_uri_actions",
}


def extract(pdf_path: str | Path) -> dict:
    """Produce the declared output schema for the sandbox child.

    Bounded outputs:
      - first_page_text: <= 4096 chars
      - pdf_creator / pdf_producer: <= 200 chars (AC18.2-sanitized)
    """
    path = Path(pdf_path)
    try:
        pdf_bytes = path.read_bytes()
    except OSError as exc:
        return {"error": f"read_failed:{type(exc).__name__}"}

    flags = scan_active_content(pdf_bytes)

    first_page_text = ""
    page_count = None
    creator = None
    producer = None
    creation_date = None
    try:
        import io

        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_count = len(reader.pages)
        if page_count:
            try:
                first_page_text = reader.pages[0].extract_text() or ""
            except Exception:  # noqa: BLE001
                first_page_text = ""
        meta = reader.metadata or {}
        creator = meta.get("/Creator") if isinstance(meta, dict) else getattr(meta, "creator", None)
        producer = meta.get("/Producer") if isinstance(meta, dict) else getattr(meta, "producer", None)
        creation_date = (
            meta.get("/CreationDate")
            if isinstance(meta, dict)
            else getattr(meta, "creation_date_raw", None)
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "error": f"parse_failed:{type(exc).__name__}",
            **flags,
            "first_page_text": "",
            "page_count": None,
            "pdf_creator": None,
            "pdf_producer": None,
            "pdf_creation_date": None,
        }

    if first_page_text and len(first_page_text) > 4096:
        first_page_text = first_page_text[:4096]

    return {
        "first_page_text": first_page_text,
        "page_count": page_count,
        "pdf_creator": sanitize_metadata_field(
            str(creator) if creator is not None else None
        ),
        "pdf_producer": sanitize_metadata_field(
            str(producer) if producer is not None else None
        ),
        "pdf_creation_date": sanitize_metadata_field(
            str(creation_date) if creation_date is not None else None
        ),
        **flags,
    }


def validate_payload(payload: dict) -> bool:
    """Return True iff `payload` matches the declared output schema.

    Called by the PARENT after deserializing child stdout — before the
    fields are handed to db_writer.
    """
    if not isinstance(payload, dict):
        return False
    if "error" in payload:
        return True  # error payload is valid (caller picks up fallback path)
    if not SCHEMA.issubset(payload.keys()):
        return False
    if payload["first_page_text"] is not None and len(payload["first_page_text"]) > 4096:
        return False
    for k in ("pdf_creator", "pdf_producer"):
        v = payload.get(k)
        if v is not None and len(v) > 200:
            return False
    for k in ("pdf_has_javascript", "pdf_has_launch", "pdf_has_embedded", "pdf_has_uri_actions"):
        if payload.get(k) not in (0, 1):
            return False
    pc = payload.get("page_count")
    if pc is not None and not (isinstance(pc, int) and pc >= 0):
        return False
    return True


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print(json.dumps({"error": "usage"}))
        sys.exit(2)
    print(json.dumps(extract(sys.argv[1])))
