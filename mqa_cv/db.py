import sqlite3
from pathlib import Path


class Database:
    def __init__(self, db_path: Path | str = "analysis.db"):
        self.db_path = Path(db_path)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self) -> None:
        """Inizializza lo schema del database SQLite allineato a reporting.py."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    catalogue_id TEXT,
                    source_url TEXT,
                    rdf_path TEXT,
                    sha256 TEXT,
                    fetch_status TEXT,
                    fetch_http_status INTEGER,
                    fetch_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS validations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id TEXT,
                    validator_url TEXT,
                    shape_model TEXT,
                    conforms INTEGER,
                    results INTEGER,
                    violations INTEGER,
                    warnings INTEGER,
                    infos INTEGER,
                    http_status INTEGER,
                    error TEXT,
                    validated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (dataset_id) REFERENCES datasets (dataset_id),
                    UNIQUE(dataset_id, validator_url, shape_model)
                )
            """)
            conn.commit()

    def save_dataset(
        self,
        dataset_id: str,
        catalogue_id: str,
        source_url: str,
        rdf_path: str,
        sha256: str,
        fetch_status: str,
        fetch_http_status: int,
        fetch_error: str | None,
    ) -> None:
        """Inserisce o aggiorna le informazioni del dataset."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO datasets (
                    dataset_id, catalogue_id, source_url, rdf_path, sha256,
                    fetch_status, fetch_http_status, fetch_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id) DO UPDATE SET
                    catalogue_id = excluded.catalogue_id,
                    source_url = excluded.source_url,
                    rdf_path = excluded.rdf_path,
                    sha256 = excluded.sha256,
                    fetch_status = excluded.fetch_status,
                    fetch_http_status = excluded.fetch_http_status,
                    fetch_error = excluded.fetch_error
            """, (
                dataset_id, catalogue_id, source_url, rdf_path, sha256,
                fetch_status, fetch_http_status, fetch_error
            ))
            conn.commit()

    def save_validation_result(
        self,
        dataset_id: str,
        validator_url: str,
        shape_model: str,
        conforms: bool | None,
        results: int,
        violations: int,
        warnings: int,
        infos: int,
        http_status: int | None,
        error: str | None,
    ) -> None:
        """Inserisce o aggiorna l'esito della validazione SHACL."""
        conforms_val = None if conforms is None else (1 if conforms else 0)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO validations (
                    dataset_id, validator_url, shape_model, conforms, results,
                    violations, warnings, infos, http_status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dataset_id, validator_url, shape_model) DO UPDATE SET
                    conforms = excluded.conforms,
                    results = excluded.results,
                    violations = excluded.violations,
                    warnings = excluded.warnings,
                    infos = excluded.infos,
                    http_status = excluded.http_status,
                    error = excluded.error,
                    validated_at = CURRENT_TIMESTAMP
            """, (
                dataset_id, validator_url, shape_model, conforms_val, results,
                violations, warnings, infos, http_status, error
            ))
            conn.commit()