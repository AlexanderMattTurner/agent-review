"""The environment `post-review-command` runs in, pinned.

A consumer hands its merge gate over as a COMMAND STRING, so nothing in that
repository can see what this workflow exports around it. Its gate reads
`GH_TOKEN`, `GH_REPO`, `PR` and the head sha, and a gate that finds one missing
does not fail — it returns without posting, and the required check keeps whatever
verdict it had before the review. That is a silently stale gate, so the contract
is asserted here, in the repository that owns it.

The head sha is `REPORT_SHA`, read LIVE rather than taken from the event: a read
runs long enough for a push to land inside it, and a verdict posted on the
event's sha leaves the live head with no event able to clear it.
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


def test_the_job_that_posts_the_review_re_evaluates_the_gate() -> None:
    """A review posted with the workflow GITHUB_TOKEN fires no workflow, so the
    job that posts one must run this step. The skip path re-posts from `gate`,
    and the two never run in one workflow run: post needs a prepared read, gate
    needs a prepare that skipped, so no head gets a second verdict."""
    assert set(_gate_steps()) == {"post", "gate"}
    jobs = workflow_jobs(WORKFLOW)
    assert "needs.prepare.outputs.prepared == 'true'" in jobs["post"]["if"]
    assert "needs.prepare.outputs.skip == 'true'" in jobs["gate"]["if"]


def test_a_gate_only_run_cannot_cancel_a_findings_post() -> None:
    """GitHub cancels a PENDING job when another joins its concurrency group,
    whatever cancel-in-progress says, so a gate-only re-post queued in post's
    group would discard a completed read's findings."""
    jobs = workflow_jobs(WORKFLOW)
    groups = {job: jobs[job]["concurrency"]["group"] for job in ("post", "gate")}
    assert groups["post"] != groups["gate"], groups


@pytest.mark.parametrize("job", ["post", "gate"])
@pytest.mark.parametrize("name", REQUIRED)
def test_the_gate_command_is_handed_the_environment_it_reads(
    job: str, name: str
) -> None:
    step = _gate_steps()[job]
    assert name in step["env"], f"the {job} job runs the caller's gate without {name}"


@pytest.mark.parametrize("job", ["post", "gate"])
def test_the_gate_post_reads_the_head_live(job: str) -> None:
    """A verdict on the event's sha strands the live head: the push that moved it
    yields to this run, so no later event posts one. RED if REPORT_SHA goes back
    to the event payload here. The fallback is the head `decide` resolved, which
    a dispatch run has although its payload names no pull request."""
    step = _gate_steps()[job]
    assert "REPORT_SHA" not in step["env"], "the head is read, not taken"
    assert re.search(r"headRefOid", step["run"]), "no live head read"
    assert "REPORT_SHA=" in step["run"] and "export REPORT_SHA" in step["run"]
    assert step["env"]["EVENT_HEAD_SHA"] == "${{ needs.decide.outputs.head_sha }}"
