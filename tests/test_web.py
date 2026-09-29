import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from fpreporter import db
from fpreporter.web import queries
from fpreporter.web.app import create_app


def ms(year, month, day, hour=12):
    return int(datetime(year, month, day, hour).astimezone().timestamp() * 1000)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "fp.sqlite"
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO collection_runs (id, started_at, finished_at, status, window_start_ms, window_end_ms, "
        "total_true, total_false, builds_added, builds_updated) "
        "VALUES (1, '2026-09-28T15:00:00Z', '2026-09-28T15:01:00Z', 'ok', 0, ?, 3, 7, 3, 0)",
        (ms(2026, 9, 28),),
    )
    conn.execute(
        "INSERT INTO collection_runs (id, started_at, finished_at, status, window_start_ms, error) "
        "VALUES (2, '2026-09-29T15:00:00Z', '2026-09-29T15:00:05Z', 'failed', ?, 'PayloadError: boom <b>')",
        (ms(2026, 9, 28),),
    )
    builds = [
        ("team/app", 10, "SUCCESS", ms(2026, 9, 1), "https://jenkins/job/team/job/app/10/", "alice", "Alice"),
        ("team/app", 11, "FAILURE", ms(2026, 9, 8), "job/team/job/app/11/", "bob", "<script>alert(1)</script>"),
        ("Other Job", 3, "RUNNING", ms(2026, 9, 9), "https://jenkins/job/Other%20Job/3/", "SYSTEM/AUTOMATED",
         "Started by timer"),
    ]
    for job, number, result, started, url, author_id, author_name in builds:
        conn.execute(
            "INSERT INTO force_pass_builds (job_full_name, build_number, result, started_at_ms, url, param_value, "
            "author_id, author_name, cause, parameters_json, first_seen_run_id, last_updated_run_id) "
            "VALUES (?, ?, ?, ?, ?, 'true', ?, ?, 'Started by user', ?, 1, 1)",
            (job, number, result, started, url, author_id, author_name,
             json.dumps({"FORCE_PASS": "true", "ENV": "prod", "PASSWORD": "****"})),
        )
    conn.executemany(
        "INSERT INTO run_job_counts (run_id, job_full_name, true_count, false_count) VALUES (1, ?, ?, ?)",
        [("team/app", 2, 6), ("Other Job", 1, 1)],
    )
    conn.close()
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path, jenkins_url="https://jenkins/"))


@pytest.fixture
def conn(db_path):
    c = db.connect_read_only(db_path)
    yield c
    c.close()


# ---------------------------------------------------------------- pages

@pytest.mark.parametrize("path", ["/", "/?period=all", "/?period=bogus", "/builds", "/jobs", "/runs"])
def test_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "FORCE_PASS audit" in response.text


def test_ui_is_read_only_and_has_no_api_docs(client):
    assert client.get("/docs").status_code == 404
    assert client.post("/builds").status_code == 405


def test_builds_page_escapes_html(client):
    body = client.get("/builds").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_relative_jenkins_url_is_made_absolute(client):
    body = client.get("/builds").text
    assert 'href="https://jenkins/job/team/job/app/11/"' in body


def test_builds_filter_by_job(client):
    body = client.get("/builds?job=team").text
    assert "2 builds" in body
    assert "Other Job" not in body.split('id="results"')[1]


def test_htmx_request_returns_partial(client):
    response = client.get("/builds?author=alice", headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "1 build" in response.text


def test_build_detail_with_slashes_and_spaces(client):
    assert client.get("/build", params={"job": "team/app", "number": 11}).status_code == 200
    response = client.get("/build", params={"job": "Other Job", "number": 3})
    assert response.status_code == 200
    assert "Started by timer" in response.text
    assert "PASSWORD" in response.text and "****" in response.text


def test_build_detail_links_are_generated_from_list(client):
    assert 'href="/build?job=Other+Job&amp;number=3"' in client.get("/builds").text


def test_unknown_build_is_404_page(client):
    response = client.get("/build", params={"job": "team/app", "number": 999})
    assert response.status_code == 404
    assert "Not found" in response.text


def test_malformed_build_request_is_400_page(client):
    response = client.get("/build?job=team/app&number=abc")
    assert response.status_code == 400
    assert "<html" in response.text


def test_runs_page_shows_escaped_error(client):
    body = client.get("/runs").text
    assert "PayloadError: boom &lt;b&gt;" in body
    assert 'href="/builds?run=1"' in body


def test_jobs_page_ratio_and_search(client):
    body = client.get("/jobs").text
    assert "25.0%" in body  # team/app: 2 / (2 + 6)
    assert "50.0%" in body  # Other Job: 1 / (1 + 1)
    partial = client.get("/jobs?q=other", headers={"HX-Request": "true"}).text
    assert "Other Job" in partial and "team/app" not in partial


def test_multibranch_job_names_are_decoded_and_searchable(db_path, client):
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO force_pass_builds (job_full_name, build_number, result, started_at_ms, url, param_value, "
        "author_id, author_name, first_seen_run_id, last_updated_run_id) "
        "VALUES ('proj/grp%2Frepo/MR-7', 1, 'SUCCESS', 0, 'https://j/', 'true', 'a', 'A', 1, 1)"
    )
    conn.close()

    body = client.get("/builds?job=grp/repo").text
    assert "1 build" in body
    assert "proj/grp/repo/MR-7" in body                      # decoded for display
    assert 'href="/build?job=proj%2Fgrp%252Frepo%2FMR-7&amp;number=1"' in body  # raw name kept in the link
    # Follow the link exactly as a browser would.
    assert client.get("/build?job=proj%2Fgrp%252Frepo%2FMR-7&number=1").status_code == 200


def add_build_with_params(db_path, number, parameters):
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO force_pass_builds (job_full_name, build_number, result, started_at_ms, url, param_value, "
        "author_id, author_name, parameters_json, first_seen_run_id, last_updated_run_id) "
        "VALUES ('reasons/job', ?, 'SUCCESS', 0, 'https://j/', 'true', 'a', 'A', ?, 1, 1)",
        (number, json.dumps(parameters)),
    )
    conn.close()


def test_reason_column_shows_and_filters(db_path, client):
    add_build_with_params(db_path, 1, {"FORCE_PASS": "true", "FORCE_PASS_REASON": "urgent hotfix <P1>"})
    add_build_with_params(db_path, 2, {"FORCE_PASS": "true", "FORCE_PASS_REASON": "sonar flake"})

    body = client.get("/builds").text
    assert "<th>Reason</th>" in body
    assert "urgent hotfix &lt;P1&gt;" in body
    assert "none given" in body  # fixture builds have no reason

    filtered = client.get("/builds?reason=hotfix", headers={"HX-Request": "true"}).text
    assert "1 build" in filtered and "urgent hotfix" in filtered and "sonar flake" not in filtered

    detail = client.get("/build", params={"job": "reasons/job", "number": 1}).text
    assert "urgent hotfix &lt;P1&gt;" in detail


def test_reason_parameter_is_configurable(db_path):
    add_build_with_params(db_path, 1, {"FORCE_PASS": "true", "WHY.NOT": "custom reason"})
    client = TestClient(create_app(db_path, reason_parameter="WHY.NOT"))
    assert "custom reason" in client.get("/builds?job=reasons").text


@pytest.mark.parametrize(
    "job, repository",
    [
        ("proj/grp%2Frepo/MR-26630", "proj/grp%2Frepo"),
        ("proj/repo/PR-7", "proj/repo"),
        ("proj/repo/main", "proj/repo/main"),        # branch jobs stay on their own
        ("proj/repo/MR-7-hotfix", "proj/repo/MR-7-hotfix"),
        ("MR-5", "MR-5"),                             # no parent to group under
        ("team/app", "team/app"),
    ],
)
def test_repository_of(job, repository):
    assert queries.repository_of(job) == repository


def add_mr_jobs(db_path):
    conn = db.connect(db_path)
    for job, number, true_count, false_count in [("p/grp%2Frepo/MR-1", 1, 1, 3), ("p/grp%2Frepo/MR-2", 1, 2, 0)]:
        conn.execute(
            "INSERT INTO force_pass_builds (job_full_name, build_number, result, started_at_ms, url, param_value, "
            "author_id, author_name, first_seen_run_id, last_updated_run_id) "
            "VALUES (?, ?, 'SUCCESS', ?, 'https://j/', 'true', 'a', 'A', 1, 1)",
            (job, number, ms(2026, 9, 20)),
        )
        conn.execute(
            "INSERT INTO run_job_counts (run_id, job_full_name, true_count, false_count) VALUES (1, ?, ?, ?)",
            (job, true_count, false_count),
        )
    conn.close()


def test_jobs_grouped_by_repository(db_path, conn):
    add_mr_jobs(db_path)
    rows = {r["name"]: r for r in queries.job_stats(conn)}

    assert set(rows) == {"p/grp%2Frepo", "team/app", "Other Job"}
    repo = rows["p/grp%2Frepo"]
    assert (repo["jobs"], repo["true_count"], repo["false_count"]) == (2, 3, 3)
    assert repo["ratio"] == pytest.approx(0.5)

    ungrouped = {r["name"] for r in queries.job_stats(conn, by_repository=False)}
    assert {"p/grp%2Frepo/MR-1", "p/grp%2Frepo/MR-2"} <= ungrouped


def test_top_repositories(db_path, conn):
    add_mr_jobs(db_path)
    top = queries.top_repositories(conn, since_ms=0)
    assert top[0] == {"repository": "p/grp%2Frepo", "n": 2, "jobs": 2}


def test_repository_pages(db_path, client):
    add_mr_jobs(db_path)

    jobs = client.get("/jobs").text
    assert "p/grp/repo" in jobs and "p/grp/repo/MR-1" not in jobs
    assert 'href="/builds?repo=p%2Fgrp%252Frepo"' in jobs

    by_job = client.get("/jobs?group=job").text
    assert "p/grp/repo/MR-1" in by_job

    builds = client.get("/builds?repo=p%2Fgrp%252Frepo").text
    assert "2 builds" in builds
    assert "Repository p/grp/repo" in builds

    assert "Top repositories" in client.get("/?period=all").text


def test_missing_database_shows_error_page(tmp_path):
    client = TestClient(create_app(tmp_path / "missing.sqlite"))
    response = client.get("/")
    assert response.status_code == 503
    assert "Database unavailable" in response.text


def test_static_css_served(client):
    assert client.get("/static/style.css").status_code == 200


# ---------------------------------------------------------------- queries

def test_build_filter_round_trip():
    f = queries.BuildFilter.from_query({"job": " app ", "from": "2026-09-01", "sort": "job", "dir": "asc", "page": "2"})
    assert f.job == "app"
    assert f.date_from == date(2026, 9, 1)
    assert f.to_query() == {"job": "app", "from": "2026-09-01", "sort": "job", "dir": "asc", "page": "2"}
    assert f.to_query(page=1, sort="started") == {"job": "app", "from": "2026-09-01", "dir": "asc"}


def test_build_filter_ignores_invalid_input():
    f = queries.BuildFilter.from_query({"sort": "1; DROP TABLE x", "page": "-3", "from": "yesterday", "run": "x"})
    assert (f.sort, f.page, f.date_from, f.run) == ("started", 1, None, None)


def test_search_builds_date_range_is_inclusive(conn):
    f = queries.BuildFilter(date_from=date(2026, 9, 8), date_to=date(2026, 9, 9))
    page = queries.search_builds(conn, f)
    assert [r["build_number"] for r in page.rows] == [3, 11]


def test_search_builds_like_wildcards_are_literal(conn):
    assert queries.search_builds(conn, queries.BuildFilter(job="%")).total == 0


def test_search_builds_sorting_and_run_filter(conn):
    page = queries.search_builds(conn, queries.BuildFilter(sort="number", desc=False, run=1))
    assert [r["build_number"] for r in page.rows] == [3, 10, 11]


def test_page_beyond_last_is_clamped(conn):
    page = queries.search_builds(conn, queries.BuildFilter(page=99))
    assert page.page == 1 and page.pages == 1 and len(page.rows) == 3


def test_weekly_force_passes_buckets_by_monday(conn):
    weeks = queries.weekly_force_passes(conn, weeks=3, today=date(2026, 9, 10))  # a Thursday
    assert [(w["week"], w["count"]) for w in weeks] == [
        (date(2026, 8, 24), 0),
        (date(2026, 8, 31), 1),  # Tue 1 Sep
        (date(2026, 9, 7), 2),   # Tue 8 Sep, Wed 9 Sep
    ]


def test_top_authors_groups_automated(conn):
    authors = queries.top_authors(conn, since_ms=0)
    assert {a["author_name"] for a in authors} == {"Alice", "<script>alert(1)</script>", "Automated triggers"}


def test_kpis(conn):
    k = queries.kpis(conn)
    assert (k["all_time"], k["true_total"], k["false_total"], k["running"]) == (3, 3, 7, 1)
    assert k["ratio"] == pytest.approx(0.3)
