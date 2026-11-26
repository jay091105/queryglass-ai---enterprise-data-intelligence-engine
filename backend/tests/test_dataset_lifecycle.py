import sqlite3

from fastapi.testclient import TestClient

import backend.main as main
from backend.services import dataset_workflow as workflow
from backend.db.session import get_table_schema_details


def test_schema_inspection_reports_declared_unique_nullable_and_actual_types(tmp_path):
    database = tmp_path / "schema.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metadata (id INTEGER PRIMARY KEY, label VARCHAR(32) UNIQUE NOT NULL, "
            "optional_value)"
        )

    schema = get_table_schema_details("metadata", str(database))
    columns = {column["name"]: column for column in schema["columns"]}

    assert columns["id"]["is_pk"] is True
    assert columns["label"]["type"] == "VARCHAR(32)"
    assert columns["label"]["is_unique"] is True
    assert columns["label"]["not_null"] is True
    assert columns["optional_value"]["type"] == ""
    assert columns["optional_value"]["not_null"] is False


def test_workflow_schema_is_initialized_once_per_database_path(tmp_path, monkeypatch):
    database = tmp_path / "workflow-schema.sqlite3"
    original_connect = workflow._connect
    connections = 0

    def count_connections(path):
        nonlocal connections
        connections += 1
        return original_connect(path)

    monkeypatch.setattr(workflow, "_connect", count_connections)

    workflow.init_workflow_tables(str(database))
    workflow.init_workflow_tables(str(database))

    assert connections == 1


def test_upload_replacement_is_global_and_preserves_non_upload_tables(tmp_path, monkeypatch):
    database = tmp_path / "active.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE customer_owned (value TEXT)")
        connection.execute("INSERT INTO customer_owned VALUES ('keep')")

    with TestClient(main.app) as client:
        first_response = client.post(
            "/api/datasets/upload",
            files={"file": ("first.csv", b"value\nold\nold\n", "text/csv")},
        )
        assert first_response.status_code == 200
        first = first_response.json()["dataset"]
        processed = client.post(
            f"/api/datasets/workflow/{first['dataset_id']}/preprocess",
            json={"options": {"remove_duplicates": True}},
        )
        assert processed.status_code == 200
        run_id = processed.json()["latest_run"]["run_id"]
        stored = client.post(f"/api/datasets/workflow/{first['dataset_id']}/runs/{run_id}/store")
        assert stored.status_code == 200
        old_processed_table = stored.json()["version"]["table_name"]

        second_response = client.post(
            "/api/datasets/upload",
            files={"file": ("second.csv", b"item\nnew\n", "text/csv")},
        )
        assert second_response.status_code == 200
        second = second_response.json()["dataset"]
        listing = client.get("/api/datasets").json()
        stale_status = client.get(f"/api/datasets/workflow/{first['dataset_id']}").status_code

    assert listing["active_dataset"]["dataset_id"] == second["dataset_id"]
    assert first["raw_table"] not in [item["name"] for item in listing["datasets"]]
    assert old_processed_table not in [item["name"] for item in listing["datasets"]]
    assert second["raw_table"] in [item["name"] for item in listing["datasets"]]
    assert stale_status == 404
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM customer_owned").fetchone() == ("keep",)
        assert connection.execute(
            "SELECT dataset_id FROM _queryglass_active_dataset WHERE active_key='active'"
        ).fetchone() == (second["dataset_id"],)


def test_failed_replacement_rolls_back_old_active_data_and_metadata(tmp_path, monkeypatch):
    database = tmp_path / "rollback.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        first = client.post(
            "/api/datasets/upload",
            files={"file": ("first.csv", b"value\npreserved\n", "text/csv")},
        ).json()["dataset"]

        original_sanitizer = workflow.sanitize_table_name

        def fail_during_publish(_name):
            raise RuntimeError("forced replacement failure")

        monkeypatch.setattr(workflow, "sanitize_table_name", fail_during_publish)
        failed = client.post(
            "/api/datasets/upload",
            files={"file": ("replacement.csv", b"value\nnot-published\n", "text/csv")},
        )
        monkeypatch.setattr(workflow, "sanitize_table_name", original_sanitizer)
        assert failed.status_code == 500
        active = client.get("/api/datasets").json()["active_dataset"]

    assert active["dataset_id"] == first["dataset_id"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(f'SELECT value FROM "{first["raw_table"]}"').fetchall() == [("preserved",)]
        assert connection.execute(
            "SELECT COUNT(*) FROM _queryglass_datasets WHERE dataset_id=?",
            (first["dataset_id"],),
        ).fetchone()[0] == 1


def test_upload_during_active_preprocessing_is_rejected_without_replacement(tmp_path, monkeypatch):
    database = tmp_path / "busy-replacement.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        active = client.post(
            "/api/datasets/upload",
            files={"file": ("current.csv", b"value\nkeep\n", "text/csv")},
        ).json()["dataset"]
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE _queryglass_datasets SET status='PREPROCESSING' WHERE dataset_id=?",
                (active["dataset_id"],),
            )
            connection.commit()
        response = client.post(
            "/api/datasets/upload",
            files={"file": ("next.csv", b"value\nnew\n", "text/csv")},
        )
        current = client.get("/api/datasets").json()["active_dataset"]

    assert response.status_code == 409
    assert current["dataset_id"] == active["dataset_id"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(f'SELECT value FROM "{active["raw_table"]}"').fetchall() == [("keep",)]
