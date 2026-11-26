"""Tests for the database-backed dataset ingestion workflow.

Covers: raw persistence on upload, initial quality analysis, preprocessing
gating and preservation of raw data, report recalculation, processed-version
storage (including idempotency and failure paths), and dataset isolation.
"""

import sqlite3
import time

import pandas as pd
from fastapi.testclient import TestClient

import backend.main as main
from backend.services import dataset_workflow as dw


def upload_csv(
    client: TestClient,
    content: bytes,
    filename: str = "data.csv",
    dataset_identity: str | None = None,
):
    data = {"dataset_identity": dataset_identity} if dataset_identity else None
    return client.post(
        "/api/datasets/upload",
        files={"file": (filename, content, "text/csv")},
        data=data,
    )


def test_workflow_dataset_list_is_available_before_any_upload(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        response = client.get("/api/datasets/workflow")

    assert response.status_code == 200
    assert response.json() == {"datasets": []}


def fetch_table(db_path: str, table: str) -> list[tuple]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(f'SELECT * FROM "{table}"').fetchall()


def preprocess_and_store(client: TestClient, dataset_id: str, options: dict | None = None):
    preprocessed = client.post(
        f"/api/datasets/workflow/{dataset_id}/preprocess",
        json={"options": options or {}},
    )
    assert preprocessed.status_code == 200
    run_id = preprocessed.json()["latest_run"]["run_id"]
    stored = client.post(f"/api/datasets/workflow/{dataset_id}/runs/{run_id}/store")
    assert stored.status_code == 200
    return preprocessed.json(), stored.json()


def set_dataset_status(db_path: str, dataset_id: str, status: str) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE _queryglass_datasets SET status=? WHERE dataset_id=?",
            (status, dataset_id),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# Upload and initial quality analysis
# ---------------------------------------------------------------------------


def test_upload_persists_raw_data_and_initial_quality_report(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    csv = (
        b"student_id,score,city\n"
        b"001,90,NYC\n"
        b"002,,LA\n"
        b"002,,LA\n"
        b"003,88,NYC\n"
        b"004,broken,SF\n"
        b"005,91,\n"
    )
    with TestClient(main.app) as client:
        response = upload_csv(client, csv)
        assert response.status_code == 200
        body = response.json()

        dataset = body["dataset"]
        assert dataset["status"] == "RAW_ANALYZED"
        assert dataset["filename"] == "data.csv"
        assert dataset["row_count"] == 6
        assert dataset["column_count"] == 3
        assert dataset["allowed_actions"]["preprocess"] is True
        assert dataset["allowed_actions"]["store_processed"] is False
        assert dataset["runs"] == []
        assert dataset["versions"] == []
        assert dataset["processed_report"] is None

        metrics = dataset["raw_report"]["metrics"]
        assert metrics["rows"] == 6
        assert metrics["columns"] == 3
        assert metrics["total_cells"] == 18
        # Two empty score cells plus one empty city cell.
        assert metrics["missing_cells"] == 3
        assert metrics["rows_with_missing"] == 3
        assert metrics["duplicate_rows"] == 1
        assert metrics["duplicate_rows_involved"] == 2
        assert metrics["invalid_values"] == 1
        assert metrics["type_mismatches"]["by_column"]["score"]["dominant_type"] == "numeric"
        assert metrics["total_issues"] == 3 + 1 + 1

    # Raw rows are durably stored, including the exact duplicate row.
    rows = fetch_table(str(database), dataset["raw_table"])
    assert len(rows) == 6
    assert any(row[0] == "001" for row in rows)


def test_upload_does_not_preprocess_or_store_automatically(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        response = upload_csv(client, b"a,b\n1,2\n3,4\n")
        dataset_id = response.json()["dataset"]["dataset_id"]

        snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()
        assert snapshot["runs"] == []
        assert snapshot["versions"] == []
        assert snapshot["processed_report"] is None
        assert snapshot["allowed_actions"]["store_processed"] is False

        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert not any(name.endswith("_processed") for name in tables)


def test_background_upload_job_reports_completion_and_actual_row_count(tmp_path, monkeypatch):
    database = tmp_path / "upload-job.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        started = client.post(
            "/api/datasets/upload-jobs",
            files={"file": ("job.csv", b"a,b\n1,2\n3,4\n5,6\n", "text/csv")},
        )
        assert started.status_code == 200
        job_id = started.json()["job_id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/datasets/upload-jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)

    assert job["status"] == "completed", job
    assert job["stage"] == "completed"
    assert job["processed_rows"] == job["total_rows"] == 3
    assert job["elapsed_seconds"] >= 0
    assert job["result"]["dataset"]["row_count"] == 3
    assert job["result"]["filename"] == "job.csv"


def test_background_upload_job_preserves_parse_errors(tmp_path, monkeypatch):
    database = tmp_path / "upload-job-error.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        started = client.post(
            "/api/datasets/upload-jobs",
            files={"file": ("bad.csv", b"", "text/csv")},
        )
        assert started.status_code == 200
        job_id = started.json()["job_id"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/datasets/upload-jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)

    assert job["status"] == "failed"
    assert job["stage"] == "failed"
    assert "Could not read dataset" in job["error"]
    assert job["result"] is None


def test_sqlite_insert_progress_uses_configured_chunks(tmp_path, monkeypatch):
    database = tmp_path / "insert-progress.sqlite3"
    monkeypatch.setenv("QUERYGLASS_SQLITE_INSERT_CHUNK_ROWS", "2")
    inserted = []

    main.save_to_sqlite(
        pd.DataFrame({"value": ["a", "b", "c", "d", "e"]}),
        "progress_rows",
        str(database),
        progress_callback=lambda current, total: inserted.append((current, total)),
    )

    assert inserted == [(2, 5), (4, 5), (5, 5)]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM progress_rows").fetchone()[0] == 5


def test_upload_rejects_unsupported_and_empty_files(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    dw.init_workflow_tables(str(database))

    with TestClient(main.app) as client:
        unsupported = client.post(
            "/api/datasets/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert unsupported.status_code == 415

        header_only = upload_csv(client, b"a,b\n", "empty.csv")
        assert header_only.status_code == 400

        # CSV parsing is deliberately lenient (encoding fallback), but binary
        # garbage with an Excel extension must be rejected by the parser.
        malformed = client.post(
            "/api/datasets/upload",
            files={"file": ("broken.xlsx", b"PK\x03\x04not-an-excel-file", "application/vnd.ms-excel")},
        )
        assert malformed.status_code == 400

    assert fetch_table(str(database), "_queryglass_datasets") == []


def test_upload_database_failure_marks_failed_and_disables_preprocessing(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    def fail_save(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(dw, "save_to_sqlite", fail_save)

    with TestClient(main.app) as client:
        response = upload_csv(client, b"a,b\n1,2\n")
        assert response.status_code == 500

        detail = response.json()["detail"]
        assert "Raw data could not be persisted" in detail["message"]
        dataset_id = detail["dataset"]["dataset_id"]
        snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()
        assert snapshot["status"] == "FAILED"
        assert snapshot["error_message"]
        assert snapshot["allowed_actions"]["preprocess"] is False

        blocked = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess", json={"options": {}}
        )
        assert blocked.status_code == 409

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    # The half-written raw table was cleaned up; only registry tables remain.
    assert not any(name in {"a", "data"} for name in tables)


def test_missing_values_ignore_zero_false_and_na_literals(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    csv = b"flag,value\n0,10\nFalse,NA\nNA,0\n,11\n"
    with TestClient(main.app) as client:
        response = upload_csv(client, csv)
        metrics = response.json()["dataset"]["raw_report"]["metrics"]

    # Only the one empty flag cell is missing; 0/False/NA are valid values.
    assert metrics["missing_cells"] == 1
    assert metrics["missing_by_column"]["flag"]["missing_count"] == 1
    assert metrics["missing_by_column"]["value"]["missing_count"] == 0


def test_duplicate_detection_counts_extra_rows_and_groups(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    csv = b"k\nA\nA\nB\nC\nA\n"
    with TestClient(main.app) as client:
        response = upload_csv(client, csv)
        metrics = response.json()["dataset"]["raw_report"]["metrics"]

    assert metrics["duplicate_rows"] == 2
    assert metrics["duplicate_rows_involved"] == 3
    assert metrics["duplicate_groups"] == 1
    assert metrics["duplicate_pct"] == 40.0


def test_type_mismatch_detection_uses_dominant_type(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    values = "\n".join(str(i) for i in range(1, 10)) + "\nX\n"
    csv = f"amount\n{values}".encode()
    with TestClient(main.app) as client:
        response = upload_csv(client, csv)
        metrics = response.json()["dataset"]["raw_report"]["metrics"]

    assert metrics["invalid_values"] == 1
    assert metrics["type_mismatches"]["by_column"]["amount"]["mismatch_count"] == 1
    assert metrics["type_mismatches"]["by_column"]["amount"]["dominant_type"] == "numeric"


def test_large_dataset_upload_and_analysis(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    rows = 20_000
    body = "\n".join(f"{i},{i * 2}" for i in range(rows))
    csv = f"x,y\n{body}\n".encode()

    with TestClient(main.app) as client:
        response = upload_csv(client, csv)
        assert response.status_code == 200
        metrics = response.json()["dataset"]["raw_report"]["metrics"]
        assert metrics["rows"] == rows
        assert metrics["duplicate_rows"] == 0
        assert metrics["invalid_values"] == 0

    assert len(fetch_table(str(database), "data")) == rows


# ---------------------------------------------------------------------------
# Preprocessing gating and execution
# ---------------------------------------------------------------------------


def test_preprocess_requires_stored_and_analyzed_state(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    dw.init_workflow_tables(str(database))

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO _queryglass_datasets "
            "(dataset_id, filename, raw_table, created_at, row_count, column_count, columns_meta, status) "
            "VALUES ('ds_pending', 'pending.csv', 'pending', '2026-01-01', 1, 1, '[]', 'UPLOADED')"
        )
        connection.commit()

    with TestClient(main.app) as client:
        blocked = client.post("/api/datasets/workflow/ds_pending/preprocess", json={"options": {}})
        assert blocked.status_code == 409
        assert "Raw data must be stored and analyzed first" in blocked.json()["detail"]

        missing = client.post("/api/datasets/workflow/ds_unknown/preprocess", json={"options": {}})
        assert missing.status_code == 404


def test_preprocess_rejects_concurrent_second_run(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n2\n").json()["dataset"]["dataset_id"]
        set_dataset_status(str(database), dataset_id, "PREPROCESSING")

        second = client.post(f"/api/datasets/workflow/{dataset_id}/preprocess", json={"options": {}})
        assert second.status_code == 409
        assert "already in progress" in second.json()["detail"]


def test_preprocessing_preserves_raw_data_and_recalculates_reports(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    csv = b"name,age\nAlice,30\nBob,\nBob,\nCara,25\n"
    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, csv).json()["dataset"]["dataset_id"]
        raw_before = fetch_table(str(database), "data")

        response = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess",
            json={"options": {"remove_duplicates": True, "missing_value_strategy": "leave"}},
        )
        assert response.status_code == 200
        snapshot = response.json()

    # Raw table is byte-for-byte unchanged.
    assert fetch_table(str(database), "data") == raw_before
    assert len(raw_before) == 4

    assert snapshot["status"] == "PREPROCESSED"
    assert snapshot["allowed_actions"]["store_processed"] is True

    raw_metrics = snapshot["raw_report"]["metrics"]
    processed_metrics = snapshot["processed_report"]["metrics"]
    assert raw_metrics["duplicate_rows"] == 1
    assert processed_metrics["duplicate_rows"] == 0
    assert processed_metrics["rows"] == 3
    # Missing values were left unchanged; the count only dropped because one
    # removed duplicate row carried a missing cell.
    assert raw_metrics["missing_cells"] == 2
    assert processed_metrics["missing_cells"] == 1

    comparison = {row["metric"]: row for row in snapshot["comparison"]["rows"]}
    assert comparison["total_rows"]["before"] == 4
    assert comparison["total_rows"]["after"] == 3
    assert comparison["duplicate_rows"]["after"] == 0
    assert snapshot["comparison"]["improved"] is True


def test_failed_preprocessing_keeps_raw_data_and_allows_retry(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    def fail_preprocess(*args, **kwargs):
        raise RuntimeError("boom")

    original_preprocess = dw.preprocess_dataframe
    monkeypatch.setattr(dw, "preprocess_dataframe", fail_preprocess)

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n2\n").json()["dataset"]["dataset_id"]
        raw_before = fetch_table(str(database), "data")

        failed = client.post(f"/api/datasets/workflow/{dataset_id}/preprocess", json={"options": {}})
        assert failed.status_code == 422
        assert "Preprocessing failed" in failed.json()["detail"]

        snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()
        assert fetch_table(str(database), "data") == raw_before
        assert snapshot["status"] == "RAW_ANALYZED"
        assert snapshot["latest_run"]["status"] == "FAILED"
        assert "boom" in snapshot["latest_run"]["error_details"]
        assert snapshot["allowed_actions"]["store_processed"] is False

    monkeypatch.setattr(dw, "preprocess_dataframe", original_preprocess)

    with TestClient(main.app) as client:
        retried = client.post(f"/api/datasets/workflow/{dataset_id}/preprocess", json={"options": {}})
        assert retried.status_code == 200
        snapshot = retried.json()
        assert snapshot["latest_run"]["status"] == "COMPLETED"
        assert snapshot["status"] == "PREPROCESSED"
        assert fetch_table(str(database), "data") == raw_before


def test_invalid_preprocess_options_are_rejected(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n").json()["dataset"]["dataset_id"]

        unknown = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess",
            json={"options": {"not_a_real_option": True}},
        )
        assert unknown.status_code == 422

        wrong_type = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess",
            json={"options": {"remove_duplicates": "yes"}},
        )
        assert wrong_type.status_code == 422


def test_preprocessed_comparison_reports_no_improvement_honestly(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    # Clean dataset: preprocessing has nothing to fix.
    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a,b\n1,x\n2,y\n").json()["dataset"]["dataset_id"]

        response = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess",
            json={"options": {"remove_duplicates": False, "missing_value_strategy": "leave"}},
        )
        assert response.status_code == 200
        comparison = response.json()["comparison"]

    assert comparison["improved"] is False
    assert comparison["total_issues_before"] == comparison["total_issues_after"] == 0


# ---------------------------------------------------------------------------
# Storing processed versions
# ---------------------------------------------------------------------------


def test_store_requires_completed_run(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n").json()["dataset"]["dataset_id"]

        missing_run = client.post(
            f"/api/datasets/workflow/{dataset_id}/runs/run_missing/store"
        )
        assert missing_run.status_code == 404


def test_store_processed_version_is_transactional_idempotent_and_retrievable(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    csv = b"name,age\nAlice,30\nBob,25\nBob,25\n"
    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, csv).json()["dataset"]["dataset_id"]
        raw_before = fetch_table(str(database), "data")

        preprocessed = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess",
            json={"options": {"remove_duplicates": True}},
        )
        run_id = preprocessed.json()["latest_run"]["run_id"]

        stored = client.post(f"/api/datasets/workflow/{dataset_id}/runs/{run_id}/store")
        assert stored.status_code == 200
        version = stored.json()["version"]
        assert version["row_count"] == 2
        assert version["column_count"] == 2

        again = client.post(f"/api/datasets/workflow/{dataset_id}/runs/{run_id}/store")
        assert again.status_code == 200
        assert again.json()["already_stored"] is True
        assert again.json()["version"]["version_id"] == version["version_id"]

        snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()
        assert snapshot["status"] == "PROCESSED_STORED"
        assert len(snapshot["versions"]) == 1
        assert snapshot["allowed_actions"]["store_processed"] is False
        assert snapshot["allowed_actions"]["preprocess"] is True

    # Exactly one processed table exists, and the raw table is intact.
    with sqlite3.connect(database) as connection:
        processed_tables = [
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM _queryglass_processed_versions"
            )
        ]
    assert processed_tables == [version["table_name"]]
    assert fetch_table(str(database), "data") == raw_before

    with TestClient(main.app) as client:
        rows_response = client.get(f"/api/datasets/{version['table_name']}/rows")
        assert rows_response.status_code == 200
        assert rows_response.json()["total_rows"] == 2

        listing = client.get("/api/datasets").json()
        assert version["table_name"] in [item["name"] for item in listing["datasets"]]


def test_reprocessing_same_dataset_replaces_the_only_processed_table(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"name,age\nAlice,30\nBob,25\nBob,25\n").json()["dataset"]["dataset_id"]
        _, initial_store = preprocess_and_store(client, dataset_id, {"remove_duplicates": True})
        initial_version = initial_store["version"]

        _, replacement_store = preprocess_and_store(client, dataset_id, {"remove_duplicates": True})
        replacement = replacement_store["version"]
        snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()

        assert replacement_store["replaced_previous"] is True
        assert replacement["table_name"] == initial_version["table_name"]
        assert len(snapshot["versions"]) == 1
        assert len(snapshot["runs"]) == 1
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM _queryglass_processed_versions").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM _queryglass_processing_runs").fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE '_queryglass_stage_%'"
            ).fetchone()[0] == 0


def test_reupload_replaces_every_managed_upload_and_preserves_non_upload_tables(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE customer_owned (value TEXT)")
        connection.execute("INSERT INTO customer_owned VALUES ('keep')")

    with TestClient(main.app) as client:
        original = upload_csv(client, b"value\nold\nold\n", "sales.csv").json()["dataset"]
        _, original_store = preprocess_and_store(
            client, original["dataset_id"], {"remove_duplicates": True}
        )
        old_raw_table = original["raw_table"]
        processed_table = original_store["version"]["table_name"]

        unrelated = upload_csv(client, b"category\nkeep\n", "other.csv").json()["dataset"]
        _, unrelated_store = preprocess_and_store(client, unrelated["dataset_id"])
        unrelated_table = unrelated_store["version"]["table_name"]

        replacement = upload_csv(
            client,
            b"value\nnew-a\nnew-b\n",
            "sales.csv",
            dataset_identity=original["dataset_identity"],
        ).json()["dataset"]
        assert replacement["dataset_identity"] == original["dataset_identity"]
        latest = replacement["raw_table"]
        assert client.get(f"/api/datasets/workflow/{original['dataset_id']}").status_code == 404
        tables = {item["name"] for item in client.get("/api/datasets").json()["datasets"]}
        assert old_raw_table == latest
        assert processed_table not in tables
        assert latest in tables
        assert unrelated["raw_table"] not in tables
        assert unrelated_table not in tables
        assert fetch_table(str(database), latest) == [("new-a",), ("new-b",)]

        with sqlite3.connect(database) as connection:
            registered = connection.execute(
                "SELECT dataset_id, dataset_identity FROM _queryglass_datasets "
                "WHERE dataset_identity=?",
                (original["dataset_identity"],),
            ).fetchall()
            stored_versions = connection.execute(
                "SELECT dataset_id, table_name FROM _queryglass_processed_versions"
            ).fetchall()
            assert registered == [(replacement["dataset_id"], original["dataset_identity"])]
            assert stored_versions == []
            assert connection.execute(
                "SELECT COUNT(*) FROM _queryglass_quality_reports WHERE dataset_id=?",
                (replacement["dataset_id"],),
            ).fetchone()[0] == 1
            assert connection.execute("SELECT value FROM customer_owned").fetchone() == ("keep",)


def test_new_upload_replaces_active_dataset_even_when_filename_matches(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        first = upload_csv(client, b"value\nfirst\n", "shared.csv").json()["dataset"]
        preprocess_and_store(client, first["dataset_id"])
        second = upload_csv(client, b"value\nsecond\n", "shared.csv").json()["dataset"]
        preprocess_and_store(client, second["dataset_id"])
        assert first["dataset_identity"] != second["dataset_identity"]

        updated_first = upload_csv(
            client,
            b"value\nupdated-first\n",
            "shared.csv",
            dataset_identity=first["dataset_identity"],
        ).json()["dataset"]
        updated_version = preprocess_and_store(client, updated_first["dataset_id"])[1]["version"]

        assert fetch_table(str(database), updated_version["table_name"]) == [("updated-first",)]
        assert client.get(f"/api/datasets/workflow/{second['dataset_id']}").status_code == 404
        assert client.get(f"/api/datasets/workflow/{first['dataset_id']}").status_code == 404
        assert client.get(f"/api/datasets/workflow/{updated_first['dataset_id']}").status_code == 200
        with sqlite3.connect(database) as connection:
            registered = connection.execute("SELECT dataset_id FROM _queryglass_datasets").fetchall()
            assert registered == [(updated_first["dataset_id"],)]


def test_existing_workflow_rows_migrate_to_independent_dataset_identities(tmp_path):
    database = tmp_path / "legacy-workflow.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE _queryglass_datasets ("
            "dataset_id TEXT PRIMARY KEY, filename TEXT NOT NULL, raw_table TEXT NOT NULL UNIQUE, "
            "created_at TEXT NOT NULL, row_count INTEGER NOT NULL, column_count INTEGER NOT NULL, "
            "columns_meta TEXT NOT NULL, status TEXT NOT NULL, error_message TEXT)"
        )
        connection.executemany(
            "INSERT INTO _queryglass_datasets "
            "(dataset_id, filename, raw_table, created_at, row_count, column_count, columns_meta, status) "
            "VALUES (?, 'same.csv', ?, '2026-01-01', 1, 1, '[]', 'RAW_ANALYZED')",
            [("ds_first", "same"), ("ds_second", "same_2")],
        )
        connection.commit()

    dw.init_workflow_tables(str(database))
    with sqlite3.connect(database) as connection:
        identities = connection.execute(
            "SELECT dataset_id, dataset_identity FROM _queryglass_datasets ORDER BY dataset_id"
        ).fetchall()
        identity_rows = connection.execute(
            "SELECT identity_id FROM _queryglass_dataset_identities ORDER BY identity_id"
        ).fetchall()

    assert identities == [("ds_first", "ds_first"), ("ds_second", "ds_second")]
    assert identity_rows == [("ds_first",), ("ds_second",)]


def test_failed_preprocessing_keeps_previous_valid_dataset_and_metadata(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        original = upload_csv(client, b"value\nvalid\n", "sales.csv").json()["dataset"]
        _, stored = preprocess_and_store(client, original["dataset_id"])
        previous_table = stored["version"]["table_name"]
        previous_rows = fetch_table(str(database), previous_table)
        monkeypatch.setattr(
            dw,
            "preprocess_dataframe",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("forced preprocessing failure")),
        )
        failed = client.post(
            f"/api/datasets/workflow/{original['dataset_id']}/preprocess",
            json={"options": {}},
        )
        assert failed.status_code == 422
        assert fetch_table(str(database), previous_table) == previous_rows
        assert client.get(f"/api/datasets/workflow/{original['dataset_id']}").status_code == 200
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM _queryglass_processed_versions"
            ).fetchone()[0] == 1


def test_replacement_write_failure_rolls_back_old_table_and_metadata(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        original = upload_csv(client, b"value\nold\n", "sales.csv").json()["dataset"]
        _, stored = preprocess_and_store(client, original["dataset_id"])
        previous_table = stored["version"]["table_name"]
        previous_rows = fetch_table(str(database), previous_table)

        preprocessed = client.post(
            f"/api/datasets/workflow/{original['dataset_id']}/preprocess",
            json={"options": {}},
        ).json()
        run_id = preprocessed["latest_run"]["run_id"]
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER reject_processed_version BEFORE INSERT "
                "ON _queryglass_processed_versions BEGIN SELECT RAISE(ABORT, 'forced write failure'); END"
            )
            connection.commit()

        failed_store = client.post(
            f"/api/datasets/workflow/{original['dataset_id']}/runs/{run_id}/store"
        )
        assert failed_store.status_code == 500
        assert fetch_table(str(database), previous_table) == previous_rows
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM _queryglass_processed_versions WHERE table_name=?",
                (previous_table,),
            ).fetchone()[0] == 1
            assert connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE '_queryglass_stage_%'"
            ).fetchone()[0] == 1
            connection.execute("DROP TRIGGER reject_processed_version")
            connection.commit()


def test_store_fails_cleanly_when_staged_table_missing(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n2\n").json()["dataset"]["dataset_id"]
        snapshot = client.post(
            f"/api/datasets/workflow/{dataset_id}/preprocess", json={"options": {}}
        ).json()
        run_id = snapshot["latest_run"]["run_id"]
        staged_name = f"_queryglass_stage_{run_id}"

        with sqlite3.connect(database) as connection:
            connection.execute(f'DROP TABLE "{staged_name}"')
            connection.commit()

        store = client.post(f"/api/datasets/workflow/{dataset_id}/runs/{run_id}/store")
        assert store.status_code == 409
        assert "staged processed table is missing" in store.json()["detail"]

        after = client.get(f"/api/datasets/workflow/{dataset_id}").json()
        assert after["status"] == "PREPROCESSED"
        assert after["versions"] == []


def test_workflow_apis_expose_only_the_authoritative_active_upload(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        first_id = upload_csv(client, b"a\n1\n", "first.csv").json()["dataset"]["dataset_id"]
        second_id = upload_csv(client, b"b\n9\n9\n", "second.csv").json()["dataset"]["dataset_id"]

        first_status = client.get(f"/api/datasets/workflow/{first_id}").status_code
        second = client.get(f"/api/datasets/workflow/{second_id}").json()

        assert first_status == 404
        assert second["filename"] == "second.csv"
        assert second["raw_report"]["metrics"]["duplicate_rows"] == 1

        listing = client.get("/api/datasets/workflow").json()["datasets"]
        assert {item["dataset_id"] for item in listing} == {second_id}


def test_workflow_state_survives_client_restart(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n2\n2\n").json()["dataset"]["dataset_id"]
        client.post(f"/api/datasets/workflow/{dataset_id}/preprocess", json={"options": {}})

    # A brand-new client (simulating a page refresh / backend restart).
    with TestClient(main.app) as client:
        snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()
        assert snapshot["status"] == "PREPROCESSED"
        assert snapshot["raw_report"]["metrics"]["duplicate_rows"] == 1
        assert snapshot["processed_report"]["metrics"]["duplicate_rows"] == 0
        assert snapshot["allowed_actions"]["store_processed"] is True


def test_upload_interrupted_mid_analysis_recovers_on_next_view(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        dataset_id = upload_csv(client, b"a\n1\n2\n").json()["dataset"]["dataset_id"]

    # Simulate a crash between the ANALYZING transition and the report write.
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM _queryglass_quality_reports")
        connection.execute(
            "UPDATE _queryglass_datasets SET status='ANALYZING' WHERE dataset_id=?",
            (dataset_id,),
        )
        connection.commit()

    with TestClient(main.app) as client:
        # Recovery runs in a background thread; poll until it resolves.
        snapshot = {}
        for _ in range(50):
            snapshot = client.get(f"/api/datasets/workflow/{dataset_id}").json()
            if snapshot["status"] != "ANALYZING":
                break
            time.sleep(0.2)

    assert snapshot["status"] == "RAW_ANALYZED"
    assert snapshot["raw_report"]["metrics"]["rows"] == 2
    assert snapshot["allowed_actions"]["preprocess"] is True


def test_upload_interrupted_before_raw_table_marks_failed(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    dw.init_workflow_tables(str(database))

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO _queryglass_datasets "
            "(dataset_id, filename, raw_table, created_at, row_count, column_count, columns_meta, status) "
            "VALUES ('ds_lost', 'lost.csv', 'never_created', '2026-01-01', 1, 1, '[]', 'ANALYZING')"
        )
        connection.commit()

    with TestClient(main.app) as client:
        snapshot = {}
        for _ in range(50):
            snapshot = client.get("/api/datasets/workflow/ds_lost").json()
            if snapshot["status"] != "ANALYZING":
                break
            time.sleep(0.2)

    assert snapshot["status"] == "FAILED"
    assert "upload the file again" in snapshot["error_message"]
    assert snapshot["allowed_actions"]["preprocess"] is False


def test_legacy_inplace_preprocess_blocked_for_workflow_raw_tables(tmp_path, monkeypatch):
    database = tmp_path / "workflow.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        raw_table = upload_csv(client, b"a\n1\n").json()["dataset"]["raw_table"]

        blocked = client.post(f"/api/datasets/{raw_table}/preprocess", json={"options": {}})
        assert blocked.status_code == 409
        assert "preserved raw dataset" in blocked.json()["detail"]


# ---------------------------------------------------------------------------
# Regression: preserved behaviors
# ---------------------------------------------------------------------------


def test_saved_legacy_table_can_still_be_preprocessed_in_place(tmp_path, monkeypatch):
    database = tmp_path / "datasets.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    main.save_to_sqlite(
        pd.DataFrame(
            {
                "student id": [" 001 ", "002", "002"],
                "sleep hours": ["7.5", "8.0", "8.0"],
            }
        ),
        "student_sleep",
        str(database),
        if_exists="fail",
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/api/datasets/student_sleep/preprocess",
            json={
                "options": {
                    "remove_duplicates": True,
                    "standardize_column_names": True,
                    "convert_data_types": True,
                    "missing_value_strategy": "leave",
                }
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["saved"] is True
    assert result["rows"] == 2
    text_cleaning = next(
        item for item in result["report"]["transformations"] if item["category"] == "text_cleaning"
    )
    assert text_cleaning["count"] == 1

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            'SELECT student_id, sleep_hours FROM "student_sleep" ORDER BY student_id'
        ).fetchall()

    assert rows == [("001", 7.5), ("002", 8.0)]


def test_dashboard_and_history_initialize_history_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "dashboard.sqlite3"))

    with TestClient(main.app) as client:
        dashboard_response = client.get("/api/dashboard")
        history_response = client.get("/api/history")

    assert dashboard_response.status_code == 200
    assert dashboard_response.json()["history"]["total_queries"] == 0
    assert history_response.status_code == 200
    assert history_response.json()["items"] == []


def test_dataset_rows_are_read_only_searchable_and_bounded(tmp_path, monkeypatch):
    database = tmp_path / "dataset-rows.sqlite3"
    monkeypatch.setattr(main, "DB_PATH", str(database))
    main.save_to_sqlite(
        pd.DataFrame(
            {
                "student_id": ["001", "002", "003"],
                "notes": ["A% result", "A_ result", "other"],
            }
        ),
        "student_preview",
        str(database),
        if_exists="fail",
    )

    with TestClient(main.app) as client:
        first_page = client.get("/api/datasets/student_preview/rows?limit=2")
        second_page = client.get("/api/datasets/student_preview/rows?offset=2&limit=2")
        searched = client.get(
            "/api/datasets/student_preview/rows",
            params={"search": "%", "limit": 100},
        )
        oversized = client.get("/api/datasets/student_preview/rows?limit=101")
        unknown = client.get("/api/datasets/not_a_table/rows")

    assert first_page.status_code == 200
    assert first_page.json()["columns"] == ["student_id", "notes"]
    assert [row["student_id"] for row in first_page.json()["rows"]] == ["001", "002"]
    assert first_page.json()["total_rows"] == 3
    assert first_page.json()["has_more"] is True
    assert [row["student_id"] for row in second_page.json()["rows"]] == ["003"]
    assert second_page.json()["has_more"] is False
    assert [row["student_id"] for row in searched.json()["rows"]] == ["001"]
    assert oversized.status_code == 422
    assert unknown.status_code == 404

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM "student_preview"'
        ).fetchone()[0] == 3
