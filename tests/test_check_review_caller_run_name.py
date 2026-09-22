"""The guard that keeps a reviewer caller's run name attributable to its PR.

Every case runs the real script as a subprocess over a workflow file written to
disk, and asserts the exit status and what the message names.
"""

import subprocess
import sys
import textwrap

import pytest

from tests._helpers import REPO_ROOT

CHECK = REPO_ROOT / ".github" / "scripts" / "check-review-caller-run-name.py"

CALLER = textwrap.dedent(
    """\
    name: Claude reviewers
    {run_name}on:
      pull_request_target:
        types: [opened]
      workflow_dispatch:
        inputs:
          pr:
            required: true
            type: string

    jobs:
      review:
        uses: owner/agent-review/.github/workflows/review.yaml@abc123
        with:
          reviewer-ref: abc123
    {extra}"""
)

GOOD_NAME = (
    "run-name: Claude reviewers"
    "${{ inputs.pr && format(' — PR {0}', inputs.pr) || '' }}\n"
)


def run(tmp_path, body: str, name: str = "caller.yaml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(CHECK), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def caller(run_name: str = "", extra: str = "") -> str:
    return CALLER.format(run_name=run_name, extra=extra)


@pytest.mark.parametrize(
    "run_name",
    [
        GOOD_NAME,
        "run-name: Claude reviewers — PR ${{ inputs.pr }}\n",
        "run-name: PR ${{ github.event.inputs.pr }}\n",
    ],
    ids=["format-idiom", "direct-interpolation", "bare-suffix"],
)
def test_a_run_name_ending_with_the_pull_request_number_passes(tmp_path, run_name):
    result = run(tmp_path, caller(run_name))
    assert result.returncode == 0, result.stderr


def test_a_caller_with_no_run_name_is_refused(tmp_path):
    result = run(tmp_path, caller())
    assert result.returncode == 1
    assert "declares no `run-name:`" in result.stderr
    assert "re-dispatching a failing read" in result.stderr


@pytest.mark.parametrize(
    "run_name",
    [
        "run-name: Claude reviewers\n",
        "run-name: PR ${{ inputs.pr }} (accumulated)\n",
        "run-name: reviewers ${{ inputs.pr }}\n",
        "run-name: PR ${{ inputs.pr }} for pull request 12\n",
    ],
    ids=["no-number", "text-after-the-number", "number-without-PR", "trailing-prose"],
)
def test_a_run_name_the_dispatcher_cannot_match_is_refused(tmp_path, run_name):
    result = run(tmp_path, caller(run_name))
    assert result.returncode == 1
    assert "does not end with the pull request number" in result.stderr


def test_a_caller_that_turns_accumulated_reads_off_needs_no_run_name(tmp_path):
    result = run(tmp_path, caller(extra="      max-delta-reviews-per-pr: 0\n"))
    assert result.returncode == 0, result.stderr


def test_a_caller_that_leaves_the_budget_at_its_default_still_needs_one(tmp_path):
    result = run(tmp_path, caller(extra="      max-delta-reviews-per-pr: 1\n"))
    assert result.returncode == 1
    assert "declares no `run-name:`" in result.stderr


def test_a_workflow_that_calls_no_reviewer_is_left_alone(tmp_path):
    body = textwrap.dedent(
        """\
        name: Something else
        on: [push]
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: echo hi
        """
    )
    result = run(tmp_path, body)
    assert result.returncode == 0, result.stderr


def test_a_local_call_of_the_reviewer_counts_too(tmp_path):
    body = caller().replace(
        "owner/agent-review/.github/workflows/review.yaml@abc123",
        "./.github/workflows/review.yaml",
    )
    result = run(tmp_path, body)
    assert result.returncode == 1


def test_every_refused_file_is_named_once(tmp_path):
    first = tmp_path / "a.yaml"
    second = tmp_path / "b.yaml"
    for path in (first, second):
        path.write_text(caller(), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(CHECK), str(first), str(second)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.count("declares no `run-name:`") == 2


def test_this_repository_s_own_caller_satisfies_the_guard():
    caller_path = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"
    result = subprocess.run(
        [sys.executable, str(CHECK), str(caller_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
