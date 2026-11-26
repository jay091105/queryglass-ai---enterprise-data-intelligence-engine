"""Database-backed dataset ingestion workflow for QueryGlass AI.

Implements the user-controlled lifecycle:

    Upload -> raw data persisted in SQLite -> initial quality report ->
    (explicit user action) preprocessing run -> processed quality report +
    before/after comparison -> (explicit user action) processed dataset replaced.

Preprocessing writes its output to a hidden staging table
(``_queryglass_stage_*``). A validated staged result and removal of the prior
dataset identity's tables and metadata are committed in one SQLite transaction.
All state transitions are recorded in the ``_queryglass_*`` registry tables so
the workflow survives page refreshes and API restarts.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from backend.db.session import sanitize_table_name, save_to_sqlite
from backend.services.data_quality import analyze_quality, preprocess_dataframe
from backend.services.payload import json_value

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATASET_STATES = (
    "UPLOADED",
    "RAW_STORED",
    "ANALYZING",
    "RAW_ANALYZED",
    "PREPROCESSING",
    "PREPROCESSED",
    "PROCESSED_STORED",
    "FAILED",
)

# States from which a new preprocessing run may be claimed. All of them imply
# the raw dataset is durably stored and its initial quality report exists.
PREPROCESSABLE_STATES = {"RAW_ANALYZED", "PREPROCESSED", "PROCESSED_STORED"}

_STAGING_PREFIX = "_queryglass_stage_"
_INITIALIZED_DATABASES: dict[str, tuple[int, int]] = {}
_INITIALIZATION_LOCK = threading.Lock()


class WorkflowError(Exception):
    """Workflow rule violation or failure carrying an HTTP status code."""

    def __init__(self, message: str, status_code: int = 409, stage: str = "", dataset_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.stage = stage
        self.dataset_id = dataset_id


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_workflow_tables(db_path: str) -> None:
    """Idempotently create the workflow registry tables and indexes."""
    resolved_path = str(Path(db_path).resolve())
    try:
        stat = Path(resolved_path).stat()
        database_signature = (stat.st_size, stat.st_mtime_ns)
    except FileNotFoundError:
        database_signature = (0, 0)
    with _INITIALIZATION_LOCK:
        if _INITIALIZED_DATABASES.get(resolved_path) == database_signature:
            return
        schema_path = PROJECT_ROOT / "database" / "app_tables.sql"
        schema = schema_path.read_text(encoding="utf-8")
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(3):
            try:
                with closing(_connect(db_path)) as connection:
                    connection.executescript(schema)
                    columns = {
                        row["name"]
                        for row in connection.execute("PRAGMA table_info(_queryglass_datasets)")
                    }
                    if "dataset_identity" not in columns:
                        connection.execute(
                            "ALTER TABLE _queryglass_datasets ADD COLUMN dataset_identity TEXT"
                        )
                    connection.execute(
                        "UPDATE _queryglass_datasets SET dataset_identity=dataset_id "
                        "WHERE dataset_identity IS NULL OR dataset_identity=''"
                    )
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_queryglass_datasets_identity "
                        "ON _queryglass_datasets(dataset_identity)"
                    )
                    datasets = connection.execute(
                        "SELECT dataset_id, dataset_identity, filename, status "
                        "FROM _queryglass_datasets"
                    ).fetchall()
                    for row in datasets:
                        connection.execute(
                            "INSERT OR IGNORE INTO _queryglass_dataset_identities "
                            "(identity_id, normalized_filename, active_dataset_id) VALUES (?, ?, ?)",
                            (
                                row["dataset_identity"],
                                _normalize_filename(row["filename"]),
                                row["dataset_id"] if row["status"] == "PROCESSED_STORED" else None,
                            ),
                        )
                    active = connection.execute(
                        "SELECT dataset_id FROM _queryglass_active_dataset WHERE active_key='active'"
                    ).fetchone()
                    if active is None or (
                        active["dataset_id"]
                        and not connection.execute(
                            "SELECT 1 FROM _queryglass_datasets WHERE dataset_id=?",
                            (active["dataset_id"],),
                        ).fetchone()
                    ):
                        latest = connection.execute(
                            "SELECT dataset_id FROM _queryglass_datasets "
                            "WHERE status <> 'FAILED' ORDER BY created_at DESC, rowid DESC LIMIT 1"
                        ).fetchone()
                        connection.execute(
                            "INSERT OR REPLACE INTO _queryglass_active_dataset "
                            "(active_key, dataset_id, updated_at) VALUES ('active', ?, ?)",
                            (latest["dataset_id"] if latest else None, _now()),
                        )
                    connection.commit()
                try:
                    stat = Path(resolved_path).stat()
                    _INITIALIZED_DATABASES[resolved_path] = (stat.st_size, stat.st_mtime_ns)
                except FileNotFoundError:
                    _INITIALIZED_DATABASES[resolved_path] = (0, 0)
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or attempt == 2:
                    raise
                last_error = error
                threading.Event().wait(0.1 * (attempt + 1))
        if last_error is not None:
            raise last_error


def _normalize_filename(filename: str) -> str:
    return unicodedata.normalize("NFKC", Path(filename).name).strip().casefold()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _table_taken(db_path: str, table_name: str) -> bool:
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
    return row is not None


def _unique_table_name(db_path: str, base: str) -> str:
    candidate = base
    suffix = 2
    while _table_taken(db_path, candidate):
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def get_active_dataset_id(db_path: str) -> str | None:
    """Return the single backend-authoritative active uploaded dataset ID."""
    init_workflow_tables(db_path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT dataset_id FROM _queryglass_active_dataset WHERE active_key='active'"
        ).fetchone()
    return row["dataset_id"] if row else None


def get_active_dataset(db_path: str) -> dict[str, Any] | None:
    """Return metadata for the active uploaded dataset, if one is available."""
    init_workflow_tables(db_path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT d.dataset_id, d.dataset_identity, d.filename, d.raw_table, d.created_at, "
            "d.row_count, d.column_count, d.status "
            "FROM _queryglass_active_dataset a JOIN _queryglass_datasets d "
            "ON d.dataset_id=a.dataset_id WHERE a.active_key='active'"
        ).fetchone()
    return dict(row) if row else None


def _record_failed_upload(
    db_path: str,
    dataset_id: str,
    filename: str,
    columns_meta: str,
    row_count: int,
    column_count: int,
    error: str,
) -> None:
    """Keep the previous active dataset untouched while recording a failed attempt."""
    with closing(_connect(db_path)) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO _queryglass_datasets "
            "(dataset_id, dataset_identity, filename, raw_table, created_at, row_count, "
            "column_count, columns_meta, status, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'FAILED', ?)",
            (
                dataset_id,
                f"failed_{dataset_id}",
                filename,
                f"_failed_upload_{dataset_id}",
                _now(),
                row_count,
                column_count,
                columns_meta,
                error,
            ),
        )
        connection.commit()


def _drop_table_quietly(db_path: str, table_name: str | None) -> None:
    if not table_name:
        return
    try:
        with closing(_connect(db_path)) as connection:
            connection.execute(f"DROP TABLE IF EXISTS {_quote(table_name)}")
            connection.commit()
    except sqlite3.Error:
        logger.warning("Could not drop table %s", table_name, exc_info=True)


def _set_dataset_status(db_path: str, dataset_id: str, status: str, error: str | None = None) -> None:
    with closing(_connect(db_path)) as connection:
        connection.execute(
            "UPDATE _queryglass_datasets SET status=?, error_message=? WHERE dataset_id=?",
            (status, error, dataset_id),
        )
        connection.commit()


def _release_identity_processing(db_path: str, dataset_id: str) -> None:
    with closing(_connect(db_path)) as connection:
        connection.execute(
            "UPDATE _queryglass_dataset_identities SET processing_dataset_id=NULL "
            "WHERE processing_dataset_id=?",
            (dataset_id,),
        )
        connection.commit()


def _store_report(db_path: str, dataset_id: str, run_id: str | None, stage: str, metrics: dict[str, Any]) -> None:
    with closing(_connect(db_path)) as connection:
        connection.execute(
            "INSERT INTO _queryglass_quality_reports (dataset_id, run_id, stage, metrics, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (dataset_id, run_id, stage, json.dumps(json_value(metrics)), _now()),
        )
        connection.commit()


def _latest_report(db_path: str, dataset_id: str, stage: str, run_id: str | None = None) -> dict[str, Any] | None:
    with closing(_connect(db_path)) as connection:
        if run_id is None:
            row = connection.execute(
                "SELECT metrics, created_at FROM _queryglass_quality_reports "
                "WHERE dataset_id=? AND stage=? AND run_id IS NULL ORDER BY report_id DESC LIMIT 1",
                (dataset_id, stage),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT metrics, created_at FROM _queryglass_quality_reports "
                "WHERE dataset_id=? AND stage=? AND run_id=? ORDER BY report_id DESC LIMIT 1",
                (dataset_id, stage, run_id),
            ).fetchone()
    if row is None:
        return None
    return {"stage": stage, "created_at": row["created_at"], "metrics": json.loads(row["metrics"])}


# ---------------------------------------------------------------------------
# Option validation
# ---------------------------------------------------------------------------

_ALLOWED_OPTIONS = {
    "remove_duplicates": bool,
    "duplicate_subset": list,
    "missing_value_strategy": str,
    "numeric_fill_value": (str, int, float),
    "categorical_fill_value": (str, int, float),
    "column_strategies": dict,
    "drop_columns": list,
    "clean_whitespace": bool,
    "standardize_column_names": bool,
    "convert_data_types": bool,
    "standardize_categorical": bool,
    "invalid_value_strategy": str,
    "outlier_strategy": str,
    "remove_empty_rows": bool,
    "remove_empty_columns": bool,
}


def validate_preprocess_options(options: dict[str, Any]) -> dict[str, Any]:
    """Whitelist and type-check user-supplied preprocessing options."""
    cleaned: dict[str, Any] = {}
    for key, value in (options or {}).items():
        expected = _ALLOWED_OPTIONS.get(key)
        if expected is None:
            raise WorkflowError(f"Unknown preprocessing option '{key}'.", 422, "validate_options")
        if isinstance(expected, tuple):
            if not isinstance(value, expected):
                raise WorkflowError(f"Option '{key}' must be one of {[t.__name__ for t in expected]}.", 422, "validate_options")
        elif not isinstance(value, expected):
            raise WorkflowError(f"Option '{key}' must be a {expected.__name__}.", 422, "validate_options")
        cleaned[key] = value
    return cleaned


# ---------------------------------------------------------------------------
# Upload / raw persistence
# ---------------------------------------------------------------------------

def ingest_upload(
    db_path: str,
    filename: str,
    frame: pd.DataFrame,
    dataset_identity: str | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Atomically replace the application's active uploaded dataset.

    The new payload is first written to an isolated upload staging table and
    analyzed without touching the active dataset. Publishing the replacement,
    removing only tables registered as workflow-managed, and changing the
    active pointer happen in one SQLite transaction.
    """
    init_workflow_tables(db_path)

    columns = [str(c) for c in frame.columns]
    if len(set(columns)) != len(columns):
        duplicates = sorted({c for c in columns if columns.count(c) > 1})
        raise WorkflowError(
            f"Dataset contains duplicate column names ({', '.join(duplicates)}); "
            "rename them so every column can be stored faithfully.",
            422,
            "parse",
        )
    if frame.empty:
        raise WorkflowError("Uploaded dataset contains no data rows.", 422, "parse")

    dataset_id = f"ds_{uuid.uuid4().hex[:16]}"
    identity_id = dataset_identity or dataset_id
    created_at = _now()
    columns_meta = json.dumps([{"name": str(c), "dtype": str(frame[c].dtype)} for c in frame.columns])
    stage_table = f"_queryglass_stage_upload_{uuid.uuid4().hex}"
    if progress_callback:
        progress_callback("analyzing", 0, len(frame))
    try:
        metrics = analyze_quality(frame)
    except Exception as exc:
        logger.exception("ingest_upload - quality analysis failed for %s", dataset_id)
        _record_failed_upload(
            db_path, dataset_id, filename, columns_meta, len(frame), len(frame.columns),
            f"Quality analysis failed: {exc}",
        )
        raise WorkflowError(f"Initial quality analysis failed: {exc}", 500, "analyze") from exc

    try:
        if progress_callback:
            progress_callback("storing", 0, len(frame))
        save_to_sqlite(
            frame,
            stage_table,
            db_path,
            if_exists="fail",
            progress_callback=(
                (lambda processed, total: progress_callback("storing", processed, total))
                if progress_callback else None
            ),
        )
    except Exception as exc:
        logger.exception("ingest_upload - raw staging persistence failed for %s", dataset_id)
        _drop_table_quietly(db_path, stage_table)
        message = f"Raw data could not be persisted to the database, so preprocessing stays unavailable. Error: {exc}"
        _record_failed_upload(
            db_path, dataset_id, filename, columns_meta, len(frame), len(frame.columns), message,
        )
        raise WorkflowError(message, 500, "raw_store", dataset_id=dataset_id) from exc

    raw_table = ""
    try:
        with closing(_connect(db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")

            active_work = connection.execute(
                "SELECT d.dataset_id FROM _queryglass_active_dataset a "
                "JOIN _queryglass_datasets d ON d.dataset_id=a.dataset_id "
                "WHERE a.active_key='active' AND d.status='PREPROCESSING'"
            ).fetchone()
            if active_work:
                raise WorkflowError(
                    "The active dataset is being preprocessed. Wait for that run to finish before uploading a replacement.",
                    409,
                    "replace",
                )

            old_datasets = connection.execute(
                "SELECT dataset_id, raw_table FROM _queryglass_datasets"
            ).fetchall()
            old_versions = connection.execute(
                "SELECT table_name FROM _queryglass_processed_versions"
            ).fetchall()
            old_stages = connection.execute(
                "SELECT staged_table FROM _queryglass_processing_runs WHERE staged_table IS NOT NULL"
            ).fetchall()

            managed_tables = {
                row["raw_table"] for row in old_datasets if row["raw_table"]
            }
            managed_tables.update(row["table_name"] for row in old_versions if row["table_name"])
            managed_tables.update(row["staged_table"] for row in old_stages if row["staged_table"])
            for table_name in managed_tables:
                if table_name != stage_table:
                    connection.execute(f"DROP TABLE IF EXISTS {_quote(table_name)}")

            connection.execute("DELETE FROM _queryglass_quality_reports")
            connection.execute("DELETE FROM _queryglass_processed_versions")
            connection.execute("DELETE FROM _queryglass_processing_runs")
            connection.execute("DELETE FROM _queryglass_table_fingerprints")
            connection.execute("DELETE FROM _queryglass_effective_types")
            connection.execute("DELETE FROM _queryglass_datasets")
            connection.execute("DELETE FROM _queryglass_dataset_identities")

            base_name = sanitize_table_name(Path(filename or "dataset").stem) or "imported_dataset"
            raw_table = base_name
            suffix = 2
            while connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (raw_table,),
            ).fetchone():
                raw_table = f"{base_name}_{suffix}"
                suffix += 1

            connection.execute(f"ALTER TABLE {_quote(stage_table)} RENAME TO {_quote(raw_table)}")
            connection.execute(
                "INSERT INTO _queryglass_datasets "
                "(dataset_id, dataset_identity, filename, raw_table, created_at, row_count, "
                "column_count, columns_meta, status, error_message) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RAW_ANALYZED', NULL)",
                (
                    dataset_id, identity_id, filename, raw_table, created_at,
                    len(frame), len(frame.columns), columns_meta,
                ),
            )
            connection.execute(
                "INSERT INTO _queryglass_dataset_identities "
                "(identity_id, normalized_filename, active_dataset_id, processing_dataset_id) "
                "VALUES (?, ?, NULL, NULL)",
                (identity_id, _normalize_filename(filename)),
            )
            connection.execute(
                "INSERT INTO _queryglass_quality_reports "
                "(dataset_id, run_id, stage, metrics, created_at) VALUES (?, NULL, 'raw', ?, ?)",
                (dataset_id, json.dumps(json_value(metrics)), created_at),
            )
            connection.execute(
                "INSERT OR REPLACE INTO _queryglass_active_dataset "
                "(active_key, dataset_id, updated_at) VALUES ('active', ?, ?)",
                (dataset_id, created_at),
            )
            connection.commit()
        if progress_callback:
            progress_callback("publishing", len(frame), len(frame))
    except WorkflowError:
        _drop_table_quietly(db_path, stage_table)
        raise
    except Exception as exc:
        logger.exception("ingest_upload - replacement transaction failed for %s", dataset_id)
        _drop_table_quietly(db_path, stage_table)
        message = f"Dataset replacement failed; the previously active dataset was preserved. Error: {exc}"
        _record_failed_upload(
            db_path, dataset_id, filename, columns_meta, len(frame), len(frame.columns), message,
        )
        raise WorkflowError(message, 500, "replace", dataset_id=dataset_id) from exc

    logger.info(
        "ingest_upload - dataset %s stored as %s (%s rows, %s columns)",
        dataset_id, raw_table, len(frame), len(frame.columns),
    )
    try:
        from backend.services.table_fingerprinter import TableFingerprinter
        from backend.services.effective_type_profiler import EffectiveTypeProfiler
        fingerprinter = TableFingerprinter()
        fingerprint = fingerprinter.compute_fingerprint(db_path, raw_table, dataset_id)
        fingerprinter.save_fingerprint(db_path, fingerprint)
        profiler = EffectiveTypeProfiler()
        profile = profiler.profile_table(db_path, raw_table, dataset_id)
        profiler.save_profile(db_path, profile)
    except Exception:
        logger.exception(
            "ingest_upload - fingerprint/effective-type profiling failed for %s; publish remains valid",
            dataset_id,
        )
    return get_dataset_snapshot(db_path, dataset_id)


# ---------------------------------------------------------------------------
# Snapshots / listing
# ---------------------------------------------------------------------------

def _parse_run(row: sqlite3.Row) -> dict[str, Any]:
    transformations = json.loads(row["transformations"]) if row["transformations"] else None
    if isinstance(transformations, dict):
        # Early runs stored the full report dict; normalize to the entry list.
        transformations = transformations.get("transformations", [])
    return {
        "run_id": row["run_id"],
        "dataset_id": row["dataset_id"],
        "status": row["status"],
        "options": json.loads(row["operation_config"]),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "rows_before": row["rows_before"],
        "cols_before": row["cols_before"],
        "rows_after": row["rows_after"],
        "cols_after": row["cols_after"],
        "transformations": transformations,
        "error_details": row["error_details"],
        "finalized": bool(row["finalized"]),
    }


def _comparison(raw: dict[str, Any], processed: dict[str, Any]) -> dict[str, Any]:
    def metric(report: dict[str, Any], key: str) -> Any:
        return report["metrics"].get(key)

    fields = [
        ("total_rows", lambda r: metric(r, "rows")),
        ("total_columns", lambda r: metric(r, "columns")),
        ("total_cells", lambda r: metric(r, "total_cells")),
        ("missing_cells", lambda r: metric(r, "missing_cells")),
        ("missing_pct", lambda r: metric(r, "missing_pct")),
        ("rows_with_missing", lambda r: metric(r, "rows_with_missing")),
        ("duplicate_rows", lambda r: metric(r, "duplicate_rows")),
        ("duplicate_pct", lambda r: metric(r, "duplicate_pct")),
        ("invalid_values", lambda r: metric(r, "invalid_values")),
        ("type_mismatches", lambda r: metric(r, "type_mismatches")["total"] if isinstance(metric(r, "type_mismatches"), dict) else metric(r, "type_mismatches")),
        ("empty_rows", lambda r: metric(r, "empty_rows")),
        ("empty_columns", lambda r: len(metric(r, "empty_columns") or [])),
        ("outliers", lambda r: metric(r, "outliers")["total"] if isinstance(metric(r, "outliers"), dict) else 0),
        ("constant_columns", lambda r: len(metric(r, "constant_columns") or [])),
        ("total_issues", lambda r: metric(r, "total_issues")),
    ]
    rows = []
    for name, getter in fields:
        before, after = getter(raw), getter(processed)
        try:
            delta = after - before if isinstance(before, (int, float)) and isinstance(after, (int, float)) else None
        except TypeError:
            delta = None
        if isinstance(delta, float):
            delta = round(delta, 2)
        rows.append({"metric": name, "before": before, "after": after, "delta": delta})
    total_before = raw["metrics"].get("total_issues")
    total_after = processed["metrics"].get("total_issues")
    return {
        "rows": rows,
        "total_issues_before": total_before,
        "total_issues_after": total_after,
        "improved": isinstance(total_before, (int, float)) and isinstance(total_after, (int, float)) and total_after < total_before,
        "counting_rules": raw["metrics"].get("issue_counting_rules"),
    }


def _recover_one_stuck_dataset(db_path: str, dataset_id: str, raw_table: str) -> None:
    """Finish or fail a single upload that a restart or crash interrupted."""
    if not _table_taken(db_path, raw_table):
        _set_dataset_status(
            db_path, dataset_id, "FAILED",
            "Raw data was lost before analysis completed; upload the file again.",
        )
        _release_identity_processing(db_path, dataset_id)
        return
    if _latest_report(db_path, dataset_id, "raw") is not None:
        _set_dataset_status(db_path, dataset_id, "RAW_ANALYZED")
        _release_identity_processing(db_path, dataset_id)
        return
    try:
        with closing(_connect(db_path)) as connection:
            frame = pd.read_sql_query(f"SELECT * FROM {_quote(raw_table)}", connection)
        metrics = analyze_quality(frame)
        _store_report(db_path, dataset_id, None, "raw", metrics)
        _set_dataset_status(db_path, dataset_id, "RAW_ANALYZED")
        _release_identity_processing(db_path, dataset_id)
        logger.info("Recovered interrupted analysis for dataset %s", dataset_id)
    except Exception as exc:
        logger.exception("Recovery analysis failed for dataset %s", dataset_id)
        _set_dataset_status(db_path, dataset_id, "FAILED", f"Quality analysis failed: {exc}")
        _release_identity_processing(db_path, dataset_id)


_recovery_in_progress: set[str] = set()


def _recover_stuck_datasets(db_path: str) -> None:
    """Dispatch background recovery for uploads interrupted by a crash.

    Large analyses run in a daemon thread so listing and snapshots return the
    persisted ANALYZING state immediately; the UI polls until it resolves.
    """
    with closing(_connect(db_path)) as connection:
        stuck = connection.execute(
            "SELECT dataset_id, raw_table FROM _queryglass_datasets "
            "WHERE status IN ('UPLOADED','ANALYZING')"
        ).fetchall()
    pending = [row for row in stuck if row["dataset_id"] not in _recovery_in_progress]
    if not pending:
        return
    for row in pending:
        _recovery_in_progress.add(row["dataset_id"])

    def _run() -> None:
        try:
            for row in pending:
                _recover_one_stuck_dataset(db_path, row["dataset_id"], row["raw_table"])
        finally:
            for row in pending:
                _recovery_in_progress.discard(row["dataset_id"])

    threading.Thread(target=_run, daemon=True, name="queryglass-workflow-recovery").start()


def get_dataset_snapshot(db_path: str, dataset_id: str) -> dict[str, Any] | None:
    init_workflow_tables(db_path)
    _recover_stuck_datasets(db_path)
    with closing(_connect(db_path)) as connection:
        dataset = connection.execute(
            "SELECT * FROM _queryglass_datasets WHERE dataset_id=?", (dataset_id,)
        ).fetchone()
        if dataset is None:
            return None
        runs = [
            _parse_run(row)
            for row in connection.execute(
                "SELECT * FROM _queryglass_processing_runs WHERE dataset_id=? ORDER BY started_at DESC, rowid DESC",
                (dataset_id,),
            ).fetchall()
        ]
        versions = [
            {
                "version_id": row["version_id"],
                "dataset_id": row["dataset_id"],
                "run_id": row["run_id"],
                "table_name": row["table_name"],
                "row_count": row["row_count"],
                "column_count": row["column_count"],
                "operation_config": json.loads(row["operation_config"]),
                "created_at": row["created_at"],
            }
            for row in connection.execute(
                "SELECT * FROM _queryglass_processed_versions WHERE dataset_id=? ORDER BY created_at DESC, rowid DESC",
                (dataset_id,),
            ).fetchall()
        ]

    raw_report = _latest_report(db_path, dataset_id, "raw")
    latest_completed = next((run for run in runs if run["status"] == "COMPLETED"), None)
    processed_report = (
        _latest_report(db_path, dataset_id, "processed", latest_completed["run_id"]) if latest_completed else None
    )

    snapshot = {
        "dataset_id": dataset["dataset_id"],
        "dataset_identity": dataset["dataset_identity"],
        "filename": dataset["filename"],
        "raw_table": dataset["raw_table"],
        "created_at": dataset["created_at"],
        "row_count": dataset["row_count"],
        "column_count": dataset["column_count"],
        "columns": json.loads(dataset["columns_meta"]),
        "status": dataset["status"],
        "error_message": dataset["error_message"],
        "raw_report": raw_report,
        "processed_report": processed_report,
        "runs": runs,
        "latest_run": runs[0] if runs else None,
        "versions": versions,
        "allowed_actions": _allowed_actions(dataset["status"], runs),
    }
    if raw_report and processed_report:
        snapshot["comparison"] = _comparison(raw_report, processed_report)
    return snapshot


def _allowed_actions(status: str, runs: list[dict[str, Any]]) -> dict[str, bool]:
    latest = runs[0] if runs else None
    can_preprocess = status in PREPROCESSABLE_STATES
    can_store = bool(
        latest
        and latest["status"] == "COMPLETED"
        and not latest["finalized"]
        and status in {"PREPROCESSED", "PROCESSED_STORED"}
    )
    return {
        "preprocess": can_preprocess,
        "store_processed": can_store,
        "raw_available": status in {*PREPROCESSABLE_STATES, "PREPROCESSING"},
    }


def list_datasets(db_path: str) -> list[dict[str, Any]]:
    init_workflow_tables(db_path)
    _recover_stuck_datasets(db_path)
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT d.dataset_id, d.filename, d.raw_table, d.created_at, d.status, "
            "d.row_count, d.column_count, d.error_message, "
            "(SELECT COUNT(*) FROM _queryglass_processed_versions v WHERE v.dataset_id = d.dataset_id) AS version_count "
            "FROM _queryglass_datasets d JOIN _queryglass_active_dataset a ON a.dataset_id=d.dataset_id "
            "WHERE a.active_key='active' ORDER BY d.created_at DESC, d.rowid DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def is_registered_raw_table(db_path: str, table_name: str) -> bool:
    """True when table_name is the durable raw table of a workflow dataset.

    Used to keep the legacy in-place preprocess endpoint from overwriting raw
    data that the workflow guarantees to preserve.
    """
    try:
        with closing(_connect(db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM _queryglass_datasets WHERE raw_table=? LIMIT 1", (table_name,)
            ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


# ---------------------------------------------------------------------------
# Preprocessing runs
# ---------------------------------------------------------------------------

def start_preprocess_run(db_path: str, dataset_id: str, options: dict[str, Any]) -> dict[str, Any]:
    """Claim the dataset for a preprocessing run, execute it, record reports.

    The claim is a transactional state transition, so two concurrent requests
    cannot both preprocess the same dataset and a crash mid-run leaves a
    recoverable RUNNING row that is marked FAILED by the next attempt.
    """
    init_workflow_tables(db_path)
    options = validate_preprocess_options(options)

    with closing(_connect(db_path)) as connection:
        dataset = connection.execute(
            "SELECT dataset_id, dataset_identity, filename, raw_table, status "
            "FROM _queryglass_datasets WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if dataset is None:
            raise WorkflowError("Dataset was not found.", 404, "claim")
        if dataset["status"] == "PREPROCESSING":
            raise WorkflowError(
                "A preprocessing run is already in progress for this dataset.", 409, "claim"
            )
        if dataset["status"] not in PREPROCESSABLE_STATES:
            raise WorkflowError(
                f"Preprocessing is not allowed while the dataset status is '{dataset['status']}'. "
                "Raw data must be stored and analyzed first.",
                409,
                "claim",
            )
        active = connection.execute(
            "SELECT dataset_id FROM _queryglass_active_dataset WHERE active_key='active'"
        ).fetchone()
        if active is None or active["dataset_id"] != dataset_id:
            raise WorkflowError("Only the currently active uploaded dataset can be preprocessed.", 409, "claim")

        connection.execute("BEGIN IMMEDIATE")
        try:
            claimed = connection.execute(
                "UPDATE _queryglass_datasets SET status='PREPROCESSING' "
                "WHERE dataset_id=? AND status IN ('RAW_ANALYZED','PREPROCESSED','PROCESSED_STORED')",
                (dataset_id,),
            ).rowcount
            if claimed == 0:
                raise WorkflowError(
                    "Another preprocessing run started concurrently for this dataset.", 409, "claim"
                )
            identity_lock = connection.execute(
                "SELECT processing_dataset_id, active_dataset_id "
                "FROM _queryglass_dataset_identities WHERE identity_id=?",
                (dataset["dataset_identity"],),
            ).fetchone()
            if identity_lock and identity_lock["processing_dataset_id"] not in (None, dataset_id):
                locked_dataset = connection.execute(
                    "SELECT status FROM _queryglass_datasets WHERE dataset_id=?",
                    (identity_lock["processing_dataset_id"],),
                ).fetchone()
                if locked_dataset is None or locked_dataset["status"] != "PREPROCESSING":
                    connection.execute(
                        "UPDATE _queryglass_dataset_identities SET processing_dataset_id=NULL "
                        "WHERE identity_id=?",
                        (dataset["dataset_identity"],),
                    )
            identity_claimed = connection.execute(
                "UPDATE _queryglass_dataset_identities SET processing_dataset_id=? "
                "WHERE identity_id=? AND (processing_dataset_id IS NULL OR processing_dataset_id=?)",
                (dataset_id, dataset["dataset_identity"], dataset_id),
            ).rowcount
            if identity_claimed == 0:
                raise WorkflowError(
                    "Another upload for this dataset is already being preprocessed. "
                    "Wait for it to finish before starting another run.",
                    409,
                    "claim",
                )
            stale = connection.execute(
                "SELECT run_id, staged_table FROM _queryglass_processing_runs "
                "WHERE dataset_id=? AND status='RUNNING'",
                (dataset_id,),
            ).fetchall()
            for row in stale:
                connection.execute(
                    "UPDATE _queryglass_processing_runs SET status='FAILED', "
                    "error_details='Interrupted or superseded before completion.' WHERE run_id=?",
                    (row["run_id"],),
                )
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            staged_table = f"{_STAGING_PREFIX}{run_id}"
            connection.execute(
                "INSERT INTO _queryglass_processing_runs "
                "(run_id, dataset_id, operation_config, status, started_at, rows_before, cols_before) "
                "VALUES (?, ?, ?, 'RUNNING', ?, ?, ?)",
                (run_id, dataset_id, json.dumps(json_value(options)), _now(), None, None),
            )
            connection.commit()
        except WorkflowError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise WorkflowError(f"Could not start preprocessing run: {exc}", 500, "claim") from exc

    stale_tables = [row["staged_table"] for row in stale]
    for table in stale_tables:
        _drop_table_quietly(db_path, table)

    raw_table = dataset["raw_table"]
    try:
        with closing(_connect(db_path)) as connection:
            original = pd.read_sql_query(f"SELECT * FROM {_quote(raw_table)}", connection)
        rows_before, cols_before = len(original), len(original.columns)
        with closing(_connect(db_path)) as connection:
            connection.execute(
                "UPDATE _queryglass_processing_runs SET rows_before=?, cols_before=? WHERE run_id=?",
                (rows_before, cols_before, run_id),
            )
            connection.commit()

        cleaned, prep_report = preprocess_dataframe(original, options=options)
        if cleaned.empty:
            raise ValueError("Preprocessing removed every row; refusing to store an empty result.")
        save_to_sqlite(cleaned, staged_table, db_path, if_exists="replace")
        processed_metrics = analyze_quality(cleaned)
    except Exception as exc:
        logger.exception("start_preprocess_run - preprocessing failed for %s run %s", dataset_id, run_id)
        _drop_table_quietly(db_path, staged_table)
        with closing(_connect(db_path)) as connection:
            connection.execute(
                "UPDATE _queryglass_processing_runs SET status='FAILED', completed_at=?, error_details=? WHERE run_id=?",
                (_now(), str(exc), run_id),
            )
            connection.execute(
                "UPDATE _queryglass_dataset_identities SET processing_dataset_id=NULL "
                "WHERE identity_id=? AND processing_dataset_id=?",
                (dataset["dataset_identity"], dataset_id),
            )
            connection.commit()
        _set_dataset_status(db_path, dataset_id, dataset["status"])
        raise WorkflowError(
            f"Preprocessing failed; the raw dataset was not modified. Error: {exc}", 422, "preprocess"
        ) from exc

    try:
        report_dict = json_value(prep_report.to_dict())
        with closing(_connect(db_path)) as connection:
            connection.execute(
                "UPDATE _queryglass_processing_runs SET status='COMPLETED', completed_at=?, "
                "rows_after=?, cols_after=?, staged_table=?, transformations=? WHERE run_id=?",
                (_now(), len(cleaned), len(cleaned.columns), staged_table, json.dumps(report_dict.get("transformations", [])), run_id),
            )
            connection.execute(
                "INSERT INTO _queryglass_quality_reports "
                "(dataset_id, run_id, stage, metrics, created_at) VALUES (?, ?, 'processed', ?, ?)",
                (dataset_id, run_id, json.dumps(json_value(processed_metrics)), _now()),
            )
            connection.execute(
                "UPDATE _queryglass_datasets SET status='PREPROCESSED' WHERE dataset_id=?",
                (dataset_id,),
            )
            connection.execute(
                "UPDATE _queryglass_dataset_identities SET processing_dataset_id=NULL "
                "WHERE identity_id=? AND processing_dataset_id=?",
                (dataset["dataset_identity"], dataset_id),
            )
            connection.commit()
    except Exception as exc:
        logger.exception("Could not record completed preprocessing for %s run %s", dataset_id, run_id)
        _drop_table_quietly(db_path, staged_table)
        with closing(_connect(db_path)) as connection:
            connection.execute(
                "UPDATE _queryglass_processing_runs SET status='FAILED', completed_at=?, error_details=? "
                "WHERE run_id=?",
                (_now(), str(exc), run_id),
            )
            connection.execute(
                "DELETE FROM _queryglass_quality_reports WHERE dataset_id=? AND run_id=?",
                (dataset_id, run_id),
            )
            connection.execute(
                "UPDATE _queryglass_datasets SET status=? WHERE dataset_id=?",
                (dataset["status"], dataset_id),
            )
            connection.execute(
                "UPDATE _queryglass_dataset_identities SET processing_dataset_id=NULL "
                "WHERE identity_id=? AND processing_dataset_id=?",
                (dataset["dataset_identity"], dataset_id),
            )
            connection.commit()
        raise WorkflowError(
            f"Preprocessing completed but could not be recorded safely; the previous stored data is unchanged. Error: {exc}",
            500,
            "preprocess",
        ) from exc

    # Drop staging tables left by earlier runs that were never finalized.
    with closing(_connect(db_path)) as connection:
        older = connection.execute(
            "SELECT staged_table FROM _queryglass_processing_runs "
            "WHERE dataset_id=? AND run_id<>? AND finalized=0 AND staged_table IS NOT NULL",
            (dataset_id, run_id),
        ).fetchall()
    for row in older:
        _drop_table_quietly(db_path, row["staged_table"])

    logger.info(
        "start_preprocess_run - run %s completed for %s (%s -> %s rows)",
        run_id, dataset_id, rows_before, len(cleaned),
    )
    snapshot = get_dataset_snapshot(db_path, dataset_id)
    assert snapshot is not None
    return snapshot


def get_run(db_path: str, dataset_id: str, run_id: str) -> dict[str, Any] | None:
    init_workflow_tables(db_path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM _queryglass_processing_runs WHERE dataset_id=? AND run_id=?",
            (dataset_id, run_id),
        ).fetchone()
    return _parse_run(row) if row is not None else None


# ---------------------------------------------------------------------------
# Final storage
# ---------------------------------------------------------------------------

def finalize_run(db_path: str, dataset_id: str, run_id: str) -> dict[str, Any]:
    """Atomically publish a validated run and remove the prior identity version."""
    init_workflow_tables(db_path)
    if get_active_dataset_id(db_path) != dataset_id:
        raise WorkflowError("Only the currently active uploaded dataset can be stored.", 409, "store")
    with closing(_connect(db_path)) as connection:
        dataset = connection.execute(
            "SELECT dataset_id, dataset_identity, filename, raw_table, status "
            "FROM _queryglass_datasets WHERE dataset_id=?",
            (dataset_id,),
        ).fetchone()
        if dataset is None:
            raise WorkflowError("Dataset was not found.", 404, "store")
        run = connection.execute(
            "SELECT * FROM _queryglass_processing_runs WHERE dataset_id=? AND run_id=?",
            (dataset_id, run_id),
        ).fetchone()
        if run is None:
            raise WorkflowError("Processing run was not found for this dataset.", 404, "store")
        if run["status"] != "COMPLETED":
            raise WorkflowError(
                f"Processed data can only be stored after preprocessing succeeds; run status is '{run['status']}'.",
                409,
                "store",
            )

        connection.execute("BEGIN IMMEDIATE")
        try:
            dataset = connection.execute(
                "SELECT dataset_id, dataset_identity, filename, raw_table, status "
                "FROM _queryglass_datasets WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
            if dataset is None:
                raise WorkflowError("The active dataset changed before this result could be stored.", 409, "store")
            active = connection.execute(
                "SELECT dataset_id FROM _queryglass_active_dataset WHERE active_key='active'"
            ).fetchone()
            if active is None or active["dataset_id"] != dataset_id:
                raise WorkflowError("Only the currently active uploaded dataset can be stored.", 409, "store")
            run = connection.execute(
                "SELECT * FROM _queryglass_processing_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run["finalized"]:
                existing = connection.execute(
                    "SELECT * FROM _queryglass_processed_versions WHERE run_id=?", (run_id,)
                ).fetchone()
                connection.rollback()
                if existing:
                    return {
                        "stored": True,
                        "already_stored": True,
                        "replaced_previous": False,
                        "version": _version_row(existing),
                    }
                raise WorkflowError(
                    "Run is marked finalized but no processed version exists; re-run preprocessing before storing.",
                    409,
                    "store",
                )
            identity_lock = connection.execute(
                "SELECT processing_dataset_id, active_dataset_id "
                "FROM _queryglass_dataset_identities WHERE identity_id=?",
                (dataset["dataset_identity"],),
            ).fetchone()
            if identity_lock is None:
                raise WorkflowError("The dataset identity metadata is missing.", 409, "store")
            if identity_lock["processing_dataset_id"] not in (None, dataset_id):
                raise WorkflowError(
                    "Another upload for this dataset is currently being preprocessed. "
                    "Wait for it to finish before storing this result.",
                    409,
                    "store",
                )

            staged_table = run["staged_table"]
            if not staged_table or not staged_table.startswith(_STAGING_PREFIX) or not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (staged_table,)
            ).fetchone():
                raise WorkflowError(
                    "The staged processed table is missing; run preprocessing again before storing.",
                    409,
                    "store",
                )
            staged_columns = connection.execute(
                f"PRAGMA table_info({_quote(staged_table)})"
            ).fetchall()
            staged_row_count = connection.execute(
                f"SELECT COUNT(*) FROM {_quote(staged_table)}"
            ).fetchone()[0]
            if len(staged_columns) != run["cols_after"] or staged_row_count != run["rows_after"]:
                raise WorkflowError(
                    "The staged processed dataset failed its schema or row-count check. "
                    "The previously stored dataset was preserved; run preprocessing again.",
                    422,
                    "store",
                )

            identity_datasets = connection.execute(
                "SELECT dataset_id, raw_table, status FROM _queryglass_datasets "
                "WHERE dataset_identity=? AND dataset_id<>?",
                (dataset["dataset_identity"], dataset_id),
            ).fetchall()
            if any(row["status"] == "PREPROCESSING" for row in identity_datasets):
                raise WorkflowError(
                    "Another upload for this dataset is currently being preprocessed. "
                    "Wait for it to finish before storing this result.",
                    409,
                    "store",
                )

            previous_versions = connection.execute(
                "SELECT v.table_name FROM _queryglass_processed_versions v "
                "JOIN _queryglass_datasets d ON d.dataset_id=v.dataset_id "
                "WHERE d.dataset_identity=?",
                (dataset["dataset_identity"],),
            ).fetchall()
            replaced_previous = bool(previous_versions or identity_lock["active_dataset_id"])

            related_ids = [
                row["dataset_id"] for row in identity_datasets
            ] + [dataset_id]
            placeholders = ",".join("?" for _ in related_ids)
            obsolete_runs = connection.execute(
                f"SELECT staged_table FROM _queryglass_processing_runs "
                f"WHERE dataset_id IN ({placeholders}) AND run_id<>?",
                (*related_ids, run_id),
            ).fetchall()
            current_versions = connection.execute(
                f"SELECT table_name FROM _queryglass_processed_versions "
                f"WHERE dataset_id IN ({placeholders})",
                related_ids,
            ).fetchall()

            tables_to_drop: set[str] = set()
            for row in identity_datasets:
                if row["raw_table"] and not row["raw_table"].startswith("_queryglass_"):
                    tables_to_drop.add(row["raw_table"])
            for row in (*obsolete_runs, *previous_versions, *current_versions):
                table_name = row["staged_table"] if "staged_table" in row.keys() else row["table_name"]
                if table_name and (
                    table_name.startswith(_STAGING_PREFIX)
                    or not table_name.startswith("_queryglass_")
                ):
                    tables_to_drop.add(table_name)
            tables_to_drop.discard(staged_table)

            for table_name in tables_to_drop:
                connection.execute(f"DROP TABLE IF EXISTS {_quote(table_name)}")

            for old_dataset in identity_datasets:
                old_id = old_dataset["dataset_id"]
                connection.execute("DELETE FROM _queryglass_quality_reports WHERE dataset_id=?", (old_id,))
                connection.execute("DELETE FROM _queryglass_processed_versions WHERE dataset_id=?", (old_id,))
                connection.execute("DELETE FROM _queryglass_processing_runs WHERE dataset_id=?", (old_id,))
                connection.execute("DELETE FROM _queryglass_table_fingerprints WHERE dataset_id=?", (old_id,))
                connection.execute("DELETE FROM _queryglass_effective_types WHERE dataset_id=?", (old_id,))
                connection.execute("DELETE FROM _queryglass_datasets WHERE dataset_id=?", (old_id,))
            connection.execute(
                "DELETE FROM _queryglass_processed_versions WHERE dataset_id=?", (dataset_id,)
            )
            connection.execute(
                "DELETE FROM _queryglass_processing_runs WHERE dataset_id=? AND run_id<>?",
                (dataset_id, run_id),
            )
            connection.execute(
                "DELETE FROM _queryglass_quality_reports WHERE dataset_id=? "
                "AND run_id IS NOT NULL AND run_id<>?",
                (dataset_id, run_id),
            )

            version_id = f"pv_{uuid.uuid4().hex[:16]}"
            source_stem = Path(dataset["filename"]).stem or "dataset"
            base = sanitize_table_name(f"{source_stem}_processed")
            final_table = base
            suffix = 2
            while connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (final_table,),
            ).fetchone():
                final_table = f"{base}_{suffix}"
                suffix += 1

            created_at = _now()
            connection.execute(f"ALTER TABLE {_quote(staged_table)} RENAME TO {_quote(final_table)}")
            connection.execute(
                "INSERT INTO _queryglass_processed_versions "
                "(version_id, dataset_id, run_id, table_name, row_count, column_count, operation_config, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version_id,
                    dataset_id,
                    run_id,
                    final_table,
                    run["rows_after"],
                    run["cols_after"],
                    run["operation_config"],
                    created_at,
                ),
            )
            connection.execute(
                "UPDATE _queryglass_processing_runs SET finalized=1 WHERE run_id=?", (run_id,)
            )
            connection.execute(
                "UPDATE _queryglass_datasets SET status='PROCESSED_STORED' WHERE dataset_id=?",
                (dataset_id,),
            )
            connection.execute(
                "UPDATE _queryglass_dataset_identities "
                "SET active_dataset_id=?, processing_dataset_id=NULL WHERE identity_id=?",
                (dataset_id, dataset["dataset_identity"]),
            )
            connection.commit()
        except WorkflowError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            logger.exception("finalize_run - storing processed version failed for run %s", run_id)
            raise WorkflowError(f"Storing the processed dataset failed: {exc}", 500, "store") from exc

    logger.info("finalize_run - version %s stored as %s for dataset %s", version_id, final_table, dataset_id)
    try:
        from backend.services.table_fingerprinter import TableFingerprinter
        from backend.services.effective_type_profiler import EffectiveTypeProfiler
        fingerprinter = TableFingerprinter()
        fingerprint = fingerprinter.compute_fingerprint(db_path, final_table, dataset_id)
        fingerprinter.save_fingerprint(db_path, fingerprint)
        profiler = EffectiveTypeProfiler()
        profile = profiler.profile_table(db_path, final_table, dataset_id)
        profiler.save_profile(db_path, profile)
    except Exception:
        logger.exception(
            "finalize_run - fingerprint/effective-type profiling failed for %s; storage remains valid",
            dataset_id,
        )
    return {
        "stored": True,
        "already_stored": False,
        "replaced_previous": replaced_previous,
        "version": {
            "version_id": version_id,
            "dataset_id": dataset_id,
            "run_id": run_id,
            "table_name": final_table,
            "row_count": run["rows_after"],
            "column_count": run["cols_after"],
            "operation_config": json.loads(run["operation_config"]),
            "created_at": created_at,
        },
    }


def _version_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "version_id": row["version_id"],
        "dataset_id": row["dataset_id"],
        "run_id": row["run_id"],
        "table_name": row["table_name"],
        "row_count": row["row_count"],
        "column_count": row["column_count"],
        "operation_config": json.loads(row["operation_config"]),
        "created_at": row["created_at"],
    }
