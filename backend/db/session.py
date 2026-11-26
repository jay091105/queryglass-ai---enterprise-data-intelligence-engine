"""
SQLite persistence, schema inspection, query history, and safe execution.
"""

# ==============================================================================
# Source section: db_manager.py
# ==============================================================================

import datetime
import logging
import os
import re
import sqlite3
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_DB_PATH = str((PROJECT_ROOT / "database.db").resolve())
logger = logging.getLogger(__name__)

def sanitize_table_name(name: str) -> str:
    """Sanitizes user input into a safe SQLite identifier."""
    if not name:
        return "imported_dataset"
    cleaned = re.sub(r"[^\w\s-]", "", name).strip()
    cleaned = re.sub(r"[-\s]+", "_", cleaned)
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"tbl_{cleaned}"
    return cleaned.lower()


def table_exists(table_name: str, db_name: str = CANONICAL_DB_PATH) -> bool:
    """Checks if a table already exists in the SQLite database."""
    if not table_name:
        return False
    try:
        with sqlite3.connect(db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table_name,),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error:
        return False


def save_to_sqlite(
    df: pd.DataFrame,
    table_name: str,
    db_name: str = CANONICAL_DB_PATH,
    if_exists: str = "replace",
    progress_callback=None,
):
    """
    Original function signature:
    Persists a DataFrame to SQLite table.
    """
    safe_name = sanitize_table_name(table_name)
    with sqlite3.connect(db_name) as conn:
        configured_chunk_size = os.getenv("QUERYGLASS_SQLITE_INSERT_CHUNK_ROWS", "1000")
        try:
            requested_chunk_size = int(configured_chunk_size)
        except ValueError as exc:
            raise ValueError(
                "QUERYGLASS_SQLITE_INSERT_CHUNK_ROWS must be a positive integer."
            ) from exc
        if requested_chunk_size < 1:
            raise ValueError("QUERYGLASS_SQLITE_INSERT_CHUNK_ROWS must be a positive integer.")
        # Stay under the bound-parameter budget for wide tables.
        chunksize = max(1, min(requested_chunk_size, 20000 // max(1, len(df.columns))))
        inserted_rows = 0

        def insert_with_progress(table, connection, keys, data_iter):
            nonlocal inserted_rows
            rows = list(data_iter)
            connection.execute(
                table.insert_statement(num_rows=len(rows)),
                [value for row in rows for value in row],
            )
            inserted_rows += len(rows)
            progress_callback(inserted_rows, len(df))
            return len(rows)

        df.to_sql(
            safe_name,
            conn,
            if_exists=if_exists,
            index=False,
            method=insert_with_progress if progress_callback else "multi",
            chunksize=chunksize,
        )
    return safe_name

def init_history_db(db_name=CANONICAL_DB_PATH):
    """Creates the _queryglass_history table if it does not exist."""
    try:
        with sqlite3.connect(db_name) as conn:
            schema_path = PROJECT_ROOT / "database" / "app_tables.sql"
            conn.executescript(schema_path.read_text(encoding="utf-8"))
            conn.commit()
    except sqlite3.Error:
        logger.exception("Could not initialize application history schema")
        return False
    return True


def log_history(
    question: str,
    sql_query: str,
    status: str,
    row_count: int = 0,
    execution_time_ms: float = 0.0,
    error_message: str = "",
    db_name: str = CANONICAL_DB_PATH,
):
    """Logs a query execution record into the persistent SQLite store."""
    if not init_history_db(db_name):
        return False
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with sqlite3.connect(db_name) as conn:
            conn.execute(
                """
                INSERT INTO _queryglass_history 
                (timestamp, question, sql_query, status, row_count, execution_time_ms, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_str,
                    question or "",
                    sql_query or "",
                    status,
                    int(row_count),
                    round(float(execution_time_ms), 2),
                    error_message or "",
                ),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Could not persist query history")
        return False
    return True


def get_history(limit: int = 100, db_name: str = CANONICAL_DB_PATH):
    """Retrieves execution history sorted with newest records first from _queryglass_query_runs."""
    init_history_db(db_name)
    try:
        with sqlite3.connect(db_name) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 
                    query_id as id,
                    execution_timestamp as timestamp,
                    user_question as question,
                    generated_sql as sql_query,
                    execution_status as status,
                    row_count,
                    execution_time_ms,
                    error_message
                FROM _queryglass_query_runs
                ORDER BY execution_timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        logger.exception("Could not retrieve query history")
        return []


def get_history_stats(db_name=CANONICAL_DB_PATH):
    """Return query-history counts and timings from _queryglass_query_runs (authoritative source)."""
    init_history_db(db_name)
    try:
        with sqlite3.connect(db_name) as conn:
            cursor = conn.cursor()
            
            # Query authoritative _queryglass_query_runs table
            cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN execution_status = 'success' THEN 1 ELSE 0 END) as successful,
                    SUM(CASE WHEN execution_status IN ('syntax_error', 'runtime_error', 'timeout') THEN 1 ELSE 0 END) as blocked,
                    AVG(CASE WHEN execution_status = 'success' THEN execution_time_ms ELSE NULL END) as avg_duration_ms
                FROM _queryglass_query_runs
            """)
            
            row = cursor.fetchone()
            total = row[0] or 0
            success_count = row[1] or 0
            blocked_count = row[2] or 0
            avg_ms = row[3]
            
            return {
                "total_queries": total,
                "success_count": success_count,
                "blocked_count": blocked_count,
                "failed_count": 0,
                "status_counts": {
                    "SUCCESS": success_count,
                    "BLOCKED": blocked_count,
                },
                "avg_ms": round(float(avg_ms), 1) if avg_ms is not None else 0.0,
                "success_rate": round(success_count / total * 100, 1) if total else 0.0,
            }
    except sqlite3.Error as e:
        logger.warning(f"Failed to get query run stats: {e}")
        return {
            "total_queries": 0,
            "success_count": 0,
            "blocked_count": 0,
            "failed_count": 0,
            "status_counts": {},
            "avg_ms": 0.0,
            "success_rate": 0.0,
        }

def get_columns(table_name, db_name=CANONICAL_DB_PATH):
    """Original signature: Returns PRAGMA table_info rows for table_name."""
    if not table_name:
        return []
    try:
        with sqlite3.connect(db_name) as conn:
            cursor = conn.cursor()
            escaped_table = table_name.replace("'", "''")
            cursor.execute(f"PRAGMA table_info('{escaped_table}')")
            return cursor.fetchall()
    except sqlite3.Error:
        return []


def get_tables(db_name=CANONICAL_DB_PATH):
    """Returns list of user tables in the SQLite database."""
    if not Path(db_name).exists():
        return []
    try:
        with sqlite3.connect(db_name) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_queryglass_%' ORDER BY name"
            )
            return [row[0] for row in cursor.fetchall()]
    except sqlite3.Error:
        return []


def get_table_row_count(table_name, db_name=CANONICAL_DB_PATH):
    """Returns the exact row count of table_name."""
    if not table_name or not Path(db_name).exists():
        return 0
    try:
        with sqlite3.connect(db_name) as conn:
            cursor = conn.cursor()
            quoted_table = table_name.replace('"', '""')
            cursor.execute(f'SELECT COUNT(*) FROM "{quoted_table}"')
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def get_table_schema_details(table_name, db_name=CANONICAL_DB_PATH):
    """
    Returns rich metadata about a table:
    - columns: list of dicts with name, type, is_pk, not_null, default_val
    - pks: list of PK column names
    - fks: list of foreign keys
    - indexes: list of index names
    - type_counts: count of numeric, string, date/time, and other columns
    """
    if not table_name or not Path(db_name).exists():
        return {
            "columns": [],
            "pks": [],
            "fks": [],
            "indexes": [],
            "type_counts": {"num": 0, "str": 0, "time": 0, "other": 0},
        }

    try:
        with sqlite3.connect(db_name) as conn:
            cursor = conn.cursor()
            escaped_table = table_name.replace("'", "''")
            # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
            cursor.execute(f"PRAGMA table_info('{escaped_table}')")
            raw_cols = cursor.fetchall()

            cursor.execute(f"PRAGMA foreign_key_list('{escaped_table}')")
            fk_rows = cursor.fetchall()

            cursor.execute(f"PRAGMA index_list('{escaped_table}')")
            index_rows = cursor.fetchall()
            unique_columns: set[str] = set()
            indexes_details = []
            for index in index_rows:
                index_name = index[1].replace("'", "''")
                index_info = conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
                index_columns = [column[2] for column in index_info if column[2]]
                is_unique = bool(index[2])
                is_partial = bool(index[4]) if len(index) > 4 else False
                if is_unique and not is_partial and len(index_columns) == 1:
                    unique_columns.add(index_columns[0])
                indexes_details.append(
                    {
                        "name": index[1],
                        "unique": is_unique,
                        "origin": index[3] if len(index) > 3 else "",
                        "partial": is_partial,
                        "columns": index_columns,
                    }
                )
    except sqlite3.Error:
        return {
            "columns": [],
            "pks": [],
            "fks": [],
            "indexes": [],
            "type_counts": {"num": 0, "str": 0, "time": 0, "other": 0},
        }

    cols = []
    pks = []
    type_counts = {"num": 0, "str": 0, "time": 0, "other": 0}

    for col in raw_cols:
        col_name = col[1]
        col_type = (col[2] or "").upper()
        is_pk = bool(col[5])
        if is_pk:
            pks.append(col_name)

        if any(t in col_type for t in ["INT", "REAL", "FLOAT", "DOUBLE", "NUMERIC", "DECIMAL"]):
            type_counts["num"] += 1
        elif any(t in col_type for t in ["CHAR", "TEXT", "CLOB", "VARCHAR"]):
            type_counts["str"] += 1
        elif any(t in col_type for t in ["DATE", "TIME"]):
            type_counts["time"] += 1
        else:
            type_counts["other"] += 1

        cols.append({
            "name": col_name,
            "type": col[2] or "",
            "is_pk": is_pk,
            "not_null": bool(col[3]),
            "is_unique": col_name in unique_columns,
            "is_fk": any(fk[3] == col_name for fk in fk_rows),
            "default_value": col[4],
        })

    foreign_keys = [
        {
            "id": fk[0],
            "sequence": fk[1],
            "table": fk[2],
            "from": fk[3],
            "to": fk[4],
            "on_update": fk[5],
            "on_delete": fk[6],
            "match": fk[7],
        }
        for fk in fk_rows
    ]
    return {
        "columns": cols,
        "pks": pks,
        "fks": [fk["table"] for fk in foreign_keys],
        "foreign_keys": foreign_keys,
        "indexes": [idx["name"] for idx in indexes_details],
        "indexes_details": indexes_details,
        "type_counts": type_counts,
    }


def get_database_stats(db_name=CANONICAL_DB_PATH):
    """Returns database size, sqlite version, and total row count across user tables."""
    db_path = Path(db_name)
    if not db_path.exists():
        return {
            "exists": False,
            "size_bytes": 0,
            "size_mb": 0.0,
            "sqlite_version": sqlite3.sqlite_version,
            "tables": [],
            "total_rows": 0,
        }

    tables = get_tables(db_name)
    total_rows = sum(get_table_row_count(t, db_name) for t in tables)
    size_bytes = db_path.stat().st_size

    return {
        "exists": True,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "sqlite_version": sqlite3.sqlite_version,
        "tables": tables,
        "total_rows": total_rows,
    }

