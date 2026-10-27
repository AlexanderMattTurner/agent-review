"""template-sync's `dry-run` input must be compared as a boolean.

The input is declared `type: boolean`, so GitHub hands the expression a real
boolean. Comparing it to the STRING 'true' never matches: GitHub casts across
types to a number, so `true == 'true'` compares 1 with NaN. The dry-run branch
was therefore dead and the pull-request branch always ran — every "dry run"
dispatch opened a real pull request, with no error and nothing in the log to say
so.

That is invisible from inside the workflow, which is why it survived long enough
to reach every downstream repo. These tests read the workflow GitHub actually
executes and pin the type agreement between the declaration and its reader: the
caller declares the dispatch input and forwards it to the template's reusable
sync workflow, which owns the step conditions that read it.
"""

import yaml

from tests._helpers import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "template-sync.yaml"
INPUT_NAME = "dry-run"

# `on` is the YAML 1.1 boolean True, which is what safe_load gives back for the
# unquoted key. Reading it as the string "on" finds nothing and the whole suite
# passes vacuously.
ON_KEY = True


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_input_is_declared_boolean():
    spec = _workflow()[ON_KEY]["workflow_dispatch"]["inputs"][INPUT_NAME]
    assert spec["type"] == "boolean"


def test_the_default_is_a_boolean_not_a_string():
    # A quoted "false" default is the same type confusion one step earlier: it
    # reads as truthy wherever a caller forwards it verbatim.
    spec = _workflow()[ON_KEY]["workflow_dispatch"]["inputs"][INPUT_NAME]
    assert spec["default"] is False, f"default is {spec['default']!r}, not a boolean"


def test_the_caller_forwards_a_boolean():
    # A scheduled run carries no inputs, so a bare `inputs.dry-run` forwards
    # null. The comparison yields a real boolean on both triggers.
    forwarded = _workflow()["jobs"]["sync"]["with"][INPUT_NAME]
    assert forwarded == "${{ inputs.dry-run == true }}"
