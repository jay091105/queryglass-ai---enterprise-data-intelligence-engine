"""Persistence and summary queries for reliability evaluations."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=5)


def initialize_reliability_history(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS _queryglass_reliability_history (
                evaluation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                question TEXT NOT NULL,
                table_name TEXT NOT NULL,
                generated_sql TEXT NOT NULL,
                provider_model TEXT,
                source TEXT NOT NULL,
                is_test_case INTEGER NOT NULL DEFAULT 0,
                hallucination_case INTEGER NOT NULL DEFAULT 0,
                passed INTEGER NOT NULL,
                accuracy_score INTEGER,
                risk_score INTEGER,
                validation_json TEXT NOT NULL,
                execution_status TEXT NOT NULL,
                execution_metadata_json TEXT
            )
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(_queryglass_reliability_history)"
            )
        }
        if "hallucination_case" not in columns:
            connection.execute(
                "ALTER TABLE _queryglass_reliability_history "
                "ADD COLUMN hallucination_case INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_queryglass_reliability_created
            ON _queryglass_reliability_history(created_at)
            """
        )
        connection.commit()


def record_evaluation(
    db_path: str,
    *,
    question: str,
    table_name: str,
    sql: str,
    validation: dict[str, Any],
    source: str = "generated",
    is_test_case: bool = False,
    hallucination_case: bool = False,
    provider_model: str | None = None,
    execution_status: str = "Not executed",
    execution_metadata: dict[str, Any] | None = None,
    passed_override: bool | None = None,
) -> str:
    initialize_reliability_history(db_path)
    evaluation_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reliability = validation.get("reliability", {})
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO _queryglass_reliability_history (
                evaluation_id, created_at, updated_at, question, table_name,
                generated_sql, provider_model, source, is_test_case,
                hallucination_case, passed, accuracy_score, risk_score, validation_json,
                execution_status, execution_metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evaluation_id,
                now,
                now,
                question[:2000],
                table_name[:100],
                sql[:20000],
                provider_model,
                source,
                int(is_test_case),
                int(hallucination_case),
                int(
                    bool(validation.get("valid"))
                    if passed_override is None else passed_override
                ),
                reliability.get("answer_accuracy_score"),
                reliability.get("hallucination_risk_score"),
                json.dumps(validation, ensure_ascii=True, separators=(",", ":")),
                execution_status,
                json.dumps(execution_metadata, ensure_ascii=True, separators=(",", ":"))
                if execution_metadata is not None else None,
            ),
        )
        connection.commit()
    return evaluation_id


def update_execution(
    db_path: str,
    evaluation_id: str,
    *,
    question: str,
    table_name: str,
    sql: str,
    validation: dict[str, Any],
    execution_status: str,
    execution_metadata: dict[str, Any] | None = None,
) -> bool:
    initialize_reliability_history(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reliability = validation.get("reliability", {})
    with _connect(db_path) as connection:
        cursor = connection.execute(
            """
            UPDATE _queryglass_reliability_history
            SET updated_at=?, question=?, table_name=?, generated_sql=?,
                passed=?, accuracy_score=?, risk_score=?,
                validation_json=?, execution_status=?, execution_metadata_json=?
            WHERE evaluation_id=?
            """,
            (
                now,
                question[:2000],
                table_name[:100],
                sql[:20000],
                int(bool(validation.get("valid"))),
                reliability.get("answer_accuracy_score"),
                reliability.get("hallucination_risk_score"),
                json.dumps(validation, ensure_ascii=True, separators=(",", ":")),
                execution_status,
                json.dumps(execution_metadata, ensure_ascii=True, separators=(",", ":"))
                if execution_metadata is not None else None,
                evaluation_id,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1


def update_validation(
    db_path: str,
    evaluation_id: str,
    *,
    question: str,
    table_name: str,
    sql: str,
    validation: dict[str, Any],
) -> bool:
    initialize_reliability_history(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    reliability = validation.get("reliability", {})
    with _connect(db_path) as connection:
        cursor = connection.execute(
            """
            UPDATE _queryglass_reliability_history
            SET updated_at=?, question=?, table_name=?, generated_sql=?,
                passed=?, accuracy_score=?, risk_score=?, validation_json=?,
                execution_status='Not executed', execution_metadata_json=NULL
            WHERE evaluation_id=?
            """,
            (
                now,
                question[:2000],
                table_name[:100],
                sql[:20000],
                int(bool(validation.get("valid"))),
                reliability.get("answer_accuracy_score"),
                reliability.get("hallucination_risk_score"),
                json.dumps(validation, ensure_ascii=True, separators=(",", ":")),
                evaluation_id,
            ),
        )
        connection.commit()
        return cursor.rowcount == 1


def reliability_summary(db_path: str) -> dict[str, Any]:
    initialize_reliability_history(db_path)
    with _connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        query_stats = connection.execute(
            """
            SELECT COUNT(*) AS evaluated,
                   COALESCE(SUM(passed), 0) AS passed,
                   AVG(accuracy_score) AS average_accuracy,
                   COALESCE(SUM(CASE WHEN passed=0 THEN 1 ELSE 0 END), 0) AS rejected
            FROM _queryglass_reliability_history
            WHERE is_test_case=0
            """
        ).fetchone()
        test_stats = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(passed), 0) AS passed
            FROM _queryglass_reliability_history
            WHERE is_test_case=1 AND hallucination_case=1
            """
        ).fetchone()
        all_test_stats = connection.execute(
            "SELECT COUNT(*) AS total FROM _queryglass_reliability_history WHERE is_test_case=1"
        ).fetchone()
        recent_rows = connection.execute(
            """
            SELECT evaluation_id, created_at, question, table_name, generated_sql,
            source, is_test_case, hallucination_case, passed, accuracy_score, risk_score,
                   validation_json, execution_status, execution_metadata_json
            FROM _queryglass_reliability_history
            ORDER BY created_at DESC
            LIMIT 500
            """
        ).fetchall()

    events: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    trends: dict[str, list[int]] = {}
    for row in recent_rows:
        item = dict(row)
        try:
            validation = json.loads(item.pop("validation_json"))
        except (TypeError, json.JSONDecodeError):
            validation = {}
        issues = (
            validation.get("semantic_validation", {}).get("issues", [])
            if isinstance(validation, dict) else []
        )
        categories = sorted({
            str(issue.get("type"))
            for issue in issues
            if isinstance(issue, dict) and issue.get("type")
        })
        item["issue_categories"] = categories
        try:
            item["execution_metadata"] = (
                json.loads(item["execution_metadata_json"])
                if item["execution_metadata_json"] else None
            )
        except (TypeError, json.JSONDecodeError):
            item["execution_metadata"] = None
        item.pop("execution_metadata_json", None)
        if not item["is_test_case"]:
            for category in categories:
                category_counts[category] = category_counts.get(category, 0) + 1
            day = str(item["created_at"])[:10]
            if item["accuracy_score"] is not None:
                trends.setdefault(day, []).append(int(item["accuracy_score"]))
        if len(events) < 25:
            item["generated_sql"] = item["generated_sql"][:4000]
            events.append(item)

    test_total = int(test_stats["total"])
    return {
        "queries_evaluated": int(query_stats["evaluated"]),
        "queries_passed": int(query_stats["passed"]),
        "queries_rejected": int(query_stats["rejected"]),
        "average_accuracy_score": (
            round(float(query_stats["average_accuracy"]), 1)
            if query_stats["average_accuracy"] is not None else None
        ),
        "test_cases_run": int(all_test_stats["total"]),
        "hallucination_cases_evaluated": test_total,
        "hallucination_detection_rate": (
            round(int(test_stats["passed"]) / test_total * 100, 1)
            if test_total else None
        ),
        "most_frequent_failure_categories": sorted(
            (
                {"category": category, "count": count}
                for category, count in category_counts.items()
            ),
            key=lambda item: (-item["count"], item["category"]),
        )[:8],
        "score_trends": [
            {"date": day, "average_accuracy_score": round(sum(scores) / len(scores), 1)}
            for day, scores in sorted(trends.items())[-30:]
        ],
        "recent_evaluations": events,
    }
