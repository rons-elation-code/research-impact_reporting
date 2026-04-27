-- 008_rename_reports_to_corpus.sql
-- ONE-SHOT migration. Run once in PGAdmin. Rolls back on any error.
-- DO NOT RUN without completing preflight checks above.
BEGIN;

SET LOCAL lock_timeout = '5s';

-- 1. Rename table
ALTER TABLE lava_impact.reports RENAME TO corpus;

-- 2. Rename constraints (20 total: 17 from 001 + 3 from 007)
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_sha_len_chk TO corpus_sha_len_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_size_chk TO corpus_size_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_ct_chk TO corpus_ct_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_disc_chk TO corpus_disc_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_platform_chk TO corpus_platform_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_class_chk TO corpus_class_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_conf_chk TO corpus_conf_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_attr_chk TO corpus_attr_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_redirect_chk TO corpus_redirect_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_js_chk TO corpus_js_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_launch_chk TO corpus_launch_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_embed_chk TO corpus_embed_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_uri_chk TO corpus_uri_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_fpt_len_chk TO corpus_fpt_len_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_creator_chk TO corpus_creator_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_producer_chk TO corpus_producer_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_year_src_chk TO corpus_year_src_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_mt_chk TO corpus_mt_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_mg_chk TO corpus_mg_chk;
ALTER TABLE lava_impact.corpus RENAME CONSTRAINT reports_et_chk TO corpus_et_chk;

-- 3. Rename indexes (8 total: 4 from 001 + 1 from 005 + 3 from 007)
ALTER INDEX lava_impact.idx_reports_ein RENAME TO idx_corpus_ein;
ALTER INDEX lava_impact.idx_reports_classification RENAME TO idx_corpus_classification;
ALTER INDEX lava_impact.idx_reports_year RENAME TO idx_corpus_year;
ALTER INDEX lava_impact.idx_reports_platform RENAME TO idx_corpus_platform;
ALTER INDEX lava_impact.idx_reports_discovered_via RENAME TO idx_corpus_discovered_via;
ALTER INDEX lava_impact.idx_reports_material_type RENAME TO idx_corpus_material_type;
ALTER INDEX lava_impact.idx_reports_material_group RENAME TO idx_corpus_material_group;
ALTER INDEX lava_impact.idx_reports_event_type RENAME TO idx_corpus_event_type;

-- 4. Rename view (preserves owner, grants, and filtering semantics automatically)
ALTER VIEW lava_impact.reports_public RENAME TO corpus_public;

COMMIT;
