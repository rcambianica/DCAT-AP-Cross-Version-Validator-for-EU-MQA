import sqlite3
from pathlib import Path


class Database:
    def __init__(self, db_path: Path | str = "analysis.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_schema(self) -> None:
        """Create tables and migrate older project databases in-place."""
        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    catalogue_id TEXT,
                    source_url TEXT,
                    rdf_path TEXT,
                    sha256 TEXT,
                    fetch_status TEXT NOT NULL DEFAULT 'pending',
                    fetch_http_status INTEGER,
                    fetch_error TEXT,
                    position INTEGER,
                    discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fetched_at TIMESTAMP,
                    validation_status TEXT NOT NULL DEFAULT 'pending',
                    validation_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(datasets)")}
            migrations = {
                "position": "INTEGER",
                "discovered_at": "TIMESTAMP",
                "fetched_at": "TIMESTAMP",
                "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
                "validation_error": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE datasets ADD COLUMN {name} {definition}")

            conn.execute("""
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_datasets_queue ON datasets(fetch_status, validation_status, position, dataset_id)")
            conn.commit()

    def add_discovered_datasets(self, dataset_ids: list[str], catalogue_id: str) -> tuple[int, int]:
        """Insert new IDs only; preserve states of already-known datasets."""
        inserted = 0
        with self.get_connection() as conn:
            for dataset_id in dataset_ids:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO datasets
                        (dataset_id, catalogue_id, fetch_status, validation_status)
                    VALUES (?, ?, 'pending', 'pending')
                """, (dataset_id, catalogue_id))
                inserted += cur.rowcount
            # Rebuild deterministic positions for the current inventory.
            rows = conn.execute("SELECT dataset_id FROM datasets WHERE catalogue_id=? ORDER BY dataset_id ASC", (catalogue_id,)).fetchall()
            conn.executemany("UPDATE datasets SET position=? WHERE dataset_id=?", [(i, row["dataset_id"]) for i, row in enumerate(rows, 1)])
            total = len(rows)
            conn.commit()
        return inserted, total

    def get_pending_fetch(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM datasets WHERE fetch_status IN ('pending','fetch_error','error') ORDER BY position ASC, dataset_id ASC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self.get_connection() as conn:
            return conn.execute(sql, params).fetchall()

    def get_pending_validation(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM datasets WHERE fetch_status='ok' AND validation_status IN ('pending','validation_error') ORDER BY position ASC, dataset_id ASC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self.get_connection() as conn:
            return conn.execute(sql, params).fetchall()

    def update_fetch(self, dataset_id: str, *, status: str, source_url: str | None = None,
                     rdf_path: str | None = None, sha256: str | None = None,
                     http_status: int | None = None, error: str | None = None) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                UPDATE datasets SET fetch_status=?, source_url=COALESCE(?,source_url),
                    rdf_path=COALESCE(?,rdf_path), sha256=COALESCE(?,sha256),
                    fetch_http_status=?, fetch_error=?,
                    fetched_at=CASE WHEN ?='ok' THEN CURRENT_TIMESTAMP ELSE fetched_at END
                WHERE dataset_id=?
            """, (status, source_url, rdf_path, sha256, http_status, error, status, dataset_id))
            conn.commit()

    def update_validation_status(self, dataset_id: str, status: str, error: str | None = None) -> None:
        with self.get_connection() as conn:
            conn.execute("UPDATE datasets SET validation_status=?, validation_error=? WHERE dataset_id=?", (status, error, dataset_id))
            conn.commit()

    def save_dataset(self, dataset_id: str, catalogue_id: str, source_url: str, rdf_path: str,
                     sha256: str, fetch_status: str, fetch_http_status: int,
                     fetch_error: str | None) -> None:
        """Backward-compatible helper for existing callers."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO datasets (dataset_id,catalogue_id,source_url,rdf_path,sha256,fetch_status,fetch_http_status,fetch_error)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(dataset_id) DO UPDATE SET catalogue_id=excluded.catalogue_id,
                    source_url=excluded.source_url,rdf_path=excluded.rdf_path,sha256=excluded.sha256,
                    fetch_status=excluded.fetch_status,fetch_http_status=excluded.fetch_http_status,fetch_error=excluded.fetch_error
            """, (dataset_id,catalogue_id,source_url,rdf_path,sha256,fetch_status,fetch_http_status,fetch_error))
            conn.commit()

    def save_validation_result(self, dataset_id: str, validator_url: str, shape_model: str,
                               conforms: bool | None, results: int, violations: int,
                               warnings: int, infos: int, http_status: int | None,
                               error: str | None) -> None:
        conforms_val = None if conforms is None else int(conforms)
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO validations (dataset_id,validator_url,shape_model,conforms,results,violations,warnings,infos,http_status,error)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dataset_id,validator_url,shape_model) DO UPDATE SET
                    conforms=excluded.conforms,results=excluded.results,violations=excluded.violations,
                    warnings=excluded.warnings,infos=excluded.infos,http_status=excluded.http_status,
                    error=excluded.error,validated_at=CURRENT_TIMESTAMP
            """, (dataset_id,validator_url,shape_model,conforms_val,results,violations,warnings,infos,http_status,error))
            conn.commit()

    def mark_completed_if_validated(self, dataset_id: str, validator_url: str, profiles: list[str]) -> bool:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT shape_model, conforms, http_status, error FROM validations WHERE dataset_id=? AND validator_url=?", (dataset_id, validator_url)).fetchall()
            by_profile = {r["shape_model"]: r for r in rows}
            done = all(p in by_profile and by_profile[p]["http_status"] == 200 and by_profile[p]["conforms"] is not None and not by_profile[p]["error"] for p in profiles)
            conn.execute("UPDATE datasets SET validation_status=?, validation_error=? WHERE dataset_id=?", ("completed" if done else "validation_error", None if done else "One or more profile validations failed or are missing", dataset_id))
            conn.commit()
            return done
