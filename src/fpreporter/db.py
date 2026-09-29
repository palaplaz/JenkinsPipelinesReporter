"""SQLite schema, migrations and connection handling.

Schema versions are tracked with PRAGMA user_version. To change the schema,
append a new script to MIGRATIONS; never edit one that has already shipped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS: list[str] = [
    # v1: initial schema
    """
    CREATE TABLE collection_runs (
        id                    INTEGER PRIMARY KEY,
        started_at            TEXT    NOT NULL,           -- ISO-8601 UTC, local clock
        finished_at           TEXT,
        status                TEXT    NOT NULL CHECK (status IN ('running', 'ok', 'failed')),
        window_start_ms       INTEGER NOT NULL,           -- inclusive, Jenkins epoch millis
        window_end_ms         INTEGER,                    -- exclusive; reported by the script
        total_true            INTEGER,
        total_false           INTEGER,
        builds_added          INTEGER,
        builds_updated        INTEGER,
        script_schema_version INTEGER,
        error                 TEXT
    );

    CREATE TABLE run_job_counts (
        run_id        INTEGER NOT NULL REFERENCES collection_runs(id),
        job_full_name TEXT    NOT NULL,
        true_count    INTEGER NOT NULL,
        false_count   INTEGER NOT NULL,
        PRIMARY KEY (run_id, job_full_name)
    );

    CREATE TABLE force_pass_builds (
        job_full_name       TEXT    NOT NULL,
        build_number        INTEGER NOT NULL,
        result              TEXT    NOT NULL,             -- 'RUNNING' until the build finishes
        started_at_ms       INTEGER NOT NULL,
        url                 TEXT    NOT NULL,
        param_value         TEXT    NOT NULL,
        author_id           TEXT    NOT NULL,
        author_name         TEXT    NOT NULL,
        cause               TEXT,
        parameters_json     TEXT    NOT NULL DEFAULT '{}',
        first_seen_run_id   INTEGER NOT NULL REFERENCES collection_runs(id),
        last_updated_run_id INTEGER NOT NULL REFERENCES collection_runs(id),
        PRIMARY KEY (job_full_name, build_number)
    );

    CREATE INDEX ix_fpb_started_at ON force_pass_builds (started_at_ms);
    CREATE INDEX ix_fpb_author     ON force_pass_builds (author_id);
    CREATE INDEX ix_fpb_first_seen ON force_pass_builds (first_seen_run_id);
    CREATE INDEX ix_fpb_running    ON force_pass_builds (job_full_name, build_number)
        WHERE result = 'RUNNING';
    CREATE INDEX ix_rjc_job        ON run_job_counts (job_full_name);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)
RUNNING = "RUNNING"


class SchemaVersionError(Exception):
    """Raised when the database file was created by a newer version of the tool."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Opens a read-write connection and brings the schema up to date.

    Used by the collector. Enables WAL so the web UI can read while a run writes.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
    _configure(conn)
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def connect_read_only(db_path: str | Path) -> sqlite3.Connection:
    """Opens a read-only connection for the web UI. Never creates or migrates."""
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Database not found: {path} (run 'fpreporter collect' or 'init-db' first)")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    _configure(conn)
    version = _user_version(conn)
    if version != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(f"Database schema is v{version}, this tool expects v{SCHEMA_VERSION}")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Applies pending migrations, each in its own transaction. Returns the resulting version."""
    current = _user_version(conn)
    if current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database schema is v{current}, newer than this tool's v{SCHEMA_VERSION}"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS[version - 1]
        # executescript() commits implicitly, so wrap BEGIN/COMMIT inside the script itself.
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    return SCHEMA_VERSION


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def get_watermark(conn: sqlite3.Connection) -> int:
    """Start of the next collection window: end of the last successful run, or 0 (full history)."""
    row = conn.execute(
        "SELECT MAX(window_end_ms) FROM collection_runs WHERE status = 'ok'"
    ).fetchone()
    return row[0] or 0


def get_running_builds(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Builds stored as RUNNING, to be re-checked on the next run."""
    return [
        (row["job_full_name"], row["build_number"])
        for row in conn.execute(
            "SELECT job_full_name, build_number FROM force_pass_builds "
            "WHERE result = ? ORDER BY job_full_name, build_number",
            (RUNNING,),
        )
    ]


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")


def _user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]
