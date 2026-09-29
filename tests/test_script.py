import pytest

from fpreporter import script
from helpers import script_input as extract_input


def test_template_has_placeholder_once():
    assert script.load_template().count(script.PLACEHOLDER) == 1


def test_template_markers_match_python_side():
    template = script.load_template()
    assert f"'{script.BEGIN_MARKER}'" in template
    assert f"'{script.END_MARKER}'" in template
    assert f"SCHEMA_VERSION = {script.SCRIPT_SCHEMA_VERSION}" in template


def test_render_embeds_input():
    rendered = script.render("FORCE_PASS", since_ms=1_000, lag_ms=300_000, recheck=[("team/app", 7)])

    assert script.PLACEHOLDER not in rendered
    assert extract_input(rendered) == {
        "parameterName": "FORCE_PASS",
        "sinceMs": 1_000,
        "lagMs": 300_000,
        "recheck": [{"job": "team/app", "number": 7}],
    }


@pytest.mark.parametrize("job", ["it's/quoted", 'double"quote', "back\\slash", "ünïcode/Jöb", "${evil}"])
def test_render_handles_awkward_job_names(job):
    rendered = script.render("FORCE_PASS", 0, 0, recheck=[(job, 1)])
    assert extract_input(rendered)["recheck"][0]["job"] == job
