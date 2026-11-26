import type { ResultVerificationContract as ReliabilityResultVerificationContract } from "@/types/reliability";

export type Dataset = {
  name: string;
  rows: number;
  schema?: Schema;
};

export type Column = {
  name: string;
  type?: string;
  is_pk?: boolean;
  not_null?: boolean;
  is_unique?: boolean;
  is_fk?: boolean;
  default_value?: string | null;
};

export type ForeignKey = {
  from: string;
  table: string;
  to: string;
};

export type Schema = {
  table: string;
  rows: number;
  columns: Column[];
  foreign_keys?: ForeignKey[];
  indexes_details?: Array<Record<string, unknown>>;
};

export type DatasetRowsResponse = {
  table: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  total_rows: number;
  offset: number;
  limit: number;
  has_more: boolean;
};

export type HistoryRecord = {
  id: number;
  timestamp: string;
  question: string;
  sql_query: string;
  status: string;
  row_count: number;
  execution_time_ms: number;
  error_message?: string;
};

export type HistoryResponse = {
  items: HistoryRecord[];
  stats: {
    total_queries: number;
    success_count: number;
    blocked_count: number;
    failed_count: number;
    avg_ms: number;
    success_rate: number;
  };
};

export type ReliabilityIssue = {
  type?: string;
  message?: string;
  missing_object?: string | null;
  original_requirement?: string | null;
  responsible_sql_section?: string | null;
  recommended_correction?: string | null;
  [key: string]: unknown;
};

export type ReliabilityDimension = {
  weight: number;
  score: number | null;
  status: string;
};

export type ResultVerification = {
  database_source?: string | null;
  actual_columns?: string[];
  actual_column_count?: number | null;
  actual_row_count?: number | null;
  total_result_count?: number | null;
  truncated?: boolean;
  null_counts?: Record<string, number>;
  [key: string]: unknown;
};

export type ResultVerificationContract = ReliabilityResultVerificationContract;

export type ReliabilityReport = {
  answer_accuracy_score: number | null;
  hallucination_risk_score: number | null;
  hallucination_risk_level: string;
  critical_failure: boolean;
  evaluation_stage?: string;
  dimensions: Record<string, ReliabilityDimension>;
  validation_status: string;
  execution_status: string;
  result_verification?: ResultVerification | null;
  evaluated_weight: number | null;
  issues: ReliabilityIssue[];
};

export type ValidationResult = {
  valid: boolean;
  sql_present?: boolean;
  syntax_valid: boolean | null;
  schema_valid: boolean | null;
  operation: string;
  safety: string;
  error_category?: string | null;
  confidence_label?: string;
  confidence_reasons?: string[];
  reliability_id?: string | null;
  reliability?: ReliabilityReport | null;
  validation_checks?: {
    syntax?: unknown;
    read_only_safety?: unknown;
    schema?: unknown;
    semantic?: unknown;
    result?: unknown;
  } | null;
  semantic_validation?: {
    status?: string;
    requirements?: unknown[];
    issues?: ReliabilityIssue[];
  } | null;
  audit?: Record<string, unknown>;
  hallucination?: {
    is_valid?: boolean;
    errors?: string[];
    warnings?: string[];
    invalid_ctes?: string[];
  };
  pipeline?: Record<string, unknown>;
};

export type QueryResult = {
  status: string;
  columns?: string[];
  rows?: Record<string, unknown>[];
  row_count?: number;
  duration_ms?: number;
  truncated?: boolean;
  validation?: ValidationResult;
  reliability_id?: string | null;
  reliability?: ReliabilityReport | null;
  result_verification?: ResultVerification | null;
  verification?: ResultVerificationContract | null;
  result_verification_contract?: ResultVerificationContract | null;
  trace_query_id?: string | null;
};

export type ReliabilityTest = {
  id: string;
  category: string;
  input: string;
  expected_behavior: string;
  must_reject: boolean;
};

export type ReliabilityTestResult = {
  test_id: string;
  category: string;
  test_input: string;
  expected_behavior: string;
  actual_response: string;
  generated_sql: string | null;
  schema_validation_result: unknown;
  semantic_validation_result: unknown;
  execution_status: string | null;
  result_verification: ResultVerification | null;
  accuracy_score: number | null;
  hallucination_risk_score: number | null;
  hallucination_risk_level: string | null;
  status: string;
};

export type ReliabilityTestRun = {
  results: ReliabilityTestResult[];
  passed: number;
  failed: number;
  total: number;
};

export type AiReport = {
  report_id: string;
  query_run: Record<string, unknown>;
  validation: Record<string, unknown> | null;
  semantic_validation: Record<string, unknown> | null;
  reliability: ReliabilityReport | null;
  overall_verification_status?: string | null;
  issues: unknown[];
  requirements: unknown;
  requirement_checks: Array<{ category: string; requirement: unknown; status: "verified" | "not_verified"; reason?: string | null }>;
  scoring_methodology: Record<string, unknown>;
  result_verification: Record<string, unknown> | null;
  verification: Record<string, unknown> | null;
  reliability_id?: string | null;
  metadata_available: boolean;
};

export type ReliabilitySummary = {
  queries_evaluated: number;
  queries_passed: number;
  queries_rejected: number;
  average_accuracy_score: number | null;
  test_cases_run: number;
  hallucination_detection_rate: number | null;
  most_frequent_failure_categories: unknown;
  score_trends: unknown;
  recent_evaluations: unknown[];
};

export type DashboardResponse = {
  database: {
    exists: boolean;
    sqlite_version: string;
    size_mb: number;
    tables: string[];
  };
  tables: Array<{ name: string; rows: number; columns: number }>;
  active_dataset?: ActiveDataset | null;
  history: HistoryResponse["stats"];
  recent_queries: HistoryRecord[];
};

export type ActiveDataset = {
  dataset_id: string;
  dataset_identity: string;
  filename: string;
  raw_table: string;
  created_at: string;
  row_count: number;
  column_count: number;
  status: string;
};


export type SettingsResponse = {
  backend: string;
  database_exists: boolean;
  provider_configured: boolean;
  model: string;
  secrets_sent_to_browser: boolean;
  limits: {
    upload_bytes: number;
    query_timeout_seconds: number;
    result_preview_rows: number;
  };
};

export type QualityMetrics = {
  rows: number;
  columns: number;
  total_cells: number;
  missing_cells: number;
  missing_pct: number;
  rows_with_missing: number;
  missing_by_column: Record<string, { missing_count: number; missing_pct: number }>;
  duplicate_rows: number;
  duplicate_pct: number;
  duplicate_groups: number;
  duplicate_rows_involved: number;
  duplicate_criteria: string;
  key_duplicates?: { columns: string[]; extra_rows: number; criteria: string } | null;
  invalid_values: number;
  type_mismatches: { total: number; by_column: Record<string, { dominant_type: string; mismatch_count: number }> };
  invalid_definition: string;
  outliers: { total: number; method: string; by_column: Record<string, { outlier_count: number; lower_bound: number; upper_bound: number }> };
  constant_columns: string[];
  empty_rows: number;
  empty_columns: string[];
  parsing_errors: number;
  parsing_errors_note: string;
  total_issues: number;
  issue_counting_rules: string;
};

export type QualityReport = {
  stage: string;
  created_at: string;
  metrics: QualityMetrics;
};

export type ComparisonRow = { metric: string; before: number | null; after: number | null; delta: number | null };

export type Comparison = {
  rows: ComparisonRow[];
  total_issues_before: number | null;
  total_issues_after: number | null;
  improved: boolean;
  counting_rules?: string;
};

export type ProcessingRun = {
  run_id: string;
  dataset_id: string;
  status: string;
  options: Record<string, unknown>;
  started_at: string;
  completed_at: string | null;
  rows_before: number | null;
  cols_before: number | null;
  rows_after: number | null;
  cols_after: number | null;
  transformations: Array<{ category: string; description: string; count?: number }> | null;
  error_details: string | null;
  finalized: boolean;
};

export type ProcessedVersion = {
  version_id: string;
  dataset_id: string;
  run_id: string;
  table_name: string;
  row_count: number;
  column_count: number;
  operation_config: Record<string, unknown>;
  created_at: string;
};

export type WorkflowDataset = {
  dataset_id: string;
  dataset_identity: string;
  filename: string;
  raw_table: string;
  created_at: string;
  row_count: number;
  column_count: number;
  columns: Array<{ name: string; dtype: string }>;
  status: string;
  error_message: string | null;
  raw_report: QualityReport | null;
  processed_report: QualityReport | null;
  runs: ProcessingRun[];
  latest_run: ProcessingRun | null;
  versions: ProcessedVersion[];
  allowed_actions: { preprocess: boolean; store_processed: boolean; raw_available: boolean };
  comparison?: Comparison;
};

export type PreprocessRule = {
  id: string;
  code: string;
  title: string;
  description: string;
  impactText: string;
  category: string;
  option_key: string;
  type: "toggle" | "select";
  default: boolean | string;
  choices?: Array<{ value: string; label: string }>;
};
