import logging

import pytest

from fpreporter import cli, db
from fpreporter.lock import exclusive_lock


@pytest.fixture(autouse=True)
def release_log_file():
    yield
    # The CLI attaches a FileHandler; close it so Windows can delete tmp_path.
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[jenkins]\nurl = "https://j"\n[storage]\ndb_path = "data/fp.sqlite"\n', encoding="utf-8")
    return path


def test_init_db_creates_database_and_log(config, tmp_path):
    assert cli.main(["--config", str(config), "init-db"]) == cli.EXIT_OK

    ro = db.connect_read_only(tmp_path / "data" / "fp.sqlite")
    ro.close()
    assert (tmp_path / "data" / "fpreporter.log").exists()


def test_runs_without_console_streams(config, tmp_path, monkeypatch):
    # pythonw.exe (used by the scheduled task) sets sys.stderr to None.
    monkeypatch.setattr(cli.sys, "stderr", None)
    assert cli.main(["--config", str(config), "init-db"]) == cli.EXIT_OK
    assert "Database ready" in (tmp_path / "data" / "fpreporter.log").read_text(encoding="utf-8")


def test_missing_config_returns_config_error(tmp_path):
    assert cli.main(["--config", str(tmp_path / "nope.toml"), "collect"]) == cli.EXIT_CONFIG_ERROR


def test_collect_without_credentials_returns_config_error(config, monkeypatch):
    monkeypatch.delenv("JENKINS_USER", raising=False)
    monkeypatch.delenv("JENKINS_TOKEN", raising=False)
    assert cli.main(["--config", str(config), "collect"]) == cli.EXIT_CONFIG_ERROR


def test_collect_refuses_to_run_while_another_collection_holds_the_lock(config, tmp_path, monkeypatch):
    monkeypatch.setenv("JENKINS_USER", "u")
    monkeypatch.setenv("JENKINS_TOKEN", "t")
    monkeypatch.setattr(cli.JenkinsClient, "run_script", lambda self, s: pytest.fail("must not contact Jenkins"))

    with exclusive_lock(tmp_path / "data" / cli.LOCK_FILE):
        assert cli.main(["--config", str(config), "collect"]) == cli.EXIT_ALREADY_RUNNING


def test_collect_exit_code_reflects_run_status(config, monkeypatch):
    monkeypatch.setenv("JENKINS_USER", "u")
    monkeypatch.setenv("JENKINS_TOKEN", "t")
    monkeypatch.setattr(cli.JenkinsClient, "run_script", lambda self, s: "no markers here")
    assert cli.main(["--config", str(config), "collect"]) == cli.EXIT_RUN_FAILED
