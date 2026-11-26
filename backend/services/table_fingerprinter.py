import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from collections import Counter
from backend.schemas.fingerprint import ColumnMetadata, ContentSignature, TableFingerprint


class TableFingerprinter:
    def compute_fingerprint(self, db_path: str, table_name: str, dataset_id: str = "") -> TableFingerprint:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            table_info = conn.execute(f'PRAGMA table_info("{table_name.replace(chr(34), chr(34)*2)}")').fetchall()
            fk_rows = conn.execute(f'PRAGMA foreign_key_list("{table_name.replace(chr(34), chr(34)*2)}")').fetchall()
            unique_columns = set()
            index_rows = conn.execute(
                f'PRAGMA index_list("{table_name.replace(chr(34), chr(34)*2)}")'
            ).fetchall()
            for index_row in index_rows:
                if index_row["unique"]:
                    index_name = index_row["name"].replace('"', '""')
                    index_columns = conn.execute(
                        f'PRAGMA index_info("{index_name}")'
                    ).fetchall()
                    unique_columns.update(row["name"] for row in index_columns)
            columns = []
            for index, row in enumerate(table_info, start=1):
                columns.append(ColumnMetadata(
                    name=row["name"], type=row["type"] or "", position=index,
                    nullable=not bool(row["notnull"]) and row["pk"] == 0,
                    primary_key=bool(row["pk"]), unique=row["name"] in unique_columns,
                ))
            row_count = conn.execute(f'SELECT COUNT(*) FROM "{table_name.replace(chr(34), chr(34)*2)}"').fetchone()[0]
            conn.row_factory = None
            cursor = conn.execute(f'SELECT * FROM "{table_name.replace(chr(34), chr(34)*2)}" ORDER BY 1 ASC LIMIT 1000')
            sample_rows = cursor.fetchall()

        structural_payload = [(c.name, c.type, c.position, c.nullable, c.primary_key, c.unique) for c in columns]
        structural_hash = hashlib.sha256(json.dumps(structural_payload, sort_keys=True).encode()).hexdigest()
        canonical = []
        distribution = Counter()
        for row in sample_rows:
            row_values = [json.dumps(v, sort_keys=True, default=str) if not isinstance(v, (str, int, float, type(None))) else str(v) for v in row]
            canonical.extend(row_values)
            distribution.update(row_values)
        content_hash = hashlib.sha256("\u241e".join(canonical).encode()).hexdigest()
        fingerprint_hash = hashlib.sha256(f"{structural_hash}:{content_hash}".encode()).hexdigest()
        signature = ContentSignature(
            sample_hash=content_hash, row_count=row_count, sample_size=len(sample_rows),
            value_distribution=dict(distribution.most_common(100)),
        )
        return TableFingerprint(
            table_name=table_name, dataset_id=dataset_id or None, fingerprint_hash=fingerprint_hash,
            structural_hash=structural_hash, content_hash=content_hash, columns=columns,
            content_signature=signature, computed_at=datetime.now(timezone.utc).isoformat(),
        )

    def save_fingerprint(self, db_path: str, fingerprint: TableFingerprint) -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS _queryglass_table_fingerprints (
                fingerprint_id TEXT PRIMARY KEY, table_name TEXT NOT NULL, dataset_id TEXT,
                fingerprint_hash TEXT NOT NULL, structural_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
                columns_json TEXT NOT NULL, content_signature_json TEXT NOT NULL, computed_at TEXT NOT NULL,
                UNIQUE(table_name, dataset_id))""")
            conn.execute("""INSERT INTO _queryglass_table_fingerprints
                (fingerprint_id, table_name, dataset_id, fingerprint_hash, structural_hash, content_hash, columns_json, content_signature_json, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(table_name, dataset_id) DO UPDATE SET fingerprint_hash=excluded.fingerprint_hash,
                structural_hash=excluded.structural_hash, content_hash=excluded.content_hash,
                columns_json=excluded.columns_json, content_signature_json=excluded.content_signature_json,
                computed_at=excluded.computed_at""",
                (hashlib.sha256(f"{fingerprint.table_name}:{fingerprint.dataset_id}".encode()).hexdigest(),
                 fingerprint.table_name, fingerprint.dataset_id, fingerprint.fingerprint_hash,
                 fingerprint.structural_hash, fingerprint.content_hash,
                 json.dumps([c.model_dump() for c in fingerprint.columns]),
                 json.dumps(fingerprint.content_signature.model_dump() if fingerprint.content_signature else {}),
                 fingerprint.computed_at or datetime.now(timezone.utc).isoformat()))

    def load_fingerprint(self, db_path: str, table_name: str, dataset_id: str = "") -> TableFingerprint | None:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT * FROM _queryglass_table_fingerprints WHERE table_name=? AND dataset_id IS ?",
                    (table_name, dataset_id or None),
                ).fetchone()
                if not row:
                    return None
                columns = json.loads(row["columns_json"]) if row["columns_json"] else []
                signature = json.loads(row["content_signature_json"]) if row["content_signature_json"] else {}
                return TableFingerprint(
                    table_name=row["table_name"], dataset_id=row["dataset_id"], fingerprint_hash=row["fingerprint_hash"],
                    structural_hash=row["structural_hash"], content_hash=row["content_hash"],
                    columns=columns, content_signature=signature or None, computed_at=row["computed_at"],
                )
        except sqlite3.Error:
            return None

    def compare_fingerprints(self, current: TableFingerprint, stored: TableFingerprint) -> dict[str, bool]:
        return current.matches(stored)
