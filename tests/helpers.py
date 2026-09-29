import base64
import json
import re

from fpreporter import script


def script_input(rendered: str) -> dict:
    """Decodes the input fpreporter embedded into the rendered Groovy script."""
    match = re.search(r"def INPUT_B64 = '([A-Za-z0-9+/=]*)'", rendered)
    assert match, "INPUT_B64 assignment not found"
    return json.loads(base64.b64decode(match.group(1)))


def script_output(payload: dict, noise: str = "") -> str:
    """Formats a payload the way collect.groovy prints it."""
    return f"{noise}{script.BEGIN_MARKER}\n{json.dumps(payload)}\n{script.END_MARKER}\n"
