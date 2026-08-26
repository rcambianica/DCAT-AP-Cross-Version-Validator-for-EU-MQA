import sqlite3
from pathlib import Path


DATASET_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    catalogue_id TEXT NOT NULL,
    source_url TEXT,
    rdf_path TEXT,
    sha256 TEXT,
    discovery_status TEXT NOT NULL DEFAULT 'discovered',
    fetch_status TEXT,
    fetch_http_status INTEGER,
    fetch_error TEXT,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    fetched_at TEXT
);
"""


VALIDATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS validations (
    dataset_id TEXT NOT NULL,
    shape_model TEXT NOT NULL,
    validator_url TEXT NOT NULL,
    conforms INTEGER,
    results INTEGER NOT NULL DEFAULT 0,
    violations INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    infos INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    error TEXT,
    validated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_id, shape_model, validator_url),
    FOREIGN KEY (dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_validations_shape
ON validations(validator_url, shape_model, conforms);
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_validations_v1(conn: sqlite3.Connection) -> None:
    """Migra il DB MVP precedente aggiungendo la provenienza del validatore."""
    if not _table_exists(conn, "validations"):
        conn.executescript(VALIDATIONS_SCHEMA)
        return

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(validations)").fetchall()
    }
    if "validator_url" in columns:
        conn.executescript(VALIDATIONS_SCHEMA)
        return

    conn.execute("ALTER TABLE validations RENAME TO validations_v1")
    conn.executescript(VALIDATIONS_SCHEMA)
    conn.execute(
        """
        INSERT INTO validations(
            dataset_id, shape_model, validator_url, conforms, results,
            violations, warnings, infos, http_status, error, validated_at
        )
        SELECT
            dataset_id, shape_model, 'legacy://unspecified', conforms, results,
            violations, warnings, infos, http_status, error, validated_at
        FROM validations_v1
        """
    )
    conn.execute("DROP TABLE validations_v1")
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DATASET_SCHEMA)
    _migrate_validations_v1(conn)
    return conn
