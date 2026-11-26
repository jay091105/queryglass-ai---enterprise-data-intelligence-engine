"""FastAPI adapter for QueryGlass AI's existing Python services.

Convenience launcher (from the project root):
    python backend\app.py

Or run the ASGI app from the project root:
    python -m uvicorn backend.main:app --host 127.0.0.1 --port 8002

Do not run this implementation file directly; use the launcher or Uvicorn
with its package import path.

"""

from __future__ import annotations

import io

import logging

import os

import sqlite3

import time

import shutil
import tempfile
from pathlib import Path

from typing import Any

import pandas as pd

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from pydantic import BaseModel, Field

from starlette.concurrency import run_in_threadpool


from backend.config import settings as app_settings
from backend.services.data_quality import preprocess_dataframe
from backend.services.dataset_workflow import (
    WorkflowError,
    finalize_run,
    get_dataset_snapshot,
    get_active_dataset,
    get_run,
    ingest_upload,
    is_registered_raw_table,
    list_datasets,
    start_preprocess_run,
)
from backend.services.payload import frame_payload as _frame_payload
from backend.services.payload import json_value as _json_value
from backend.services.upload_jobs import (
    create_upload_job,
    get_upload_job,
    shutdown_upload_jobs,
    submit_upload_job,
    update_upload_job,
)

from backend.db.session import (

    get_database_stats, get_history, get_history_stats, get_table_row_count,

    get_tables, get_table_schema_details, log_history, save_to_sqlite,

)

from backend.services.file_loader import inspect_dataset, load_file

from backend.services.sql_engine import (
    HallucinationDetector,
    SQLParser,
    SQLValidationPipeline,
    SchemaContext,
    audit_sql,
    generate_sql,
    validate_sql_grounded,
)
from backend.services.reliability import semantic_validate, score_reliability
from backend.services.reliability_cases import get_reliability_test_cases
from backend.services.reliability_history import (
    record_evaluation,
    reliability_summary,
    update_execution,
    update_validation,
)
from backend.db.migrations import run_migrations
from backend.services.question_plan_builder import QuestionPlanBuilder
from backend.services.result_verification_engine import ResultVerificationEngine
from backend.services.query_run_persistence import QueryRunPersistence
from backend.services.table_fingerprinter import TableFingerprinter
from backend.schemas.query_run import QueryRun
from backend.schemas.sql import (
    ExecuteRequest,
    GenerateRequest,
    ReliabilityTestRunRequest,
    StoredDatasetPreprocessRequest,
    ValidateRequest,
    WorkflowPreprocessRequest,
)

# Configure debug logging

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

logger = logging.getLogger(__name__)

DB_PATH = app_settings.database_path

# 5GB upload limit (increased from 128MB). Set QUERYGLASS_MAX_UPLOAD_BYTES in .env to override

MAX_UPLOAD_BYTES = app_settings.max_upload_bytes

FRONTEND_ORIGINS = list(app_settings.cors_origins)

logger.info(f"QueryGlass AI API Starting")

logger.info(f"Database: {DB_PATH}")

logger.info(f"Max Upload Size: {MAX_UPLOAD_BYTES / (1024**3):.2f} GB")

logger.info(f"CORS Origins: {FRONTEND_ORIGINS}")

app = FastAPI(title="QueryGlass AI API", version="1.0.0", description="REST API for the QueryGlass Text-to-SQL application")

@app.on_event("startup")
def startup_traceability_migrations() -> None:
    try:
        migrated = run_migrations(DB_PATH)
        if migrated:
            logger.info("Traceability migrations ready: %s", ", ".join(migrated))
    except Exception:
        logger.exception("Traceability migration startup failed; the application will continue.")

@app.on_event("shutdown")
def shutdown_background_services() -> None:
    shutdown_upload_jobs(wait=True)

app.add_middleware(CORSMiddleware, allow_origins=FRONTEND_ORIGINS, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"])

# Debug middleware to log all requests and responses

from starlette.middleware.base import BaseHTTPMiddleware

from starlette.requests import Request

from starlette.responses import Response

class DebugMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next: Any) -> Response:

        method = request.method

        path = request.url.path

        logger.info(f">>> {method} {path}")

        try:

            response = await call_next(request)

            logger.info(f"<<< {method} {path} - Status: {response.status_code}")

            return response

        except Exception as e:

            logger.error(f"!!! {method} {path} - Error: {str(e)}", exc_info=True)

            raise

app.add_middleware(DebugMiddleware)

def root() -> dict[str, Any]:

    """Provide a useful response when the API origin is opened directly."""

    logger.debug("GET / - Root endpoint accessed")

    return {

        "service": "QueryGlass AI API",

        "status": "ok",

        "health": "/api/health",

        "docs": "/docs",

        "frontend": "http://localhost:3000",

    }

def _table_or_404(table_name: str) -> None:

    if table_name not in get_tables(DB_PATH):

        raise HTTPException(404, "Dataset table was not found in the configured SQLite database.")

def _resolve_table_name(table_name: str | None) -> str:
    """Resolve an omitted table only when the active dataset is unambiguous."""
    tables = get_tables(DB_PATH)
    if table_name:
        if table_name not in tables:
            raise HTTPException(404, "Dataset table was not found in the configured SQLite database.")
        return table_name

    active = get_active_dataset(DB_PATH)
    active_table = active.get("raw_table") if active else None
    if active_table and active_table in tables:
        return active_table
    if len(tables) == 1:
        return tables[0]
    if not tables:
        raise HTTPException(404, "No dataset is available. Upload a dataset or create a table before querying.")
    raise HTTPException(
        422,
        "More than one table is available and there is no active uploaded dataset. Select a table to continue.",
    )

def _schema(table_name: str) -> str:

    _table_or_404(table_name)

    ctx = SchemaContext(table_name, DB_PATH)

    return ctx.to_schema_text()

def _validate(sql: str, question: str, table_name: str) -> dict[str, Any]:

    _table_or_404(table_name)
    ctx = SchemaContext(table_name, DB_PATH)
    schema = ctx.to_schema_text()

    if not sql.strip():
        semantic = semantic_validate(question, "", ctx, DB_PATH)
        issues = semantic["issues"]
        missing_tables = any(issue["type"] == "missing_table" for issue in issues)
        missing_columns = any(issue["type"] == "missing_column" for issue in issues)
        unsupported = any(
            issue["type"] in {"unsupported_diagnosis", "prompt_injection"}
            for issue in issues
        )
        error_category = (
            "unknown_table" if missing_tables else
            "unknown_column" if missing_columns else
            "unsupported_request" if unsupported else
            "awaiting_generation"
        )
        result = {
            "valid": False,
            "sql_present": False,
            "error_category": error_category,
            "syntax_valid": None,
            "schema_valid": None,
            "operation": "NOT_GENERATED",
            "safety": "not_evaluated",
            "confidence": None,
            "confidence_label": "Not evaluated",
            "confidence_reasons": [issue["message"] for issue in issues],
            "audit": None,
            "hallucination": None,
            "pipeline": None,
            "semantic_validation": semantic,
        }
        result["reliability"] = score_reliability(result, semantic)
        result["validation_checks"] = {
            "syntax": "not_evaluated",
            "read_only_safety": "not_evaluated",
            "schema": "not_evaluated",
            "semantic": semantic["status"],
            "result": "not_evaluated",
        }
        return result

    audit = audit_sql(sql, schema, table_name, question, DB_PATH)

    grounded = validate_sql_grounded(sql, question, table_name, DB_PATH, schema_ctx=ctx)

    schema_stage = grounded.get("stages", {}).get("schema", {})
    if "invalid_tables" in schema_stage:
        hallucination = {
            "is_valid": schema_stage["passed"],
            "errors": schema_stage["errors"],
            "warnings": [],
            "invalid_tables": schema_stage["invalid_tables"],
            "invalid_columns": schema_stage["invalid_columns"],
            "invalid_ctes": schema_stage["invalid_ctes"],
        }
    else:
        hallucination = HallucinationDetector.validate_against_schema(sql, ctx)

    semantic = semantic_validate(
        question,
        sql,
        ctx,
        DB_PATH,
        schema_validation=schema_stage,
    )
    operation = audit.get("operation", "UNKNOWN")

    safe_read = operation == "SELECT"
    if not audit.get("syntax_passed"):
        semantic["issues"].append({
            "type": "syntax_error",
            "message": audit.get("syntax_error") or "SQL syntax validation failed.",
            "missing_object": None,
            "requirement": "Provide one complete SQLite SELECT statement.",
            "sql_section": "query",
            "recommendation": "Correct the SQL syntax and validate it again.",
            "critical": True,
        })
    if not safe_read:
        semantic["issues"].append({
            "type": "unsafe_sql",
            "message": f"Operation '{operation}' is not permitted; only read-only SELECT statements are supported.",
            "missing_object": None,
            "requirement": "Use a read-only query against the selected dataset.",
            "sql_section": "statement",
            "recommendation": "Replace the operation with a read-only SELECT.",
            "critical": True,
        })

    reasons = [audit.get("message", "")]
    reasons.extend(hallucination.get("errors", []))

    intent = grounded.get("stages", {}).get("intent", {})

    if intent.get("concerns"):

        reasons.extend(intent["concerns"])

    reasons.extend(issue["message"] for issue in semantic["issues"])

    if operation in {"UPDATE", "DELETE", "INSERT"}:
        reasons.append(
            f"{operation} changes stored data and is not permitted. Only read-only SQL is allowed."
        )
    elif operation == "TRUNCATE":
        reasons.append(
            "TRUNCATE is a destructive table operation and is blocked. "
            "Only read-only SQL statements are supported."
        )
    elif operation in {"DROP", "ALTER", "CREATE", "ATTACH", "DETACH"}:
        reasons.append(
            f"{operation} changes database structure and is blocked. "
            "Only read-only SQL statements are supported."
        )

    valid = (
        bool(audit.get("is_valid"))
        and bool(hallucination.get("is_valid"))
        and bool(grounded.get("is_valid"))
        and bool(semantic.get("passed"))
    )
    error_category = None
    if not valid:
        validation_errors = [str(error).lower() for error in hallucination.get("errors", [])]
        syntax_error = str(audit.get("syntax_error") or "").lower()
        if any("unauthorized table" in error for error in validation_errors):
            error_category = "safety_rejection"
        elif hallucination.get("invalid_ctes"):
            error_category = "invalid_cte_reference"
        elif hallucination.get("invalid_tables"):
            error_category = "unknown_table"
        elif hallucination.get("invalid_columns"):
            error_category = "unknown_column"
        elif any(issue["type"] == "missing_table" for issue in semantic["issues"]):
            error_category = "unknown_table"
        elif any(issue["type"] == "missing_column" for issue in semantic["issues"]):
            error_category = "unknown_column"
        elif any(issue["type"] == "unsupported_diagnosis" for issue in semantic["issues"]):
            error_category = "unsupported_request"
        elif any(issue["type"] == "prompt_injection" for issue in semantic["issues"]):
            error_category = "unsupported_request"
        elif operation in {"PRAGMA", "EXPLAIN", "UNKNOWN", "COMMAND"}:
            error_category = "unsupported_operation"
        elif operation in {
            "UPDATE", "DELETE", "INSERT", "DROP", "ALTER", "CREATE",
            "TRUNCATE", "ATTACH", "DETACH",
        }:
            error_category = "safety_rejection"
        elif (
            "only read-only" in syntax_error
            or "multiple statement" in syntax_error
        ):
            error_category = "safety_rejection"
        elif any(issue["critical"] for issue in semantic["issues"]):
            error_category = "semantic_mismatch"
        elif "unable to open database file" in syntax_error:
            error_category = "database_connection_error"
        elif "no such table:" in syntax_error:
            error_category = "unknown_table"
        elif "no such column:" in syntax_error:
            error_category = "unknown_column"
        elif not audit.get("syntax_passed"):
            error_category = "syntax_error"
        elif intent.get("concerns"):
            error_category = "intent_mismatch"
        else:
            error_category = "safety_rejection"

    result = {

        "valid": valid,
        "sql_present": True,

        "error_category": error_category,

        "syntax_valid": bool(audit.get("syntax_passed")),

        "schema_valid": bool(audit.get("schema_passed")) and bool(hallucination.get("is_valid")),

        "operation": operation,

        "safety": "read_only" if safe_read else "blocked",

        "confidence": None,

        "confidence_label": "Not calibrated",

        "confidence_reasons": reasons,

        "audit": audit,

        "hallucination": hallucination,

        "pipeline": grounded,

    }
    result["semantic_validation"] = semantic
    result["reliability"] = score_reliability(result, semantic)
    result["validation_checks"] = {
        "syntax": "valid" if result["syntax_valid"] else "invalid",
        "read_only_safety": "safe" if result["safety"] == "read_only" else "blocked",
        "schema": "valid" if result["schema_valid"] else "invalid",
        "semantic": semantic["status"],
        "result": "not_evaluated",
    }

    fingerprint_check = None
    effective_types_payload = None
    try:
        fingerprinter = TableFingerprinter()
        dataset = get_active_dataset(DB_PATH)
        dataset_id = dataset.get("dataset_id") if dataset else None
        current_fingerprint = fingerprinter.compute_fingerprint(DB_PATH, table_name, dataset_id)
        stored_fingerprint = fingerprinter.load_fingerprint(DB_PATH, table_name, dataset_id)
        if stored_fingerprint is None:
            fingerprinter.save_fingerprint(DB_PATH, current_fingerprint)
            fingerprint_check = {
                "consistent": True,
                "structure_match": True,
                "content_match": True,
                "overall_match": True,
                "changes_detected": [],
                "baseline_created": True,
            }
        else:
            matches = fingerprinter.compare_fingerprints(current_fingerprint, stored_fingerprint)
            changes = []
            if not matches["structure_match"]:
                changes.append("Table structure has changed")
            if not matches["content_match"]:
                changes.append("Table content has changed")
            fingerprint_check = {
                "consistent": matches["overall_match"],
                "structure_match": matches["structure_match"],
                "content_match": matches["content_match"],
                "overall_match": matches["overall_match"],
                "changes_detected": changes,
                "baseline_created": False,
            }
        if fingerprint_check["changes_detected"]:
            result["validation_checks"]["schema"] = "warning"
            result.setdefault("confidence_reasons", []).append(
                "Fingerprint mismatch detected: " + "; ".join(fingerprint_check["changes_detected"])
            )
    except Exception:
        logger.exception("Fingerprint validation degraded; continuing with schema validation")
        fingerprint_check = {
            "consistent": None,
            "structure_match": None,
            "content_match": None,
            "overall_match": None,
            "changes_detected": ["Fingerprint validation unavailable"],
            "baseline_created": False,
        }

    try:
        from backend.services.effective_type_profiler import EffectiveTypeProfiler
        dataset = get_active_dataset(DB_PATH)
        dataset_id = dataset.get("dataset_id") if dataset else None
        profiler = EffectiveTypeProfiler()
        effective_profile = profiler.load_profile(DB_PATH, table_name, dataset_id)
        if effective_profile is None:
            effective_profile = profiler.profile_table(DB_PATH, table_name, dataset_id)
            profiler.save_profile(DB_PATH, effective_profile)
        effective_types_payload = effective_profile.model_dump(mode="json")
    except Exception:
        logger.exception("Effective type profiling degraded; continuing with declared schema types")

    result["fingerprint_check"] = fingerprint_check
    result["effective_types"] = effective_types_payload
    result["stage"] = "validating"
    return result

def health() -> dict[str, Any]:

    logger.debug("GET /api/health - Health check requested")

    stats = get_database_stats(DB_PATH)

    if not stats["exists"]:
        logger.error("GET /api/health - Configured database does not exist: %s", DB_PATH)
        raise HTTPException(status_code=503, detail="Configured database is unavailable.")

    try:
        with sqlite3.connect(DB_PATH) as connection:
            connection.execute("SELECT 1")
    except sqlite3.Error:
        logger.exception("GET /api/health - Database connectivity check failed")
        raise HTTPException(status_code=503, detail="Configured database is unavailable.")

    response = {"status": "ok", "database_exists": stats["exists"], "database": Path(DB_PATH).name, "provider_configured": bool(os.getenv("NVIDIA_API_KEY")), "model": os.getenv("NVIDIA_MODEL", "mistralai/mistral-large-4-1t-a52b"), "api_key_exposed": False}

    logger.debug(f"GET /api/health - Response: {response}")

    return response

def dashboard() -> dict[str, Any]:

    logger.debug("GET /api/dashboard - Dashboard data requested")

    stats = get_database_stats(DB_PATH)

    history_stats = get_history_stats(DB_PATH)

    recent = get_history(8, DB_PATH)

    total_tables = []

    for name in stats["tables"]:

        total_tables.append({"name": name, "rows": get_table_row_count(name, DB_PATH), "columns": len(get_table_schema_details(name, DB_PATH)["columns"])})

    return {
        "database": stats,
        "tables": total_tables,
        "active_dataset": get_active_dataset(DB_PATH),
        "history": history_stats,
        "recent_queries": recent,
    }

def datasets() -> dict[str, Any]:

    logger.debug("GET /api/datasets - Datasets list requested")

    stats = get_database_stats(DB_PATH)

    response = {
        "datasets": [
            {
                "name": t,
                "rows": get_table_row_count(t, DB_PATH),
                "schema": get_table_schema_details(t, DB_PATH),
            }
            for t in stats["tables"]
        ],
        "active_dataset": get_active_dataset(DB_PATH),
    }

    logger.debug(f"GET /api/datasets - Found {len(response['datasets'])} datasets")

    return response

def get_preprocessing_rules() -> dict[str, Any]:

    """Return the preprocessing operations supported by the ingestion workflow."""

    logger.debug("GET /api/datasets/preprocessing-rules - Preprocessing rules requested")

    rules_response = {

        "rules": [

            {
                "id": "rule-dedupe",
                "code": "DEDUPE-01",
                "title": "Remove duplicate rows",
                "description": "Remove exact duplicate rows across all columns (first occurrence kept).",
                "impactText": "Removes redundant records safely.",
                "category": "dedupe",
                "option_key": "remove_duplicates",
                "type": "toggle",
                "default": True,
            },
            {
                "id": "rule-impute",
                "code": "IMPUTE-01",
                "title": "Handle missing values",
                "description": "Choose how missing cells are treated across the dataset.",
                "impactText": "Improves data completeness.",
                "category": "impute",
                "option_key": "missing_value_strategy",
                "type": "select",
                "default": "leave",
                "choices": [
                    {"value": "leave", "label": "Leave missing values unchanged"},
                    {"value": "drop_rows", "label": "Drop affected rows"},
                    {"value": "median", "label": "Fill numeric columns with median"},
                    {"value": "mean", "label": "Fill numeric columns with mean"},
                    {"value": "mode", "label": "Fill categorical columns with mode"},
                ],
            },
            {
                "id": "rule-trim",
                "code": "TRIM-01",
                "title": "Trim unnecessary whitespace",
                "description": "Trim leading/trailing whitespace from text values.",
                "impactText": "Improves consistency.",
                "category": "trim",
                "option_key": "clean_whitespace",
                "type": "toggle",
                "default": True,
            },
            {
                "id": "rule-cast",
                "code": "CAST-01",
                "title": "Correct data types",
                "description": "Convert numeric text columns to real numeric types; identifiers keep leading zeros.",
                "impactText": "Improves query compatibility.",
                "category": "cast",
                "option_key": "convert_data_types",
                "type": "toggle",
                "default": True,
            },
            {
                "id": "rule-categorical",
                "code": "CATEGORICAL-01",
                "title": "Standardize categorical values",
                "description": "Collapse extra spaces and case-only variants in low-cardinality text columns.",
                "impactText": "Improves grouping and filtering consistency.",
                "category": "normalize",
                "option_key": "standardize_categorical",
                "type": "toggle",
                "default": False,
            },
            {
                "id": "rule-invalid",
                "code": "INVALID-01",
                "title": "Handle invalid values",
                "description": "Treat values that violate their column's dominant type.",
                "impactText": "Removes values that break numeric or date queries.",
                "category": "invalid",
                "option_key": "invalid_value_strategy",
                "type": "select",
                "default": "leave",
                "choices": [
                    {"value": "leave", "label": "Keep invalid values"},
                    {"value": "convert_to_null", "label": "Convert invalid values to null"},
                    {"value": "drop_rows", "label": "Drop rows containing invalid values"},
                ],
            },
            {
                "id": "rule-outliers",
                "code": "OUTLIER-01",
                "title": "Handle outliers",
                "description": "Treat values outside 1.5×IQR bounds in numeric columns.",
                "impactText": "Reduces skew from extreme values.",
                "category": "outliers",
                "option_key": "outlier_strategy",
                "type": "select",
                "default": "leave",
                "choices": [
                    {"value": "leave", "label": "Keep outliers"},
                    {"value": "cap", "label": "Cap outliers to IQR bounds"},
                    {"value": "remove_rows", "label": "Remove rows containing outliers"},
                ],
            },
            {
                "id": "rule-empty-rows",
                "code": "EMPTYROW-01",
                "title": "Remove empty rows",
                "description": "Drop rows in which every value is missing.",
                "impactText": "Removes blank records.",
                "category": "structure",
                "option_key": "remove_empty_rows",
                "type": "toggle",
                "default": False,
            },
            {
                "id": "rule-empty-cols",
                "code": "EMPTYCOL-01",
                "title": "Remove empty columns",
                "description": "Drop columns in which every value is missing.",
                "impactText": "Removes columns that carry no information.",
                "category": "structure",
                "option_key": "remove_empty_columns",
                "type": "toggle",
                "default": False,
            },
        ]

    }

    logger.debug(f"GET /api/datasets/preprocessing-rules - Returning {len(rules_response['rules'])} rules")

    return rules_response

def _ingest_upload_payload(
    name: str,
    payload: Any,
    dataset_identity: str | None = None,
    progress_callback=None,
) -> dict[str, Any]:

    """Parse an uploaded payload and persist it; runs in a worker thread."""

    stream = io.BytesIO(payload) if isinstance(payload, bytes) else payload
    stream.seek(0)

    try:

        logger.debug(f"POST /api/datasets/upload - Parsing {Path(name).suffix.lower()} file")

        frame = load_file(stream, name)

        logger.debug(f"POST /api/datasets/upload - Successfully parsed {len(frame)} rows, {len(frame.columns)} columns")

    except Exception as exc:

        logger.exception("POST /api/datasets/upload - Parse error")

        raise HTTPException(

            400,

            f"Could not read dataset '{name}'. The file may be corrupted, empty, or in an unsupported format. Error: {exc}"

        ) from exc

    # Persist the raw dataset durably and run the initial quality analysis.

    try:

        snapshot = ingest_upload(
            DB_PATH,
            name,
            frame,
            dataset_identity,
            progress_callback=progress_callback,
        )

    except WorkflowError as exc:

        logger.warning(

            "POST /api/datasets/upload - Workflow failed at stage '%s': %s", exc.stage, exc.message

        )

        detail: Any = exc.message

        if exc.dataset_id:

            failed_snapshot = get_dataset_snapshot(DB_PATH, exc.dataset_id)

            if failed_snapshot is not None:

                detail = {"message": exc.message, "dataset": failed_snapshot}

        raise HTTPException(exc.status_code, detail) from exc

    if progress_callback:
        progress_callback("finalizing", len(frame), len(frame))

    logger.info(

        "POST /api/datasets/upload - Dataset %s stored as raw table %s",

        snapshot["dataset_id"],

        snapshot["raw_table"],

    )

    return {

        "dataset": snapshot,

        "filename": name,

        "inspection": inspect_dataset(frame),

        "preview": _frame_payload(frame)

    }


def _process_upload_job(job_id: str, file_path: str, name: str, dataset_identity: str | None) -> None:
    def report_progress(stage: str, processed_rows: int, total_rows: int) -> None:
        update_upload_job(
            job_id,
            status="running",
            stage=stage,
            processed_rows=processed_rows,
            total_rows=total_rows,
        )

    update_upload_job(job_id, status="running", stage="parsing")
    try:
        with open(file_path, "rb") as stream:
            result = _ingest_upload_payload(name, stream, dataset_identity, report_progress)
        update_upload_job(
            job_id,
            status="completed",
            stage="completed",
            processed_rows=result["inspection"]["row_count"],
            total_rows=result["inspection"]["row_count"],
            result=result,
        )
    except Exception as exc:
        logger.exception("Background upload job %s failed", job_id)
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        message = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
        update_upload_job(job_id, status="failed", stage="failed", error=message)
    finally:
        try:
            os.remove(file_path)
        except FileNotFoundError:
            pass

async def upload_dataset(
    file: UploadFile = File(...),
    dataset_identity: str | None = Form(default=None),
) -> dict[str, Any]:

    logger.debug(f"POST /api/datasets/upload - File upload started: {file.filename}")

    name = Path(file.filename or "").name

    suffix = Path(name).suffix.lower()

    # Validate file type

    if suffix not in {".csv", ".xls", ".xlsx"}:

        logger.warning(f"POST /api/datasets/upload - Invalid file type: {suffix}")

        raise HTTPException(

            415,

            f"File type '{suffix}' is not supported. Please upload CSV (.csv), Excel 2003 (.xls), or Excel 2007+ (.xlsx) files."

        )

    try:
        file_size = file.size
        if file_size is None:
            file.file.seek(0, 2)
            file_size = file.file.tell()
            file.file.seek(0)

    except Exception as exc:

        logger.exception("POST /api/datasets/upload - File size check failed")

        raise HTTPException(500, f"Failed to inspect uploaded file: {exc}") from exc

    # Check file size

    file_size_mb = file_size / (1024 * 1024)

    max_size_mb = MAX_UPLOAD_BYTES / (1024 * 1024)

    if file_size > MAX_UPLOAD_BYTES:

        logger.warning(f"POST /api/datasets/upload - File too large: {file_size_mb:.2f} MB (max: {max_size_mb:.2f} MB)")

        raise HTTPException(

            413,

            f"File size ({file_size_mb:.2f} MB) exceeds the maximum allowed size of {max_size_mb:.2f} MB. "

            f"Please compress your data or split it into smaller files."

        )

    logger.debug(f"POST /api/datasets/upload - File size: {file_size_mb:.2f} MB")

    # Parsing and SQLite persistence block for seconds (or minutes on large
    # files); running them on the event loop would freeze the whole API, so
    # they are offloaded to a worker thread.
    return await run_in_threadpool(_ingest_upload_payload, name, file.file, dataset_identity)


async def start_upload_job(
    file: UploadFile = File(...),
    dataset_identity: str | None = Form(default=None),
) -> dict[str, Any]:
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if suffix not in {".csv", ".xls", ".xlsx"}:
        raise HTTPException(
            415,
            f"File type '{suffix}' is not supported. Please upload CSV (.csv), Excel 2003 (.xls), or Excel 2007+ files.",
        )

    try:
        file_size = file.size
        if file_size is None:
            file.file.seek(0, 2)
            file_size = file.file.tell()
            file.file.seek(0)
    except Exception as exc:
        logger.exception("POST /api/datasets/upload-jobs - File size check failed")
        raise HTTPException(500, f"Failed to inspect uploaded file: {exc}") from exc

    if file_size > MAX_UPLOAD_BYTES:
        size_mb = file_size / (1024 * 1024)
        max_size_mb = MAX_UPLOAD_BYTES / (1024 * 1024)
        raise HTTPException(
            413,
            f"File size ({size_mb:.2f} MB) exceeds the maximum allowed size of {max_size_mb:.2f} MB.",
        )

    temporary_path = ""
    try:
        def persist_upload() -> str:
            with tempfile.NamedTemporaryFile(
                prefix="queryglass-upload-",
                suffix=suffix,
                delete=False,
            ) as destination:
                file.file.seek(0)
                shutil.copyfileobj(file.file, destination, length=8 * 1024 * 1024)
                return destination.name

        temporary_path = await run_in_threadpool(persist_upload)
        job_id = create_upload_job(name, file_size)
        submit_upload_job(_process_upload_job, job_id, temporary_path, name, dataset_identity)
    except Exception as exc:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass
        logger.exception("POST /api/datasets/upload-jobs - Could not start upload job")
        raise HTTPException(500, f"Could not start dataset ingestion: {exc}") from exc

    return get_upload_job(job_id) or {"job_id": job_id, "status": "queued"}


def upload_job_status(job_id: str) -> dict[str, Any]:
    job = get_upload_job(job_id)
    if job is None:
        raise HTTPException(404, "Upload job was not found or has expired.")
    return job

def workflow_datasets() -> dict[str, Any]:

    """List every dataset registered by the ingestion workflow."""

    logger.debug("GET /api/datasets/workflow - Workflow dataset list requested")

    return {"datasets": list_datasets(DB_PATH)}

def workflow_dataset_detail(dataset_id: str) -> dict[str, Any]:

    """Return the persisted workflow state, reports, runs, and versions."""

    snapshot = get_dataset_snapshot(DB_PATH, dataset_id)

    if snapshot is None:

        raise HTTPException(404, "Dataset was not found.")

    return snapshot

def workflow_run_detail(dataset_id: str, run_id: str) -> dict[str, Any]:

    run = get_run(DB_PATH, dataset_id, run_id)

    if run is None:

        raise HTTPException(404, "Processing run was not found for this dataset.")

    return run

def workflow_preprocess(dataset_id: str, request: WorkflowPreprocessRequest) -> dict[str, Any]:

    """Start a preprocessing run for a stored dataset (explicit user action)."""

    logger.info("POST /api/datasets/workflow/%s/preprocess - Preprocessing requested", dataset_id)

    try:

        snapshot = start_preprocess_run(DB_PATH, dataset_id, request.options)

    except WorkflowError as exc:

        logger.warning(

            "POST /api/datasets/workflow/%s/preprocess - Failed at stage '%s': %s",

            dataset_id, exc.stage, exc.message,

        )

        raise HTTPException(exc.status_code, exc.message) from exc

    logger.info("POST /api/datasets/workflow/%s/preprocess - Run completed", dataset_id)

    return snapshot


def preprocess_stored_dataset(
    table_name: str,
    request: StoredDatasetPreprocessRequest,
) -> dict[str, Any]:
    _table_or_404(table_name)
    if is_registered_raw_table(DB_PATH, table_name):
        raise HTTPException(
            409,
            "This table is the preserved raw dataset of the ingestion workflow. "
            "Use the workflow preprocessing endpoint so the raw data stays intact.",
        )
    quoted_table = '"' + table_name.replace('"', '""') + '"'

    try:
        with sqlite3.connect(DB_PATH) as connection:
            original = pd.read_sql_query(f"SELECT * FROM {quoted_table}", connection)
    except Exception as exc:
        logger.exception(
            "POST /api/datasets/%s/preprocess - Could not read stored table",
            table_name,
        )
        raise HTTPException(500, f"Could not read stored dataset: {exc}") from exc

    try:
        cleaned, report = preprocess_dataframe(original, options=request.options)
    except Exception as exc:
        logger.exception(
            "POST /api/datasets/%s/preprocess - Preprocessing failed",
            table_name,
        )
        raise HTTPException(422, f"Preprocessing failed: {exc}") from exc

    try:
        saved_table = save_to_sqlite(cleaned, table_name, DB_PATH, if_exists="replace")
    except Exception as exc:
        logger.exception(
            "POST /api/datasets/%s/preprocess - Could not update stored table",
            table_name,
        )
        raise HTTPException(
            500,
            f"Preprocessing completed but the stored dataset could not be updated: {exc}",
        ) from exc

    return {
        "saved": True,
        "table_name": saved_table,
        "rows": get_table_row_count(saved_table, DB_PATH),
        "preview": _frame_payload(cleaned),
        "inspection": inspect_dataset(cleaned),
        "report": _json_value(report.to_dict()),
    }


def workflow_store_processed(dataset_id: str, run_id: str) -> dict[str, Any]:

    """Persist the processed output of a completed run (explicit user action)."""

    logger.info("POST /api/datasets/workflow/%s/runs/%s/store - Store processed data requested", dataset_id, run_id)

    try:

        result = finalize_run(DB_PATH, dataset_id, run_id)

    except WorkflowError as exc:

        logger.warning(

            "POST /api/datasets/workflow/%s/runs/%s/store - Failed at stage '%s': %s",

            dataset_id, run_id, exc.stage, exc.message,

        )

        raise HTTPException(exc.status_code, exc.message) from exc

    logger.info(

        "POST /api/datasets/workflow/%s/runs/%s/store - Stored as %s",

        dataset_id, run_id, result["version"]["table_name"],

    )

    return result

def dataset_schema(table_name: str) -> dict[str, Any]:

    _table_or_404(table_name)

    return {"table": table_name, "rows": get_table_row_count(table_name, DB_PATH), **get_table_schema_details(table_name, DB_PATH)}

def delete_dataset_table(table_name: str) -> dict[str, Any]:

    """Delete a visible dataset table and clean up its workflow metadata."""

    _table_or_404(table_name)
    quoted_table = '"' + table_name.replace('"', '""') + '"'

    try:
        with sqlite3.connect(DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            dataset = connection.execute(
                "SELECT dataset_id, dataset_identity, status FROM _queryglass_datasets WHERE raw_table=?",
                (table_name,),
            ).fetchone()
            version = connection.execute(
                "SELECT dataset_id, version_id FROM _queryglass_processed_versions WHERE table_name=?",
                (table_name,),
            ).fetchone()

            if dataset and dataset["status"] == "PREPROCESSING":
                raise HTTPException(409, "This dataset is currently being preprocessed and cannot be deleted yet.")

            tables_to_drop = {table_name}
            if dataset:
                dataset_id = dataset["dataset_id"]
                related = connection.execute(
                    "SELECT staged_table FROM _queryglass_processing_runs WHERE dataset_id=? "
                    "AND staged_table IS NOT NULL "
                    "UNION SELECT table_name FROM _queryglass_processed_versions WHERE dataset_id=?",
                    (dataset_id, dataset_id),
                ).fetchall()
                tables_to_drop.update(row["staged_table"] for row in related if row["staged_table"])

                connection.execute("DELETE FROM _queryglass_quality_reports WHERE dataset_id=?", (dataset_id,))
                connection.execute("DELETE FROM _queryglass_processed_versions WHERE dataset_id=?", (dataset_id,))
                connection.execute("DELETE FROM _queryglass_processing_runs WHERE dataset_id=?", (dataset_id,))
                connection.execute("DELETE FROM _queryglass_table_fingerprints WHERE dataset_id=?", (dataset_id,))
                connection.execute("DELETE FROM _queryglass_effective_types WHERE dataset_id=?", (dataset_id,))
                connection.execute("DELETE FROM _queryglass_datasets WHERE dataset_id=?", (dataset_id,))
                connection.execute(
                    "UPDATE _queryglass_dataset_identities SET active_dataset_id=NULL, "
                    "processing_dataset_id=NULL WHERE identity_id=? AND active_dataset_id=?",
                    (dataset["dataset_identity"], dataset_id),
                )
                connection.execute(
                    "UPDATE _queryglass_active_dataset SET dataset_id=NULL, updated_at=? "
                    "WHERE active_key='active' AND dataset_id=?",
                    (time.strftime("%Y-%m-%dT%H:%M:%S"), dataset_id),
                )
                connection.execute(
                    "DELETE FROM _queryglass_dataset_identities WHERE identity_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM _queryglass_datasets WHERE dataset_identity=?)",
                    (dataset["dataset_identity"], dataset["dataset_identity"]),
                )
            elif version:
                connection.execute(
                    "DELETE FROM _queryglass_processed_versions WHERE version_id=?",
                    (version["version_id"],),
                )
                connection.execute(
                    "UPDATE _queryglass_datasets SET status='RAW_ANALYZED' WHERE dataset_id=? "
                    "AND status='PROCESSED_STORED'",
                    (version["dataset_id"],),
                )
                connection.execute(
                    "UPDATE _queryglass_dataset_identities SET active_dataset_id=NULL "
                    "WHERE active_dataset_id=?",
                    (version["dataset_id"],),
                )

            for candidate in tables_to_drop:
                quoted_candidate = '"' + candidate.replace('"', '""') + '"'
                connection.execute(f"DROP TABLE IF EXISTS {quoted_candidate}")
            connection.commit()
    except HTTPException:
        raise
    except sqlite3.Error as exc:
        logger.exception("DELETE /api/datasets/%s - Could not delete dataset", table_name)
        raise HTTPException(500, f"Could not delete dataset: {exc}") from exc

    logger.info("DELETE /api/datasets/%s - Dataset deleted", table_name)
    return {"deleted": True, "table_name": table_name}

def dataset_rows(table_name: str, offset: int = 0, limit: int = 25, search: str = "") -> dict[str, Any]:

    """Return a bounded, read-only page from an existing dataset table."""

    _table_or_404(table_name)

    escaped_table = table_name.replace('"', '""')
    escaped_search = search.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    conditions: list[str] = []
    parameters: list[Any] = []

    if search:
        with sqlite3.connect(f"{Path(DB_PATH).resolve().as_uri()}?mode=ro", uri=True) as connection:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{escaped_table}")')
            ]
        if columns:
            conditions.append(
                "(" + " OR ".join(
                    f'CAST("{column.replace(chr(34), chr(34) * 2)}" AS TEXT) LIKE ? ESCAPE \'!\''
                    for column in columns
                ) + ")"
            )
            parameters.extend([f"%{escaped_search}%"] * len(columns))

    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    table_sql = f'"{escaped_table}"'
    database_uri = f"{Path(DB_PATH).resolve().as_uri()}?mode=ro"

    try:
        with sqlite3.connect(database_uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            total_rows = int(connection.execute(
                f"SELECT COUNT(*) FROM {table_sql}{where_clause}",
                parameters,
            ).fetchone()[0])
            cursor = connection.execute(
                f"SELECT * FROM {table_sql}{where_clause} LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            )
            rows = [_json_value(dict(row)) for row in cursor.fetchall()]
            columns = [str(column[0]) for column in cursor.description or []]
    except sqlite3.Error as exc:
        logger.exception("GET /api/datasets/%s/rows - Could not read dataset", table_name)
        raise HTTPException(500, "Could not read dataset rows.") from exc

    return {
        "table": table_name,
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(rows) < total_rows,
    }

def generate(request: GenerateRequest) -> dict[str, Any]:

    try:

        table_name = _resolve_table_name(request.table_name)
        schema_ctx = SchemaContext(table_name, DB_PATH)
        schema = schema_ctx.to_schema_text()
        question_plan = QuestionPlanBuilder(schema_ctx, get_tables(DB_PATH)).parse(request.question)
        if not question_plan.can_generate_sql:
            raise HTTPException(
                400,
                {
                    "message": "Generation blocked: " + question_plan.get_error_message(),
                    "question_plan": question_plan.model_dump(),
                    "error": "unsupported_request",
                    "stage": "planning",
                    "recovery": "Choose a table and columns from the live dataset schema.",
                },
            )
        question_precheck = semantic_validate(
            request.question,
            "",
            schema_ctx,
            DB_PATH,
        )
        if any(issue["critical"] for issue in question_precheck["issues"]):
            validation = _validate("", request.question, table_name)
            return {
                "sql": "",
                "table_name": table_name,
                "question_plan": question_plan.model_dump(),
                "validation": validation,
                "reliability": None,
                "reliability_id": None,
                "explanation": "Generation blocked: " + "; ".join(
                    issue["message"] for issue in question_precheck["issues"]
                ),
                "planning": "blocked",
            }

        sql = generate_sql(request.question, schema, table_name)

    except HTTPException:

        raise

    except Exception as exc:

        raise HTTPException(503, f"SQL generation is unavailable: {exc}") from exc

    validation = _validate(sql, request.question, table_name)
    reliability_id = None
    if sql.strip():
        reliability_id = record_evaluation(
            DB_PATH,
            question=request.question,
            table_name=table_name,
            sql=sql,
            validation=validation,
            source="generated",
            provider_model=os.getenv("NVIDIA_MODEL", "configured provider"),
        )

    explanation = (
        "No SQL was generated. Review the validation issues and ask for an answer supported by the selected dataset."
        if not sql
        else
        "Generated SQL is shown for review. Validate it before execution; semantic checks may be inconclusive for ambiguous requests."
    )
    if not sql:
        question_words = set(request.question.casefold().split())
        temporal_markers = ("date", "time", "timestamp", "created", "updated", "recorded")
        has_temporal_column = any(
            any(marker in column.casefold() for marker in temporal_markers)
            for column in schema_ctx.get_column_names()
        )
        if question_words.intersection({"recent", "latest", "newest", "oldest"}) and not has_temporal_column:
            explanation = (
                "The selected table has no date or time column, so record recency cannot be determined. "
                "Try asking for a bounded preview, such as 'Show 10 records'."
            )

    return {
        "sql": sql,
        "table_name": table_name,
        "question_plan": question_plan.model_dump(),
        "validation": validation,
        "reliability": validation["reliability"] if sql.strip() else None,
        "reliability_id": reliability_id,
        "planning": "complete",
        "explanation": explanation,
    }

def validate(request: ValidateRequest) -> dict[str, Any]:

    validation = _validate(request.sql, request.question, request.table_name)
    if request.reliability_id:
        updated = update_validation(
            DB_PATH,
            request.reliability_id,
            question=request.question,
            table_name=request.table_name,
            sql=request.sql,
            validation=validation,
        )
        if not updated:
            raise HTTPException(400, "The reliability evaluation was not found.")
        reliability_id = request.reliability_id
    else:
        reliability_id = record_evaluation(
            DB_PATH,
            question=request.question,
            table_name=request.table_name,
            sql=request.sql,
            validation=validation,
            source="validated",
            provider_model=os.getenv("NVIDIA_MODEL", "configured provider"),
        )
    validation["reliability_id"] = reliability_id
    return validation

def _single_statement(sql: str) -> str:

    text = sql.strip()

    if not text:

        raise HTTPException(400, "Enter one complete SQL statement.")

    if not SQLParser.is_single_statement(text):
        raise HTTPException(400, "Only one SQL statement is permitted.")

    statement = text.rstrip(";").strip()

    if not statement or not sqlite3.complete_statement(statement + ";"):

        raise HTTPException(400, "Enter one complete SQL statement.")

    return statement

def _run_readonly(sql: str, allowed_table: str) -> tuple[list[str], list[dict[str, Any]], int]:

    uri = Path(DB_PATH).as_uri() + "?mode=ro"

    start = time.perf_counter()

    try:

        with sqlite3.connect(uri, uri=True, timeout=2) as conn:

            conn.execute("PRAGMA query_only=ON")

            conn.set_progress_handler(lambda: int(time.perf_counter() - start > 5), 10000)

            conn.execute("PRAGMA trusted_schema=OFF")

            def authorize_readonly(action, arg1, arg2, database_name, trigger_name):
                if action == sqlite3.SQLITE_READ:
                    return (
                        sqlite3.SQLITE_OK
                        if (arg1 or "").casefold() == allowed_table.casefold()
                        else sqlite3.SQLITE_DENY
                    )
                if action == sqlite3.SQLITE_FUNCTION:
                    function = (arg2 or arg1 or "").casefold()
                    return sqlite3.SQLITE_DENY if function == "load_extension" else sqlite3.SQLITE_OK
                if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            conn.set_authorizer(authorize_readonly)

            cursor = conn.execute(sql)

            columns = [item[0] for item in cursor.description or []]

            rows = [dict(zip(columns, row)) for row in cursor.fetchmany(501)]
            total_row_count = len(rows)
            while True:
                remaining = cursor.fetchmany(10000)
                if not remaining:
                    break
                total_row_count += len(remaining)

    except sqlite3.Error as exc:

        message = str(exc)
        lower_message = message.lower()
        if "unable to open database" in lower_message or "database is locked" in lower_message:
            category = "database_connection_error"
        elif lower_message.startswith("no such table:"):
            category = "unknown_table"
        elif lower_message.startswith("no such column:"):
            category = "unknown_column"
        elif "not authorized" in lower_message or "prohibited" in lower_message:
            category = "safety_rejection"
        elif "syntax error" in lower_message or "incomplete input" in lower_message:
            category = "syntax_error"
        else:
            category = "sql_execution_error"
        status_code = 503 if category == "database_connection_error" else 400
        raise HTTPException(
            status_code,
            {"message": f"Query execution failed: {message}", "error_category": category},
        ) from exc

    return columns, rows, total_row_count

def _result_verification_metadata(
    columns: list[str],
    rows: list[dict[str, Any]],
    truncated: bool,
    table_name: str | None = None,
    duration_ms: float | None = None,
    total_row_count: int | None = None,
) -> dict[str, Any]:
    preview_count = len(rows)
    exact_count = (
        int(total_row_count)
        if total_row_count is not None
        else preview_count
    )
    if not truncated:
        exact_count = preview_count

    return {
        "source": "database_execution",
        "columns": columns,
        "rows_returned": len(rows),
        "row_count_exact": True,
        "truncated": truncated,
        "database_source": table_name,
        "actual_columns": list(columns),
        "actual_column_count": len(columns),
        "actual_row_count": len(rows),
        "total_result_count": exact_count,
        "execution_status": "success",
        "duration_ms": duration_ms,
        "null_counts": (
            {
                column: sum(row.get(column) is None for row in rows)
                for column in columns
            }
            if rows
            else {}
        ),
        "null_count_scope": "returned_preview",
    }

def _save_execution_reliability(
    request: ExecuteRequest,
    validation: dict[str, Any],
    status: str,
    metadata: dict[str, Any] | None = None,
) -> str:

    validation["reliability"] = score_reliability(
        validation,
        validation["semantic_validation"],
        execution={"status": status},
    )
    validation["validation_checks"]["result"] = (
        "verified" if status == "SUCCESS" else "failed"
    )
    if request.reliability_id:
        updated = update_execution(
            DB_PATH,
            request.reliability_id,
            question=request.question,
            table_name=request.table_name,
            sql=request.sql,
            validation=validation,
            execution_status=status,
            execution_metadata=metadata,
        )
        if not updated:
            raise HTTPException(400, "The reliability evaluation was not found.")
        return request.reliability_id
    return record_evaluation(
        DB_PATH,
        question=request.question,
        table_name=request.table_name,
        sql=request.sql,
        validation=validation,
        source="execution",
        provider_model=os.getenv("NVIDIA_MODEL", "configured provider"),
    )

def _persist_query_run_trace(
    *,
    question: str,
    sql: str,
    table_name: str,
    validation: dict[str, Any],
    status: str,
    row_count: int = 0,
    duration_ms: float = 0.0,
    error_message: str | None = None,
    truncated: bool = False,
    actual_columns: list[str] | None = None,
    total_result_count: int | None = None,
    reliability_id: str | None = None,
    result_verification_metadata: dict[str, Any] | None = None,
) -> str | None:
    """Persist an execution trace without changing the query response on failure."""
    try:
        ctx = SchemaContext(table_name, DB_PATH)
        evidence = ResultVerificationEngine().validate(sql, ctx, get_tables(DB_PATH))

        semantic_issues = (
            validation.get("semantic_validation", {}).get("issues", [])
            if isinstance(validation, dict)
            else []
        )
        result_grounding = (
            "ungrounded"
            if semantic_issues
            else "well_grounded"
            if status == "SUCCESS"
            else "partially_grounded"
        )
        verification = ResultVerificationEngine().verify(
            {
                "status": (
                    "SUCCESS"
                    if status == "SUCCESS"
                    else "SYNTAX_ERROR"
                    if status in {"BLOCKED", "SYNTAX_ERROR"}
                    else "TIMEOUT"
                    if status == "TIMEOUT"
                    else "RUNTIME_ERROR"
                ),
                "result_grounding": result_grounding,
                "issues": semantic_issues,
                "semantic_status": validation.get("semantic_validation", {}).get("status"),
                "coverage_status": validation.get("semantic_validation", {}).get("sql_coverage_status"),
                "database_source": table_name,
                "actual_columns": actual_columns or [],
                "actual_column_count": len(actual_columns or []),
                "actual_row_count": row_count,
                "total_result_count": total_result_count,
                "truncated": truncated,
                "execution_time_ms": duration_ms,
            },
            evidence,
            question=question,
            sql=sql,
        )

        active = get_active_dataset(DB_PATH)
        dataset_id = active.get("dataset_id") if active else None
        fingerprint_hash = None
        try:
            fingerprint = TableFingerprinter().compute_fingerprint(DB_PATH, table_name, dataset_id)
            fingerprint_hash = fingerprint.fingerprint_hash
        except Exception:
            logger.exception("Could not compute fingerprint for query trace %s", table_name)

        plan = QuestionPlanBuilder(ctx, get_tables(DB_PATH)).parse(question)
        run = QueryRun.from_verification(
            verification,
            question=question,
            sql=sql,
            table_name=table_name,
            row_count=row_count,
            execution_time_ms=duration_ms,
            error_message=error_message,
            dataset_id=dataset_id,
            executed_sql=sql,
            question_plan_json=plan.model_dump(mode="json"),
            fingerprint_hash=fingerprint_hash,
            provider_model=os.getenv("NVIDIA_MODEL", "configured provider"),
            truncation_status="truncated" if truncated else "complete",
            reliability_id=reliability_id,
            validation_json=validation,
            result_verification_json={
                "metadata": result_verification_metadata,
                "contract": verification.to_json_dict(),
            },
        )
        if not QueryRunPersistence().save(DB_PATH, run):
            logger.error("Could not save query report %s", run.query_id)
            return None
        return run.query_id
    except Exception:
        logger.exception("Traceability persistence failed for SQL execution")
        return None

def execute(request: ExecuteRequest) -> dict[str, Any]:

    sql = _single_statement(request.sql)

    validation = _validate(sql, request.question, request.table_name)

    if not validation["valid"]:

        log_history(request.question, sql, "BLOCKED", 0, 0, "Server-side validation failed", DB_PATH)
        try:
            ctx = SchemaContext(request.table_name, DB_PATH)
            evidence = ResultVerificationEngine().validate(sql, ctx, get_tables(DB_PATH))
            semantic = validation.get("semantic_validation", {})
            blocked_contract = ResultVerificationEngine().verify(
                {
                    "status": "SYNTAX_ERROR",
                    "issues": semantic.get("issues", []),
                    "semantic_status": semantic.get("status"),
                    "coverage_status": semantic.get("sql_coverage_status"),
                    "database_source": request.table_name,
                },
                evidence,
                question=request.question,
                sql=sql,
            ).to_json_dict()
        except Exception:
            logger.exception("Could not build blocked result verification contract")
            blocked_contract = None
        reliability_id = _save_execution_reliability(
            request,
            validation,
            "BLOCKED",
        )
        trace_query_id = _persist_query_run_trace(
            question=request.question,
            sql=sql,
            table_name=request.table_name,
            validation=validation,
            status="BLOCKED",
            error_message="Server-side validation failed",
            reliability_id=reliability_id,
        )

        raise HTTPException(422, {
            "message": "SQL did not pass server-side validation.",
            "validation": validation,
            "reliability_id": reliability_id,
            "trace_query_id": trace_query_id,
            "stage": "blocked",
            "verification": blocked_contract,
            "result_verification_contract": blocked_contract,
        })

    op = validation["operation"]

    if op == "SELECT":

        started = time.perf_counter()

        try:
            columns, rows, total_row_count = _run_readonly(sql, request.table_name)
        except HTTPException as error:
            validation["valid"] = False
            validation["semantic_validation"]["issues"].append({
                "type": "execution_error",
                "message": str(error.detail),
                "missing_object": None,
                "requirement": "Execute the validated read-only query successfully.",
                "sql_section": "execution",
                "recommendation": "Review the execution error and correct the query before retrying.",
                "critical": False,
            })
            reliability_id = _save_execution_reliability(request, validation, "FAILED")
            trace_query_id = _persist_query_run_trace(
                question=request.question,
                sql=sql,
                table_name=request.table_name,
                validation=validation,
                status="FAILED",
                error_message=str(error.detail),
                reliability_id=reliability_id,
            )
            try:
                ctx = SchemaContext(request.table_name, DB_PATH)
                evidence = ResultVerificationEngine().validate(sql, ctx, get_tables(DB_PATH))
                failed_contract = ResultVerificationEngine().verify(
                    {
                        "status": "RUNTIME_ERROR",
                        "issues": validation.get("semantic_validation", {}).get("issues", []),
                        "semantic_status": validation.get("semantic_validation", {}).get("status"),
                        "coverage_status": validation.get("semantic_validation", {}).get("sql_coverage_status"),
                        "database_source": request.table_name,
                    },
                    evidence,
                    question=request.question,
                    sql=sql,
                ).to_json_dict()
                error.detail = (
                    {
                        **(error.detail if isinstance(error.detail, dict) else {}),
                        "trace_query_id": trace_query_id,
                        "reliability_id": reliability_id,
                        "verification": failed_contract,
                        "result_verification_contract": failed_contract,
                    }
                    if isinstance(error.detail, dict)
                    else {
                        "message": str(error.detail),
                        "trace_query_id": trace_query_id,
                        "reliability_id": reliability_id,
                        "verification": failed_contract,
                        "result_verification_contract": failed_contract,
                    }
                )
            except Exception:
                logger.exception("Could not build runtime failure result verification contract")
            raise

        duration = round((time.perf_counter() - started) * 1000, 2)

        truncated = len(rows) > 500

        rows = rows[:500]
        execution_metadata = _result_verification_metadata(
            columns,
            rows,
            truncated,
            table_name=request.table_name,
            duration_ms=duration,
            total_row_count=total_row_count,
        )
        reliability_id = _save_execution_reliability(
            request,
            validation,
            "SUCCESS",
            execution_metadata,
        )

        log_history(request.question, sql, "SUCCESS", len(rows), duration, "", DB_PATH)

        try:
            ctx = SchemaContext(request.table_name, DB_PATH)
            evidence = ResultVerificationEngine().validate(sql, ctx, get_tables(DB_PATH))
            semantic = validation.get("semantic_validation", {})
            verification = ResultVerificationEngine().verify(
                {
                    "status": "SUCCESS",
                    "issues": semantic.get("issues", []),
                    "semantic_status": semantic.get("status"),
                    "coverage_status": semantic.get("sql_coverage_status"),
                    "database_source": request.table_name,
                    "actual_columns": columns,
                    "actual_column_count": len(columns),
                    "actual_row_count": len(rows),
                    "total_result_count": total_row_count,
                    "truncated": truncated,
                    "execution_time_ms": duration,
                },
                evidence,
                question=request.question,
                sql=sql,
            )
            result_verification_contract = verification.to_json_dict()
        except Exception:
            logger.exception("Failed to create traceability contract for query execution")
            result_verification_contract = None

        trace_query_id = _persist_query_run_trace(
            question=request.question,
            sql=sql,
            table_name=request.table_name,
            validation=validation,
            status="SUCCESS",
            row_count=len(rows),
            duration_ms=duration,
            truncated=truncated,
            actual_columns=columns,
            total_result_count=total_row_count,
            reliability_id=reliability_id,
            result_verification_metadata=execution_metadata,
        )

        return {
            "status": "SUCCESS",
            "columns": columns,
            "rows": _json_value(rows),
            "row_count": len(rows),
            "truncated": truncated,
            "duration_ms": duration,
            "validation": validation,
            "reliability": validation["reliability"],
            "result_verification": execution_metadata,
            "reliability_id": reliability_id,
            "trace_query_id": trace_query_id,
            "result_verification_contract": result_verification_contract,
            "verification": result_verification_contract,
            "stage": "complete",
        }

    raise HTTPException(403, "Only read-only SELECT statements are supported.")

def history(limit: int = 100) -> dict[str, Any]:

    return {"items": get_history(max(1, min(limit, 500)), DB_PATH), "stats": get_history_stats(DB_PATH)}

def reliability_overview() -> dict[str, Any]:

    return reliability_summary(DB_PATH)

def reliability_test_catalog() -> dict[str, Any]:

    return {"items": get_reliability_test_cases()}

def run_reliability_tests(request: ReliabilityTestRunRequest) -> dict[str, Any]:

    _table_or_404(request.table_name)
    test_cases = get_reliability_test_cases()
    if request.test_id:
        test_cases = [case for case in test_cases if case["id"] == request.test_id]
        if not test_cases:
            raise HTTPException(404, "Reliability test case was not found.")

    schema_ctx = SchemaContext(request.table_name, DB_PATH)
    schema = schema_ctx.to_schema_text()
    prechecks = {
        case["id"]: semantic_validate(
            case["input"],
            "",
            schema_ctx,
            DB_PATH,
        )
        for case in test_cases
    }
    if not os.getenv("NVIDIA_API_KEY") and any(
        not any(issue["critical"] for issue in prechecks[case["id"]]["issues"])
        for case in test_cases
    ):
        raise HTTPException(
            503,
            "The configured AI provider is unavailable because NVIDIA_API_KEY is not set; run schema-rejection tests individually or configure the provider.",
        )
    results = []
    for case in test_cases:
        question = case["input"]
        precheck = prechecks[case["id"]]
        if any(issue["critical"] for issue in precheck["issues"]):
            sql = ""
        else:
            sql = generate_sql(question, schema, request.table_name)
        validation = _validate(sql, question, request.table_name)
        execution_status = "Not executed"
        result_verification = None
        if validation["valid"]:
            try:
                columns, rows, total_row_count = _run_readonly(sql, request.table_name)
            except HTTPException as error:
                validation["valid"] = False
                validation["semantic_validation"]["issues"].append({
                    "type": "execution_error",
                    "message": str(error.detail),
                    "missing_object": None,
                    "requirement": "Execute the validated read-only query successfully.",
                    "sql_section": "execution",
                    "recommendation": "Review the execution error before retrying the test.",
                    "critical": False,
                })
                execution_status = "FAILED"
                validation["reliability"] = score_reliability(
                    validation,
                    validation["semantic_validation"],
                    execution={"status": execution_status},
                )
                validation["validation_checks"]["result"] = "failed"
            else:
                truncated = len(rows) > 500
                rows = rows[:500]
                result_verification = _result_verification_metadata(
                    columns,
                    rows,
                    truncated,
                    table_name=request.table_name,
                    total_row_count=total_row_count,
                )
                execution_status = "SUCCESS"
                validation["reliability"] = score_reliability(
                    validation,
                    validation["semantic_validation"],
                    execution={"status": execution_status},
                )
                validation["validation_checks"]["result"] = "verified"
        precheck_requires_rejection = any(
            issue["critical"] for issue in precheck["issues"]
        )
        expected_rejection = bool(case["must_reject"] or precheck_requires_rejection)
        if case["id"] == "constant-false-fallback":
            detected_false_fallback = any(
                issue["type"] == "unrelated_sql"
                for issue in validation["semantic_validation"]["issues"]
            )
            passed = detected_false_fallback or (
                validation["valid"]
                and "where 0" not in sql.casefold()
                and "where false" not in sql.casefold()
            )
        elif expected_rejection:
            observed_issue_types = {
                issue["type"]
                for issue in validation["semantic_validation"]["issues"]
            }
            passed = (
                not validation["valid"]
                and (
                    validation["reliability"]["critical_failure"]
                    or precheck_requires_rejection
                )
                and (
                    not case.get("expected_issue_types")
                    or bool(observed_issue_types.intersection(case["expected_issue_types"]))
                )
            )
        else:
            passed = validation["valid"]

        semantic_issues = validation["semantic_validation"]["issues"]
        actual_response = (
            (
                f"The query executed successfully and returned {result_verification['rows_returned']} rows."
                if result_verification["rows_returned"]
                else "The query executed successfully and returned zero rows; this is a valid empty result."
            )
            if execution_status == "SUCCESS"
            else "Rejected: " + "; ".join(
                issue["message"] for issue in semantic_issues
            ) if semantic_issues else "; ".join(validation["confidence_reasons"])
        )
        result = {
            "test_id": case["id"],
            "category": case["category"],
            "test_input": question,
            "expected_behavior": case["expected_behavior"],
            "actual_response": actual_response,
            "generated_sql": sql,
            "schema_validation_result": {
                "status": (
                    "not_evaluated"
                    if validation["schema_valid"] is None
                    else "valid" if validation["schema_valid"] else "invalid"
                ),
                "issues": (
                    validation["hallucination"].get("errors", [])
                    if validation["hallucination"]
                    else semantic_issues
                ),
            },
            "semantic_validation_result": validation["semantic_validation"],
            "execution_status": execution_status,
            "result_verification": result_verification,
            "accuracy_score": validation["reliability"]["answer_accuracy_score"],
            "hallucination_risk_score": validation["reliability"]["hallucination_risk_score"],
            "hallucination_risk_level": validation["reliability"]["hallucination_risk_level"],
            "status": "PASS" if passed else "FAIL",
        }
        record_evaluation(
            DB_PATH,
            question=question,
            table_name=request.table_name,
            sql=sql,
            validation=validation,
            source="reliability_test",
            is_test_case=True,
            hallucination_case=bool(case.get("hallucination_case") or expected_rejection),
            provider_model=os.getenv("NVIDIA_MODEL", "configured provider"),
            execution_status=execution_status,
            execution_metadata=result_verification,
            passed_override=passed,
        )
        results.append(result)
    return {
        "results": results,
        "passed": sum(result["status"] == "PASS" for result in results),
        "failed": sum(result["status"] == "FAIL" for result in results),
        "total": len(results),
    }

def settings() -> dict[str, Any]:

    return {"backend": "connected", "database_exists": Path(DB_PATH).exists(), "provider_configured": bool(os.getenv("NVIDIA_API_KEY")), "model": os.getenv("NVIDIA_MODEL", "mistralai/mistral-large-4-1t-a52b"), "secrets_sent_to_browser": False, "limits": {"upload_bytes": MAX_UPLOAD_BYTES, "query_timeout_seconds": 5, "result_preview_rows": 500}}

def export_table(table_name: str):

    _table_or_404(table_name)

    with sqlite3.connect(Path(DB_PATH).as_uri() + "?mode=ro", uri=True) as conn:

        frame = pd.read_sql_query(f'SELECT * FROM "{table_name.replace(chr(34), chr(34)*2)}" LIMIT 100000', conn)

    csv_text = frame.to_csv(index=False)
    return PlainTextResponse(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table_name}.csv"'},
    )

# ============================================================================

# Hallucination Detection & Reduction Endpoints

# ============================================================================

class HallucinationDetectRequest(BaseModel):

    question: str = ""

    sql: str = Field(min_length=1, max_length=20000)

    table_name: str = Field(min_length=1, max_length=100)

class HallucinationReduceRequest(BaseModel):

    question: str = ""

    sql: str = Field(min_length=1, max_length=20000)

    table_name: str = Field(min_length=1, max_length=100)

    max_repair_attempts: int = Field(default=2, ge=0, le=5)

class HallucinationIssue(BaseModel):

    severity: str  # "error" | "warning" | "info"

    category: str  # "schema" | "syntax" | "intent" | "relationship" | "function"

    message: str

    evidence: str

    location: str | None = None  # e.g., column name, table name

    suggestion: str | None = None

class HallucinationDetectResponse(BaseModel):

    is_valid: bool

    issues: list[HallucinationIssue]

    confidence_before: float

    confidence_label: str

    validation_details: dict[str, Any]

class HallucinationReduceResponse(BaseModel):

    original_sql: str

    corrected_sql: str | None

    issues_before: list[HallucinationIssue]

    issues_after: list[HallucinationIssue]

    confidence_before: float

    confidence_after: float

    confidence_change: float

    validation_checklist: dict[str, bool]

    unresolved_issues: list[HallucinationIssue]

    repair_summary: list[str]

def _compute_confidence(validation: dict[str, Any], hallucination: dict[str, Any], grounded: dict[str, Any]) -> float:

    """Compute a transparent confidence score based on available validation evidence."""

    checks_completed = 0

    checks_passed = 0

    # Syntax check

    checks_completed += 1

    if validation.get("syntax_passed"):

        checks_passed += 1

    # Schema check

    checks_completed += 1

    if validation.get("schema_passed") and hallucination.get("is_valid"):

        checks_passed += 1

    # Intent check

    checks_completed += 1

    intent = grounded.get("stages", {}).get("intent", {})

    if intent.get("likely_answers_question"):

        checks_passed += 1

    # Hazard level check (read-only is safer)

    checks_completed += 1

    if validation.get("hazard_level", 3) == 0:

        checks_passed += 1

    if checks_completed == 0:

        return 0.0

    return round((checks_passed / checks_completed) * 100, 1)

def _build_issues(validation: dict[str, Any], hallucination: dict[str, Any], grounded: dict[str, Any], sql: str) -> list[HallucinationIssue]:

    """Build structured hallucination issues from validation results."""

    issues = []

    # Syntax issues

    if not validation.get("syntax_passed"):

        issues.append(HallucinationIssue(

            severity="error",

            category="syntax",

            message="SQL syntax error",

            evidence=validation.get("syntax_error", "Syntax validation failed"),

            location="query",

            suggestion="Fix the SQL syntax error reported by SQLite"

        ))

    # Schema issues - invalid tables

    for table in hallucination.get("invalid_tables", []):

        issues.append(HallucinationIssue(

            severity="error",

            category="schema",

            message=f"Table '{table}' does not exist",

            evidence=f"Query references table '{table}' which is not in the schema",

            location=table,

            suggestion=f"Remove reference to '{table}' or use a valid table name"

        ))

    # Schema issues - invalid columns

    for column in hallucination.get("invalid_columns", []):

        issues.append(HallucinationIssue(

            severity="error",

            category="schema",

            message=f"Column '{column}' does not exist",

            evidence=f"Query references column '{column}' which is not in the target table schema",

            location=column,

            suggestion=f"Replace '{column}' with a valid column name from the schema"

        ))

    # Intent issues

    intent = grounded.get("stages", {}).get("intent", {})

    for concern in intent.get("concerns", []):

        issues.append(HallucinationIssue(

            severity="warning",

            category="intent",

            message="Potential intent mismatch",

            evidence=concern,

            location="query",

            suggestion="Review the query to ensure it answers the original question"

        ))

    # Hazard level warnings

    hazard_level = validation.get("hazard_level", 0)

    if hazard_level == 2:

        issues.append(HallucinationIssue(

            severity="warning",

            category="relationship",

            message="Data mutation detected (UPDATE/DELETE)",

            evidence="Query modifies data; requires explicit confirmation",

            location="query",

            suggestion="Ensure mutation is intended and has appropriate WHERE clause"

        ))

    elif hazard_level == 3:

        issues.append(HallucinationIssue(

            severity="error",

            category="relationship",

            message="Destructive DDL operation blocked",

            evidence=f"Operation '{validation.get('operation')}' is prohibited",

            location="query",

            suggestion="Use only SELECT, UPDATE, or DELETE with proper constraints"

        ))

    # Schema details from audit

    if not validation.get("schema_passed") and validation.get("schema_details"):

        issues.append(HallucinationIssue(

            severity="error",

            category="schema",

            message="Schema validation failed",

            evidence=validation.get("schema_details"),

            location="query",

            suggestion="Ensure all table and column references exist in the schema"

        ))

    return issues

def _build_validation_checklist(validation: dict[str, Any], hallucination: dict[str, Any], grounded: dict[str, Any]) -> dict[str, bool]:

    """Build a validation checklist showing passed/failed checks."""

    return {

        "syntax_valid": bool(validation.get("syntax_passed")),

        "schema_valid": bool(validation.get("schema_passed")) and bool(hallucination.get("is_valid")),

        "intent_consistent": bool(grounded.get("stages", {}).get("intent", {}).get("passed")),

        "read_only_safe": validation.get("hazard_level", 3) == 0,

        "single_statement": validation.get("security_gate") != "BLOCKED_DDL",

    }

def detect_hallucinations(request: HallucinationDetectRequest) -> HallucinationDetectResponse:

    """Detect potential hallucinations in SQL against the target schema."""

    schema = _schema(request.table_name)

    audit = audit_sql(request.sql, schema, request.table_name, request.question, DB_PATH)

    grounded = validate_sql_grounded(request.sql, request.question, request.table_name, DB_PATH)

    ctx = SchemaContext(request.table_name, DB_PATH)

    hallucination = HallucinationDetector.validate_against_schema(request.sql, ctx)

    issues = _build_issues(audit, hallucination, grounded, request.sql)

    confidence = _compute_confidence(audit, hallucination, grounded)

    checklist = _build_validation_checklist(audit, hallucination, grounded)

    return HallucinationDetectResponse(

        is_valid=bool(audit.get("is_valid")) and bool(hallucination.get("is_valid")) and grounded.get("is_valid", False),

        issues=issues,

        confidence_before=confidence,

        confidence_label="Not calibrated" if confidence < 100 else "High",

        validation_details={

            "audit": audit,

            "hallucination": hallucination,

            "pipeline": grounded,

            "checklist": checklist,

        }

    )

def reduce_hallucinations(request: HallucinationReduceRequest) -> HallucinationReduceResponse:

    """Detect hallucinations and attempt to produce a corrected SQL query."""

    schema = _schema(request.table_name)

    # Initial validation

    audit = audit_sql(request.sql, schema, request.table_name, request.question, DB_PATH)

    grounded = validate_sql_grounded(request.sql, request.question, request.table_name, DB_PATH)

    ctx = SchemaContext(request.table_name, DB_PATH)

    hallucination = HallucinationDetector.validate_against_schema(request.sql, ctx)

    issues_before = _build_issues(audit, hallucination, grounded, request.sql)

    confidence_before = _compute_confidence(audit, hallucination, grounded)

    checklist_before = _build_validation_checklist(audit, hallucination, grounded)

    # Attempt repair using the validation pipeline

    pipeline = SQLValidationPipeline(request.table_name, DB_PATH, max_repair_attempts=request.max_repair_attempts)

    repair_result = pipeline.validate_and_repair(request.sql, request.question)

    corrected_sql = repair_result.get("sql_final")

    # Validate corrected SQL if repair was attempted

    issues_after = issues_before

    confidence_after = confidence_before

    checklist_after = checklist_before

    repair_summary = []

    unresolved_issues = [i for i in issues_before if i.severity == "error"]

    if corrected_sql and corrected_sql != request.sql:

        # Re-validate corrected SQL

        audit_corrected = audit_sql(corrected_sql, schema, request.table_name, request.question, DB_PATH)

        grounded_corrected = validate_sql_grounded(corrected_sql, request.question, request.table_name, DB_PATH)

        hallucination_corrected = HallucinationDetector.validate_against_schema(corrected_sql, ctx)

        issues_after = _build_issues(audit_corrected, hallucination_corrected, grounded_corrected, corrected_sql)

        confidence_after = _compute_confidence(audit_corrected, hallucination_corrected, grounded_corrected)

        checklist_after = _build_validation_checklist(audit_corrected, hallucination_corrected, grounded_corrected)

        unresolved_issues = [i for i in issues_after if i.severity == "error"]

        # Build repair summary

        for attempt in repair_result.get("repair_history", []):

            repair_summary.append(f"Attempt {attempt['attempt']}: {len(attempt['suggestions'])} suggestions - {', '.join(attempt['suggestions'][:3])}")

        if not repair_result.get("repair_history"):

            # Check what changed

            if set(hallucination.get("invalid_columns", [])) != set(hallucination_corrected.get("invalid_columns", [])):

                repair_summary.append("Invalid column references corrected based on schema")

            if set(hallucination.get("invalid_tables", [])) != set(hallucination_corrected.get("invalid_tables", [])):

                repair_summary.append("Invalid table references corrected")
    elif corrected_sql is None and not repair_result.get("is_valid"):
        repair_summary.append(
            "Automatic repair could not resolve all schema violations; manual intervention required"
        )
        # Use suggestions already grounded in the validation findings. Do not
        # reference an optional repair class that may not be imported/available.
        for issue in issues_before:
            if issue.suggestion:
                item = f"{issue.location or issue.category}: {issue.suggestion}"
                if item not in repair_summary:
                    repair_summary.append(item)
    confidence_change = round(confidence_after - confidence_before, 1)
    return HallucinationReduceResponse(
        original_sql=request.sql,
        corrected_sql=corrected_sql,
        issues_before=issues_before,
        issues_after=issues_after,
        confidence_before=confidence_before,
        confidence_after=confidence_after,
        confidence_change=confidence_change,
        validation_checklist=checklist_after,
        unresolved_issues=unresolved_issues,
        repair_summary=repair_summary
    )


from backend.api import (
    dashboard,
    health,
    history as history_routes,
    reliability as reliability_routes,
    schema,
    settings as settings_routes,
    sql,
)

app.include_router(health.router)
app.include_router(dashboard.router)
app.include_router(schema.router)
app.include_router(sql.router)
app.include_router(history_routes.router)
app.include_router(reliability_routes.router)
app.include_router(settings_routes.router)
