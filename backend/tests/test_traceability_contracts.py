import sqlite3

from fastapi.testclient import TestClient
import backend.main as main

from backend.db.migrations import run_migrations
from backend.api.history import _requirement_checks
from backend.schemas.result_verification import (
    ExecutionStatus,
    ResultGrounding,
    ValidationEvidence,
    ResultVerificationContract,
)
from backend.schemas.query_run import QueryRun
from backend.services.query_run_persistence import QueryRunPersistence
from backend.services.result_verification_engine import ResultVerificationEngine
from backend.services.table_fingerprinter import TableFingerprinter


def test_reliability_contract_bounds_and_serialization():
    contract = ResultVerificationContract(
        execution_status=ExecutionStatus.SUCCESS,
        result_grounding=ResultGrounding.WELL_GROUNDED,
        validation_evidence=ValidationEvidence(),
        reliability_score=1.0,
    )
    payload = contract.to_json_dict()
    assert payload["execution_status"] == "success"
    assert payload["result_grounding"] == "well_grounded"
    assert payload["reliability_score"] == 1.0


def test_migrations_fingerprint_persistence_and_round_trip(tmp_path):
    database = tmp_path / "traceability.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO sample (name) VALUES ('alpha')")

    assert "001_add_traceability_tables.sql" in run_migrations(str(database))
    fingerprinter = TableFingerprinter()
    current = fingerprinter.compute_fingerprint(str(database), "sample", "dataset-1")
    fingerprinter.save_fingerprint(str(database), current)
    loaded = fingerprinter.load_fingerprint(str(database), "sample", "dataset-1")

    assert loaded is not None
    assert fingerprinter.compare_fingerprints(current, loaded)["overall_match"] is True

    persistence = QueryRunPersistence()
    engine = ResultVerificationEngine()
    evidence = engine.validate("SELECT id, name FROM sample", None, ["sample"])
    contract = engine.verify({"status": "SUCCESS"}, evidence)
    run = QueryRun.from_verification(contract, "Show sample", "SELECT id, name FROM sample", "sample", 1, 2.5)
    assert persistence.save(str(database), run) is True
    assert persistence.get_query_run(str(database), run.query_id)["generated_sql"].startswith("SELECT")


def test_execution_status_and_grounding_are_orthogonal():
    engine = ResultVerificationEngine()
    evidence = ValidationEvidence()
    contract = engine.verify(
        {"status": "SUCCESS", "result_grounding": "ungrounded"},
        evidence,
    )
    assert contract.execution_status == ExecutionStatus.SUCCESS
    assert contract.result_grounding == ResultGrounding.UNGROUNDED


def test_successful_execute_persists_trace_and_history(tmp_path, monkeypatch):
    database = tmp_path / "execute-trace.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (id INTEGER, amount REAL)")
        connection.executemany("INSERT INTO sales VALUES (?, ?)", [(1, 10.5), (2, 20.0)])

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/execute",
            json={"question": "Show sales amounts", "sql": "SELECT id, amount FROM sales", "table_name": "sales"},
        )
        history = client.get("/api/query-runs")

    assert response.status_code == 200
    assert response.json()["verification"]["execution_status"] == "success"
    assert history.status_code == 200
    assert any(item["generated_sql"] == "SELECT id, amount FROM sales" for item in history.json()["items"])


def test_blocked_execute_persists_security_trace(tmp_path, monkeypatch):
    database = tmp_path / "blocked-trace.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (id INTEGER)")

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/execute",
            json={"question": "Delete all sales", "sql": "DELETE FROM sales", "table_name": "sales"},
        )
        history = client.get("/api/history/query-runs")

    assert response.status_code == 422
    assert history.status_code == 200
    assert any(
        item["execution_status"] == "syntax_error"
        for item in history.json()["items"]
    )


def test_query_report_connects_execution_validation_and_history(tmp_path, monkeypatch):
    database = tmp_path / "query-report.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (id INTEGER, amount REAL)")
        connection.execute("INSERT INTO sales VALUES (1, 25.0)")

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        executed = client.post(
            "/api/sql/execute",
            json={
                "question": "Show sales amounts",
                "sql": "SELECT id, amount FROM sales",
                "table_name": "sales",
            },
        )
        history = client.get("/api/query-runs")

        assert executed.status_code == 200
        execution = executed.json()
        report_id = execution["trace_query_id"]
        report = client.get(f"/api/query-runs/{report_id}/report")

    assert history.status_code == 200
    assert any(item["query_id"] == report_id for item in history.json()["items"])
    assert report.status_code == 200
    payload = report.json()
    assert payload["report_id"] == report_id
    assert payload["query_run"]["reliability_id"] == execution["reliability_id"]
    assert payload["query_run"]["user_question"] == "Show sales amounts"
    assert payload["query_run"]["generated_sql"] == "SELECT id, amount FROM sales"
    assert payload["validation"]["syntax_valid"] is True
    assert payload["result_verification"]["actual_columns"] == ["id", "amount"]
    assert payload["result_verification"]["total_result_count"] == 1
    assert payload["verification"]["execution_status"] == "success"
    assert payload["metadata_available"] is True
    assert "not scientifically calibrated" in payload["scoring_methodology"]["disclaimer"]


def test_rejected_query_report_preserves_evidence_and_reason(tmp_path, monkeypatch):
    database = tmp_path / "rejected-query-report.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (id INTEGER, amount REAL)")

    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        rejected = client.post(
            "/api/sql/execute",
            json={
                "question": "Show category values",
                "sql": "SELECT category FROM sales",
                "table_name": "sales",
            },
        )

        assert rejected.status_code == 422
        detail = rejected.json()["detail"]
        report = client.get(
            f"/api/query-runs/{detail['trace_query_id']}/report"
        )

    assert report.status_code == 200
    payload = report.json()
    assert payload["query_run"]["execution_status"] == "syntax_error"
    assert any(
        issue["type"] == "missing_column"
        for issue in payload["issues"]
    )
    assert payload["overall_verification_status"] == "ungrounded"
    assert payload["result_verification"] is None
    assert payload["metadata_available"] is True


def test_report_requirement_checks_do_not_upgrade_incomplete_evidence():
    semantic = {
        "status": "valid",
        "sql_coverage_status": "verified",
        "requirements": {
            "filters": [{"column": "amount", "operator": "gt", "value": 10}],
            "group_by_columns": ["region"],
            "aggregation": {"function": "sum", "column": "amount"},
            "requested_outputs": ["region"],
            "top_n": None,
            "rank_column": None,
        },
        "issues": [],
    }

    checks = _requirement_checks(semantic)
    assert checks
    assert all(check["status"] == "verified" for check in checks)

    semantic["sql_coverage_status"] = "not_fully_verified"
    incomplete_checks = _requirement_checks(semantic)
    assert all(check["status"] == "not_verified" for check in incomplete_checks)


def test_upload_creates_fingerprint_and_effective_profile(tmp_path, monkeypatch):
    database = tmp_path / "upload-trace.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        response = client.post(
            "/api/datasets/upload",
            files={"file": ("house.csv", b"price,location\n100,NY\n200,LA\n", "text/csv")},
        )

    assert response.status_code == 200
    dataset_id = response.json()["dataset"]["dataset_id"]
    table_name = response.json()["dataset"]["raw_table"]
    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            "SELECT fingerprint_hash FROM _queryglass_table_fingerprints "
            "WHERE table_name=? AND dataset_id=?",
            (table_name, dataset_id),
        ).fetchone()
        profile = connection.execute(
            "SELECT columns_json FROM _queryglass_effective_types "
            "WHERE table_name=? AND dataset_id=?",
            (table_name, dataset_id),
        ).fetchone()

    assert fingerprint is not None
    assert profile is not None
