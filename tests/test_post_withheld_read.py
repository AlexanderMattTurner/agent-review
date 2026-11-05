"""post-withheld-read.sh records a read prepare withheld from an untrusted author.
The review it posts is what review-findings-gate.sh holds the merge on, and what
tells the pull request why. It posts one per pull request, not one per push.

The real script and the real `gh` post to a localhost GitHub (FakeReviewPoster),
so the REST paging and the reviewer-login match are the real ones.
"""

import subprocess
from pathlib import Path

from tests._fake_github import FakeReviewPoster
from tests._helpers import REPO_ROOT, reviewer_marker

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "post-withheld-read.sh"
WITHHELD = reviewer_marker("READ_WITHHELD_MARKER")
REASON = "The diff needs 9 read legs, and an author whose association is NONE may start at most 4.\n"


def _run(github: FakeReviewPoster, pr_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **github.env,
            "GH_REPO": github.repo,
            "PR": str(github.pr),
            "PR_INPUT_DIR": str(pr_dir),
            "RETRY_BASE_DELAY": "0",
        },
    )


def _pr_dir(tmp_path: Path, reason: str | None = REASON) -> Path:
    pr_dir = tmp_path / "pr-input"
    pr_dir.mkdir()
    if reason is not None:
        (pr_dir / "withheld.txt").write_text(reason, encoding="utf-8")
    return pr_dir


def test_a_withheld_read_posts_one_review_naming_why(tmp_path: Path) -> None:
    with FakeReviewPoster(tmp_path) as github:
        proc = _run(github, _pr_dir(tmp_path))
        assert proc.returncode == 0, proc.stderr
        [review] = github.of_kind("review")
    assert review["event"] == "COMMENT"
    assert WITHHELD in review["body"]
    assert REASON in review["body"]
    # It claims no read and no head, so it spends no budget.
    assert reviewer_marker("WHOLE_DIFF_READ_MARKER") not in review["body"]
    assert "<!-- review-coverage " not in review["body"]


def test_a_second_withheld_push_posts_nothing_more(tmp_path: Path) -> None:
    with FakeReviewPoster(tmp_path) as github:
        pr_dir = _pr_dir(tmp_path)
        assert _run(github, pr_dir).returncode == 0
        proc = _run(github, pr_dir)
        assert proc.returncode == 0, proc.stderr
        assert len(github.of_kind("review")) == 1


def test_a_withheld_read_after_another_review_still_posts(tmp_path: Path) -> None:
    """The pair: only a withheld notice as the NEWEST review suppresses a post."""
    with FakeReviewPoster(tmp_path) as github:
        github.posted.append(("review", {"body": "an earlier read"}))
        proc = _run(github, _pr_dir(tmp_path))
        assert proc.returncode == 0, proc.stderr
        assert len(github.of_kind("review")) == 2


def test_a_missing_reason_fails_loud(tmp_path: Path) -> None:
    with FakeReviewPoster(tmp_path) as github:
        proc = _run(github, _pr_dir(tmp_path, reason=None))
        assert proc.returncode != 0
        assert "withheld.txt" in proc.stderr
        assert github.posted == []
