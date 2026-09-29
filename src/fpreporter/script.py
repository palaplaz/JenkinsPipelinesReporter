"""Renders the Groovy collector script with its run-specific input."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from importlib import resources

PLACEHOLDER = "@@FP_INPUT_B64@@"
BEGIN_MARKER = "###FP_JSON_BEGIN###"
END_MARKER = "###FP_JSON_END###"
SCRIPT_SCHEMA_VERSION = 1


def load_template() -> str:
    return resources.files("fpreporter").joinpath("groovy/collect.groovy").read_text(encoding="utf-8")


def render(
    parameter_name: str,
    since_ms: int,
    lag_ms: int,
    recheck: Iterable[tuple[str, int]] = (),
) -> str:
    """Returns the script with input embedded as base64 JSON.

    Base64 keeps job names and parameter names from ever needing Groovy string escaping.
    """
    template = load_template()
    if template.count(PLACEHOLDER) != 1:
        raise RuntimeError(f"collect.groovy must contain {PLACEHOLDER} exactly once")

    payload = {
        "parameterName": parameter_name,
        "sinceMs": since_ms,
        "lagMs": lag_ms,
        "recheck": [{"job": job, "number": number} for job, number in recheck],
    }
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return template.replace(PLACEHOLDER, encoded)
