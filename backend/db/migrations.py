import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def get_migration_files() -> list[Path]:
    directory = Path(__file__).resolve().parent / "migrations"
    if not directory.exists():
        return []
    return sorted(directory.glob("*.sql"))


def execute_migration(db_path: str, migration_file: Path) -> bool:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.executescript(migration_file.read_text(encoding="utf-8"))
            conn.commit()
        logger.info("Executed migration %s", migration_file.name)
        return True
    except sqlite3.Error as exc:
        message = str(exc).lower()
        if "already exists" in message:
            logger.debug("Migration %s was already applied", migration_file.name)
            return True
        logger.exception("Migration %s failed: %s", migration_file.name, exc)
        return False


def run_migrations(db_path: str) -> list[str]:
    results = []
    for migration in get_migration_files():
        if execute_migration(db_path, migration):
            results.append(migration.name)
    _ensure_query_run_columns(db_path)
    return results


def _ensure_query_run_columns(db_path: str) -> None:
    columns = {
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
    try:
        with sqlite3.connect(db_path) as conn:
            existing = {
                row[1].lower()
                for row in conn.execute(
                    "PRAGMA table_info(_queryglass_query_runs)"
                ).fetchall()
            }
            for name, column_type in columns.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE _queryglass_query_runs ADD COLUMN {name} {column_type}"
                    )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Could not ensure query-run traceability columns")
