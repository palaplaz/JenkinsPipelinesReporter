"""FastAPI application factory: templates, filters, static files and error handling."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from fpreporter import db
from fpreporter.web import queries, routes

WEB_DIR = Path(__file__).parent


def create_app(
    db_path: str | Path,
    jenkins_url: str = "",
    reason_parameter: str = queries.DEFAULT_REASON_PARAMETER,
) -> FastAPI:
    # No OpenAPI/docs pages: this is a UI, not an API.
    app = FastAPI(title="FORCE_PASS audit", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = Path(db_path)
    app.state.reason_parameter = reason_parameter
    app.state.templates = _templates(jenkins_url.rstrip("/"))
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(routes.router)

    @app.exception_handler(FileNotFoundError)
    @app.exception_handler(db.SchemaVersionError)
    async def database_unavailable(request: Request, exc: Exception) -> HTMLResponse:
        return _error_page(app, request, 503, "Database unavailable", str(exc))

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError) -> HTMLResponse:
        return _error_page(app, request, 400, "Bad request", "The link is missing or has invalid parameters.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> HTMLResponse:
        title = "Not found" if exc.status_code == 404 else f"Error {exc.status_code}"
        return _error_page(app, request, exc.status_code, title, str(exc.detail))

    return app


def _error_page(app: FastAPI, request: Request, status: int, title: str, message: str) -> HTMLResponse:
    return app.state.templates.TemplateResponse(
        request, "error.html", {"title": title, "message": message}, status_code=status
    )


def _templates(jenkins_url: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=WEB_DIR / "templates")
    env = templates.env
    env.filters["ms"] = _format_ms
    env.filters["iso"] = _format_iso
    env.filters["pct"] = lambda v: "–" if v is None else f"{v:.1%}"
    env.filters["jenkins_url"] = lambda url: _jenkins_url(url, jenkins_url)
    # Multibranch item names are URL-encoded ("group%2Frepo"); show them decoded.
    env.filters["job_name"] = lambda name: unquote(name) if name else name
    env.globals["build_path"] = lambda job, number: f"/build?{urlencode({'job': job, 'number': number})}"
    env.globals["qs"] = lambda params: f"?{urlencode(params)}" if params else "?"
    return templates


def _format_ms(ms: int | None) -> str:
    if ms is None:
        return "–"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _format_iso(value: str | None) -> str:
    if not value:
        return "–"
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def _jenkins_url(url: str | None, base: str) -> str | None:
    """Absolute http(s) URL for a build, or None if it can't be made safe to link."""
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    # The script falls back to a relative URL when Jenkins has no root URL configured.
    if base and not url.startswith(("/", "javascript:")) and ":" not in url.split("/")[0]:
        return f"{base}/{url}"
    return None
