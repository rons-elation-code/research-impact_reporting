"""Spec 0017 — upsert_report attribution-rank merge logic.

Exercises the ON CONFLICT (content_sha256) DO UPDATE path that uses
`lava_impact.attribution_rank()` to prefer stronger attribution tiers.
Category A (real Postgres required).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("postgres_engine")


def _base_kwargs(**over):
    base = dict(
        content_sha256="f" * 64,
        source_url_redacted="https://example.org/a.pdf",
        referring_page_url_redacted=None,
        redirect_chain_redacted=None,
        source_org_ein="000000001",
        discovered_via="homepage-link",
        hosting_platform=None,
        attribution_confidence="platform_unverified",
        file_size_bytes=1024,
        page_count=None,
        first_page_text=None,
        pdf_creator=None,
        pdf_producer=None,
        pdf_creation_date=None,
        pdf_has_javascript=0,
        pdf_has_launch=0,
        pdf_has_embedded=0,
        pdf_has_uri_actions=0,
        classification=None,
        classification_confidence=None,
        classifier_model="claude-haiku-4-5",
        classifier_version=1,
        report_year=None,
        report_year_source=None,
        extractor_version=1,
    )
    base.update(over)
    return base


def test_attribution_rank_helper_ordering(postgres_engine):
    with postgres_engine.connect() as conn:
        ranks = {
            tier: int(conn.execute(
                text("SELECT lava_impact.attribution_rank(:t)"),
                {"t": tier},
            ).scalar())
            for tier in ("own_domain", "platform_verified",
                         "platform_unverified")
        }
    assert ranks["own_domain"] > ranks["platform_verified"]
    assert ranks["platform_verified"] > ranks["platform_unverified"]


def test_upsert_stronger_attribution_wins(postgres_engine):
    from lavandula.reports import db_writer

    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(
            attribution_confidence="platform_unverified",
            source_url_redacted="https://weak.example.org/a.pdf",
        ),
    )
    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(
            attribution_confidence="own_domain",
            source_url_redacted="https://strong.example.org/a.pdf",
        ),
    )
    with postgres_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT attribution_confidence, source_url_redacted "
            "FROM lava_impact.corpus WHERE content_sha256 = :s"
        ), {"s": "f" * 64}).fetchone()
    assert row[0] == "own_domain"
    assert row[1] == "https://strong.example.org/a.pdf"


def test_upsert_weaker_attribution_loses(postgres_engine):
    from lavandula.reports import db_writer

    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(
            attribution_confidence="own_domain",
            source_url_redacted="https://strong.example.org/a.pdf",
        ),
    )
    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(
            attribution_confidence="platform_unverified",
            source_url_redacted="https://weak.example.org/a.pdf",
        ),
    )
    with postgres_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT attribution_confidence, source_url_redacted "
            "FROM lava_impact.corpus WHERE content_sha256 = :s"
        ), {"s": "f" * 64}).fetchone()
    assert row[0] == "own_domain"
    assert row[1] == "https://strong.example.org/a.pdf"


def test_active_content_flags_never_downgrade(postgres_engine):
    from lavandula.reports import db_writer

    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(pdf_has_javascript=1, pdf_has_launch=1),
    )
    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(pdf_has_javascript=0, pdf_has_launch=0),
    )
    with postgres_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT pdf_has_javascript, pdf_has_launch "
            "FROM lava_impact.corpus WHERE content_sha256 = :s"
        ), {"s": "f" * 64}).fetchone()
    assert row[0] == 1
    assert row[1] == 1


def test_classification_prefers_higher_confidence(postgres_engine):
    from lavandula.reports import db_writer

    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(
            classification="other",
            classification_confidence=0.6,
        ),
    )
    db_writer.upsert_report(
        postgres_engine,
        **_base_kwargs(
            classification="annual",
            classification_confidence=0.95,
        ),
    )
    with postgres_engine.connect() as conn:
        row = conn.execute(text(
            "SELECT classification, classification_confidence "
            "FROM lava_impact.corpus WHERE content_sha256 = :s"
        ), {"s": "f" * 64}).fetchone()
    assert row[0] == "annual"
    assert float(row[1]) == pytest.approx(0.95)
