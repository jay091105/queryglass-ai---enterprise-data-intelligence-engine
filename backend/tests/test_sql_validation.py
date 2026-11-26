import sqlite3

from fastapi.testclient import TestClient

import backend.main as main
from backend.services.sql_engine import HallucinationDetector, SchemaContext, validate_sql_grounded


def test_validation_accepts_quoted_identifiers_casts_functions_and_aliases(tmp_path):
    database = tmp_path / "validation.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            'CREATE TABLE house_prices ("Price (in rupees)" TEXT, location TEXT)'
        )
        connection.commit()

    sql = """SELECT location AS city,
       AVG(CAST(REPLACE("Price (in rupees)", ',', '') AS REAL)) AS avg_price
FROM house_prices
GROUP BY location;"""
    result = HallucinationDetector.validate_against_schema(
        sql,
        SchemaContext("house_prices", str(database)),
    )

    assert result["is_valid"] is True
    assert result["errors"] == []


def test_validation_accepts_nested_aggregate_subquery(tmp_path, monkeypatch):
    database = tmp_path / "nested-validation.sqlite3"
    table_name = "student_sleep_screen_time_academic_performance_processed"
    with sqlite3.connect(database) as connection:
        connection.execute(f"CREATE TABLE {table_name} (age INTEGER, cgpa REAL)")
        connection.commit()

    sql = (
        f"SELECT MAX(age) FROM {table_name} "
        f"WHERE cgpa = (SELECT MAX(cgpa) FROM {table_name})"
    )
    question = "Find the highest age that has the highest cgpa"

    schema_result = HallucinationDetector.validate_against_schema(
        sql,
        SchemaContext(table_name, str(database)),
    )
    grounded_result = validate_sql_grounded(sql, question, table_name, str(database))
    monkeypatch.setattr(main, "DB_PATH", str(database))
    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/validate",
            json={"question": question, "sql": sql, "table_name": table_name},
        )

    assert schema_result["is_valid"] is True
    assert schema_result["errors"] == []
    assert grounded_result["is_valid"] is True
    assert grounded_result["stages"]["intent"]["concerns"] == []
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["confidence_reasons"] == ["Preflight Safe & Cleared"]


def test_validation_explains_destructive_operations(tmp_path, monkeypatch):
    database = tmp_path / "validation.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE house_prices (location TEXT)")
        connection.commit()
    monkeypatch.setattr(main, "DB_PATH", str(database))

    with TestClient(main.app) as client:
        delete_response = client.post(
            "/api/sql/validate",
            json={
                "question": "remove rows",
                "sql": "DELETE FROM house_prices WHERE location = 'x'",
                "table_name": "house_prices",
            },
        )
        truncate_response = client.post(
            "/api/sql/validate",
            json={
                "question": "clear the table",
                "sql": "TRUNCATE TABLE house_prices",
                "table_name": "house_prices",
            },
        )

    assert delete_response.status_code == 200
    assert any("DELETE changes stored data" in reason for reason in delete_response.json()["confidence_reasons"])
    assert truncate_response.status_code == 200
    assert any("TRUNCATE is a destructive table operation" in reason for reason in truncate_response.json()["confidence_reasons"])
