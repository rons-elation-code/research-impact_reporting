-- Migration 009: Add classifier_definition column + formalize corpus_class_chk
--
-- Prerequisite: Must be applied before deploying Spec 0025 code.
-- The new code writes classifier_definition on every classification attempt.

ALTER TABLE lava_corpus.corpus ADD COLUMN IF NOT EXISTS classifier_definition TEXT;

ALTER TABLE lava_corpus.corpus DROP CONSTRAINT IF EXISTS corpus_class_chk;
ALTER TABLE lava_corpus.corpus ADD CONSTRAINT corpus_class_chk
  CHECK (classification IS NULL OR classification IN
         ('annual','impact','hybrid','other','not_a_report','skipped','parse_error'));
