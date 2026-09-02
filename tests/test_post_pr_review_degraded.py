"""post-pr-review.sh's degraded path — what happens when GitHub refuses the
structured review as one payload.

The reviews API is all-or-nothing, so one comment it will not take rejects a
read that cost tens of dollars and half an hour of fan-out. The degraded path
must (1) post each finding as its own review comment, so one bad anchor costs
one finding, (2) record the read as a real COMMENT review, which is what stops
decide-pr-review-trigger.sh buying the whole read again on the next push, and
(3) raise a human-review hold when a finding was still lost, so
review_findings_gate.py cannot green on findings nobody can resolve.

Nothing here is stubbed: the real `post-pr-review.sh` runs the real
`post-pr-review.mjs` over a real review.json and diff, and the real `gh` posts
to a localhost GitHub (FakeReviewPoster) that answers the 422. That boundary is
the point — a `gh` stub would be this file's own belief about how a refusal
reaches the script, which is exactly the belief under test.
"""

import json
import subprocess
from pathlib import Path

from tests._fake_github import FakeReviewPoster
from tests._helpers import REPO_ROOT, reviewer_marker

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "post-pr-review.sh"

HEAD_SHA = "cafef00dcafef00dcafef00dcafef00dcafef00d"
DEGRADED_MARKER = "<!-- degraded-review -->"

# Two added lines per file, so both findings below anchor for real.
DIFF = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,0 +1,2 @@
+one = 1
+two = 2
diff --git a/src/b.py b/src/b.py
--- a/src/b.py
+++ b/src/b.py
@@ -9,0 +10,2 @@
+ten = 10
+eleven = 11
"""

REVIEW = {
    "summary": "Two findings.",
    "findings": [
        {
            "path": "src/a.py",
            "line": 1,
            "severity": "blocking",
            "title": "a leaks",
            "body": "close it",
        },
        {
            "path": "src/b.py",
            "line": 10,
            "severity": "warning",
            "title": "b races",
            "body": "lock it",
        },
    ],
}


def _pr_input(tmp_path: Path, review: dict | None = None) -> Path:
    """The reviewer's output dir, as the review job leaves it."""
    pr_dir = tmp_path / "pr-input"
    pr_dir.mkdir(exist_ok=True)
    (pr_dir / "review.json").write_text(json.dumps(review or REVIEW), encoding="utf-8")
    (pr_dir / "diff.txt").write_text(DIFF, encoding="utf-8")
    return pr_dir


def _run(
    github: FakeReviewPoster,
    pr_dir: Path,
    *,
    head_sha: str | None = HEAD_SHA,
    salvage_body_limit: int | None = None,
) -> subprocess.CompletedProcess:
    env = {
        **github.env,
        "GH_REPO": github.repo,
        "PR": str(github.pr),
        "PR_INPUT_DIR": str(pr_dir),
        "RETRY_BASE_DELAY": "0",  # a refused POST must not sleep out the backoff
    }
    if salvage_body_limit is not None:
        env["SALVAGE_BODY_LIMIT"] = str(salvage_body_limit)
    if head_sha is not None:
        env["HEAD_SHA"] = head_sha
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


#: What makes a posted review count against MAX_REVIEWS_PER_PR.
READ_MARKER = reviewer_marker("WHOLE_DIFF_READ_MARKER")


def _holds(github: FakeReviewPoster) -> list[dict]:
    """The needs-a-human finding threads — file-level, so no line can refuse them."""
    return [c for c in github.of_kind("comment") if c.get("subject_type") == "file"]


def test_an_accepted_review_posts_once_and_degrades_to_nothing(tmp_path):
    with FakeReviewPoster(tmp_path) as github:
        proc = _run(github, _pr_input(tmp_path))
        assert proc.returncode == 0, proc.stderr
        reviews = github.of_kind("review")
        assert len(reviews) == 1
        assert [c["path"] for c in reviews[0]["comments"]] == ["src/a.py", "src/b.py"]
        assert github.of_kind("comment") == []


def test_both_post_paths_stamp_the_review_that_records_the_read(tmp_path):
    """The marker is what makes this review spend one of `max-reviews-per-pr`'s
    reads. Missing it, decide-pr-review-trigger.sh counts the PR unread and the next
    push buys the whole read again — the same defect the degraded path closes, by
    another route. Both paths post exactly one review, and it is the one stamped."""
    for refuse in (False, True):
        with FakeReviewPoster(tmp_path / f"refuse-{refuse}") as github:
            github.refuse_structured = refuse
            proc = _run(github, _pr_input(tmp_path / f"in-{refuse}"))
            assert proc.returncode == 0, proc.stderr
            reviews = github.of_kind("review")
            assert len(reviews) == 1
            assert READ_MARKER in reviews[0]["body"]


def test_a_refused_review_posts_every_finding_as_its_own_comment(tmp_path):
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        proc = _run(github, _pr_input(tmp_path))
        assert proc.returncode == 0, proc.stderr
        comments = github.of_kind("comment")
        assert [c["path"] for c in comments] == ["src/a.py", "src/b.py"]
        # The comment object rides over verbatim, so every value keeps the type
        # and coordinate the accepted payload would have carried.
        assert [c["line"] for c in comments] == [1, 10]
        assert all(c["commit_id"] == HEAD_SHA for c in comments)
        assert all(c["side"] == "RIGHT" for c in comments)
        assert "🔴 a leaks — close it" in comments[0]["body"]
        assert "<!-- severity: warning -->" in comments[1]["body"]


def test_a_refused_review_still_records_the_read_as_a_review(tmp_path):
    # Without this the PR reads as never reviewed: decide-pr-review-trigger.sh
    # buys the whole sharded read again, and review_findings_gate.py holds the
    # merge on a review sitting right there. This is the defect the path closes.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        _run(github, _pr_input(tmp_path))
        reviews = github.of_kind("review")
        assert len(reviews) == 1
        assert reviews[0]["event"] == "COMMENT"
        assert not reviews[0].get("comments")
        assert "Two findings." in reviews[0]["body"]
        assert DEGRADED_MARKER in reviews[0]["body"]


def test_no_finding_lost_means_no_human_hold(tmp_path):
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        _run(github, _pr_input(tmp_path))
        assert _holds(github) == []


def test_a_lost_finding_raises_a_human_review_hold(tmp_path):
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        proc = _run(github, _pr_input(tmp_path))
        assert proc.returncode == 0, proc.stderr
        holds = _holds(github)
        assert len(holds) == 1
        assert holds[0]["commit_id"] == HEAD_SHA
        assert holds[0]["path"] == "src/first.py"
        body = holds[0]["body"]
        assert "1 of this review's 2 findings could not be posted" in body
        assert "🔴" in body  # the gate's pre-marker icon fallback
        assert "<!-- severity: blocking -->" in body  # what reds the gate on it


def test_the_hold_lands_before_the_review_that_greens_the_gate(tmp_path):
    # The summary review satisfies the gate's reviewed-at-all leg, so a crash
    # between the two must leave the PR unreviewed (red, another read) rather
    # than reviewed with a lost finding holding nothing.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        _run(github, _pr_input(tmp_path))
        kinds = [k for k, _ in github.posted]
        assert kinds[-1] == "review", kinds
        assert "comment" in kinds[:-1]


def test_a_missing_head_sha_fails_loud_rather_than_recording_an_empty_read(tmp_path):
    # A review comment needs a commit to anchor to. Failing here leaves the PR
    # unreviewed, which the next push re-reads — never a review with no findings.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        proc = _run(github, _pr_input(tmp_path), head_sha=None)
        assert proc.returncode != 0
        assert "HEAD_SHA required" in proc.stderr
        assert github.posted == []


def test_the_rejection_reason_reaches_the_log(tmp_path):
    # gh's rendering of the refusal is the only record of WHY the read degraded.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        proc = _run(github, _pr_input(tmp_path))
        assert "HTTP 422" in proc.stderr


def test_a_refused_findings_own_text_reaches_the_log(tmp_path):
    # A refused finding has no thread and no artifact, and the hold sends a human
    # to the run log for it. gh spells the refusal as a bare "HTTP 422", which
    # names neither the finding nor its body, so the script must echo the payload.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        proc = _run(github, _pr_input(tmp_path))
        assert "its text survives only here" in proc.stderr
        assert "<!-- severity: warning -->" in proc.stderr
        assert "src/b.py" in proc.stderr


def test_the_summary_review_reports_the_posted_count_not_the_attempted_one(tmp_path):
    # On the exact run this line exists for — a refusal that lost findings — a
    # count of what was attempted tells the reader all N landed while the hold
    # says N-k did.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        _run(github, _pr_input(tmp_path))
        body = github.of_kind("review")[0]["body"]
        assert "1 of its 2 findings were posted" in body


def test_a_second_run_re_posts_nothing_once_a_degraded_run_completed(tmp_path):
    # The marker is what makes the degraded path idempotent per surface. Without
    # the read-back, a re-run of a job that already finished duplicates every
    # finding comment on the PR.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        _run(github, _pr_input(tmp_path))
        first = list(github.posted)
        assert first, "the first run must post something for this to mean anything"
        proc = _run(github, _pr_input(tmp_path))
        assert proc.returncode == 0, proc.stderr
        assert github.posted == first


def _salvaged(github: FakeReviewPoster) -> list[dict]:
    """The PR comments carrying the refused findings' own text."""
    return github.of_kind("issue_comment")


def test_a_refused_findings_text_reaches_the_pr_not_only_the_log(tmp_path):
    # The run log ages out and needs an Actions reader, so a hold that points
    # only there loses the finding on the day someone comes to read it.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        proc = _run(github, _pr_input(tmp_path))
        assert proc.returncode == 0, proc.stderr
        salvaged = _salvaged(github)
        assert len(salvaged) == 1
        body = salvaged[0]["body"]
        assert "src/b.py" in body
        assert "lock it" in body  # the finding's own text, not a summary of it
        assert "src/a.py" not in body  # the finding that DID get a thread


def test_the_hold_links_the_salvaged_text(tmp_path):
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        _run(github, _pr_input(tmp_path))
        assert "#issuecomment-" in _holds(github)[0]["body"]


def test_no_lost_finding_salvages_nothing(tmp_path):
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        _run(github, _pr_input(tmp_path))
        assert _salvaged(github) == []


def test_a_refused_salvage_still_records_the_read(tmp_path):
    # The salvage is a courtesy; the summary review is what stops the next push
    # buying the whole read again. Losing the first must never cost the second.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/b.py",)
        github.refuse_salvage = True
        proc = _run(github, _pr_input(tmp_path))
        assert proc.returncode == 0, proc.stderr
        assert len(github.of_kind("review")) == 1
        hold = _holds(github)[0]["body"]
        assert "run log" in hold  # no link to offer, so say where to look


def test_the_salvage_packs_into_parts_when_one_comment_would_overflow(tmp_path):
    # The fixture cannot reach the real 60000-byte limit, so the limit comes down
    # instead: without this every run takes the single-part path and the packing
    # loop — the most intricate code here — is never executed at all.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/a.py", "src/b.py")
        proc = _run(github, _pr_input(tmp_path), salvage_body_limit=120)
        assert proc.returncode == 0, proc.stderr
        bodies = [c["body"] for c in _salvaged(github)]
        assert len(bodies) == 2, bodies
        # Each finding lands in exactly one part, in the order the payload had them.
        assert [i for i, b in enumerate(bodies) if "close it" in b] == [0]
        assert [i for i, b in enumerate(bodies) if "lock it" in b] == [1]
        assert "[truncated" not in "".join(bodies)  # neither finding is over the limit
        hold = _holds(github)[0]["body"]
        for part in range(1, len(bodies) + 1):
            assert f"#issuecomment-{part}" in hold


def test_a_refused_part_says_so_rather_than_passing_for_posted(tmp_path):
    # The hold links what landed. A part GitHub refuses must be loud where it
    # happens, or the reader gets a list that looks complete and is not.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/a.py", "src/b.py")
        github.refuse_salvage = True
        proc = _run(github, _pr_input(tmp_path), salvage_body_limit=120)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.count("GitHub refused salvage part") == 2, proc.stderr
        assert len(github.of_kind("review")) == 1


def test_a_finding_over_the_limit_is_marked_where_it_was_cut(tmp_path):
    # Truncating beats dropping, but a body that just stops mid-sentence gives the
    # reader no cue that the rest is in the run log.
    long_finding = {
        "summary": "One long finding.",
        "findings": [
            {
                "path": "src/a.py",
                "line": 1,
                "severity": "blocking",
                "title": "a leaks",
                # Line-broken: the cut drops the last line, so a single-line body
                # would leave nothing but the marker.
                "body": "\n".join(["close it — 🔴 " + "x" * 60] * 40),
            }
        ],
    }
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/a.py",)
        proc = _run(
            github,
            _pr_input(tmp_path, long_finding),
            salvage_body_limit=1000,
        )
        assert proc.returncode == 0, proc.stderr
        body = _salvaged(github)[0]["body"]
        assert "[truncated" in body
        assert "src/a.py" in body  # the reader still learns which finding it is
        assert len(body.encode()) < 1200  # cut, not the whole 3000-byte finding


def test_a_partly_refused_salvage_keeps_the_run_log_fallback(tmp_path):
    # The links read as the complete set. When one part was refused, the hold has
    # to say so, or the human it stops reads a list that looks whole and is not.
    with FakeReviewPoster(tmp_path) as github:
        github.refuse_structured = True
        github.refuse_comment_paths = ("src/a.py", "src/b.py")
        github.refuse_salvage_parts = (1,)
        proc = _run(github, _pr_input(tmp_path), salvage_body_limit=120)
        assert proc.returncode == 0, proc.stderr
        salvaged = _salvaged(github)
        assert len(salvaged) == 1  # part 2; part 1 was refused
        hold = _holds(github)[0]["body"]
        assert "#issuecomment-" in hold  # the part that landed is still linked
        assert "run log only" in hold  # and the refused one is not passed off as posted
