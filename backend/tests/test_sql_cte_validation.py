import sqlite3

import pytest
from fastapi.testclient import TestClient

import backend.main as main
from backend.services.sql_engine import HallucinationDetector, SchemaContext


TABLE = "student_sleep_screen_time_academic_performance_processed"


@pytest.fixture
def student_database(tmp_path, monkeypatch):
    database = tmp_path / "student_queries.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f"""CREATE TABLE {TABLE} (
                student_id TEXT,
                year_of_study INTEGER,
                major_category TEXT,
                cgpa REAL,
                weekly_study_hours REAL,
                daily_screen_time_hours REAL,
                avg_sleep_hours_weekday REAL
            )"""
        )
        connection.executemany(
            f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("s1", 1, "science", 3.8, 20, 6.0, 7.5),
                ("s2", 1, "science", 3.4, 12, 8.0, 6.0),
                ("s3", 2, "arts", 3.9, 25, 4.0, 8.0),
                ("s4", 2, "arts", 3.2, 10, 9.0, 5.5),
            ],
        )
        connection.commit()
    monkeypatch.setattr(main, "DB_PATH", str(database))
    return database


def _validate(client, sql, question=""):
    return client.post(
        "/api/sql/validate",
        json={"question": question, "sql": sql, "table_name": TABLE},
    )


def test_basic_cte_wildcard_and_window_queries_validate(student_database):
    queries = [
        f"SELECT student_id, cgpa FROM {TABLE} LIMIT 10",
        f"""WITH averages AS (
            SELECT year_of_study, AVG(cgpa) AS avg_cgpa
            FROM {TABLE}
            GROUP BY year_of_study
        )
        SELECT year_of_study, avg_cgpa FROM averages""",
        f"""WITH first_cte AS (
            SELECT year_of_study, AVG(weekly_study_hours) AS avg_hours
            FROM {TABLE}
            GROUP BY year_of_study
        ), second_cte AS (
            SELECT * FROM first_cte WHERE avg_hours > 0
        )
        SELECT year_of_study, avg_hours FROM second_cte""",
        f"""WITH subset AS (
            SELECT s.* FROM {TABLE} AS s WHERE s.cgpa IS NOT NULL
        )
        SELECT student_id, cgpa FROM subset LIMIT 10""",
        f"""SELECT student_id, year_of_study, cgpa,
            ROW_NUMBER() OVER (
                PARTITION BY year_of_study ORDER BY cgpa DESC
            ) AS rn
            FROM {TABLE}""",
        f"""SELECT student_id,
            RANK() OVER (PARTITION BY major_category ORDER BY cgpa DESC) AS r,
            DENSE_RANK() OVER (PARTITION BY major_category ORDER BY cgpa DESC) AS dr
            FROM {TABLE}""",
        f"""SELECT student_id FROM {TABLE}
            WHERE cgpa > (SELECT AVG(cgpa) FROM {TABLE})""",
        f"""SELECT substr(major_category, 1, 1) AS initial,
            date('2025-01-01') AS query_date
            FROM {TABLE} LIMIT 1""",
        f"""SELECT major_category,
            CASE WHEN AVG(cgpa) >= 3.5 THEN 'high' ELSE 'standard' END AS band,
            COUNT(*) AS student_count
            FROM {TABLE}
            GROUP BY major_category
            HAVING COUNT(*) > 0
            ORDER BY student_count DESC""",
    ]

    with TestClient(main.app) as client:
        responses = [_validate(client, query) for query in queries]

    assert all(response.status_code == 200 for response in responses)
    assert all(response.json()["valid"] for response in responses)
    assert all(response.json()["operation"] == "SELECT" for response in responses)


def test_complex_cte_window_query_validates_and_executes(student_database):
    sql = f"""WITH avg_study AS (
        SELECT year_of_study, AVG(weekly_study_hours) AS avg_hours
        FROM {TABLE}
        GROUP BY year_of_study
    ), filtered AS (
        SELECT s.*
        FROM {TABLE} s
        JOIN avg_study a ON s.year_of_study = a.year_of_study
        WHERE s.weekly_study_hours > a.avg_hours
    ), ranked AS (
        SELECT student_id, year_of_study, cgpa, weekly_study_hours,
            ROW_NUMBER() OVER (
                PARTITION BY year_of_study ORDER BY cgpa DESC
            ) AS rn
        FROM filtered
    )
    SELECT student_id, year_of_study, cgpa, weekly_study_hours
    FROM ranked
    WHERE rn <= 3
    ORDER BY year_of_study, rn"""
    question = (
        "Identify the top 3 students in each year of study based on CGPA, but "
        "only include students whose weekly study hours are above the average "
        "study hours of their respective year."
    )

    with TestClient(main.app) as client:
        validation = _validate(client, sql, question)
        execution = client.post(
            "/api/sql/execute",
            json={"question": question, "sql": sql, "table_name": TABLE},
        )

    assert validation.status_code == 200
    assert validation.json()["valid"] is True
    assert validation.json()["hallucination"]["errors"] == []
    assert execution.status_code == 200
    assert execution.json()["status"] == "SUCCESS"
    assert execution.json()["columns"] == [
        "student_id", "year_of_study", "cgpa", "weekly_study_hours",
    ]


def test_recursive_cte_validates_and_executes_against_selected_table(student_database):
    sql = f"""WITH RECURSIVE years(year_of_study) AS (
        SELECT MIN(year_of_study) FROM {TABLE}
        UNION ALL
        SELECT year_of_study + 1 FROM years
        WHERE year_of_study < (SELECT MAX(year_of_study) FROM {TABLE})
    )
    SELECT year_of_study FROM years"""

    with TestClient(main.app) as client:
        validation = _validate(client, sql)
        execution = client.post(
            "/api/sql/execute",
            json={"question": "", "sql": sql, "table_name": TABLE},
        )

    assert validation.json()["valid"] is True
    assert execution.status_code == 200
    assert execution.json()["rows"] == [
        {"year_of_study": 1},
        {"year_of_study": 2},
    ]


def test_schema_resolution_reports_only_real_unknown_objects(student_database):
    context = SchemaContext(TABLE, str(student_database))
    unknown_table = HallucinationDetector.validate_against_schema(
        "SELECT * FROM nonexistent_table",
        context,
    )
    unknown_column = HallucinationDetector.validate_against_schema(
        f"SELECT nonexistent_column FROM {TABLE}",
        context,
    )

    assert unknown_table["is_valid"] is False
    assert unknown_table["invalid_tables"] == ["nonexistent_table"]
    assert unknown_column["is_valid"] is False
    assert unknown_column["invalid_columns"] == ["nonexistent_column"]

    with TestClient(main.app) as client:
        table_response = _validate(client, "SELECT * FROM nonexistent_table")
        column_response = _validate(client, f"SELECT nonexistent_column FROM {TABLE}")

    assert table_response.json()["error_category"] == "unknown_table"
    assert column_response.json()["error_category"] == "unknown_column"

    with TestClient(main.app) as client:
        cte_response = _validate(
            client,
            f"WITH known AS (SELECT cgpa FROM {TABLE}) SELECT * FROM misspelled_cte",
        )

    assert cte_response.json()["valid"] is False
    assert cte_response.json()["error_category"] == "invalid_cte_reference"


def test_only_selected_physical_table_is_readable(student_database):
    with sqlite3.connect(student_database) as connection:
        connection.execute("CREATE TABLE unrelated_data (secret TEXT)")
        connection.commit()

    result = HallucinationDetector.validate_against_schema(
        f"SELECT s.student_id FROM {TABLE} s JOIN unrelated_data u ON 1 = 1",
        SchemaContext(TABLE, str(student_database)),
    )

    assert result["is_valid"] is False
    assert result["invalid_tables"] == ["unrelated_data"]
    assert "unauthorized table" in result["errors"][0]


def test_write_operation_inside_cte_is_rejected(student_database):
    sql = f"""WITH removed AS (
        DELETE FROM {TABLE} RETURNING *
    )
    SELECT * FROM removed"""

    with TestClient(main.app) as client:
        response = _validate(client, sql)

    assert response.json()["valid"] is False
    assert response.json()["error_category"] == "safety_rejection"
    with sqlite3.connect(student_database) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 4


@pytest.mark.parametrize(
    "sql",
    [
        f"DELETE FROM {TABLE}",
        f"UPDATE {TABLE} SET cgpa = 0",
        f"DROP TABLE {TABLE}",
        f"INSERT INTO {TABLE} (student_id) VALUES ('new')",
    ],
)
def test_mutating_statements_are_rejected_without_changing_data(student_database, sql):
    with TestClient(main.app) as client:
        response = _validate(client, sql)
        execution = client.post(
            "/api/sql/execute",
            json={"question": "", "sql": sql, "table_name": TABLE},
        )

    assert response.json()["valid"] is False
    assert execution.status_code == 422
    assert response.json()["error_category"] == "safety_rejection"
    with sqlite3.connect(student_database) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 4


def test_multiple_statements_are_rejected(student_database):
    sql = f"SELECT student_id FROM {TABLE}; DELETE FROM {TABLE};"

    with TestClient(main.app) as client:
        response = _validate(client, sql)
        execution = client.post(
            "/api/sql/execute",
            json={"question": "", "sql": sql, "table_name": TABLE},
        )

    assert response.json()["valid"] is False
    assert response.json()["error_category"] == "safety_rejection"
    assert execution.status_code == 400
    assert "one SQL statement" in execution.json()["detail"]


def test_mutation_preview_route_is_not_available(student_database):
    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/preview-mutation",
            json={
                "question": "",
                "sql": f"DELETE FROM {TABLE} WHERE student_id = 's1'",
                "table_name": TABLE,
            },
        )

    assert response.status_code == 404


def test_semicolon_in_string_literal_is_not_treated_as_an_extra_statement(student_database):
    sql = f"SELECT ';' AS punctuation FROM {TABLE} LIMIT 1;"

    with TestClient(main.app) as client:
        validation = _validate(client, sql)
        execution = client.post(
            "/api/sql/execute",
            json={"question": "", "sql": sql, "table_name": TABLE},
        )

    assert validation.json()["valid"] is True
    assert execution.status_code == 200
    assert execution.json()["rows"] == [{"punctuation": ";"}]


def test_execution_preserves_the_500_row_preview_limit(student_database):
    with sqlite3.connect(student_database) as connection:
        connection.executemany(
            f"INSERT INTO {TABLE} (student_id) VALUES (?)",
            [(f"extra-{index}",) for index in range(501)],
        )
        connection.commit()

    with TestClient(main.app) as client:
        response = client.post(
            "/api/sql/execute",
            json={
                "question": "",
                "sql": f"SELECT student_id FROM {TABLE}",
                "table_name": TABLE,
            },
        )

    assert response.status_code == 200
    assert response.json()["row_count"] == 500
    assert len(response.json()["rows"]) == 500
    assert response.json()["truncated"] is True
