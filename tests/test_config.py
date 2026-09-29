from pathlib import Path

import pytest

from fpreporter.config import ConfigError, load_credentials, load_settings

MINIMAL = """
[jenkins]
url = "https://jenkins.example.com/"

[storage]
db_path = "data/fp.sqlite"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_uses_defaults(tmp_path):
    settings = load_settings(write_config(tmp_path, MINIMAL))

    assert settings.jenkins.url == "https://jenkins.example.com"  # trailing slash stripped
    assert settings.jenkins.verify_tls is True
    assert settings.jenkins.timeout_seconds == 600
    assert settings.collector.parameter_name == "FORCE_PASS"
    assert settings.collector.lag_minutes == 5
    assert settings.web.host == "127.0.0.1"
    assert settings.web.port == 8000
    assert settings.web.reason_parameter == "FORCE_PASS_REASON"


def test_relative_db_path_resolves_against_config_dir(tmp_path, monkeypatch):
    config = write_config(tmp_path, MINIMAL)
    monkeypatch.chdir(tmp_path.parent)

    settings = load_settings(config)

    assert settings.db_path == (tmp_path / "data" / "fp.sqlite").resolve()


def test_example_config_is_valid():
    example = Path(__file__).parent.parent / "config.example.toml"
    settings = load_settings(example)
    assert settings.collector.parameter_name == "FORCE_PASS"


def test_config_path_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FPREPORTER_CONFIG", str(write_config(tmp_path, MINIMAL)))
    assert load_settings().jenkins.url == "https://jenkins.example.com"


@pytest.mark.parametrize(
    "text, message",
    [
        ("[storage]\ndb_path = 'x'", "Missing [jenkins]"),
        ("[jenkins]\nurl = 'https://j'", "Missing [storage]"),
        ("[jenkins]\nurl = 'jenkins.local'\n[storage]\ndb_path = 'x'", "must start with http"),
        ("[jenkins]\nurl = 'https://j'\ntimeout_seconds = true\n[storage]\ndb_path = 'x'", "must be of type int"),
        ("[jenkins]\nurl = 'https://j'\n[storage]\ndb_path = 'x'\n[collector]\nlag_minutes = -1", "lag_minutes"),
        ("not = [valid", "Invalid TOML"),
    ],
)
def test_invalid_config(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message.replace("[", r"\[")):
        load_settings(write_config(tmp_path, text))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.toml")


def test_credentials_from_env(monkeypatch):
    monkeypatch.setenv("JENKINS_USER", "svc-audit")
    monkeypatch.setenv("JENKINS_TOKEN", "secret-token")

    creds = load_credentials()

    assert creds.user == "svc-audit"
    assert creds.token == "secret-token"
    assert "secret-token" not in repr(creds)


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("JENKINS_USER", raising=False)
    monkeypatch.delenv("JENKINS_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="JENKINS_USER, JENKINS_TOKEN"):
        load_credentials()
