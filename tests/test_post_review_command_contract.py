"""The environment `post-review-command` runs in, pinned.

A consumer hands its merge gate over as a COMMAND STRING, so nothing in that
repository can see what this workflow exports around it. Its gate reads
`GH_TOKEN`, `GH_REPO`, `PR` and the head sha, and a gate that finds one missing
does not fail — it returns without posting, and the required check keeps whatever
verdict it had before the review. That is a silently stale gate, so the contract
is asserted here, in the repository that owns it.

The head sha is `REPORT_SHA` in both jobs. In the synthesis job it is read LIVE
rather than taken from the event: a sharded review runs long enough for a push to
land inside it, and a verdict posted on the event's sha leaves the live head with
no event able to clear it.
"""

import re

import pytest

from tests._helpers import REPO_ROOT, workflow_jobs

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review.yaml"
GATE_STEP = "Re-evaluate the caller's merge gate"
REQUIRED = ("GH_TOKEN", "GH_REPO", "PR")


def _gate_steps() -> dict[str, dict]:
    """Every step that runs the caller's `post-review-command`, by job."""
    steps = {}
    for job, body in workflow_jobs(WORKFLOW).items():
        for step in body.get("steps", []):
            if GATE_STEP in step.get("name", ""):
                steps[job] = step
    return steps


def test_both_jobs_that_post_a_review_re_evaluate_the_gate() -> None:
    """A review posted with the workflow GITHUB_TOKEN fires no workflow, so a
    job that posts one and skips this step leaves the gate unaware of it."""
    assert set(_gate_steps()) == {"review", "review_synthesis"}


@pytest.mark.parametrize("job", ["review", "review_synthesis"])
@pytest.mark.parametrize("name", REQUIRED)
def test_the_gate_command_is_handed_the_environment_it_reads(
    job: str, name: str
) -> None:
    step = _gate_steps()[job]
    assert name in step["env"], f"{job} runs the caller's gate without {name}"


def test_the_first_pass_gate_post_names_the_reviewed_head() -> None:
    """The whole-diff read posts about the head its event named."""
    env = _gate_steps()["review"]["env"]
    assert "REPORT_SHA" in env
    assert "github.event.pull_request.head.sha" in env["REPORT_SHA"]


def test_the_sharded_gate_post_reads_the_head_live() -> None:
    """A verdict on the event's sha strands the live head: the push that moved it
    yields to this run, so no later event posts one. RED if REPORT_SHA goes back
    to the event payload here."""
    step = _gate_steps()["review_synthesis"]
    assert "REPORT_SHA" not in step["env"], "the synthesis head is read, not taken"
    assert re.search(r"headRefOid", step["run"]), "no live head read"
    assert "REPORT_SHA=" in step["run"] and "export REPORT_SHA" in step["run"]
    assert "EVENT_HEAD_SHA" in step["env"], "the fallback for an unreadable head"
