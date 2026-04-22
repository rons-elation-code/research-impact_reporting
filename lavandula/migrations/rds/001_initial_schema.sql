-- Migration: 001_initial_schema
-- Date: 2026-04-22
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact
-- Mirrors current SQLite schema from:
--   lavandula/reports/schema.py
--   lavandula/nonprofits/tools/seed_enumerate.py (nonprofits_seed + runs + migrations)
--
-- Run as master user (postgres) from pgAdmin or psql.

BEGIN;

SET search_path TO lava_impact, public;

-- =========================================================================
-- schema_version — tracks which migrations have been applied
-- =========================================================================
CREATE TABLE IF NOT EXISTS schema_version (
  version     INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  applied_by  TEXT NOT NULL DEFAULT CURRENT_USER
);

-- =========================================================================
-- Default privileges — makes every table created by postgres auto-grant
-- CRUD to app_user1 and SELECT to ro_user1. Must be set BEFORE CREATE TABLE
-- so the newly-created objects inherit the grants.
-- =========================================================================
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA lava_impact
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user1;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA lava_impact
  GRANT SELECT ON TABLES TO ro_user1;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA lava_impact
  GRANT USAGE, SELECT ON SEQUENCES TO app_user1;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA lava_impact
  GRANT SELECT ON SEQUENCES TO ro_user1;

-- =========================================================================
-- nonprofits_seed — seed list of nonprofit orgs with resolver output
-- =========================================================================
CREATE TABLE IF NOT EXISTS nonprofits_seed (
  ein                     TEXT PRIMARY KEY,
  name                    TEXT,
  address                 TEXT,
  city                    TEXT,
  state                   TEXT,
  zipcode                 TEXT,
  ntee_code               TEXT,
  revenue                 BIGINT,
  subsection_code         INTEGER,
  activity_codes          TEXT,
  classification_codes    TEXT,
  foundation_code         INTEGER,
  ruling_date             TEXT,
  accounting_period       INTEGER,
  website_url             TEXT,
  website_candidates_json TEXT,
  resolver_status         TEXT,
  resolver_confidence     DOUBLE PRECISION,
  resolver_method         TEXT,
  resolver_reason         TEXT,
  notes                   TEXT,
  discovered_at           TEXT,
  run_id                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_seed_state        ON nonprofits_seed(state);
CREATE INDEX IF NOT EXISTS idx_seed_website_null ON nonprofits_seed(ein) WHERE website_url IS NULL;

-- Convenience view used by the crawler selection path
CREATE OR REPLACE VIEW nonprofits AS
  SELECT ein, website_url, resolver_status FROM nonprofits_seed;

-- =========================================================================
-- runs — seed-enumeration audit log
-- =========================================================================
CREATE TABLE IF NOT EXISTS runs (
  run_id             TEXT PRIMARY KEY,
  started_at         TEXT,
  finished_at        TEXT,
  filters_json       TEXT,
  found_count        INTEGER,
  website_hit_count  INTEGER,
  last_page_scanned  TEXT,
  exit_reason        TEXT
);

-- =========================================================================
-- reports — PDF metadata, classification, provenance
-- =========================================================================
CREATE TABLE IF NOT EXISTS reports (
  content_sha256       TEXT PRIMARY KEY,

  source_url_redacted         TEXT NOT NULL,
  referring_page_url_redacted TEXT,
  redirect_chain_json         TEXT,
  source_org_ein              TEXT NOT NULL,
  discovered_via              TEXT NOT NULL,
  hosting_platform            TEXT,

  attribution_confidence TEXT NOT NULL,

  archived_at     TEXT NOT NULL,
  content_type    TEXT NOT NULL,
  file_size_bytes BIGINT NOT NULL,
  page_count      INTEGER,

  first_page_text   TEXT,
  pdf_creator       TEXT,
  pdf_producer      TEXT,
  pdf_creation_date TEXT,

  pdf_has_javascript  SMALLINT NOT NULL DEFAULT 0,
  pdf_has_launch      SMALLINT NOT NULL DEFAULT 0,
  pdf_has_embedded    SMALLINT NOT NULL DEFAULT 0,
  pdf_has_uri_actions SMALLINT NOT NULL DEFAULT 0,

  classification            TEXT,
  classification_confidence DOUBLE PRECISION,
  classifier_model          TEXT NOT NULL,
  classifier_version        INTEGER NOT NULL DEFAULT 1,
  classified_at             TEXT,

  report_year        INTEGER,
  report_year_source TEXT,

  extractor_version INTEGER NOT NULL DEFAULT 1,

  CONSTRAINT reports_sha_len_chk CHECK (length(content_sha256) = 64),
  CONSTRAINT reports_size_chk    CHECK (file_size_bytes > 0),
  CONSTRAINT reports_ct_chk      CHECK (content_type = 'application/pdf'),
  CONSTRAINT reports_disc_chk    CHECK (discovered_via IN
                                         ('sitemap','homepage-link','subpage-link','hosting-platform')),
  CONSTRAINT reports_platform_chk CHECK (hosting_platform IS NULL OR hosting_platform IN
                                         ('issuu','flipsnack','canva','own-domain','own-cms')),
  CONSTRAINT reports_class_chk   CHECK (classification IS NULL OR classification IN
                                         ('annual','impact','hybrid','other','not_a_report')),
  CONSTRAINT reports_conf_chk    CHECK (classification_confidence IS NULL OR
                                         (classification_confidence >= 0
                                          AND classification_confidence <= 1)),
  CONSTRAINT reports_attr_chk    CHECK (attribution_confidence IN
                                         ('own_domain','platform_verified','platform_unverified')),
  CONSTRAINT reports_redirect_chk CHECK (redirect_chain_json IS NULL
                                          OR length(redirect_chain_json) <= 2048),
  CONSTRAINT reports_js_chk      CHECK (pdf_has_javascript IN (0,1)),
  CONSTRAINT reports_launch_chk  CHECK (pdf_has_launch IN (0,1)),
  CONSTRAINT reports_embed_chk   CHECK (pdf_has_embedded IN (0,1)),
  CONSTRAINT reports_uri_chk     CHECK (pdf_has_uri_actions IN (0,1)),
  CONSTRAINT reports_fpt_len_chk CHECK (first_page_text IS NULL
                                         OR length(first_page_text) <= 4096),
  CONSTRAINT reports_creator_chk CHECK (pdf_creator IS NULL OR length(pdf_creator) <= 200),
  CONSTRAINT reports_producer_chk CHECK (pdf_producer IS NULL OR length(pdf_producer) <= 200),
  CONSTRAINT reports_year_src_chk CHECK (report_year_source IS NULL OR report_year_source IN
                                          ('url','filename','first-page','pdf-creation-date'))
);

CREATE INDEX IF NOT EXISTS idx_reports_ein            ON reports(source_org_ein);
CREATE INDEX IF NOT EXISTS idx_reports_classification ON reports(classification);
CREATE INDEX IF NOT EXISTS idx_reports_year           ON reports(report_year);
CREATE INDEX IF NOT EXISTS idx_reports_platform       ON reports(hosting_platform);

-- reports_public view — mirrors SQLite; tests grep the WHERE clause
CREATE OR REPLACE VIEW reports_public AS
  SELECT content_sha256, source_org_ein, hosting_platform,
         attribution_confidence,
         archived_at, file_size_bytes, page_count,
         classification, classification_confidence,
         report_year, report_year_source,
         pdf_has_javascript, pdf_has_launch, pdf_has_embedded
  FROM reports
  WHERE attribution_confidence IN ('own_domain','platform_verified')
    AND classification IS NOT NULL
    AND classification != 'not_a_report'
    AND classification_confidence >= 0.8
    AND pdf_has_javascript = 0
    AND pdf_has_launch = 0
    AND pdf_has_embedded = 0;

-- =========================================================================
-- fetch_log — every HTTP / classifier event, for audit + debugging
-- =========================================================================
CREATE TABLE IF NOT EXISTS fetch_log (
  id             BIGSERIAL PRIMARY KEY,
  ein            TEXT,
  url_redacted   TEXT NOT NULL,
  kind           TEXT NOT NULL,
  fetch_status   TEXT NOT NULL,
  status_code    INTEGER,
  fetched_at     TEXT NOT NULL,
  elapsed_ms     INTEGER,
  notes          TEXT,
  CONSTRAINT fetch_log_kind_chk CHECK (kind IN
    ('robots','sitemap','homepage','subpage','pdf-head','pdf-get',
     'extract','classify')),
  CONSTRAINT fetch_log_status_chk CHECK (fetch_status IN
    ('ok','not_found','rate_limited','forbidden','server_error',
     'network_error','size_capped','blocked_content_type',
     'blocked_scheme','blocked_ssrf','cross_origin_blocked',
     'blocked_robots','classifier_error'))
);
CREATE INDEX IF NOT EXISTS idx_fetch_log_ein ON fetch_log(ein);

-- =========================================================================
-- crawled_orgs — per-org crawl rollup
-- =========================================================================
CREATE TABLE IF NOT EXISTS crawled_orgs (
  ein                    TEXT PRIMARY KEY,
  first_crawled_at       TEXT NOT NULL,
  last_crawled_at        TEXT NOT NULL,
  candidate_count        INTEGER NOT NULL DEFAULT 0,
  fetched_count          INTEGER NOT NULL DEFAULT 0,
  confirmed_report_count INTEGER NOT NULL DEFAULT 0
);

-- =========================================================================
-- deletion_log — audit of deleted reports
-- =========================================================================
CREATE TABLE IF NOT EXISTS deletion_log (
  id              BIGSERIAL PRIMARY KEY,
  content_sha256  TEXT NOT NULL,
  deleted_at      TEXT NOT NULL,
  reason          TEXT,
  operator        TEXT,
  pdf_unlinked    SMALLINT NOT NULL,
  CONSTRAINT deletion_log_unlinked_chk CHECK (pdf_unlinked IN (0,1))
);

-- =========================================================================
-- budget_ledger — classifier spend accounting
-- =========================================================================
CREATE TABLE IF NOT EXISTS budget_ledger (
  id                BIGSERIAL PRIMARY KEY,
  at_timestamp      TEXT NOT NULL,
  classifier_model  TEXT NOT NULL,
  sha256_classified TEXT NOT NULL,
  input_tokens      INTEGER NOT NULL,
  output_tokens     INTEGER NOT NULL,
  cents_spent       INTEGER NOT NULL,
  notes             TEXT,
  CONSTRAINT budget_cents_chk      CHECK (cents_spent >= 0),
  CONSTRAINT budget_input_chk      CHECK (input_tokens >= 0),
  CONSTRAINT budget_output_chk     CHECK (output_tokens >= 0),
  CONSTRAINT budget_sha_chk        CHECK (length(sha256_classified) = 64
                                           OR sha256_classified = 'preflight')
);
CREATE INDEX IF NOT EXISTS idx_budget_ledger_at ON budget_ledger(at_timestamp);

-- =========================================================================
-- Explicit grants on existing objects (defensive; default privileges above
-- handle NEW objects, these cover tables just created in this transaction).
-- =========================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA lava_impact TO app_user1;
GRANT SELECT ON ALL TABLES IN SCHEMA lava_impact TO ro_user1;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA lava_impact TO app_user1;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA lava_impact TO ro_user1;

-- =========================================================================
-- Record this migration
-- =========================================================================
INSERT INTO schema_version (version, name)
  VALUES (1, 'initial_schema')
  ON CONFLICT (version) DO NOTHING;

COMMIT;
