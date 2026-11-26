from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from pydantic import BaseModel, Field

from .result_verification import ExecutionStatus, ResultGrounding, ResultVerificationContract


class QueryRun(BaseModel):
    query_id: str = Field(default_factory=lambda: str(uuid4()))
    user_question: str
    generated_sql: str
    execution_status: ExecutionStatus
    result_grounding: ResultGrounding
    validation_evidence_json: dict = Field(default_factory=dict)
    reliability_score: float = Field(ge=0.0, le=1.0)
    execution_timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    table_name: Optional[str] = None
    row_count: int = 0
    execution_time_ms: float = 0.0
    error_message: Optional[str] = None
    dataset_id: Optional[str] = None
    executed_sql: Optional[str] = None
    question_plan_json: dict = Field(default_factory=dict)
    fingerprint_hash: Optional[str] = None
    effective_types_json: dict = Field(default_factory=dict)
    provider_model: Optional[str] = None
    truncation_status: Optional[str] = None
    reliability_id: Optional[str] = None
    validation_json: dict = Field(default_factory=dict)
    result_verification_json: dict = Field(default_factory=dict)
    formula_version: str = Field(default="1.0.0")
    dimension_scores_json: Optional[str] = Field(default=None)
    evaluated_coverage: Optional[int] = Field(default=None)
    accuracy_score: Optional[int] = Field(default=None)
    hallucination_risk_score: Optional[int] = Field(default=None)

    @classmethod
    def from_verification(
        cls,
        verification: ResultVerificationContract,
        question: str,
        sql: str,
        table_name: Optional[str] = None,
        row_count: int = 0,
        execution_time_ms: float = 0.0,
        error_message: Optional[str] = None,
        dataset_id: Optional[str] = None,
        executed_sql: Optional[str] = None,
        question_plan_json: Optional[dict] = None,
        fingerprint_hash: Optional[str] = None,
        effective_types_json: Optional[dict] = None,
        provider_model: Optional[str] = None,
        truncation_status: Optional[str] = None,
        reliability_id: Optional[str] = None,
        validation_json: Optional[dict] = None,
        result_verification_json: Optional[dict] = None,
    ) -> "QueryRun":
        return cls(
            user_question=question,
            generated_sql=sql,
            execution_status=verification.execution_status,
            result_grounding=verification.result_grounding,
            validation_evidence_json=verification.validation_evidence.model_dump(),
            reliability_score=verification.reliability_score,
            table_name=table_name,
            row_count=row_count,
            execution_time_ms=execution_time_ms,
            error_message=error_message,
            dataset_id=dataset_id,
            executed_sql=executed_sql,
            question_plan_json=question_plan_json or {},
            fingerprint_hash=fingerprint_hash,
            effective_types_json=effective_types_json or {},
            provider_model=provider_model,
            truncation_status=truncation_status,
            reliability_id=reliability_id,
            validation_json=validation_json or {},
            result_verification_json=result_verification_json or {},
        )
