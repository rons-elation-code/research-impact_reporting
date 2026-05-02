-- Migration 012: Phone enrichment columns (Spec 0031)
ALTER TABLE lava_corpus.nonprofits_seed ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE lava_corpus.nonprofits_seed ADD COLUMN IF NOT EXISTS phone_source TEXT;
