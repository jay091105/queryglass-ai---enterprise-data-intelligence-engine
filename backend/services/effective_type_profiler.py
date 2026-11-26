import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from backend.schemas.effective_type import EffectiveType, ColumnEffectiveType, TableEffectiveTypes


class EffectiveTypeProfiler:
    def profile_table(self, db_path: str, table_name: str, dataset_id: str = "") -> TableEffectiveTypes:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(f'SELECT * FROM "{table_name.replace(chr(34), chr(34)*2)}" LIMIT 1000').fetchall()
            schema = conn.execute(f'PRAGMA table_info("{table_name.replace(chr(34), chr(34)*2)}")').fetchall()
        columns = []
        for index, col in enumerate(schema):
            values = [row[index] for row in rows if row[index] is not None]
            columns.append(self._analyze_column(col[1], col[2] or "", values))
        return TableEffectiveTypes(table_name=table_name, dataset_id=dataset_id or None, columns=columns, computed_at=datetime.now(timezone.utc).isoformat())

    def _analyze_column(self, name: str, schema_type: str, values: list) -> ColumnEffectiveType:
        effective, confidence = self._infer(values)
        text_values = [str(v) for v in values]
        counter = Counter(text_values)
        numeric_range = None
        if effective in {EffectiveType.NUMERIC, EffectiveType.INTEGER, EffectiveType.FLOAT}:
            nums = []
            for v in values:
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    pass
            if nums:
                numeric_range = {
                    "min": min(nums),
                    "max": max(nums),
                    "mean": sum(nums) / len(nums),
                }
        examples = [v for v, _ in counter.most_common(5)]
        return ColumnEffectiveType(
            name=name, schema_type=schema_type, effective_type=effective, confidence=confidence,
            sample_size=len(values), pattern_examples=examples,
            numeric_range=numeric_range, top_values=dict(counter.most_common(10)),
            cardinality=len(counter),
        )

    def _infer(self, values: list) -> tuple[EffectiveType, float]:
        if not values:
            return EffectiveType.NULL, 1.0
        nums = 0
        for value in values:
            try:
                Decimal(str(value).strip())
                nums += 1
            except (InvalidOperation, ValueError, TypeError):
                pass
        numeric_ratio = nums / len(values)
        if numeric_ratio >= 0.95:
            return (EffectiveType.INTEGER if all(float(v).is_integer() for v in values if str(v).strip()) else EffectiveType.FLOAT), numeric_ratio

        text = [str(v).strip().lower() for v in values]
        boolean_values = {"true", "false", "yes", "no", "0", "1"}
        if len(set(text)) <= len(text) and set(text).issubset(boolean_values):
            return EffectiveType.BOOLEAN, sum(v in boolean_values for v in text) / len(text)
        date_ratio = sum(bool(re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", v)) for v in text) / len(text)
        if date_ratio >= 0.8:
            return EffectiveType.DATE, date_ratio
        cardinality = len(set(text))
        if cardinality <= max(20, int(len(values) * 0.2)):
            return EffectiveType.ENUM, cardinality / max(1, len(values))
        return EffectiveType.STRING, 0.8

    def save_profile(self, db_path: str, profile: TableEffectiveTypes) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS _queryglass_effective_types (
                profile_id TEXT PRIMARY KEY, table_name TEXT NOT NULL, dataset_id TEXT,
                columns_json TEXT NOT NULL, computed_at TEXT NOT NULL,
                UNIQUE(table_name, dataset_id))""")
            profile_id = f"{profile.table_name}:{profile.dataset_id or ''}"
            conn.execute("""INSERT INTO _queryglass_effective_types
                (profile_id, table_name, dataset_id, columns_json, computed_at)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(table_name, dataset_id) DO UPDATE SET
                columns_json=excluded.columns_json, computed_at=excluded.computed_at""",
                (profile_id, profile.table_name, profile.dataset_id,
                 json.dumps([c.model_dump(mode="json") for c in profile.columns]),
                 profile.computed_at or datetime.now(timezone.utc).isoformat()))

    def load_profile(self, db_path: str, table_name: str, dataset_id: str = "") -> TableEffectiveTypes | None:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM _queryglass_effective_types WHERE table_name=? AND dataset_id IS ?",
                    (table_name, dataset_id or None),
                ).fetchone()
                if not row:
                    return None
                return TableEffectiveTypes(
                    table_name=row["table_name"], dataset_id=row["dataset_id"],
                    columns=json.loads(row["columns_json"]), computed_at=row["computed_at"],
                )
        except sqlite3.Error:
            return None
