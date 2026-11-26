import sqlite3

from fastapi.testclient import TestClient

import backend.main as main
from backend.schemas.result_verification import ResultGrounding, ValidationEvidence
from backend.services.result_verification_engine import ResultVerificationEngine


def test_successful_select_returns_genuine_execution_metadata(tmp_path, monkeypatch):
    database = tmp_path / "result_metadata.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (id INTEGER, amount REAL, note TEXT)")
        connection.execute("INSERT INTO sales VALUES (1, 10.5, NULL)")

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/execute",
            json={
                "question": "Show sales ID and amount where amount is greater than 10.",
                "sql": "SELECT id, amount FROM sales WHERE amount > 10",
                "table_name": "sales",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    verification = payload["result_verification"]
    contract = payload["verification"]
    assert verification["database_source"] == "sales"
    assert verification["actual_columns"] == ["id", "amount"]
    assert verification["actual_column_count"] == 2
    assert verification["actual_row_count"] == 1
    assert verification["truncated"] is False
    assert verification["duration_ms"] >= 0
    assert contract["execution_status"] == "success"
    assert contract["actual_columns"] == ["id", "amount"]
    assert contract["actual_row_count"] == 1


def test_cte_and_alias_output_columns_are_sql_result_columns(tmp_path, monkeypatch):
    database = tmp_path / "cte_metadata.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (category TEXT, amount REAL)")
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?)",
            [("A", 10.0), ("A", 20.0), ("B", 5.0)],
        )

    monkeypatch.setattr(main, "DB_PATH", str(database))
    sql = (
        "WITH totals AS (SELECT category, SUM(amount) AS total_amount "
        "FROM sales GROUP BY category) "
        "SELECT category, total_amount FROM totals"
    )
    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/execute",
            json={"question": "Sum amount by category.", "sql": sql, "table_name": "sales"},
        )

    assert response.status_code == 200
    metadata = response.json()["result_verification"]
    assert metadata["actual_columns"] == ["category", "total_amount"]
    assert metadata["actual_column_count"] == 2
    assert metadata["actual_row_count"] == 2


def test_zero_row_null_and_truncation_metadata(tmp_path, monkeypatch):
    database = tmp_path / "result_edge_metadata.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items (id INTEGER, note TEXT)")
        connection.executemany("INSERT INTO items VALUES (?, ?)", [(index, None) for index in range(1, 503)])

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        zero = client.post(
            "/api/sql/execute",
            json={"question": "Show items above 999.", "sql": "SELECT id, note FROM items WHERE id > 999", "table_name": "items"},
        )
        truncated = client.post(
            "/api/sql/execute",
            json={"question": "Show all items.", "sql": "SELECT id, note FROM items", "table_name": "items"},
        )

    assert zero.status_code == 200
    zero_meta = zero.json()["result_verification"]
    assert zero_meta["actual_row_count"] == 0
    assert zero_meta["truncated"] is False
    assert zero_meta["null_counts"] == {}

    assert truncated.status_code == 200
    truncated_meta = truncated.json()["result_verification"]
    assert truncated_meta["actual_row_count"] == 500
    assert truncated_meta["truncated"] is True
    assert truncated_meta["row_count_exact"] is True
    assert truncated_meta["total_result_count"] == 502
    assert truncated_meta["null_counts"]["note"] == 500


def test_successful_execution_without_requirement_evidence_is_only_partially_grounded():
    engine = ResultVerificationEngine()
    contract = engine.verify(
        {
            "status": "SUCCESS",
            "issues": [],
            "semantic_status": "not_fully_verified",
            "coverage_status": "not_fully_verified",
            "database_source": "sales",
            "actual_columns": ["id"],
            "actual_column_count": 1,
            "actual_row_count": 1,
            "truncated": False,
            "execution_time_ms": 2.0,
        },
        ValidationEvidence(),
        question="Show all sales",
        sql="SELECT id FROM sales",
    )

    assert contract.result_grounding == ResultGrounding.PARTIALLY_GROUNDED
    assert contract.grounding_evidence_status == "insufficient"
    assert contract.reliability_score < 1.0


def test_missing_filter_and_failed_execution_are_not_well_grounded(tmp_path, monkeypatch):
    database = tmp_path / "wrong_answer.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (id INTEGER, amount REAL)")
        connection.execute("INSERT INTO sales VALUES (1, 100)")

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        blocked = client.post(
            "/api/sql/execute",
            json={
                "question": "Show sales where amount is greater than 50.",
                "sql": "SELECT id, amount FROM sales",
                "table_name": "sales",
            },
        )
        failed = client.post(
            "/api/sql/execute",
            json={
                "question": "Show sales",
                "sql": "SELECT missing_column FROM sales",
                "table_name": "sales",
            },
        )

    assert blocked.status_code == 422
    blocked_detail = blocked.json()["detail"]
    assert blocked_detail["verification"]["result_grounding"] in {
        "ungrounded",
        "partially_grounded",
    }
    assert failed.status_code in {400, 422}
