"""All SQL used by the web UI. Every function takes a (read-only) connection and returns plain data."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

PAGE_SIZE = 50

BUILD_SORTS = {
    "started": "started_at_ms",
    "job": "job_full_name",
    "number": "build_number",
    "result": "result",
    "author": "author_name",
}
JOB_SORTS = {
    "job": "name",
    "true": "true_count",
    "false": "false_count",
    "ratio": "ratio",
    "last": "last_force_pass_ms",
}
PERIODS = {"30d": 30, "90d": 90, "1y": 365, "all": None}
DEFAULT_PERIOD = "90d"


# ---------------------------------------------------------------- shared helpers

def local_midnight_ms(d: date) -> int:
    """Epoch millis of 00:00 local time on the given day."""
    return int(datetime(d.year, d.month, d.day).astimezone().timestamp() * 1000)


def days_ago_ms(days: int) -> int:
    return int((time.time() - days * 86_400) * 1000)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _name_match(column: str) -> str:
    # Multibranch item names are URL-encoded ("group%2Frepo"); match searches against the decoded form too.
    return f"({column} LIKE ? ESCAPE '\\' OR REPLACE({column}, '%2F', '/') LIKE ? ESCAPE '\\')"


_JOB_MATCH = _name_match("job_full_name")


def _job_match_params(text: str) -> list[str]:
    pattern = f"%{_escape_like(text)}%"
    return [pattern, pattern]


# Merge/pull request jobs of a multibranch project ("<project>/MR-123") are grouped under the project.
_CHANGE_REQUEST_JOB = re.compile(r"(?:MR|PR)-\d+")


def repository_of(job_full_name: str) -> str:
    """The job's repository: its parent for MR-/PR- jobs, otherwise the job itself."""
    parent, _, leaf = job_full_name.rpartition("/")
    return parent if parent and _CHANGE_REQUEST_JOB.fullmatch(leaf) else job_full_name


def _register_functions(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.create_function("repository_of", 1, repository_of, deterministic=True)
    return conn


DEFAULT_REASON_PARAMETER = "FORCE_PASS_REASON"
_REASON = "json_extract(parameters_json, ?)"


def _json_path(parameter_name: str) -> str:
    """JSON path selecting a top-level key, quoted so names with dots or spaces work."""
    return '$."' + parameter_name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_int(value: str | None, default: int | None = None, minimum: int | None = None) -> int | None:
    try:
        parsed = int(value) if value not in (None, "") else default
    except ValueError:
        return default
    if parsed is not None and minimum is not None and parsed < minimum:
        return default
    return parsed


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------- runs

def last_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def list_runs(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM collection_runs ORDER BY id DESC")]


# ---------------------------------------------------------------- builds

@dataclass(frozen=True)
class BuildFilter:
    job: str = ""
    repo: str = ""
    author: str = ""
    reason: str = ""
    result: str = ""
    date_from: date | None = None
    date_to: date | None = None
    run: int | None = None
    sort: str = "started"
    desc: bool = True
    page: int = 1

    @classmethod
    def from_query(cls, params) -> BuildFilter:
        sort = params.get("sort", "started")
        return cls(
            job=params.get("job", "").strip(),
            repo=params.get("repo", ""),
            author=params.get("author", "").strip(),
            reason=params.get("reason", "").strip(),
            result=params.get("result", "").strip(),
            date_from=_parse_date(params.get("from")),
            date_to=_parse_date(params.get("to")),
            run=_parse_int(params.get("run")),
            sort=sort if sort in BUILD_SORTS else "started",
            desc=params.get("dir", "desc") != "asc",
            page=_parse_int(params.get("page"), default=1, minimum=1),
        )

    def to_query(self, **changes) -> dict[str, str]:
        """Query parameters for this filter (with changes applied), omitting defaults."""
        f = replace(self, **changes)
        params = {
            "job": f.job,
            "repo": f.repo,
            "author": f.author,
            "reason": f.reason,
            "result": f.result,
            "from": f.date_from.isoformat() if f.date_from else "",
            "to": f.date_to.isoformat() if f.date_to else "",
            "run": str(f.run) if f.run is not None else "",
            "sort": f.sort if f.sort != "started" else "",
            "dir": "asc" if not f.desc else "",
            "page": str(f.page) if f.page > 1 else "",
        }
        return {k: v for k, v in params.items() if v}


@dataclass(frozen=True)
class BuildPage:
    rows: list[dict]
    total: int
    page: int
    pages: int = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "pages", max(1, math.ceil(self.total / PAGE_SIZE)))


def search_builds(
    conn: sqlite3.Connection, f: BuildFilter, reason_parameter: str = DEFAULT_REASON_PARAMETER
) -> BuildPage:
    _register_functions(conn)
    reason_path = _json_path(reason_parameter)
    where, params = [], []
    if f.job:
        where.append(_JOB_MATCH)
        params += _job_match_params(f.job)
    if f.repo:
        where.append("repository_of(job_full_name) = ?")
        params.append(f.repo)
    if f.author:
        where.append("(author_id LIKE ? ESCAPE '\\' OR author_name LIKE ? ESCAPE '\\')")
        params += [f"%{_escape_like(f.author)}%"] * 2
    if f.reason:
        where.append(f"{_REASON} LIKE ? ESCAPE '\\'")
        params += [reason_path, f"%{_escape_like(f.reason)}%"]
    if f.result:
        where.append("result = ?")
        params.append(f.result)
    if f.date_from:
        where.append("started_at_ms >= ?")
        params.append(local_midnight_ms(f.date_from))
    if f.date_to:
        where.append("started_at_ms < ?")
        params.append(local_midnight_ms(f.date_to + timedelta(days=1)))
    if f.run is not None:
        where.append("first_seen_run_id = ?")
        params.append(f.run)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM force_pass_builds {where_sql}", params).fetchone()[0]
    page = min(f.page, max(1, math.ceil(total / PAGE_SIZE)))
    direction = "DESC" if f.desc else "ASC"
    # Sort column comes from a whitelist; tie-break on the primary key for a stable order.
    rows = conn.execute(
        f"""
        SELECT job_full_name, build_number, result, started_at_ms, url, author_id, author_name, cause,
               {_REASON} AS reason
        FROM force_pass_builds {where_sql}
        ORDER BY {BUILD_SORTS[f.sort]} {direction}, job_full_name {direction}, build_number {direction}
        LIMIT ? OFFSET ?
        """,
        [reason_path, *params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
    ).fetchall()
    return BuildPage(rows=[dict(r) for r in rows], total=total, page=page)


def distinct_results(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT result FROM force_pass_builds ORDER BY result")]


def get_build(conn: sqlite3.Connection, job: str, number: int) -> dict | None:
    row = conn.execute(
        """
        SELECT b.*, f.started_at AS first_seen_at, u.started_at AS last_updated_at
        FROM force_pass_builds b
        JOIN collection_runs f ON f.id = b.first_seen_run_id
        JOIN collection_runs u ON u.id = b.last_updated_run_id
        WHERE b.job_full_name = ? AND b.build_number = ?
        """,
        (job, number),
    ).fetchone()
    if row is None:
        return None
    build = dict(row)
    build["parameters"] = json.loads(build.pop("parameters_json"))
    return build


# ---------------------------------------------------------------- jobs

def job_stats(
    conn: sqlite3.Connection, q: str = "", sort: str = "true", desc: bool = True, by_repository: bool = True
) -> list[dict]:
    """Per-job (or per-repository) true/false counts. `jobs` is how many jobs a row covers."""
    _register_functions(conn)
    key = "repository_of(job_full_name)" if by_repository else "job_full_name"
    sort_col = JOB_SORTS.get(sort, JOB_SORTS["true"])
    direction = "DESC" if desc else "ASC"
    return [
        dict(r)
        for r in conn.execute(
            f"""
            WITH counts AS (
                SELECT {key} AS name, SUM(true_count) AS true_count, SUM(false_count) AS false_count,
                       COUNT(DISTINCT job_full_name) AS jobs
                FROM run_job_counts GROUP BY name
            ), last_fp AS (
                SELECT {key} AS name, MAX(started_at_ms) AS last_force_pass_ms
                FROM force_pass_builds GROUP BY name
            )
            SELECT c.name, c.true_count, c.false_count, c.jobs,
                   CAST(c.true_count AS REAL) / NULLIF(c.true_count + c.false_count, 0) AS ratio,
                   l.last_force_pass_ms
            FROM counts c LEFT JOIN last_fp l USING (name)
            WHERE {_name_match("name")}
            ORDER BY {sort_col} {direction} NULLS LAST, name
            """,
            _job_match_params(q),
        )
    ]


# ---------------------------------------------------------------- dashboard

def kpis(conn: sqlite3.Connection) -> dict:
    def fp_since(ms: int) -> int:
        return conn.execute("SELECT COUNT(*) FROM force_pass_builds WHERE started_at_ms >= ?", (ms,)).fetchone()[0]

    true_total, false_total = conn.execute(
        "SELECT COALESCE(SUM(true_count), 0), COALESCE(SUM(false_count), 0) FROM run_job_counts"
    ).fetchone()
    return {
        "last_7d": fp_since(days_ago_ms(7)),
        "last_30d": fp_since(days_ago_ms(30)),
        "all_time": fp_since(0),
        "true_total": true_total,
        "false_total": false_total,
        "ratio": true_total / (true_total + false_total) if true_total + false_total else None,
        "running": conn.execute("SELECT COUNT(*) FROM force_pass_builds WHERE result = 'RUNNING'").fetchone()[0],
    }


def weekly_force_passes(conn: sqlite3.Connection, weeks: int = 26, today: date | None = None) -> list[dict]:
    """Force passes per local week (Monday start) for the last N weeks, oldest first, gaps filled with 0."""
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    mondays = [this_monday - timedelta(weeks=i) for i in reversed(range(weeks))]
    counts = dict(
        conn.execute(
            """
            SELECT date(started_at_ms / 1000, 'unixepoch', 'localtime', 'weekday 0', '-6 days') AS week, COUNT(*)
            FROM force_pass_builds WHERE started_at_ms >= ? GROUP BY week
            """,
            (local_midnight_ms(mondays[0]),),
        ).fetchall()
    )
    return [{"week": m, "count": counts.get(m.isoformat(), 0)} for m in mondays]


def top_repositories(conn: sqlite3.Connection, since_ms: int, limit: int = 10) -> list[dict]:
    _register_functions(conn)
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT repository_of(job_full_name) AS repository, COUNT(*) AS n, COUNT(DISTINCT job_full_name) AS jobs
            FROM force_pass_builds WHERE started_at_ms >= ?
            GROUP BY repository ORDER BY n DESC, repository LIMIT ?
            """,
            (since_ms, limit),
        )
    ]


def top_authors(conn: sqlite3.Connection, since_ms: int, limit: int = 10) -> list[dict]:
    # Automated builds share author_id 'SYSTEM/AUTOMATED' and are grouped together.
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT author_id,
                   CASE WHEN author_id = 'SYSTEM/AUTOMATED' THEN 'Automated triggers' ELSE MAX(author_name) END
                       AS author_name,
                   COUNT(*) AS n
            FROM force_pass_builds WHERE started_at_ms >= ?
            GROUP BY author_id ORDER BY n DESC, author_id LIMIT ?
            """,
            (since_ms, limit),
        )
    ]


def period_start_ms(period: str) -> int:
    days = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])
    return 0 if days is None else days_ago_ms(days)
