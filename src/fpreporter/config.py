"""Loads settings from a TOML file and Jenkins credentials from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV_VAR = "FPREPORTER_CONFIG"
DEFAULT_CONFIG_FILE = "config.toml"
USER_ENV_VAR = "JENKINS_USER"
TOKEN_ENV_VAR = "JENKINS_TOKEN"


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class JenkinsSettings:
    url: str
    verify_tls: bool = True
    timeout_seconds: int = 600


@dataclass(frozen=True)
class CollectorSettings:
    parameter_name: str = "FORCE_PASS"
    lag_minutes: int = 5


@dataclass(frozen=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    # Build parameter shown as the "Reason" column; taken from the stored parameters.
    reason_parameter: str = "FORCE_PASS_REASON"


@dataclass(frozen=True)
class Settings:
    jenkins: JenkinsSettings
    collector: CollectorSettings
    db_path: Path
    web: WebSettings


@dataclass(frozen=True)
class Credentials:
    user: str
    token: str

    def __repr__(self) -> str:
        return f"Credentials(user={self.user!r}, token='****')"


def resolve_config_path(explicit: str | os.PathLike | None = None) -> Path:
    """Explicit path > FPREPORTER_CONFIG env var > ./config.toml."""
    raw = explicit or os.environ.get(CONFIG_ENV_VAR) or DEFAULT_CONFIG_FILE
    return Path(raw).expanduser().resolve()


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    config_path = resolve_config_path(path)
    try:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {config_path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {config_path}: {e}") from None

    return _parse(data, base_dir=config_path.parent)


def load_credentials() -> Credentials:
    """Reads the Jenkins user and API token. Only the collector needs these."""
    user = os.environ.get(USER_ENV_VAR, "").strip()
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    missing = [name for name, value in ((USER_ENV_VAR, user), (TOKEN_ENV_VAR, token)) if not value]
    if missing:
        raise ConfigError(f"Missing environment variable(s): {', '.join(missing)}")
    return Credentials(user=user, token=token)


def _parse(data: dict, base_dir: Path) -> Settings:
    jenkins = _section(data, "jenkins")
    collector = _section(data, "collector", required=False)
    storage = _section(data, "storage")
    web = _section(data, "web", required=False)

    url = _get(jenkins, "jenkins", "url", str).rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"[jenkins].url must start with http:// or https://, got {url!r}")

    db_path = Path(_get(storage, "storage", "db_path", str)).expanduser()
    if not db_path.is_absolute():
        db_path = (base_dir / db_path).resolve()

    lag_minutes = _get(collector, "collector", "lag_minutes", int, CollectorSettings.lag_minutes)
    if lag_minutes < 0:
        raise ConfigError("[collector].lag_minutes must be >= 0")

    timeout = _get(jenkins, "jenkins", "timeout_seconds", int, JenkinsSettings.timeout_seconds)
    if timeout <= 0:
        raise ConfigError("[jenkins].timeout_seconds must be > 0")

    return Settings(
        jenkins=JenkinsSettings(
            url=url,
            verify_tls=_get(jenkins, "jenkins", "verify_tls", bool, True),
            timeout_seconds=timeout,
        ),
        collector=CollectorSettings(
            parameter_name=_get(collector, "collector", "parameter_name", str, CollectorSettings.parameter_name),
            lag_minutes=lag_minutes,
        ),
        db_path=db_path,
        web=WebSettings(
            host=_get(web, "web", "host", str, WebSettings.host),
            port=_get(web, "web", "port", int, WebSettings.port),
            reason_parameter=_get(web, "web", "reason_parameter", str, WebSettings.reason_parameter),
        ),
    )


def _section(data: dict, name: str, required: bool = True) -> dict:
    section = data.get(name)
    if section is None:
        if required:
            raise ConfigError(f"Missing [{name}] section")
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table")
    return section


_MISSING = object()


def _get(section: dict, section_name: str, key: str, type_: type, default=_MISSING):
    if key not in section:
        if default is _MISSING:
            raise ConfigError(f"Missing [{section_name}].{key}")
        return default
    value = section[key]
    # bool is a subclass of int; don't accept true/false where a number is expected.
    if not isinstance(value, type_) or (type_ is int and isinstance(value, bool)):
        raise ConfigError(f"[{section_name}].{key} must be of type {type_.__name__}")
    return value
