"""Command line entry point: fpreporter [--config PATH] {collect,init-db,serve}."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from fpreporter import db
from fpreporter.collector import collect
from fpreporter.config import ConfigError, load_credentials, load_settings
from fpreporter.jenkins_client import JenkinsClient

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_CONFIG_ERROR = 2

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fpreporter", description="FORCE_PASS audit reporter for Jenkins")
    parser.add_argument("--config", help="Path to config.toml (default: $FPREPORTER_CONFIG or ./config.toml)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect", help="Run one collection against Jenkins and store the results")
    commands.add_parser("init-db", help="Create or migrate the database without contacting Jenkins")
    serve = commands.add_parser("serve", help="Start the read-only web UI")
    serve.add_argument("--host", help="Override [web].host")
    serve.add_argument("--port", type=int, help="Override [web].port")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    _setup_logging(settings.db_path.parent / "fpreporter.log")

    if args.command == "init-db":
        db.connect(settings.db_path).close()
        logging.getLogger(__name__).info("Database ready at %s (schema v%d)", settings.db_path, db.SCHEMA_VERSION)
        return EXIT_OK

    if args.command == "serve":
        return _serve(settings, host=args.host or settings.web.host, port=args.port or settings.web.port)

    try:
        credentials = load_credentials()
    except ConfigError as e:
        logging.getLogger(__name__).error("Configuration error: %s", e)
        return EXIT_CONFIG_ERROR

    client = JenkinsClient(settings.jenkins, credentials)
    conn = db.connect(settings.db_path)
    try:
        summary = collect(conn, settings.collector, client.run_script)
    finally:
        conn.close()
    return EXIT_OK if summary.status == "ok" else EXIT_RUN_FAILED


def _serve(settings, host: str, port: int) -> int:
    # Imported lazily so collect/init-db (the scheduled task) don't load the web stack.
    import uvicorn

    from fpreporter.web.app import create_app

    if not settings.db_path.exists():
        logging.getLogger(__name__).error("No database at %s; run 'fpreporter collect' first", settings.db_path)
        return EXIT_CONFIG_ERROR
    app = create_app(
        settings.db_path, jenkins_url=settings.jenkins.url, reason_parameter=settings.web.reason_parameter
    )
    print(f"FORCE_PASS audit UI: http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return EXIT_OK


def _setup_logging(log_file: Path) -> None:
    # Scheduled runs have no console, so always log to a file next to the database as well.
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stderr is not None:  # None under pythonw.exe, which the scheduled task uses
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers, force=True)


if __name__ == "__main__":
    sys.exit(main())
