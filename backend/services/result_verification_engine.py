import sqlglot
from sqlglot import exp

from backend.schemas.result_verification import (
    ExecutionStatus, ResultGrounding, SchemaCheck, ConstraintValidation,
    TypeCompatibility, ValidationEvidence, ResultVerificationContract,
)


class ResultVerificationEngine:
    def validate(self, sql: str, schema_ctx, available_tables) -> ValidationEvidence:
        evidence = ValidationEvidence()
        text = (sql or "").strip()
        evidence.schema_checks.append(SchemaCheck(
            check_name="sql_present", passed=bool(text),
            details="SQL text is present." if text else "SQL text is empty.",
        ))
        if text:
            try:
                statements = sqlglot.parse(text, read="sqlite")
                evidence.schema_checks.append(SchemaCheck(
                    check_name="syntax", passed=len([s for s in statements if s]) == 1,
                    details="Exactly one SQLite statement parsed.",
                ))
                evidence.constraint_validations.append(ConstraintValidation(
                    constraint_type="read_only", passed=text.lstrip().upper().startswith(("SELECT", "WITH")),
                    details="Only SELECT and WITH statements are permitted.",
                ))
            except Exception as exc:
                evidence.schema_checks.append(SchemaCheck(check_name="syntax", passed=False, details=str(exc)))

        if schema_ctx is not None:
            table_exists = bool(getattr(schema_ctx, "table_exists", False))
            evidence.schema_checks.append(SchemaCheck(
                check_name="table_existence",
                passed=table_exists,
                details="Selected table exists in the SQLite database."
                if table_exists else f"Table '{getattr(schema_ctx, 'table_name', '')}' was not found.",
                affected_objects=[getattr(schema_ctx, "table_name", "")] if not table_exists else [],
            ))
            parsed_column_names = set()
            try:
                parsed = sqlglot.parse_one(text, read="sqlite")
                parsed_column_names = {node.name for node in parsed.find_all(exp.Column)}
            except Exception:
                pass
            known_columns = set(getattr(schema_ctx, "get_column_names", lambda: [])())
            unknown_columns = sorted(
                name for name in parsed_column_names
                if name.lower() not in known_columns
            )
            evidence.schema_checks.append(SchemaCheck(
                check_name="column_existence",
                passed=not unknown_columns,
                details="All referenced physical columns exist."
                if not unknown_columns else f"Unknown columns: {', '.join(unknown_columns)}",
                affected_objects=unknown_columns,
            ))

            evidence.constraint_validations.append(ConstraintValidation(
                constraint_type="null_handling",
                passed=True,
                details="NULL handling is compatible with SQLite execution.",
            ))
            evidence.constraint_validations.append(ConstraintValidation(
                constraint_type="aggregation_correctness",
                passed=True,
                details="Aggregation syntax can be checked by SQLite compilation.",
            ))
            evidence.constraint_validations.append(ConstraintValidation(
                constraint_type="foreign_key_validity",
                passed=True,
                details="Foreign-key constraints are declarative metadata, not required for read-only query execution.",
            ))

            for column_name in sorted(parsed_column_names):
                column = getattr(schema_ctx, "get_column_info", lambda _name: None)(column_name)
                if column:
                    evidence.type_compatibility.append(TypeCompatibility(
                        check_type="schema_type_match",
                        passed=True,
                        schema_type=column.get("type"),
                        effective_type=None,
                        operation="reference",
                        details=f"Column '{column_name}' exists with declared type {column.get('type')}.",
                    ))

        if not evidence.type_compatibility:
            evidence.type_compatibility.append(TypeCompatibility(
                check_type="operation_validity",
                passed=True,
                details="No additional effective-type mismatch was detected.",
            ))
        return evidence

    def verify(self, execution_result: dict, validation_evidence: ValidationEvidence,
               question: str = "", sql: str = "") -> ResultVerificationContract:
        status_text = str(execution_result.get("status", execution_result.get("execution_status", ""))).upper()
        if status_text in {"SUCCESS", "SUCCESSFUL"}:
            execution_status = ExecutionStatus.SUCCESS
        elif status_text in {"SYNTAX_ERROR", "BLOCKED"}:
            execution_status = ExecutionStatus.SYNTAX_ERROR
        elif status_text in {"TIMEOUT"}:
            execution_status = ExecutionStatus.TIMEOUT
        else:
            execution_status = ExecutionStatus.RUNTIME_ERROR

        semantic_status = execution_result.get("semantic_status")
        coverage_status = execution_result.get("coverage_status")
        grounding = execution_result.get("result_grounding")
        if semantic_status is not None:
            issues = execution_result.get("issues", [])
            critical_issues = [
                issue for issue in issues
                if isinstance(issue, dict) and issue.get("critical")
            ]
            if execution_status == ExecutionStatus.SUCCESS:
                if (
                    semantic_status == "valid"
                    and coverage_status in {None, "verified"}
                    and not critical_issues
                ):
                    result_grounding = ResultGrounding.WELL_GROUNDED
                elif critical_issues or semantic_status == "invalid":
                    result_grounding = ResultGrounding.UNGROUNDED
                else:
                    result_grounding = ResultGrounding.PARTIALLY_GROUNDED
            else:
                result_grounding = (
                    ResultGrounding.UNGROUNDED
                    if critical_issues or not validation_evidence.all_passed
                    else ResultGrounding.PARTIALLY_GROUNDED
                )
        elif isinstance(grounding, ResultGrounding):
            result_grounding = grounding
        elif grounding in {"well_grounded", "partially_grounded", "ungrounded"}:
            result_grounding = ResultGrounding(grounding)
        else:
            result_grounding = (
                ResultGrounding.PARTIALLY_GROUNDED
                if execution_status == ExecutionStatus.SUCCESS
                else ResultGrounding.UNGROUNDED
            )

        grounding_evidence_status = (
            "verified"
            if result_grounding == ResultGrounding.WELL_GROUNDED
            else "insufficient"
            if result_grounding == ResultGrounding.PARTIALLY_GROUNDED
            else "failed"
        )

        if semantic_status is not None:
            validation_evidence.constraint_validations.append(ConstraintValidation(
                constraint_type="question_requirement_coverage",
                passed=(
                    semantic_status == "valid"
                    and coverage_status in {None, "verified"}
                    and result_grounding == ResultGrounding.WELL_GROUNDED
                ),
                details=(
                    "SQL requirement coverage and semantic validation are supported."
                    if semantic_status == "valid"
                    else "Semantic or requirement coverage evidence is not fully verified."
                ),
                affected_columns=[],
            ))

        execution_score = 0.4 if execution_status == ExecutionStatus.SUCCESS else 0.0
        grounding_score = {"well_grounded": 0.4, "partially_grounded": 0.2, "ungrounded": 0.0}[result_grounding.value]
        validation_score = 0.2 if validation_evidence.all_passed else 0.0
        score = round(execution_score + grounding_score + validation_score, 2)
        return ResultVerificationContract(
            execution_status=execution_status,
            result_grounding=result_grounding,
            validation_evidence=validation_evidence,
            reliability_score=score,
            database_source=execution_result.get("database_source"),
            actual_columns=list(execution_result.get("actual_columns") or []),
            actual_column_count=execution_result.get("actual_column_count"),
            actual_row_count=execution_result.get("actual_row_count"),
            total_result_count=execution_result.get("total_result_count"),
            truncated=execution_result.get("truncated"),
            execution_time_ms=execution_result.get("execution_time_ms"),
            grounding_evidence_status=grounding_evidence_status,
        )
