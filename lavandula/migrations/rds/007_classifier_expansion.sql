-- Migration: 007_classifier_expansion
-- Date: 2026-04-26
-- Spec: 0023 (Classifier Expansion - Full Taxonomy Labels)
-- Target: PostgreSQL (RDS lava_prod1), schema lava_impact

BEGIN;

DO $before$
BEGIN
  RAISE NOTICE '------ BEFORE ------';
  RAISE NOTICE 'reports columns: %', (
    SELECT string_agg(column_name, ', ' ORDER BY ordinal_position)
    FROM information_schema.columns
    WHERE table_schema = 'lava_impact' AND table_name = 'reports'
  );
END $before$;

-- Add columns
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS material_type TEXT;
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS material_group TEXT;
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS event_type TEXT;
ALTER TABLE lava_impact.reports ADD COLUMN IF NOT EXISTS reasoning TEXT;

-- CHECK constraints (derived from collateral_taxonomy.yaml)
ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_mt_chk
  CHECK (material_type IS NULL OR material_type IN (
    'annual_letter',
    'annual_report',
    'appeal_insert',
    'appeal_letter',
    'appeal_outer_envelope',
    'appeal_reply_device',
    'auction_lot_display_card',
    'bequest_guide',
    'bib_design',
    'bid_paddle',
    'campaign_case_for_support',
    'campaign_case_statement',
    'campaign_gift_opportunities_menu',
    'campaign_groundbreaking_piece',
    'campaign_identity_package',
    'campaign_launch_package',
    'campaign_master_brochure',
    'campaign_pledge_form',
    'campaign_progress_update',
    'campaign_prospectus',
    'community_benefit_report',
    'course_map',
    'cultivation_piece',
    'digital_appeal',
    'donor_acknowledgment',
    'donor_deck',
    'donor_impact_report',
    'donor_newsletter',
    'endowed_fund_report',
    'event_announcement',
    'event_invitation',
    'event_program',
    'event_signage',
    'feasibility_study_report',
    'financial_report',
    'finisher_certificate',
    'fund_a_need_graphic',
    'fundraising_campaign_brochure',
    'gift_vehicle_one_pager',
    'giving_society_material',
    'grateful_patient_appeal',
    'hole_sponsor_sign',
    'honoree_tribute_page',
    'impact_report',
    'legacy_society_newsletter',
    'live_auction_catalog',
    'magazine',
    'major_gift_proposal',
    'member_welcome_kit',
    'membership_acquisition_brochure',
    'membership_renewal_notice',
    'menu_card',
    'name_badge',
    'named_fund_brochure',
    'not_relevant',
    'online_auction_microsite_design',
    'other_collateral',
    'pairing_sheet',
    'parent_fund_appeal',
    'participant_welcome_pack',
    'patron_recognition_wall_artwork',
    'physician_referral_to_philanthropy',
    'place_card',
    'planned_giving_brochure',
    'planned_giving_newsletter',
    'pledge_form',
    'program_brochure',
    'program_newsletter',
    'response_card',
    'reunion_giving_piece',
    'rsvp_reply_card',
    'save_the_date',
    'seating_chart',
    'silent_auction_materials',
    'sponsor_benefits_sheet',
    'sponsor_prospectus',
    'table_card',
    'team_fundraising_kit',
    'tee_gift_card',
    'tribute_journal',
    'viewbook',
    'year_in_review'
  ));

ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_mg_chk
  CHECK (material_group IS NULL OR material_group IN (
    'appeals',
    'auction',
    'campaign',
    'day_of_event',
    'invitations',
    'major_gifts',
    'membership',
    'other',
    'peer_to_peer',
    'periodic',
    'planned_giving',
    'program_services',
    'programs_journals',
    'reports',
    'sector_specific',
    'sponsorship',
    'stewardship'
  ));

ALTER TABLE lava_impact.reports ADD CONSTRAINT reports_et_chk
  CHECK (event_type IS NULL OR event_type IN (
    'auction_event',
    'ball',
    'benefit_event',
    'breakfast_fundraiser',
    'cocktail_reception',
    'derby_polo_regatta',
    'dinner_fundraiser',
    'fashion_show',
    'food_wine_event',
    'gala',
    'golf_tournament',
    'luncheon',
    'radiothon',
    'ride_event',
    'telethon',
    'walk_run_event'
  ));

-- Indexes
CREATE INDEX IF NOT EXISTS idx_reports_material_type
  ON lava_impact.reports(material_type);
CREATE INDEX IF NOT EXISTS idx_reports_material_group
  ON lava_impact.reports(material_group);
CREATE INDEX IF NOT EXISTS idx_reports_event_type
  ON lava_impact.reports(event_type) WHERE event_type IS NOT NULL;

-- Update reports_public view
CREATE OR REPLACE VIEW lava_impact.reports_public AS
  SELECT content_sha256, source_org_ein, hosting_platform,
         attribution_confidence,
         archived_at, file_size_bytes, page_count,
         classification, classification_confidence,
         material_type, material_group, event_type,
         report_year, report_year_source,
         pdf_has_javascript, pdf_has_launch, pdf_has_embedded
  FROM lava_impact.reports
  WHERE attribution_confidence IN ('own_domain','platform_verified','wayback_archive')
    AND (
      (material_type IS NOT NULL AND material_type != 'not_relevant')
      OR
      (material_type IS NULL AND classification IS NOT NULL AND classification != 'not_a_report')
    )
    AND COALESCE(classification_confidence, 0) >= 0.8
    AND pdf_has_javascript = 0
    AND pdf_has_launch = 0
    AND pdf_has_embedded = 0;

DO $after$
BEGIN
  RAISE NOTICE '------ AFTER ------';
  RAISE NOTICE 'reports columns: %', (
    SELECT string_agg(column_name, ', ' ORDER BY ordinal_position)
    FROM information_schema.columns
    WHERE table_schema = 'lava_impact' AND table_name = 'reports'
  );
END $after$;

COMMIT;
