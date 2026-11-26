CREATE TABLE IF NOT EXISTS _queryglass_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    question TEXT,
    sql_query TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INTEGER DEFAULT 0,
    execution_time_ms REAL DEFAULT 0.0,
    error_message TEXT DEFAULT ''
);

-- Dataset ingestion workflow registry. Each source identity may have a
-- committed raw upload and at most one current processed output. Replacements
-- are staged per run and published atomically after preprocessing succeeds.
CREATE TABLE IF NOT EXISTS _queryglass_datasets (
    dataset_id TEXT PRIMARY KEY,
    dataset_identity TEXT,
    filename TEXT NOT NULL,
    raw_table TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    columns_meta TEXT NOT NULL,
    status TEXT NOT NULL,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_queryglass_datasets_status ON _queryglass_datasets(status);

CREATE TABLE IF NOT EXISTS _queryglass_dataset_identities (
    identity_id TEXT PRIMARY KEY,
    normalized_filename TEXT NOT NULL,
    active_dataset_id TEXT,
    processing_dataset_id TEXT
);

CREATE TABLE IF NOT EXISTS _queryglass_active_dataset (
    active_key TEXT PRIMARY KEY CHECK (active_key = 'active'),
    dataset_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS _queryglass_processing_runs (
    run_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    operation_config TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    rows_before INTEGER,
    cols_before INTEGER,
    rows_after INTEGER,
    cols_after INTEGER,
    staged_table TEXT,
    transformations TEXT,
    error_details TEXT,
    finalized INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queryglass_runs_dataset ON _queryglass_processing_runs(dataset_id, started_at);

CREATE TABLE IF NOT EXISTS _queryglass_processed_versions (
    version_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    table_name TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL,
    column_count INTEGER NOT NULL,
    operation_config TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queryglass_versions_dataset ON _queryglass_processed_versions(dataset_id, created_at);

CREATE TABLE IF NOT EXISTS _queryglass_quality_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id TEXT NOT NULL,
    run_id TEXT,
    stage TEXT NOT NULL,
    metrics TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queryglass_reports_dataset ON _queryglass_quality_reports(dataset_id, stage, report_id);

-- Reliability evaluations are kept separately from query execution history.
CREATE TABLE IF NOT EXISTS _queryglass_reliability_history (
    evaluation_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    question TEXT NOT NULL,
    table_name TEXT NOT NULL,
    generated_sql TEXT NOT NULL,
    provider_model TEXT,
    source TEXT NOT NULL,
    is_test_case INTEGER NOT NULL DEFAULT 0,
    hallucination_case INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL,
    accuracy_score INTEGER,
    risk_score INTEGER,
    validation_json TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    execution_metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_queryglass_reliability_created
    ON _queryglass_reliability_history(created_at);
