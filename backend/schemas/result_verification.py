"""Result verification contract schemas for query execution reliability tracking."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class ExecutionStatus(str, Enum):
    """Technical outcome of query execution."""
    SUCCESS = "success"
    SYNTAX_ERROR = "syntax_error"
    RUNTIME_ERROR = "runtime_error"
    TIMEOUT = "timeout"


class ResultGrounding(str, Enum):
    """Semantic quality of result relative to user question."""
    WELL_GROUNDED = "well_grounded"
    PARTIALLY_GROUNDED = "partially_grounded"
    UNGROUNDED = "ungrounded"


class SchemaCheck(BaseModel):
    """Individual schema validation check result."""
    check_name: str
    passed: bool
    details: Optional[str] = None
    affected_objects: List[str] = Field(default_factory=list)


class ConstraintValidation(BaseModel):
    """Constraint validation check result."""
    constraint_type: str  # "foreign_key", "null_handling", "aggregation_correctness"
    passed: bool
    details: Optional[str] = None
    affected_columns: List[str] = Field(default_factory=list)


class TypeCompatibility(BaseModel):
    """Type compatibility check result."""
    check_type: str  # "schema_type_match", "effective_type_match", "operation_validity"
    passed: bool
    schema_type: Optional[str] = None
    effective_type: Optional[str] = None
    operation: Optional[str] = None
    details: Optional[str] = None


class ValidationEvidence(BaseModel):
    """Complete validation evidence structure."""
    schema_checks: List[SchemaCheck] = Field(default_factory=list)
    constraint_validations: List[ConstraintValidation] = Field(default_factory=list)
    type_compatibility: List[TypeCompatibility] = Field(default_factory=list)
    
    @property
    def all_passed(self) -> bool:
        """Check if all validations passed."""
        return (
            all(check.passed for check in self.schema_checks) and
            all(check.passed for check in self.constraint_validations) and
            all(check.passed for check in self.type_compatibility)
        )
    
    @property
    def critical_failures(self) -> List[str]:
        """Get list of critical validation failures."""
        failures = []
        for check in self.schema_checks:
            if not check.passed:
                failures.append(f"Schema: {check.check_name}")
        for check in self.constraint_validations:
            if not check.passed:
                failures.append(f"Constraint: {check.constraint_type}")
        for check in self.type_compatibility:
            if not check.passed:
                failures.append(f"Type: {check.check_type}")
        return failures


class ResultVerificationContract(BaseModel):
    """Unified contract for query result verification.
    
    This structure represents the complete verification state of a query
    execution, including technical execution status, semantic grounding,
    detailed validation evidence, and computed reliability score.
    """
    execution_status: ExecutionStatus
    result_grounding: ResultGrounding
    validation_evidence: ValidationEvidence
    reliability_score: float = Field(ge=0.0, le=1.0)
    database_source: Optional[str] = None
    actual_columns: List[str] = Field(default_factory=list)
    actual_column_count: Optional[int] = None
    actual_row_count: Optional[int] = None
    total_result_count: Optional[int] = None
    truncated: Optional[bool] = None
    execution_time_ms: Optional[float] = None
    grounding_evidence_status: str = "not_evaluated"
    
    @field_validator('reliability_score')
    @classmethod
    def round_score(cls, v: float) -> float:
        """Round reliability score to 2 decimal places."""
        return round(v, 2)
    
    def to_json_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dictionary."""
        return {
            "execution_status": self.execution_status.value,
            "result_grounding": self.result_grounding.value,
            "validation_evidence": {
                "schema_checks": [
                    {
                        "check_name": check.check_name,
                        "passed": check.passed,
                        "details": check.details,
                        "affected_objects": check.affected_objects
                    }
                    for check in self.validation_evidence.schema_checks
                ],
                "constraint_validations": [
                    {
                        "constraint_type": cv.constraint_type,
                        "passed": cv.passed,
                        "details": cv.details,
                        "affected_columns": cv.affected_columns
                    }
                    for cv in self.validation_evidence.constraint_validations
                ],
                "type_compatibility": [
                    {
                        "check_type": tc.check_type,
                        "passed": tc.passed,
                        "schema_type": tc.schema_type,
                        "effective_type": tc.effective_type,
                        "operation": tc.operation,
                        "details": tc.details
                    }
                    for tc in self.validation_evidence.type_compatibility
                ]
            },
            "reliability_score": self.reliability_score,
            "database_source": self.database_source,
            "actual_columns": self.actual_columns,
            "actual_column_count": self.actual_column_count,
            "actual_row_count": self.actual_row_count,
            "total_result_count": self.total_result_count,
            "truncated": self.truncated,
            "execution_time_ms": self.execution_time_ms,
            "grounding_evidence_status": self.grounding_evidence_status,
        }
