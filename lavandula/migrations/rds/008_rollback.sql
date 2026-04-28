-- DO NOT RUN unless code has been rolled back to pre-spec-0024 state.
-- 008_rollback.sql
-- Reverses 008_rename_reports_to_corpus.sql.
BEGIN;

SET LOCAL lock_timeout = '5s';

-- Reverse view rename (must happen before table rename)
ALTER VIEW lava_impact.corpus_public RENAME TO reports_public;

-- Reverse table rename
ALTER TABLE lava_impact.corpus RENAME TO reports;

-- Reverse constraint renames (21 total: 1 pkey + 20 check)
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_pkey TO reports_pkey;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_sha_len_chk TO reports_sha_len_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_size_chk TO reports_size_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_ct_chk TO reports_ct_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_disc_chk TO reports_disc_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_platform_chk TO reports_platform_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_class_chk TO reports_class_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_conf_chk TO reports_conf_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_attr_chk TO reports_attr_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_redirect_chk TO reports_redirect_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_js_chk TO reports_js_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_launch_chk TO reports_launch_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_embed_chk TO reports_embed_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_uri_chk TO reports_uri_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_fpt_len_chk TO reports_fpt_len_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_creator_chk TO reports_creator_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_producer_chk TO reports_producer_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_year_src_chk TO reports_year_src_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_mt_chk TO reports_mt_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_mg_chk TO reports_mg_chk;
ALTER TABLE lava_impact.reports RENAME CONSTRAINT corpus_et_chk TO reports_et_chk;

-- Reverse index renames
ALTER INDEX lava_impact.idx_corpus_ein RENAME TO idx_reports_ein;
ALTER INDEX lava_impact.idx_corpus_classification RENAME TO idx_reports_classification;
ALTER INDEX lava_impact.idx_corpus_year RENAME TO idx_reports_year;
ALTER INDEX lava_impact.idx_corpus_platform RENAME TO idx_reports_platform;
ALTER INDEX lava_impact.idx_corpus_discovered_via RENAME TO idx_reports_discovered_via;
ALTER INDEX lava_impact.idx_corpus_material_type RENAME TO idx_reports_material_type;
ALTER INDEX lava_impact.idx_corpus_material_group RENAME TO idx_reports_material_group;
ALTER INDEX lava_impact.idx_corpus_event_type RENAME TO idx_reports_event_type;

COMMIT;
