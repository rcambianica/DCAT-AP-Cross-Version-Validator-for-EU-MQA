import gzip
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class Database:
    def __init__(self, db_path: Path | str = "analysis.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a SQLite connection and always close it after commit/rollback.

        The built-in sqlite3 connection context manager manages transactions but
        does not close the connection. Explicit closure matters when validating
        large queues with many short database operations.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            # WAL permits readers while short database writes are committed.
            # NORMAL keeps the research database durable against process crashes;
            # the most recent transaction can be lost only on OS/power failure.
            conn.execute("PRAGMA synchronous = NORMAL")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def init_schema(self) -> None:
        """Create tables and migrate older project databases in-place."""
        with self.get_connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY,
                    catalogue_id TEXT,
                    category_id TEXT,
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
                "category_id": "TEXT",
                "discovered_at": "TIMESTAMP",
                "fetched_at": "TIMESTAMP",
                "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
                "validation_error": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE datasets ADD COLUMN {name} {definition}")

            validations_columns = {r["name"] for r in conn.execute("PRAGMA table_info(validations)")} if self._table_exists(conn, "validations") else set()

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
                    execution_status TEXT NOT NULL DEFAULT 'validated',
                    report_body_gzip BLOB,
                    report_sha256 TEXT,
                    report_size_bytes INTEGER,
                    report_compressed_size_bytes INTEGER,
                    report_content_type TEXT,
                    report_format TEXT,
                    report_metadata_json TEXT,
                    report_saved_at TIMESTAMP,
                    validated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (dataset_id) REFERENCES datasets (dataset_id),
                    UNIQUE(dataset_id, validator_url, shape_model)
                )
            """)
            validations_columns = {r["name"] for r in conn.execute("PRAGMA table_info(validations)")}
            if "execution_status" not in validations_columns:
                conn.execute("ALTER TABLE validations ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'validated'")
                # Existing v0.4.1 rows did not have execution_status. An error
                # row must not become a successful validation just because the
                # migration supplied the new column's default value.

            report_columns = {
                "report_body_gzip": "BLOB",
                "report_sha256": "TEXT",
                "report_size_bytes": "INTEGER",
                "report_compressed_size_bytes": "INTEGER",
                "report_content_type": "TEXT",
                "report_format": "TEXT",
                "report_metadata_json": "TEXT",
                "report_saved_at": "TIMESTAMP",
            }
            reports_need_migration = any(name not in validations_columns for name in report_columns)
            for name, definition in report_columns.items():
                if name not in validations_columns:
                    conn.execute(f"ALTER TABLE validations ADD COLUMN {name} {definition}")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS validation_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    validation_id INTEGER NOT NULL,
                    result_index INTEGER NOT NULL,
                    result_node TEXT,
                    focus_node TEXT,
                    result_path TEXT,
                    value TEXT,
                    source_shape TEXT,
                    source_constraint_component TEXT,
                    severity TEXT,
                    message TEXT,
                    properties_json TEXT NOT NULL,
                    FOREIGN KEY (validation_id) REFERENCES validations(id) ON DELETE CASCADE,
                    UNIQUE(validation_id, result_index)
                )
            """)
            # Idempotently repair any historical rows whose error field already
            # proves that the validation operation did not complete successfully.
            conn.execute("UPDATE validations SET execution_status='error' WHERE error IS NOT NULL AND TRIM(error) <> '' AND execution_status='validated'")

            # On first upgrade to report capture, successful historical checks
            # without a persisted report are marked pending so `run` can fetch
            # their detailed reports. RDF acquisition state is not affected.
            if reports_need_migration:
                conn.execute("""
                    UPDATE datasets
                       SET validation_status='pending', validation_error=NULL
                     WHERE fetch_status='ok'
                       AND EXISTS (
                           SELECT 1 FROM validations v
                           WHERE v.dataset_id=datasets.dataset_id
                             AND v.execution_status='validated'
                             AND v.http_status=200
                             AND v.report_body_gzip IS NULL
                       )
                """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_validations_lookup ON validations(dataset_id, validator_url, shape_model, execution_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validations_profile_status ON validations(shape_model, execution_status, validated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_results_severity ON validation_results(severity, source_constraint_component)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validation_results_focus_path ON validation_results(focus_node, result_path)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_datasets_queue ON datasets(fetch_status, validation_status, position, dataset_id)")
            conn.commit()

    def add_discovered_datasets(
        self, dataset_ids: list[str], catalogue_id: str, category_id: str | None = None,
    ) -> tuple[int, int]:
        """Upsert discovered IDs, record the selected category, and assign positions.

        Existing fetch/validation state is preserved. Positions are based on
        lexical ordering of dataset IDs within the catalogue, so the processing
        queue remains reproducible across runs.
        """
        unique_ids = sorted(set(dataset_ids))
        if not unique_ids:
            with self.get_connection() as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM datasets WHERE catalogue_id=?",
                    (catalogue_id,),
                ).fetchone()[0]
            return 0, total

        with self.get_connection() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM datasets WHERE catalogue_id=?",
                (catalogue_id,),
            ).fetchone()[0]

            conn.executemany(
                """
                INSERT OR IGNORE INTO datasets
                    (dataset_id, catalogue_id, category_id, fetch_status, validation_status)
                VALUES (?, ?, ?, 'pending', 'pending')
                """,
                [(dataset_id, catalogue_id, category_id) for dataset_id in unique_ids],
            )
            # When discovery is category-filtered, record the selected category
            # for both new and already-known datasets. Unfiltered discovery does
            # not erase a category recorded by an earlier filtered discovery.
            if category_id is not None:
                conn.executemany(
                    "UPDATE datasets SET category_id=? WHERE dataset_id=? AND catalogue_id=?",
                    [(category_id, dataset_id, catalogue_id) for dataset_id in unique_ids],
                )

            # SQLite performs the complete deterministic re-numbering in SQL;
            # this avoids fetching and updating tens of thousands of rows in Python.
            conn.execute(
                """
                WITH ordered AS (
                    SELECT dataset_id,
                           ROW_NUMBER() OVER (ORDER BY dataset_id ASC) AS new_position
                    FROM datasets
                    WHERE catalogue_id=?
                )
                UPDATE datasets
                   SET position = (
                       SELECT new_position
                       FROM ordered
                       WHERE ordered.dataset_id = datasets.dataset_id
                   )
                 WHERE catalogue_id=?
                """,
                (catalogue_id, catalogue_id),
            )

            total = conn.execute(
                "SELECT COUNT(*) FROM datasets WHERE catalogue_id=?",
                (catalogue_id,),
            ).fetchone()[0]
            conn.commit()

        inserted = total - before
        return inserted, total

    def get_pending_fetch(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM datasets WHERE fetch_status IN ('pending','fetch_error','error') ORDER BY position ASC, dataset_id ASC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self.get_connection() as conn:
            return conn.execute(sql, params).fetchall()

    def get_pending_validation(
        self,
        validator_url: str,
        profiles: list[str],
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """Select RDF-ready datasets whose validation matrix is incomplete.

        A dataset is eligible when at least one expected profile has neither a
        successful validation nor an explicit skip record for the configured
        validator. This also makes a change in the discovered OpenAPI profiles
        visible on subsequent runs.
        """
        if not profiles:
            return []
        placeholders = ",".join("?" for _ in profiles)
        sql = f"""
            SELECT d.*
            FROM datasets d
            WHERE d.fetch_status='ok'
              AND (
                    d.validation_status IN ('pending','validation_error')
                    OR (
                        SELECT COUNT(DISTINCT v.shape_model)
                        FROM validations v
                        WHERE v.dataset_id=d.dataset_id
                          AND v.validator_url=?
                          AND v.shape_model IN ({placeholders})
                          AND (
                                v.execution_status='skipped'
                                OR (v.execution_status='validated' AND v.http_status=200 AND v.conforms IS NOT NULL AND v.error IS NULL)
                              )
                    ) < ?
                  )
            ORDER BY d.position ASC, d.dataset_id ASC
        """
        params: list[object] = [validator_url, *profiles, len(profiles)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.get_connection() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

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

    @staticmethod
    def _report_format(content_type: str | None, parsed: bool) -> str:
        if not parsed:
            return "http-body"
        value = (content_type or "").lower()
        if "turtle" in value:
            return "turtle"
        if "ld+json" in value or "json" in value:
            return "json-ld"
        if "rdf+xml" in value or "xml" in value:
            return "rdf-xml"
        if "n-triples" in value:
            return "n-triples"
        if "trig" in value:
            return "trig"
        if "n3" in value:
            return "n3"
        return "rdf"

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
        execution_status: str | None = None,
        report_body: bytes | None = None,
        report_content_type: str | None = None,
        report_parsed: bool = False,
        report_metadata_json: str | None = None,
        validation_details: list[dict] | None = None,
    ) -> None:
        """Upsert a validation and its gzip-compressed original report.

        Keeping the compressed body in the validation row avoids generating
        hundreds of thousands of small files at catalogue scale. Normalized
        result rows are stored separately for indexed analysis.
        """
        if execution_status is None:
            execution_status = "error" if error else "validated"
        if execution_status not in {"validated", "skipped", "error"}:
            raise ValueError(f"Unsupported execution_status: {execution_status}")
        conforms_val = None if conforms is None else int(conforms)
        details = validation_details or []
        compressed_body = gzip.compress(report_body, compresslevel=6, mtime=0) if report_body else None
        report_sha256 = hashlib.sha256(report_body).hexdigest() if report_body else None
        report_size = len(report_body) if report_body else None
        report_compressed_size = len(compressed_body) if compressed_body else None
        report_format = self._report_format(report_content_type, report_parsed) if report_body else None

        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO validations (
                    dataset_id,validator_url,shape_model,conforms,results,violations,warnings,infos,
                    http_status,error,execution_status,report_body_gzip,report_sha256,report_size_bytes,
                    report_compressed_size_bytes,report_content_type,report_format,report_metadata_json,report_saved_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(dataset_id,validator_url,shape_model) DO UPDATE SET
                    conforms=excluded.conforms,results=excluded.results,violations=excluded.violations,
                    warnings=excluded.warnings,infos=excluded.infos,http_status=excluded.http_status,
                    error=excluded.error,execution_status=excluded.execution_status,
                    report_body_gzip=excluded.report_body_gzip,report_sha256=excluded.report_sha256,
                    report_size_bytes=excluded.report_size_bytes,
                    report_compressed_size_bytes=excluded.report_compressed_size_bytes,
                    report_content_type=excluded.report_content_type,report_format=excluded.report_format,
                    report_metadata_json=excluded.report_metadata_json,
                    report_saved_at=CASE WHEN excluded.report_body_gzip IS NOT NULL THEN CURRENT_TIMESTAMP ELSE NULL END,
                    validated_at=CURRENT_TIMESTAMP
            """, (
                dataset_id, validator_url, shape_model, conforms_val, results, violations, warnings, infos,
                http_status, error, execution_status, compressed_body, report_sha256, report_size,
                report_compressed_size, report_content_type, report_format, report_metadata_json,
            ))
            validation_id = conn.execute(
                "SELECT id FROM validations WHERE dataset_id=? AND validator_url=? AND shape_model=?",
                (dataset_id, validator_url, shape_model),
            ).fetchone()[0]
            conn.execute(
                "UPDATE validations SET report_saved_at=CASE WHEN report_body_gzip IS NOT NULL THEN COALESCE(report_saved_at,CURRENT_TIMESTAMP) ELSE NULL END WHERE id=?",
                (validation_id,),
            )

            conn.execute("DELETE FROM validation_results WHERE validation_id=?", (validation_id,))
            if details:
                conn.executemany("""
                    INSERT INTO validation_results (
                        validation_id,result_index,result_node,focus_node,result_path,value,source_shape,
                        source_constraint_component,severity,message,properties_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, [
                    (
                        validation_id, detail.get("result_index", index), detail.get("result_node"),
                        detail.get("focus_node"), detail.get("result_path"), detail.get("value"),
                        detail.get("source_shape"), detail.get("source_constraint_component"),
                        detail.get("severity"), detail.get("message"), detail.get("properties_json", "{}"),
                    )
                    for index, detail in enumerate(details, start=1)
                ])
            conn.commit()

    def get_raw_report(self, dataset_id: str, profile: str) -> sqlite3.Row | None:
        """Return the most recently saved raw report for a dataset/profile."""
        with self.get_connection() as conn:
            return conn.execute("""
                SELECT report_body_gzip,report_content_type,report_format,report_sha256,
                       report_size_bytes,report_compressed_size_bytes,report_saved_at,
                       http_status,execution_status
                FROM validations
                WHERE dataset_id=? AND shape_model=? AND report_body_gzip IS NOT NULL
                ORDER BY validated_at DESC LIMIT 1
            """, (dataset_id, profile)).fetchone()

    def get_validation_details(
        self, dataset_id: str | None = None, profile: str | None = None,
        severity: str | None = None, limit: int = 50,
    ) -> list[sqlite3.Row]:
        """Query indexed SHACL result details for qualitative investigation."""
        sql = """
            SELECT d.dataset_id, v.shape_model, v.conforms, v.violations, v.warnings, v.infos,
                   v.http_status, v.report_sha256, v.report_size_bytes,
                   v.report_compressed_size_bytes, v.report_content_type, v.report_format,
                   v.report_saved_at, v.validated_at,
                   r.result_index, r.result_node, r.focus_node, r.result_path, r.value,
                   r.source_shape, r.source_constraint_component, r.severity, r.message, r.properties_json
            FROM validation_results r
            JOIN validations v ON v.id=r.validation_id
            JOIN datasets d ON d.dataset_id=v.dataset_id
            WHERE 1=1
        """
        params: list[object] = []
        if dataset_id:
            sql += " AND d.dataset_id=?"
            params.append(dataset_id)
        if profile:
            sql += " AND v.shape_model=?"
            params.append(profile)
        if severity:
            sql += " AND LOWER(r.severity)=LOWER(?)"
            params.append(severity)
        sql += " ORDER BY v.validated_at DESC, d.dataset_id, v.shape_model, r.result_index LIMIT ?"
        params.append(max(1, min(limit, 10000)))
        with self.get_connection() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def mark_completed_if_validated(
        self,
        dataset_id: str,
        validator_url: str,
        profiles: list[str],
    ) -> bool:
        if not profiles:
            return False
        placeholders = ",".join("?" for _ in profiles)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT shape_model, execution_status, conforms, http_status, error
                FROM validations
                WHERE dataset_id=? AND validator_url=? AND shape_model IN ({placeholders})
                """,
                (dataset_id, validator_url, *profiles),
            ).fetchall()
            completed_profiles = {
                r["shape_model"]
                for r in rows
                if r["execution_status"] == "skipped"
                or (
                    r["execution_status"] == "validated"
                    and r["http_status"] == 200
                    and r["conforms"] is not None
                    and r["error"] is None
                )
            }
            done = all(p in completed_profiles for p in profiles)
            error = None if done else "One or more profile validations failed or are missing"
            conn.execute(
                "UPDATE datasets SET validation_status=?, validation_error=? WHERE dataset_id=?",
                ("completed" if done else "validation_error", error, dataset_id),
            )
            conn.commit()
            return done
