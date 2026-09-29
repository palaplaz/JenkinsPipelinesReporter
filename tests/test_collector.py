import json
from datetime import datetime, timezone

import pytest

from fpreporter import db
from fpreporter.collector import PayloadError, collect, parse_payload
from fpreporter.config import CollectorSettings
from helpers import script_input, script_output

SETTINGS = CollectorSettings(parameter_name="FORCE_PASS", lag_minutes=5)
FIXED_NOW = lambda: datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)  # noqa: E731


def build(job="team/app", number=1, start=500, result="SUCCESS", **overrides):
    entry = {
        "job": job, "number": number, "result": result, "startMs": start,
        "url": f"https://jenkins/job/{job}/{number}/", "value": "true",
        "authorId": "alice", "authorName": "Alice", "cause": "Started by user Alice",
        "parameters": {"FORCE_PASS": "true", "ENV": "prod", "SECRET": "****"},
    }
    entry.update(overrides)
    return entry


def payload(since=0, end=1_000, builds=(), counts=(), rechecked=()):
    return {
        "schemaVersion": 1, "jenkinsVersion": "2.500", "parameterName": "FORCE_PASS",
        "generatedAt": end + 300_000, "windowStart": since, "windowEnd": end,
        "scannedJobs": 3, "scannedBuilds": 10,
        "counts": list(counts), "builds": list(builds), "rechecked": list(rechecked),
    }


class FakeJenkins:
    """Answers each run_script call with the next canned output and records the inputs."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.inputs = []

    def run_script(self, rendered):
        self.inputs.append(script_input(rendered))
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def run(conn, fake):
    return collect(conn, SETTINGS, fake.run_script, now=FIXED_NOW)


def test_first_run_scans_full_history_and_stores_everything(conn):
    fake = FakeJenkins(script_output(payload(
        since=0, end=1_000,
        builds=[build(number=1), build(number=2, result="RUNNING")],
        counts=[{"job": "team/app", "trueCount": 2, "falseCount": 5},
                {"job": "other", "trueCount": 0, "falseCount": 1}],
    ), noise="some println from Jenkins\n"))

    summary = run(conn, fake)

    assert fake.inputs == [{"parameterName": "FORCE_PASS", "sinceMs": 0, "lagMs": 300_000, "recheck": []}]
    assert summary.status == "ok"
    assert (summary.window_start_ms, summary.window_end_ms) == (0, 1_000)
    assert (summary.total_true, summary.total_false) == (2, 6)
    assert (summary.builds_added, summary.builds_updated) == (2, 0)

    run_row = conn.execute("SELECT * FROM collection_runs").fetchone()
    assert run_row["status"] == "ok"
    assert run_row["started_at"] == "2026-09-29T15:00:00Z"
    assert (run_row["total_true"], run_row["total_false"], run_row["window_end_ms"]) == (2, 6, 1_000)

    stored = conn.execute("SELECT * FROM force_pass_builds WHERE build_number = 1").fetchone()
    assert stored["job_full_name"] == "team/app"
    assert stored["author_name"] == "Alice"
    assert json.loads(stored["parameters_json"]) == {"FORCE_PASS": "true", "ENV": "prod", "SECRET": "****"}
    assert stored["first_seen_run_id"] == stored["last_updated_run_id"] == summary.run_id

    counts = conn.execute("SELECT job_full_name, true_count, false_count FROM run_job_counts ORDER BY 1").fetchall()
    assert [tuple(r) for r in counts] == [("other", 0, 1), ("team/app", 2, 5)]
    assert db.get_watermark(conn) == 1_000


def test_next_run_continues_from_watermark_and_rechecks_running_builds(conn):
    fake = FakeJenkins(
        script_output(payload(since=0, end=1_000, builds=[build(number=2, result="RUNNING")])),
        script_output(payload(
            since=1_000, end=2_000,
            builds=[build(number=3, start=1_500)],
            rechecked=[{"job": "team/app", "number": 2, "result": "FAILURE"}],
        )),
    )
    first = run(conn, fake)
    second = run(conn, fake)

    assert fake.inputs[1]["sinceMs"] == 1_000
    assert fake.inputs[1]["recheck"] == [{"job": "team/app", "number": 2}]
    assert (second.builds_added, second.builds_updated) == (1, 1)

    rechecked = conn.execute("SELECT * FROM force_pass_builds WHERE build_number = 2").fetchone()
    assert rechecked["result"] == "FAILURE"
    assert rechecked["first_seen_run_id"] == first.run_id
    assert rechecked["last_updated_run_id"] == second.run_id
    assert db.get_running_builds(conn) == []


def test_still_running_build_is_left_for_next_run(conn):
    fake = FakeJenkins(
        script_output(payload(end=1_000, builds=[build(number=2, result="RUNNING")])),
        script_output(payload(since=1_000, end=2_000,
                              rechecked=[{"job": "team/app", "number": 2, "result": "RUNNING"}])),
    )
    run(conn, fake)
    summary = run(conn, fake)

    assert summary.builds_updated == 0
    assert db.get_running_builds(conn) == [("team/app", 2)]


def test_deleted_running_build_stops_being_rechecked(conn):
    fake = FakeJenkins(
        script_output(payload(end=1_000, builds=[build(number=2, result="RUNNING")])),
        script_output(payload(since=1_000, end=2_000,
                              rechecked=[{"job": "team/app", "number": 2, "result": "NOT_FOUND"}])),
    )
    run(conn, fake)
    run(conn, fake)
    assert db.get_running_builds(conn) == []


@pytest.mark.parametrize(
    "output, error_fragment",
    [
        ("groovy.lang.MissingMethodException: No signature of method\n\tat Script1.run", "MissingMethodException"),
        (script_output(payload(since=999)), "windowStart"),
        (script_output({**payload(), "schemaVersion": 2}), "schemaVersion"),
        (script_output(payload(builds=[build(start=5_000)])), "outside window"),
        (script_output(payload(builds=[build(number="1")])), "number"),
    ],
)
def test_bad_output_fails_run_without_advancing_watermark(conn, output, error_fragment):
    summary = run(conn, FakeJenkins(output))

    assert summary.status == "failed"
    assert error_fragment in summary.error
    row = conn.execute("SELECT status, error, finished_at FROM collection_runs").fetchone()
    assert row["status"] == "failed"
    assert error_fragment in row["error"]
    assert row["finished_at"] is not None
    assert db.get_watermark(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM force_pass_builds").fetchone()[0] == 0


def test_jenkins_error_is_recorded(conn):
    summary = run(conn, FakeJenkins(RuntimeError("connection refused")))
    assert summary.status == "failed"
    assert "connection refused" in summary.error


def test_failure_during_persist_rolls_back_everything(conn):
    # A duplicate job in counts violates run_job_counts' primary key after the build was already inserted.
    output = script_output(payload(
        builds=[build(number=1)],
        counts=[{"job": "team/app", "trueCount": 1, "falseCount": 0},
                {"job": "team/app", "trueCount": 1, "falseCount": 0}],
    ))
    summary = run(conn, FakeJenkins(output))

    assert summary.status == "failed"
    assert conn.execute("SELECT COUNT(*) FROM force_pass_builds").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM run_job_counts").fetchone()[0] == 0


def test_failed_run_is_retried_from_same_watermark(conn):
    fake = FakeJenkins(
        script_output(payload(end=1_000)),
        "boom",
        script_output(payload(since=1_000, end=3_000)),
    )
    run(conn, fake)
    run(conn, fake)
    run(conn, fake)
    assert [i["sinceMs"] for i in fake.inputs] == [0, 1_000, 1_000]
    assert db.get_watermark(conn) == 3_000


def test_interrupted_run_is_marked_failed(conn):
    conn.execute(
        "INSERT INTO collection_runs (started_at, status, window_start_ms) VALUES ('2026-09-28T15:00:00Z', 'running', 0)"
    )
    run(conn, FakeJenkins(script_output(payload())))

    statuses = [r[0] for r in conn.execute("SELECT status FROM collection_runs ORDER BY id")]
    assert statuses == ["failed", "ok"]


def test_parse_payload_tolerates_pretty_printed_json():
    body = script_output(payload()).replace('{"schemaVersion"', '{\n    "schemaVersion"')
    assert parse_payload(body, since_ms=0, parameter_name="FORCE_PASS")["windowEnd"] == 1_000


def test_parse_payload_rejects_other_parameter():
    with pytest.raises(PayloadError, match="parameterName"):
        parse_payload(script_output(payload()), since_ms=0, parameter_name="OTHER")
