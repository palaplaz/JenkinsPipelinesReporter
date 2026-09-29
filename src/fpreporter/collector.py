"""One collection run: execute the Groovy script, validate its output, persist it."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from fpreporter import db, script
from fpreporter.config import CollectorSettings

log = logging.getLogger(__name__)

_ERROR_BODY_TAIL = 4000


class PayloadError(Exception):
    """The script output is missing, malformed, or inconsistent with the request."""


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    status: str
    window_start_ms: int
    window_end_ms: int | None = None
    total_true: int = 0
    total_false: int = 0
    builds_added: int = 0
    builds_updated: int = 0
    scanned_jobs: int = 0
    scanned_builds: int = 0
    error: str | None = None


def collect(
    conn: sqlite3.Connection,
    settings: CollectorSettings,
    run_script: Callable[[str], str],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> RunSummary:
    """Runs one collection. Failures are recorded in collection_runs and returned, not raised."""
    _fail_interrupted_runs(conn, now)

    since = db.get_watermark(conn)
    recheck = db.get_running_builds(conn)
    run_id = conn.execute(
        "INSERT INTO collection_runs (started_at, status, window_start_ms) VALUES (?, 'running', ?)",
        (_iso(now()), since),
    ).lastrowid
    log.info("Run %d: window starts at %d, re-checking %d running build(s)", run_id, since, len(recheck))

    try:
        rendered = script.render(
            settings.parameter_name,
            since_ms=since,
            lag_ms=settings.lag_minutes * 60_000,
            recheck=recheck,
        )
        body = run_script(rendered)
        payload = parse_payload(body, since_ms=since, parameter_name=settings.parameter_name)
        with db.transaction(conn):
            summary = _persist(conn, run_id, payload, now)
    except Exception as e:
        log.exception("Run %d failed", run_id)
        error = f"{type(e).__name__}: {e}"
        conn.execute(
            "UPDATE collection_runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
            (_iso(now()), error, run_id),
        )
        return RunSummary(run_id=run_id, status="failed", window_start_ms=since, error=error)

    log.info(
        "Run %d ok: window [%d, %d), %d true / %d false, %d added, %d updated",
        run_id, summary.window_start_ms, summary.window_end_ms,
        summary.total_true, summary.total_false, summary.builds_added, summary.builds_updated,
    )
    return summary


def parse_payload(body: str, since_ms: int, parameter_name: str) -> dict:
    """Extracts the JSON between the markers and checks it against what was requested."""
    begin = body.find(script.BEGIN_MARKER)
    end = body.find(script.END_MARKER, begin + 1)
    if begin < 0 or end < 0:
        raise PayloadError(f"Output markers not found. Jenkins output (tail):\n{body[-_ERROR_BODY_TAIL:]}")

    try:
        payload = json.loads(body[begin + len(script.BEGIN_MARKER):end])
    except json.JSONDecodeError as e:
        raise PayloadError(f"Invalid JSON between markers: {e}") from None

    _check(isinstance(payload, dict), "payload is not an object")
    _check(
        payload.get("schemaVersion") == script.SCRIPT_SCHEMA_VERSION,
        f"schemaVersion {payload.get('schemaVersion')!r}, expected {script.SCRIPT_SCHEMA_VERSION}",
    )
    _check(payload.get("parameterName") == parameter_name, f"parameterName {payload.get('parameterName')!r}")
    _check(payload.get("windowStart") == since_ms, f"windowStart {payload.get('windowStart')!r}, expected {since_ms}")
    window_end = payload.get("windowEnd")
    _check(_is_int(window_end) and window_end >= since_ms, f"windowEnd {window_end!r}")
    for key in ("scannedJobs", "scannedBuilds"):
        _check(_is_int(payload.get(key)), f"{key} is not an integer")

    for c in _list(payload, "counts"):
        _check(isinstance(c.get("job"), str), f"count entry without job: {c!r}")
        _check(_is_int(c.get("trueCount")) and _is_int(c.get("falseCount")), f"bad count entry: {c!r}")

    for b in _list(payload, "builds"):
        for key in ("job", "result", "url", "value", "authorId", "authorName"):
            _check(isinstance(b.get(key), str), f"build {key} missing or not a string: {b!r}")
        _check(_is_int(b.get("number")), f"build number is not an integer: {b!r}")
        _check(b.get("cause") is None or isinstance(b.get("cause"), str), f"build cause is not a string: {b!r}")
        _check(isinstance(b.get("parameters"), dict), f"build parameters is not an object: {b!r}")
        start = b.get("startMs")
        _check(_is_int(start) and since_ms <= start < window_end, f"build startMs outside window: {b!r}")

    for r in _list(payload, "rechecked"):
        _check(
            isinstance(r.get("job"), str) and _is_int(r.get("number")) and isinstance(r.get("result"), str),
            f"bad rechecked entry: {r!r}",
        )

    return payload


def _persist(conn: sqlite3.Connection, run_id: int, payload: dict, now: Callable[[], datetime]) -> RunSummary:
    added = updated = 0

    for b in payload["builds"]:
        exists = conn.execute(
            "SELECT 1 FROM force_pass_builds WHERE job_full_name = ? AND build_number = ?",
            (b["job"], b["number"]),
        ).fetchone()
        # Windows don't overlap, so a conflict should not happen; upsert anyway to stay idempotent.
        conn.execute(
            """
            INSERT INTO force_pass_builds (
                job_full_name, build_number, result, started_at_ms, url, param_value,
                author_id, author_name, cause, parameters_json, first_seen_run_id, last_updated_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (job_full_name, build_number) DO UPDATE SET
                result = excluded.result, started_at_ms = excluded.started_at_ms, url = excluded.url,
                param_value = excluded.param_value, author_id = excluded.author_id,
                author_name = excluded.author_name, cause = excluded.cause,
                parameters_json = excluded.parameters_json, last_updated_run_id = excluded.last_updated_run_id
            """,
            (
                b["job"], b["number"], b["result"], b["startMs"], b["url"], b["value"],
                b["authorId"], b["authorName"], b.get("cause"),
                json.dumps(b["parameters"], ensure_ascii=False), run_id, run_id,
            ),
        )
        if exists:
            updated += 1
        else:
            added += 1

    for r in payload["rechecked"]:
        if r["result"] == db.RUNNING:
            continue
        cursor = conn.execute(
            "UPDATE force_pass_builds SET result = ?, last_updated_run_id = ? "
            "WHERE job_full_name = ? AND build_number = ? AND result = ?",
            (r["result"], run_id, r["job"], r["number"], db.RUNNING),
        )
        updated += cursor.rowcount

    total_true = total_false = 0
    for c in payload["counts"]:
        conn.execute(
            "INSERT INTO run_job_counts (run_id, job_full_name, true_count, false_count) VALUES (?, ?, ?, ?)",
            (run_id, c["job"], c["trueCount"], c["falseCount"]),
        )
        total_true += c["trueCount"]
        total_false += c["falseCount"]

    conn.execute(
        """
        UPDATE collection_runs SET
            status = 'ok', finished_at = ?, window_end_ms = ?, total_true = ?, total_false = ?,
            builds_added = ?, builds_updated = ?, script_schema_version = ?
        WHERE id = ?
        """,
        (
            _iso(now()), payload["windowEnd"], total_true, total_false,
            added, updated, payload["schemaVersion"], run_id,
        ),
    )
    return RunSummary(
        run_id=run_id,
        status="ok",
        window_start_ms=payload["windowStart"],
        window_end_ms=payload["windowEnd"],
        total_true=total_true,
        total_false=total_false,
        builds_added=added,
        builds_updated=updated,
        scanned_jobs=payload["scannedJobs"],
        scanned_builds=payload["scannedBuilds"],
    )


def _fail_interrupted_runs(conn: sqlite3.Connection, now: Callable[[], datetime]) -> None:
    """A run left in 'running' means the process died mid-run (e.g. the PC shut down)."""
    cursor = conn.execute(
        "UPDATE collection_runs SET status = 'failed', finished_at = ?, "
        "error = 'Interrupted: process ended before the run completed' WHERE status = 'running'",
        (_iso(now()),),
    )
    if cursor.rowcount:
        log.warning("Marked %d interrupted run(s) as failed", cursor.rowcount)


def _list(payload: dict, key: str) -> list[dict]:
    value = payload.get(key)
    _check(isinstance(value, list) and all(isinstance(v, dict) for v in value), f"{key} is not a list of objects")
    return value


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise PayloadError(f"Invalid script output: {message}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
