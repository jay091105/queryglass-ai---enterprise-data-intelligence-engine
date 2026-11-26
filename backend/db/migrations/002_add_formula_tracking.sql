-- Migration 002: Add formula tracking columns for reliability evaluation integrity
-- Purpose: Track formula version and store complete evaluation evidence to prevent silent recalculation

-- Add formula version tracking
ALTER TABLE _queryglass_query_runs ADD COLUMN formula_version TEXT DEFAULT '1.0.0';

-- Add dimension scores JSON storage
ALTER TABLE _queryglass_query_runs ADD COLUMN dimension_scores_json TEXT;

-- Add evaluated coverage (percentage of dimensions evaluated)
ALTER TABLE _queryglass_query_runs ADD COLUMN evaluated_coverage INTEGER;

-- Add accuracy score (sum of dimension scores)
ALTER TABLE _queryglass_query_runs ADD COLUMN accuracy_score INTEGER;

-- Add hallucination risk score
ALTER TABLE _queryglass_query_runs ADD COLUMN hallucination_risk_score INTEGER;

-- Create index for formula version queries
CREATE INDEX IF NOT EXISTS idx_query_runs_formula_version ON _queryglass_query_runs(formula_version);

-- NOTE: _queryglass_reliability_history table is used for reliability test cases and experiments
-- It is NOT the authoritative source for production query execution history
-- Use _queryglass_query_runs as the single source of truth for all statistics and historical queries

-- Rollback script (save for emergency use):
-- DROP INDEX IF EXISTS idx_query_runs_formula_version;
-- ALTER TABLE _queryglass_query_runs DROP COLUMN hallucination_risk_score;
-- ALTER TABLE _queryglass_query_runs DROP COLUMN accuracy_score;
-- ALTER TABLE _queryglass_query_runs DROP COLUMN evaluated_coverage;
-- ALTER TABLE _queryglass_query_runs DROP COLUMN dimension_scores_json;
-- ALTER TABLE _queryglass_query_runs DROP COLUMN formula_version;
