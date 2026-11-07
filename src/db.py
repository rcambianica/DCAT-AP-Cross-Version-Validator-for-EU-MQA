import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class Database:
    def __init__(
        self,
        db_path: Path | str = "analysis.db",
        report_store_path: Path | str = "data/validation-reports",
        report_store=None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_store_path = Path(report_store_path)
        self._report_store = report_store

    def get_report_store(self, *, read_only: bool = False):
        """Open the configured PyOxigraph store lazily and reuse the instance."""
        if self._report_store is None:
            from src.report_store import ValidationReportStore
            self._report_store = ValidationReportStore(
                self.report_store_path, read_only=read_only
            )
        elif read_only and not getattr(self._report_store, "read_only", False):
            # An already-open writable store is safe for reads and avoids
            # opening the same RocksDB directory twice in one process.
            return self._report_store
        return self._report_store

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
        """Create the current schema and discard obsolete report-normalization tables.

        The report-storage redesign is intentionally a clean data boundary:
        legacy validation rows are not migrated because they point to reports
        stored as SQLite BLOBs and cannot be treated as reports already present
        in the triplestore. The old dataset inventory is also discarded so the
        new experiment starts from a clean SQLite population.
        """
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
            dataset_columns = {r["name"] for r in conn.execute("PRAGMA table_info(datasets)")}
            for name, definition in {
                "position": "INTEGER",
                "category_id": "TEXT",
                "discovered_at": "TIMESTAMP",
                "fetched_at": "TIMESTAMP",
                "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
                "validation_error": "TEXT",
            }.items():
                if name not in dataset_columns:
                    conn.execute(f"ALTER TABLE datasets ADD COLUMN {name} {definition}")

            old_validation_columns = (
                {r["name"] for r in conn.execute("PRAGMA table_info(validations)")}
                if self._table_exists(conn, "validations") else set()
            )
            # The previous schema stored compressed reports in SQLite and
            # normalized every ValidationResult into validation_results. Those
            # rows have no corresponding named graph, so reset them once.
            if old_validation_columns and (
                "report_graph_iri" not in old_validation_columns
                or "report_body_gzip" in old_validation_columns
            ):
                conn.execute("DROP TABLE IF EXISTS validation_results")
                conn.execute("DROP TABLE IF EXISTS validations")
                conn.execute("DELETE FROM datasets")
            else:
                # The table is no longer part of the schema, including when a
                # user manually retained it from an earlier release.
                conn.execute("DROP TABLE IF EXISTS validation_results")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS validations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id TEXT NOT NULL,
                    validator_url TEXT NOT NULL,
                    shape_model TEXT NOT NULL,
                    conforms INTEGER,
                    results INTEGER NOT NULL DEFAULT 0,
                    violations INTEGER NOT NULL DEFAULT 0,
                    warnings INTEGER NOT NULL DEFAULT 0,
                    infos INTEGER NOT NULL DEFAULT 0,
                    http_status INTEGER,
                    error TEXT,
                    execution_status TEXT NOT NULL DEFAULT 'pending',
                    report_graph_iri TEXT,
                    report_sha256 TEXT,
                    report_content_type TEXT,
                    report_format TEXT,
                    report_status TEXT NOT NULL DEFAULT 'not_available',
                    report_triples INTEGER,
                    report_saved_at TIMESTAMP,
                    validated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (dataset_id) REFERENCES datasets (dataset_id),
                    UNIQUE(dataset_id, validator_url, shape_model)
                )
            """)
            # Keep only the two domain tables in SQLite. PRAGMA user_version
            # records the schema revision without adding a third application table.
            conn.execute("DROP TABLE IF EXISTS schema_metadata")
            conn.execute("PRAGMA user_version = 3")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validations_lookup ON validations(dataset_id, validator_url, shape_model, execution_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validations_profile_status ON validations(shape_model, execution_status, validated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_validations_report_status ON validations(report_status, report_graph_iri)")
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
        *,
        allow_skipped: bool = True,
    ) -> list[sqlite3.Row]:
        """Select RDF-ready datasets whose validation matrix is incomplete.

        When ``allow_skipped`` is false, every expected profile must have a
        successful validation. This is used by the default full-validation run
        to revisit datasets previously processed with short-circuiting.
        """
        if not profiles:
            return []
        placeholders = ",".join("?" for _ in profiles)
        completed_predicate = "(v.execution_status='validated' AND v.http_status=200 AND v.conforms IS NOT NULL AND v.error IS NULL AND v.report_status='stored' AND v.report_graph_iri IS NOT NULL)"
        if allow_skipped:
            completed_predicate = f"(v.execution_status='skipped' OR {completed_predicate})"
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
                          AND {completed_predicate}
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

    def _remove_report_graph(self, graph_iri: str | None) -> None:
        """Best-effort cleanup for a report no longer referenced by SQLite."""
        if not graph_iri:
            return
        try:
            self.get_report_store().remove_report(graph_iri)
        except Exception:
            logger.warning(
                "Il report non più referenziato %s non è stato rimosso dal triplestore",
                graph_iri,
                exc_info=True,
            )

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
        report_format: str | None = None,
    ) -> int:
        """Persist validation metadata in SQLite and the complete RDF report in Oxigraph.

        SQLite is committed before writing to Oxigraph. ``report_status`` makes
        the two-store operation recoverable: a successful validation is only
        considered complete after its report graph has been stored and verified.
        Report graph IRIs are content-addressed to make retries idempotent.
        """
        if execution_status is None:
            execution_status = "error" if error else "validated"
        if execution_status not in {"validated", "skipped", "error"}:
            raise ValueError(f"Unsupported execution_status: {execution_status}")

        is_rdf_report = bool(report_body and report_parsed and report_format)
        report_sha256 = hashlib.sha256(report_body).hexdigest() if is_rdf_report else None
        report_status = "pending" if is_rdf_report else "not_available"
        conforms_value = None if conforms is None else int(conforms)

        with self.get_connection() as conn:
            previous = conn.execute(
                "SELECT report_graph_iri FROM validations "
                "WHERE dataset_id=? AND validator_url=? AND shape_model=?",
                (dataset_id, validator_url, shape_model),
            ).fetchone()
            previous_graph_iri = previous["report_graph_iri"] if previous else None
            conn.execute("""
                INSERT INTO validations (
                    dataset_id, validator_url, shape_model, conforms, results,
                    violations, warnings, infos, http_status, error, execution_status,
                    report_graph_iri, report_sha256, report_content_type, report_format,
                    report_status, report_triples, report_saved_at, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(dataset_id, validator_url, shape_model) DO UPDATE SET
                    conforms=excluded.conforms,
                    results=excluded.results,
                    violations=excluded.violations,
                    warnings=excluded.warnings,
                    infos=excluded.infos,
                    http_status=excluded.http_status,
                    error=excluded.error,
                    execution_status=excluded.execution_status,
                    report_content_type=excluded.report_content_type,
                    report_format=excluded.report_format,
                    report_status=excluded.report_status,
                    validated_at=CURRENT_TIMESTAMP
            """, (
                dataset_id, validator_url, shape_model, conforms_value, results,
                violations, warnings, infos, http_status, error, execution_status,
                report_content_type, report_format if is_rdf_report else None,
                report_status,
            ))
            row = conn.execute(
                "SELECT id FROM validations WHERE dataset_id=? AND validator_url=? AND shape_model=?",
                (dataset_id, validator_url, shape_model),
            ).fetchone()
            validation_id = int(row["id"])
            conn.commit()

        if not is_rdf_report:
            with self.get_connection() as conn:
                conn.execute("""
                    UPDATE validations
                       SET report_graph_iri=NULL, report_sha256=NULL,
                           report_triples=NULL, report_saved_at=NULL
                     WHERE id=?
                """, (validation_id,))
                conn.commit()
            self._remove_report_graph(previous_graph_iri)
            return validation_id

        graph_iri = f"urn:mqa:validation-report:{validation_id}:{report_sha256}"
        try:
            report_store = self.get_report_store()
            triple_count = report_store.save_report(graph_iri, report_body, report_format)
            if triple_count == 0:
                raise RuntimeError("Il report salvato non contiene triple")
            report_store.save_report_metadata(
                graph_iri,
                validation_id=validation_id,
                dataset_id=dataset_id,
                validator_url=validator_url,
                shape_model=shape_model,
                conforms=conforms,
                results=results,
                violations=violations,
                warnings=warnings,
                infos=infos,
                report_sha256=report_sha256,
                report_format=report_format,
                report_content_type=report_content_type,
                report_triples=triple_count,
            )
        except Exception as exc:
            message = f"Report SHACL non persistito nel triplestore: {exc}"
            with self.get_connection() as conn:
                conn.execute("""
                    UPDATE validations
                       SET execution_status='error', report_status='error', error=?,
                           report_graph_iri=NULL, report_sha256=NULL,
                           report_triples=NULL, report_saved_at=NULL
                     WHERE id=?
                """, (message, validation_id))
                conn.commit()
            self._remove_report_graph(previous_graph_iri)
            raise RuntimeError(message) from exc

        with self.get_connection() as conn:
            conn.execute("""
                UPDATE validations
                   SET report_graph_iri=?, report_sha256=?, report_triples=?,
                       report_status='stored', report_saved_at=CURRENT_TIMESTAMP
                 WHERE id=?
            """, (graph_iri, report_sha256, triple_count, validation_id))
            conn.commit()

        # Re-validating the same dataset/profile replaces the current report
        # reference. Remove the previous content-addressed graph only after the
        # new graph is safely stored and referenced from SQLite.
        if previous_graph_iri and previous_graph_iri != graph_iri:
            self._remove_report_graph(previous_graph_iri)
        return validation_id

    def get_validation_report(
        self, dataset_id: str, profile: str, validator_url: str | None = None,
    ) -> sqlite3.Row | None:
        sql = """
            SELECT v.*, d.dataset_id
            FROM validations v
            JOIN datasets d ON d.dataset_id=v.dataset_id
            WHERE d.dataset_id=? AND v.shape_model=?
        """
        params: list[object] = [dataset_id, profile]
        if validator_url:
            sql += " AND v.validator_url=?"
            params.append(validator_url)
        sql += " ORDER BY v.validated_at DESC LIMIT 1"
        with self.get_connection() as conn:
            return conn.execute(sql, tuple(params)).fetchone()

    def get_validation_report_rows(
        self,
        *,
        successful_only: bool = True,
        dataset_id: str | None = None,
        profile: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = """
            SELECT v.id, v.dataset_id, v.validator_url, v.shape_model, v.conforms,
                   v.results, v.violations, v.warnings, v.infos, v.report_graph_iri,
                   v.report_sha256, v.report_status, v.report_format, v.report_content_type,
                   v.report_triples, v.report_saved_at, v.validated_at
            FROM validations v
        """
        predicates = []
        params: list[object] = []
        if successful_only:
            predicates.extend([
                "v.execution_status='validated'", "v.http_status=200",
                "v.conforms IS NOT NULL", "(v.error IS NULL OR TRIM(v.error)='')",
                "v.report_status='stored'", "v.report_graph_iri IS NOT NULL",
            ])
        if dataset_id:
            predicates.append("v.dataset_id=?")
            params.append(dataset_id)
        if profile:
            predicates.append("v.shape_model=?")
            params.append(profile)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        sql += " ORDER BY v.shape_model, v.dataset_id"
        with self.get_connection() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def mark_completed_if_validated(
        self,
        dataset_id: str,
        validator_url: str,
        profiles: list[str],
        *,
        allow_skipped: bool = True,
    ) -> bool:
        if not profiles:
            return False
        placeholders = ",".join("?" for _ in profiles)
        with self.get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT shape_model, execution_status, conforms, http_status, error, report_status, report_graph_iri
                FROM validations
                WHERE dataset_id=? AND validator_url=? AND shape_model IN ({placeholders})
                """,
                (dataset_id, validator_url, *profiles),
            ).fetchall()
            completed_profiles = {
                r["shape_model"]
                for r in rows
                if (
                    allow_skipped and r["execution_status"] == "skipped"
                ) or (
                    r["execution_status"] == "validated"
                    and r["http_status"] == 200
                    and r["conforms"] is not None
                    and r["error"] is None
                    and r["report_status"] == "stored"
                    and bool(r["report_graph_iri"])
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
