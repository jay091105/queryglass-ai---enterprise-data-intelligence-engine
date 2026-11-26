export type ExecutionStatus = "success" | "syntax_error" | "runtime_error" | "timeout";
export type ResultGrounding = "well_grounded" | "partially_grounded" | "ungrounded";

export interface SchemaCheck {
  check_name: string;
  passed: boolean;
  details?: string | null;
  affected_objects: string[];
}

export interface ConstraintValidation {
  constraint_type: string;
  passed: boolean;
  details?: string | null;
  affected_columns: string[];
}

export interface TypeCompatibility {
  check_type: string;
  passed: boolean;
  schema_type?: string | null;
  effective_type?: string | null;
  operation?: string | null;
  details?: string | null;
}

export interface ValidationEvidence {
  schema_checks: SchemaCheck[];
  constraint_validations: ConstraintValidation[];
  type_compatibility: TypeCompatibility[];
}

export interface ResultVerificationContract {
  execution_status: ExecutionStatus;
  result_grounding: ResultGrounding;
  validation_evidence: ValidationEvidence;
  reliability_score: number;
  database_source?: string | null;
  actual_columns?: string[];
  actual_column_count?: number | null;
  actual_row_count?: number | null;
  total_result_count?: number | null;
  truncated?: boolean | null;
  execution_time_ms?: number | null;
  grounding_evidence_status?: "verified" | "insufficient" | "failed";
}

export interface QueryRun {
  query_id: string;
  user_question: string;
  generated_sql: string;
  execution_status: ExecutionStatus;
  result_grounding: ResultGrounding;
  validation_evidence: ValidationEvidence;
  reliability_score: number;
  execution_timestamp: string;
  table_name?: string | null;
  row_count: number;
  execution_time_ms: number;
  error_message?: string | null;
}

export interface UnsupportedRequest {
  type: string;
  description: string;
  mentioned_object?: string | null;
  suggestion: string;
}

export interface QuestionPlan {
  request_types: Array<{ type: string; confidence: number; parameters: Record<string, unknown> }>;
  mentioned_tables: string[];
  mentioned_columns: string[];
  unsupported_requests: UnsupportedRequest[];
}
