CREATE TABLE IF NOT EXISTS _queryglass_query_runs (
    query_id TEXT PRIMARY KEY,
    user_question TEXT NOT NULL,
    generated_sql TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    result_grounding TEXT NOT NULL,
    validation_evidence_json TEXT NOT NULL,
    reliability_score REAL NOT NULL CHECK (reliability_score >= 0.0 AND reliability_score <= 1.0),
    execution_timestamp TEXT NOT NULL,
    table_name TEXT,
    row_count INTEGER DEFAULT 0,
    execution_time_ms REAL DEFAULT 0,
    error_message TEXT,
    dataset_id TEXT,
    executed_sql TEXT,
    question_plan_json TEXT,
    fingerprint_hash TEXT,
    effective_types_json TEXT,
    provider_model TEXT,
    truncation_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_query_runs_timestamp ON _queryglass_query_runs(execution_timestamp);
CREATE INDEX IF NOT EXISTS idx_query_runs_status ON _queryglass_query_runs(execution_status);
CREATE INDEX IF NOT EXISTS idx_query_runs_grounding ON _queryglass_query_runs(result_grounding);
CREATE INDEX IF NOT EXISTS idx_query_runs_table ON _queryglass_query_runs(table_name);

CREATE TABLE IF NOT EXISTS _queryglass_table_fingerprints (
    fingerprint_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    dataset_id TEXT,
    fingerprint_hash TEXT NOT NULL,
    structural_hash TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    content_signature_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(table_name, dataset_id)
);

CREATE TABLE IF NOT EXISTS _queryglass_effective_types (
    profile_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    dataset_id TEXT,
    columns_json TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    UNIQUE(table_name, dataset_id)
);
