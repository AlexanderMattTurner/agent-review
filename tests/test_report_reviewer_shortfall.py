"""Behavioral tests for .github/reviewer/report-reviewer-shortfall.sh — the step
that turns every way the reviewer fell short on a run into ONE message, posted
as a comment on the caller's reviewer-defect issue, or written to the job
summary and a warning when no issue is named. The merge gate holds the pull
request on its own, from the shortfall marker on the posted review.

The real script runs against a fake `gh` on PATH that records each call and the
comment body it was handed, and can refuse the post.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "report-reviewer-shortfall.sh"
HEAD_SHA = "cafef00dcafef00dcafef00dcafef00dcafef00d"
RUN_URL = "https://github.com/owner/repo/actions/runs/42"

_FAKE_GH = """#!/usr/bin/env bash
echo "$*" >>"$FAKE_GH_CALLS"
if [[ -n "${FAKE_GH_REFUSE:-}" ]]; then echo "HTTP 403: Resource not accessible" >&2; exit 1; fi
for arg in "$@"; do
  if [[ "$arg" == body=@* ]]; then cp "${arg#body=@}" "$FAKE_GH_BODY"; fi
done
echo "https://github.com/owner/repo/issues/7#issuecomment-1"
"""


def _run(
    tmp_path: Path,
    *,
    issue: str = "",
    refuse: bool = False,
    fold: str = "success",
    post: str = "success",
    incomplete: str = "",
    shortfall: str = "",
) -> tuple[subprocess.CompletedProcess, list[str], str, str]:
    """Run the script; return (proc, gh calls, posted body, job summary)."""
    gh = tmp_path / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    pr_dir = tmp_path / "pr-input"
    pr_dir.mkdir()
    if incomplete:
        (pr_dir / "incomplete-read.txt").write_text(incomplete, encoding="utf-8")
    shortfall_file = tmp_path / "shortfalls.txt"
    if shortfall:
        shortfall_file.write_text(shortfall, encoding="utf-8")
    calls, body, summary = (tmp_path / n for n in ("calls", "body", "summary"))
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "GH_REPO": "owner/repo",
        "PR": "12",
        "HEAD_SHA": HEAD_SHA,
        "RUN_URL": RUN_URL,
        "PR_INPUT_DIR": str(pr_dir),
        "REVIEWER_SHORTFALL_FILE": str(shortfall_file),
        "REVIEWER_DEFECT_ISSUE": issue,
        "READ_RESULT": "failure",
        "FOLD_OUTCOME": fold,
        "POST_OUTCOME": post,
        "GITHUB_STEP_SUMMARY": str(summary),
        "RETRY_MAX": "1",
        "FAKE_GH_CALLS": str(calls),
        "FAKE_GH_BODY": str(body),
        **({"FAKE_GH_REFUSE": "1"} if refuse else {}),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )

    def read(path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    return proc, read(calls).splitlines(), read(body), read(summary)


def test_a_run_that_fell_short_nowhere_reports_nothing(tmp_path: Path) -> None:
    proc, calls, _, summary = _run(tmp_path, issue="7")
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert summary == ""
    assert "::warning::" not in proc.stdout


def test_every_shortfall_lands_in_one_comment_on_the_named_issue(
    tmp_path: Path,
) -> None:
    proc, calls, body, summary = _run(
        tmp_path,
        issue="7",
        fold="failure",
        post="failure",
        incomplete="1 of 2 shards delivered no review: shard-01 (src/`c`.py).\n",
        shortfall="1 of 2 findings could not be posted as review threads\n",
    )
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1
    assert "-X POST repos/owner/repo/issues/7/comments" in calls[0]
    assert "owner/repo#12" in body
    assert HEAD_SHA in body
    assert RUN_URL in body
    assert "No read leg delivered a review (read job: failure)" in body
    assert "shard-01 (src/c.py)" in body, "a PR file name cannot close the fence"
    assert "1 of 2 findings could not be posted" in body
    assert "Posting the review failed" in body
    assert summary == ""


def test_with_no_issue_named_the_report_goes_to_the_job_summary(
    tmp_path: Path,
) -> None:
    proc, calls, _, summary = _run(tmp_path, shortfall="2 of 2 findings lost\n")
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert "- 2 of 2 findings lost" in summary
    assert RUN_URL in summary
    assert "::warning::the automated reviewer fell short on PR #12" in proc.stdout


@pytest.mark.parametrize(
    ("issue", "refuse"), [("7", True), ("#7", False), ("07", False)]
)
def test_a_comment_that_cannot_post_falls_back_to_the_summary(
    tmp_path: Path, issue: str, refuse: bool
) -> None:
    """A refused comment, or an input that names no issue, must not lose the
    report and must not red the job: the gate still judges the review."""
    proc, _, _, summary = _run(
        tmp_path, issue=issue, refuse=refuse, shortfall="1 of 1 findings lost\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert "1 of 1 findings lost" in summary
    assert "::warning::" in proc.stderr + proc.stdout
