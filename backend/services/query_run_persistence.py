import json
import sqlite3
import logging
from backend.schemas.query_run import QueryRun

logger = logging.getLogger(__name__)


class QueryRunPersistence:

    def migrate_add_reliability_tracking(self, db_path: str) -> bool:
        """Add reliability tracking columns if they don't exist. Returns True if successful."""
        try:
            with sqlite3.connect(db_path) as conn:
                # Check existing columns
                existing_cols = {row[1].lower() for row in conn.execute("PRAGMA table_info(_queryglass_query_runs)").fetchall()}
                
                # Define new columns to add
                new_columns = {
                    "formula_version": "TEXT DEFAULT '1.0.0'",
                    "dimension_scores_json": "TEXT",
                    "evaluated_coverage": "INTEGER",
                    "accuracy_score": "INTEGER",
                    "hallucination_risk_score": "INTEGER",
                }
                
                # Add missing columns
                for col_name, col_def in new_columns.items():
                    if col_name not in existing_cols:
                        conn.execute(f"ALTER TABLE _queryglass_query_runs ADD COLUMN {col_name} {col_def}")
                        logger.info(f"Added column {col_name} to _queryglass_query_runs")
                
                # Create index if it doesn't exist
                conn.execute("CREATE INDEX IF NOT EXISTS idx_query_runs_formula_version ON _queryglass_query_runs(formula_version)")
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.exception(f"Failed to migrate reliability tracking columns: {e}")
            return False

    def save(self, db_path: str, query_run: QueryRun) -> bool:
        """Save query run with reliability tracking. Returns True if successful."""
        # Run migration to ensure columns exist
        if not self.migrate_add_reliability_tracking(db_path):
            logger.warning("Migration failed but continuing with save attempt")
        try:
            evidence = query_run.validation_evidence_json
            if hasattr(evidence, "model_dump"):
                evidence = evidence.model_dump()
            with sqlite3.connect(db_path) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS _queryglass_query_runs (
                        query_id TEXT PRIMARY KEY, user_question TEXT NOT NULL, generated_sql TEXT NOT NULL,
                    execution_status TEXT NOT NULL, result_grounding TEXT NOT NULL,
                    validation_evidence_json TEXT NOT NULL, reliability_score REAL NOT NULL,
                    execution_timestamp TEXT NOT NULL, table_name TEXT, row_count INTEGER DEFAULT 0,
                        execution_time_ms REAL DEFAULT 0, error_message TEXT,
                        dataset_id TEXT, executed_sql TEXT, question_plan_json TEXT,
                        fingerprint_hash TEXT, effective_types_json TEXT, provider_model TEXT,
                        truncation_status TEXT, reliability_id TEXT, validation_json TEXT,
                        result_verification_json TEXT)""")
                existing = {row[1].lower() for row in conn.execute("PRAGMA table_info(_queryglass_query_runs)").fetchall()}
                extra_columns = {
                        "dataset_id": "TEXT",
                        "executed_sql": "TEXT",
                        "question_plan_json": "TEXT",
                        "fingerprint_hash": "TEXT",
                        "effective_types_json": "TEXT",
                        "provider_model": "TEXT",
                        "truncation_status": "TEXT",
                        "reliability_id": "TEXT",
                        "validation_json": "TEXT",
                        "result_verification_json": "TEXT",
                }
                for name, column_type in extra_columns.items():
                        if name not in existing:
                            conn.execute(f"ALTER TABLE _queryglass_query_runs ADD COLUMN {name} {column_type}")
                conn.execute("""INSERT OR REPLACE INTO _queryglass_query_runs
                    (query_id, user_question, generated_sql, execution_status, result_grounding,
                     validation_evidence_json, reliability_score, execution_timestamp, table_name,
                         row_count, execution_time_ms, error_message, dataset_id, executed_sql,
                         question_plan_json, fingerprint_hash, effective_types_json, provider_model,
                         truncation_status, reliability_id, validation_json, result_verification_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (query_run.query_id, query_run.user_question, query_run.generated_sql,
                     query_run.execution_status.value, query_run.result_grounding.value,
                     json.dumps(evidence), query_run.reliability_score, query_run.execution_timestamp,
                     query_run.table_name, query_run.row_count, query_run.execution_time_ms,
                         query_run.error_message, query_run.dataset_id, query_run.executed_sql,
                         json.dumps(query_run.question_plan_json),
                         query_run.fingerprint_hash,
                         json.dumps(query_run.effective_types_json),
                         query_run.provider_model,
                         query_run.truncation_status,
                         query_run.reliability_id,
                         json.dumps(query_run.validation_json),
                         json.dumps(query_run.result_verification_json)))
                conn.commit()
            return True
        except sqlite3.Error:
            logger.exception("Failed to persist query run %s", query_run.query_id)
            return False

    def list_query_runs(self, db_path: str, status: str | None = None, grounding: str | None = None,
                        date_from: str | None = None, date_to: str | None = None, limit: int = 100) -> list[dict]:
        try:
            clauses, params = [], []
            if status:
                clauses.append("execution_status = ?"); params.append(status)
            if grounding:
                clauses.append("result_grounding = ?"); params.append(grounding)
            if date_from:
                clauses.append("execution_timestamp >= ?"); params.append(date_from)
            if date_to:
                clauses.append("execution_timestamp <= ?"); params.append(date_to)
            sql = "SELECT * FROM _queryglass_query_runs"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY execution_timestamp DESC LIMIT ?"
            params.append(max(1, min(limit, 500)))
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(sql, params).fetchall()
                return [self._record(row) for row in rows]
        except sqlite3.Error:
            logger.exception("Failed to list query runs")
            return []

    def get_query_run(self, db_path: str, query_id: str) -> dict | None:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT * FROM _queryglass_query_runs WHERE query_id=?", (query_id,)).fetchone()
                return self._record(row) if row else None
        except sqlite3.Error:
            logger.exception("Failed to retrieve query run %s", query_id)
            return None

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = dict(row)
        try:
            record["validation_evidence"] = json.loads(record.pop("validation_evidence_json"))
        except (json.JSONDecodeError, KeyError):
            record["validation_evidence"] = {}
        record["validation_evidence_json"] = record["validation_evidence"]
        for key in ("question_plan_json", "effective_types_json"):
            value = record.get(key)
            if isinstance(value, str):
                try:
                    record[key] = json.loads(value)
                except json.JSONDecodeError:
                    record[key] = {}
        for key in ("validation_json", "result_verification_json"):
            value = record.get(key)
            if isinstance(value, str):
                try:
                    record[key] = json.loads(value)
                except json.JSONDecodeError:
                    record[key] = {}
        return record
