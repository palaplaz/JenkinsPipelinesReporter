import sqlite3

import pytest

from fpreporter import db


def add_run(conn, status, window_start, window_end):
    return conn.execute(
        "INSERT INTO collection_runs (started_at, status, window_start_ms, window_end_ms) "
        "VALUES ('2026-09-29T15:00:00Z', ?, ?, ?)",
        (status, window_start, window_end),
    ).lastrowid


def add_build(conn, run_id, job, number, result):
    conn.execute(
        "INSERT INTO force_pass_builds (job_full_name, build_number, result, started_at_ms, url, "
        "param_value, author_id, author_name, first_seen_run_id, last_updated_run_id) "
        "VALUES (?, ?, ?, 1000, 'http://j/1', 'true', 'alice', 'Alice', ?, ?)",
        (job, number, result, run_id, run_id),
    )


def test_connect_creates_schema_and_enables_wal(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"collection_runs", "run_job_counts", "force_pass_builds"} <= tables


def test_migrate_is_idempotent(conn):
    assert db.migrate(conn) == db.SCHEMA_VERSION


def test_newer_schema_is_rejected(conn):
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(db.SchemaVersionError):
        db.migrate(conn)


def test_watermark_is_zero_without_successful_runs(conn):
    assert db.get_watermark(conn) == 0
    add_run(conn, "failed", 0, None)
    assert db.get_watermark(conn) == 0


def test_watermark_ignores_failed_runs(conn):
    add_run(conn, "ok", 0, 1_000)
    add_run(conn, "ok", 1_000, 2_000)
    add_run(conn, "failed", 2_000, 3_000)
    assert db.get_watermark(conn) == 2_000


def test_running_builds(conn):
    run = add_run(conn, "ok", 0, 1_000)
    add_build(conn, run, "team/app", 7, "RUNNING")
    add_build(conn, run, "team/app", 6, "SUCCESS")
    add_build(conn, run, "other", 1, "RUNNING")
    assert db.get_running_builds(conn) == [("other", 1), ("team/app", 7)]


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_build(conn, 999, "job", 1, "SUCCESS")


def test_status_check_constraint(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_run(conn, "bogus", 0, 1)


def test_read_only_connection_cannot_write(tmp_path):
    path = tmp_path / "fp.sqlite"
    db.connect(path).close()

    ro = db.connect_read_only(path)
    try:
        assert db.get_watermark(ro) == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            add_run(ro, "ok", 0, 1)
    finally:
        ro.close()


def test_read_only_requires_existing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        db.connect_read_only(tmp_path / "missing.sqlite")


def test_read_only_rejects_outdated_schema(tmp_path):
    path = tmp_path / "old.sqlite"
    sqlite3.connect(path).close()  # empty file, user_version = 0
    with pytest.raises(db.SchemaVersionError):
        db.connect_read_only(path)
