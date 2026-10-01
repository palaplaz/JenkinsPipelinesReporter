"""Page routes. Requests from htmx (HX-Request header) get just the partial that changed."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fpreporter import db
from fpreporter.web import queries
from fpreporter.web.launcher import EXIT_MESSAGES

router = APIRouter()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    # A short-lived read-only connection per request: cheap for SQLite and safe across worker threads.
    conn = db.connect_read_only(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def render(request: Request, conn: sqlite3.Connection, template: str, active: str, **context) -> HTMLResponse:
    context.update(active=active, last_run=queries.last_run(conn))
    return request.app.state.templates.TemplateResponse(request, template, context)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true" and request.headers.get("HX-Boosted") != "true"


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, period: str = queries.DEFAULT_PERIOD, conn: sqlite3.Connection = Depends(get_conn)):
    if period not in queries.PERIODS:
        period = queries.DEFAULT_PERIOD
    since = queries.period_start_ms(period)
    weekly = queries.weekly_force_passes(conn)
    return render(
        request, conn, "dashboard.html", "dashboard",
        kpis=queries.kpis(conn),
        chart=_bar_chart(weekly),
        period=period,
        periods=list(queries.PERIODS),
        top_repositories=queries.top_repositories(conn, since),
        top_authors=queries.top_authors(conn, since),
    )


@router.get("/builds", response_class=HTMLResponse)
def builds(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    f = queries.BuildFilter.from_query(request.query_params)
    context = {"f": f, "page": queries.search_builds(conn, f, request.app.state.reason_parameter)}
    if is_htmx(request):
        return request.app.state.templates.TemplateResponse(request, "builds/_results.html", context)
    return render(request, conn, "builds/list.html", "builds", results=queries.distinct_results(conn), **context)


# The job goes in the query string, not the path: multibranch names contain "%2F", which path
# decoding would turn into "/" and no longer match the stored name.
@router.get("/build", response_class=HTMLResponse)
def build_detail(request: Request, job: str, number: int, conn: sqlite3.Connection = Depends(get_conn)):
    build = queries.get_build(conn, job, number)
    if build is None:
        raise HTTPException(status_code=404, detail=f"Build {job} #{number} is not in the database")
    reason_parameter = request.app.state.reason_parameter
    return render(
        request, conn, "builds/detail.html", "builds",
        build=build, reason=build["parameters"].get(reason_parameter), reason_parameter=reason_parameter,
    )


@router.get("/jobs", response_class=HTMLResponse)
def jobs(
    request: Request, q: str = "", sort: str = "true", dir: str = "desc", group: str = "repository",
    conn: sqlite3.Connection = Depends(get_conn),
):
    sort = sort if sort in queries.JOB_SORTS else "true"
    group = group if group in ("repository", "job") else "repository"
    rows = queries.job_stats(conn, q.strip(), sort, dir != "asc", by_repository=group == "repository")
    context = {"jobs": rows, "q": q, "sort": sort, "dir": dir, "group": group}
    if is_htmx(request):
        return request.app.state.templates.TemplateResponse(request, "jobs/_table.html", context)
    return render(request, conn, "jobs/list.html", "jobs", **context)


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, msg: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    launcher = request.app.state.launcher
    run_rows = queries.list_runs(conn)
    collecting = (launcher is not None and launcher.running) or any(r["status"] == "running" for r in run_rows)
    exit_code = launcher.last_exit_code if launcher is not None and not collecting else None
    context = {
        "runs": run_rows,
        "retry_enabled": launcher is not None,
        "collecting": collecting,
        "exit_message": EXIT_MESSAGES.get(exit_code),
        "msg": msg,
    }
    if is_htmx(request):
        return request.app.state.templates.TemplateResponse(request, "runs/_table.html", context)
    return render(request, conn, "runs/list.html", "runs", **context)


@router.post("/runs/collect")
def start_collection(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    launcher = request.app.state.launcher
    if launcher is None:
        raise HTTPException(status_code=404, detail="Retrying is only available when the UI runs via 'fpreporter serve'")
    # Reject cross-site form posts: any web page could otherwise POST to this localhost UI.
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-site request rejected")

    already_running = any(r["status"] == "running" for r in queries.list_runs(conn))
    started = not already_running and launcher.start()
    return RedirectResponse(f"/runs?msg={'started' if started else 'busy'}", status_code=303)


# Wide aspect ratio (~6:1) so the SVG fills the card width without letterboxing.
CHART_BAR_WIDTH = 40
CHART_HEIGHT = 160


def _bar_chart(weekly: list[dict]) -> dict:
    """Pre-computes SVG geometry so the template stays simple."""
    peak = max((w["count"] for w in weekly), default=0)
    bars = []
    for i, w in enumerate(weekly):
        height = 0 if peak == 0 else round(w["count"] / peak * (CHART_HEIGHT - 20))
        bars.append({
            **w,
            "x": i * CHART_BAR_WIDTH + 4,
            "y": CHART_HEIGHT - height,
            "height": height,
            "label": w["week"].strftime("%d %b") if i % 4 == 0 else "",
        })
    return {
        "bars": bars,
        "peak": peak,
        "width": len(weekly) * CHART_BAR_WIDTH,
        "height": CHART_HEIGHT,
        "bar_width": CHART_BAR_WIDTH - 8,
    }
