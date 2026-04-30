-- Migration 010: 990 Leadership & Contractor Intelligence (Spec 0026)
--
-- Creates two tables for IRS 990 Part VII and Schedule J data:
--   - filing_index: tracks which 990 filings we've downloaded/parsed
--   - people: officers, directors, key employees, and contractors per filing

-- filing_index: one row per IRS 990 filing we know about
CREATE TABLE lava_corpus.filing_index (
    object_id       TEXT PRIMARY KEY,
    ein             TEXT NOT NULL,
    tax_period      TEXT NOT NULL,
    return_type     TEXT NOT NULL,
    sub_date        TEXT,
    return_ts       TIMESTAMPTZ,
    is_amended      BOOLEAN DEFAULT FALSE,
    taxpayer_name   TEXT,
    xml_batch_id    TEXT,
    filing_year     INTEGER NOT NULL,
    status          TEXT DEFAULT 'indexed',
    error_message   TEXT,
    parsed_at       TIMESTAMPTZ,
    run_id          TEXT
);

CREATE INDEX idx_filing_ein ON lava_corpus.filing_index(ein);
CREATE INDEX idx_filing_status ON lava_corpus.filing_index(status);

-- people: one row per person per filing per org
CREATE TABLE lava_corpus.people (
    id              SERIAL PRIMARY KEY,
    ein             TEXT NOT NULL,
    tax_period      TEXT NOT NULL,
    object_id       TEXT NOT NULL,
    person_name     TEXT NOT NULL,
    title           TEXT,
    person_type     TEXT NOT NULL,
    avg_hours_per_week  NUMERIC(5,1),
    reportable_comp     BIGINT,
    related_org_comp    BIGINT,
    other_comp          BIGINT,
    total_comp          BIGINT GENERATED ALWAYS AS (
        COALESCE(reportable_comp, 0) + COALESCE(related_org_comp, 0) + COALESCE(other_comp, 0)
    ) STORED,
    base_comp           BIGINT,
    bonus               BIGINT,
    other_reportable    BIGINT,
    deferred_comp       BIGINT,
    nontaxable_benefits BIGINT,
    total_comp_sch_j    BIGINT,
    services_desc   TEXT,
    is_officer          BOOLEAN DEFAULT FALSE,
    is_director         BOOLEAN DEFAULT FALSE,
    is_key_employee     BOOLEAN DEFAULT FALSE,
    is_highest_comp     BOOLEAN DEFAULT FALSE,
    is_former           BOOLEAN DEFAULT FALSE,
    extracted_at    TIMESTAMPTZ DEFAULT NOW(),
    run_id          TEXT
);

CREATE INDEX idx_people_ein ON lava_corpus.people(ein);
CREATE INDEX idx_people_ein_period ON lava_corpus.people(ein, tax_period);
CREATE UNIQUE INDEX idx_people_dedup ON lava_corpus.people(ein, object_id, person_name, person_type);
