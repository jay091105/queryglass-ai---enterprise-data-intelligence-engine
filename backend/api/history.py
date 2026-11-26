"""Query history routes."""

from fastapi import APIRouter, HTTPException, Query
import sqlite3
import logging

logger = logging.getLogger(__name__)

# Import main module later to avoid circular import
import backend.main
from backend.services.query_run_persistence import QueryRunPersistence

router = APIRouter()


def _requirement_checks(semantic: dict) -> list[dict]:
    requirements = semantic.get("requirements") or {}
    issues = semantic.get("issues") or []
    fully_verified = (
        semantic.get("status") == "valid"
        and semantic.get("sql_coverage_status") == "verified"
    )
    checks = []
    for category, values, issue_types in (
        ("filters", requirements.get("filters") or [], {
            "missing_required_filter",
            "contradictory_filter",
            "unsupported_assumption",
        }),
        ("grouping", requirements.get("group_by_columns") or [], {"incorrect_grouping"}),
        ("aggregation", [requirements["aggregation"]] if requirements.get("aggregation") else [], {"incorrect_aggregation"}),
        ("output_columns", requirements.get("requested_outputs") or [], {"missing_output", "excess_output"}),
        ("ranking", [
            item for item in (requirements.get("top_n"), requirements.get("rank_column"))
            if item is not None
        ], {"incorrect_ranking"}),
    ):
        for value in values:
            item_text = str(value)
            if isinstance(value, dict):
                item_text = ", ".join(
                    str(part) for part in value.values() if part is not None
                )
            matching_issue = next(
                (
                    issue for issue in issues
                    if isinstance(issue, dict)
                    and issue.get("type") in issue_types
                    and (
                        (
                            issue.get("missing_object") is not None
                            and str(issue["missing_object"]) in item_text
                        )
                        or item_text.casefold() in str(issue.get("requirement", "")).casefold()
                        or len(values) == 1
                    )
                ),
                None,
            )
            checks.append({
                "category": category,
                "requirement": value,
                "status": "not_verified" if matching_issue or not fully_verified else "verified",
                "reason": matching_issue.get("message") if matching_issue else (
                    None if fully_verified else "The available semantic evidence is incomplete."
                ),
            })
    return checks


@router.get("/api/query-runs/stats")
def query_run_stats():
    """Get aggregate statistics from query runs table."""
    try:
        with sqlite3.connect(backend.main.DB_PATH) as conn:
            cursor = conn.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN execution_status = 'success' THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN execution_status IN ('syntax_error', 'runtime_error', 'timeout') THEN 1 ELSE 0 END) as blocked,
                    AVG(CASE WHEN execution_status = 'success' THEN execution_time_ms ELSE NULL END) as avg_duration_ms
                FROM _queryglass_query_runs
            """)
            row = cursor.fetchone()
            if row:
                return {
                    "total_records": row[0] or 0,
                    "successful_queries": row[1] or 0,
                    "blocked_queries": row[2] or 0,
                    "avg_duration_ms": round(row[3], 2) if row[3] else 0
                }
            return {"total_records": 0, "successful_queries": 0, "blocked_queries": 0, "avg_duration_ms": 0}
    except sqlite3.Error:
        logger.exception("Failed to get query run stats")
        return {"total_records": 0, "successful_queries": 0, "blocked_queries": 0, "avg_duration_ms": 0}


@router.get("/api/history")
def history(limit: int = 100):
    return backend.main.history(limit)


@router.get("/api/query-runs")
def query_runs(status: str | None = Query(default=None), grounding: str | None = Query(default=None),
               date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
               limit: int = Query(default=100, ge=1, le=500)):
    return {"items": QueryRunPersistence().list_query_runs(
        backend.main.DB_PATH, status=status, grounding=grounding, date_from=date_from, date_to=date_to, limit=limit,
    )}


@router.get("/api/history/query-runs")
def legacy_query_runs_alias(status: str | None = Query(default=None), grounding: str | None = Query(default=None),
                           date_from: str | None = Query(default=None), date_to: str | None = Query(default=None),
                           limit: int = Query(default=50, ge=1, le=100),
                           offset: int = Query(default=0, ge=0)):
    items = QueryRunPersistence().list_query_runs(
        backend.main.DB_PATH,
        status=status,
        grounding=grounding,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
    )
    if offset:
        items = items[offset:offset + limit]
    return {
        "items": items,
        "limit": limit,
        "offset": offset,
        "filters": {
            "execution_status": status,
            "result_grounding": grounding,
            "start_date": date_from,
            "end_date": date_to,
        },
    }


@router.get("/api/query-runs/{query_id}")
def query_run(query_id: str):
    record = QueryRunPersistence().get_query_run(backend.main.DB_PATH, query_id)
    if not record:
        raise HTTPException(404, "Query run was not found.")
    return record


@router.get("/api/query-runs/{query_id}/report")
def query_run_report(query_id: str):
    record = QueryRunPersistence().get_query_run(backend.main.DB_PATH, query_id)
    if not record:
        raise HTTPException(404, "Query run was not found.")

    verification_data = record.get("result_verification_json") or {}
    validation = record.get("validation_json") or {}
    semantic = validation.get("semantic_validation") or {}
    issues = list(semantic.get("issues", []))
    hallucination = validation.get("hallucination") or {}
    for issue_type, objects, message in (
        (
            "missing_column",
            hallucination.get("invalid_columns", []),
            "Requested column is not available in the selected dataset.",
        ),
        (
            "missing_table",
            hallucination.get("invalid_tables", []),
            "Referenced table is not available to this query.",
        ),
    ):
        existing_objects = {
            issue.get("missing_object")
            for issue in issues
            if isinstance(issue, dict)
        }
        for item in objects:
            if item not in existing_objects:
                issues.append({
                    "type": issue_type,
                    "message": f"{message} ({item})",
                    "missing_object": item,
                    "recommendation": "Use a table or column present in the selected dataset.",
                })
    return {
        "report_id": record["query_id"],
        "query_run": record,
        "validation": validation,
        "semantic_validation": semantic,
        "reliability": validation.get("reliability"),
        "overall_verification_status": record.get("result_grounding"),
        "issues": issues,
        "requirements": semantic.get("requirements"),
        "requirement_checks": _requirement_checks(semantic),
        "scoring_methodology": {
            "disclaimer": (
                "These are heuristic engineering indicators, not scientifically "
                "calibrated accuracy probabilities."
            ),
            "answer_accuracy_score": (
                "Sum of available dimension points; a critical validation failure "
                "forces the score to zero. Unevaluated dimensions contribute no points."
            ),
            "hallucination_risk_score": (
                "Heuristic risk starts at 5, adds 25 when semantic validation is "
                "incomplete and 18 per reported issue (up to 70); critical failures "
                "set risk to 100. The result is capped at 100."
            ),
            "dimensions": {
                "schema_grounding": "25 points when schema references validate; 0 when invalid.",
                "question_understanding": "20 points when verified; 0 when invalid.",
                "sql_requirement_coverage": "25 points when verified; 0 when invalid.",
                "result_grounding": (
                    "20 points only when execution succeeds and semantic, requirement, "
                    "and understanding checks are verified; 0 for failed execution or "
                    "critical semantic failure."
                ),
                "unsupported_assumptions": (
                    "10 points when semantic validation passes; 0 when an unsupported "
                    "assumption is detected."
                ),
            },
        },
        "result_verification": verification_data.get("metadata"),
        "verification": verification_data.get("contract"),
        "reliability_id": record.get("reliability_id"),
        "metadata_available": bool(validation or verification_data),
    }


@router.get("/api/history/query-runs/{query_id}")
def query_run_alias(query_id: str):
    record = QueryRunPersistence().get_query_run(backend.main.DB_PATH, query_id)
    if not record:
        raise HTTPException(404, "Query run was not found.")
    return record