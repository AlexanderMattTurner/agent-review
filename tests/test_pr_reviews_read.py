"""Behavioral tests for .github/reviewer/lib/pr-reviews.bash — the ONE PR review
read every review-owing step goes through: the GraphQL document, the reviewer
filter, and the latest-by-submittedAt fold.

Nothing here is stubbed: the real helpers are sourced into bash and the real
`gh` walks a localhost GitHub (FakePRReviews) that serves ONE review per page.
That boundary is the point. The read's correctness is a PAGINATION property —
page one of `reviews` is the OLDEST page — so a `gh` stub answering one page
with no cursor would report that property from this file's own belief instead
of from gh. The server also refuses a query that binds no cursor, so a
`REVIEWS_QUERY` that lost its `$endCursor` reds every test here.

Every other consumer of this library stubs `gh` at the argv level, because each
drives a whole script whose other reads have no server. This file is where the
shared read itself meets the real client.
"""

# covers: .github/reviewer/lib/pr-reviews.bash

import json
import subprocess

import pytest

from tests._fake_github import FakePRReviews
from tests._helpers import REPO_ROOT

LIB = REPO_ROOT / ".github" / "reviewer" / "lib" / "pr-reviews.bash"

# The bot the reviewer posts as. GraphQL returns an app bot's login WITHOUT the
# REST `[bot]` suffix, which is why the server's nodes carry the bare form.
REVIEWER = "github-actions"

# The body-less COMMENTED review GitHub wraps around a standalone review-comment
# POST — authored by the reviewer bot, because that is who posts the comment.
SYNTHESIZED = {"state": "COMMENTED", "body": ""}


@pytest.fixture
def github(tmp_path):
    with FakePRReviews(tmp_path) as server:
        yield server


def _call(
    server: FakePRReviews, snippet: str, **env: str
) -> subprocess.CompletedProcess:
    """Run one read against the reviews this server holds. The snippet gets the
    owner, name and PR number as "$2", "$3" and "$4"; `env` adds environment the
    library reads, such as READS_MARKED_FROM."""
    owner, name = server.repo.split("/")
    return subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "$1"; {snippet}',
            "_",
            str(LIB),
            owner,
            name,
            str(server.pr),
        ],
        capture_output=True,
        text=True,
        env={
            **server.env,
            "REVIEWER_LOGIN_BARE": REVIEWER,
            # A failing read must not sleep out the real backoff; the ladder
            # itself still runs, so the exhausted-ladder path is the one
            # exercised.
            "RETRY_BASE_DELAY": "0",
            **env,
        },
    )


def _ndjson(server: FakePRReviews) -> list[dict]:
    proc = _call(server, 'reviewer_reviews_ndjson "$2" "$3" "$4"')
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _latest(server: FakePRReviews) -> str:
    """What both consumers compute: the budget-spending reads, folded to the
    newest one. `real_reviewer_reviews` and `latest_of_reviews` are separate so a
    caller can count the same walk it folds."""
    proc = _call(server, 'real_reviewer_reviews "$2" "$3" "$4" | latest_of_reviews')
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _spent(server: FakePRReviews, **env: str) -> int:
    """How many reads the PR has spent — the count decide-pr-review-trigger.sh
    compares against `max-reviews-per-pr`."""
    proc = _call(server, 'real_reviewer_reviews "$2" "$3" "$4" | jq -rs length', **env)
    assert proc.returncode == 0, proc.stderr
    return int(proc.stdout.strip())


def test_the_read_walks_every_page_of_the_shared_query(github):
    """gh walks this query with a cursor. The server serves one review per page
    and page one is the OLDEST, so the only qualifying review sits on page
    three: a read that stopped at page one reports an unreviewed PR.

    The server refuses a query binding no `after:` cursor, so a `REVIEWS_QUERY`
    that dropped `$endCursor` reds this file rather than passing a check for the
    `--paginate` flag that a cursor-less query carries just as happily.
    """
    github.add_review(**SYNTHESIZED, submitted_at="2026-07-01T00:00:00Z")
    github.add_review(login="a-human", submitted_at="2026-07-02T00:00:00Z")
    github.add_review(body="the real one", submitted_at="2026-07-03T00:00:00Z")
    assert [r["body"] for r in _ndjson(github)] == ["the real one"]
    assert github.paths("POST").count("/api/graphql") == 3, github.requests


def test_the_fold_picks_the_latest_by_submitted_at_across_pages(github):
    """gh emits one page's --jq output after another, so the newest review is on
    the LAST page and a fold picking by array order would answer the oldest.
    The submittedAt order here is deliberately not the page order."""
    github.add_review(body=_read("oldest"), submitted_at="2026-07-01T00:00:00Z")
    github.add_review(body=_read("newest"), submitted_at="2026-07-09T00:00:00Z")
    github.add_review(body=_read("middle"), submitted_at="2026-07-05T00:00:00Z")
    assert json.loads(_latest(github))["body"].startswith("newest")


def test_a_body_less_review_comment_is_not_a_review(github):
    """THE regression the shared filter exists for: a standalone review comment
    by the reviewer bot is wrapped in a body-less COMMENTED review, and counting
    one satisfies a reviewed-at-all condition vacuously."""
    github.add_review(**SYNTHESIZED)
    assert _ndjson(github) == []
    assert _latest(github) == ""


def test_another_actors_review_is_not_the_reviewers(github):
    github.add_review(login="a-human")
    assert _ndjson(github) == []


@pytest.mark.parametrize("state", ["COMMENTED", "APPROVED", "CHANGES_REQUESTED"])
def test_every_verdict_the_reviewer_posts_is_read(github, state):
    """The read reports WHAT the reviewer said; which states matter is each
    caller's question, so no state is dropped here."""
    github.add_review(state=state)
    assert [r["state"] for r in _ndjson(github)] == [state]


def test_a_dismissed_review_is_still_a_spent_read(github):
    """DISMISSED is deliberately NOT filtered here: a dismissed review still
    proves the one whole-diff read was spent, which is what
    decide-pr-review-trigger.sh and recheck-pr-review-owed.sh ask."""
    github.add_review(state="DISMISSED")
    assert [r["state"] for r in _ndjson(github)] == ["DISMISSED"]


def test_the_count_grows_with_every_real_review(github):
    """`max-reviews-per-pr` above 1 is a comparison against this count, so the
    count has to rise per review rather than saturate at "some review exists"."""
    github.add_review(body=_read("one"), submitted_at="2026-07-01T00:00:00Z")
    github.add_review(body=_read("two"), submitted_at="2026-07-02T00:00:00Z")
    github.add_review(**SYNTHESIZED, submitted_at="2026-07-03T00:00:00Z")
    assert _spent(github) == 2


def test_the_review_id_survives_as_a_string(github):
    """Review database ids exceed Int32, which GraphQL's Int-typed databaseId
    errors on — so the query reads fullDatabaseId and the read stringifies it."""
    github.add_review()
    assert _ndjson(github)[0]["reviewId"] == "4802416227"


def test_the_read_never_touches_the_rest_reviews_endpoint(github):
    """The one shared read is GraphQL. The server answers REST from the same
    reviews, so a read that went back to that endpoint is caught by the answer
    it computes rather than reported as an unmodelled path."""
    github.add_review()
    _ndjson(github)
    assert not [p for p in github.paths("GET") if "/reviews" in p], github.requests


def test_a_failed_read_is_non_zero_once_the_ladder_is_exhausted(github):
    """Can't-verify must reach the caller as a failure: a read that answered
    empty on an outage would report an unreviewed PR for a reviewed one."""
    github.add_review()
    github.fail_reads = True
    assert _call(github, 'reviewer_reviews_ndjson "$2" "$3" "$4"').returncode != 0


# The exact body auto-approve-skipped-pr.sh posts, read out of the ONE definition
# both it and the read share, so a renamed marker reds here rather than silently
# splitting producer from consumer.
def _lib_marker(name: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'source "$1"; printf %s "${name}"', "_", str(LIB)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, f"the library must define {name}"
    return proc.stdout


def _marker() -> str:
    return _lib_marker("AUTO_APPROVAL_MARKER")


def _read(body: str) -> str:
    """A review body stamped as a whole-diff read, the way post-pr-review.sh stamps
    it. Read out of the library so a renamed marker reds here."""
    return f"{body}\n\n{_lib_marker('WHOLE_DIFF_READ_MARKER')}"


def test_the_stand_in_approval_does_not_spend_the_one_read(github):
    """The auto-approve job posts its APPROVE with GITHUB_TOKEN, so it carries the
    reviewer's own login and a non-empty body. Counted as a review it latches the
    PR permanently unread: the reviewer's decide step answers "already reviewed"
    on every later event, so a PR that leaves the skip class — or that asks for a
    read with the `needs-auto-review` label — never gets its first pass."""
    github.add_review(
        state="APPROVED",
        body=f"{_marker()}\nAutomated approval: this PR type isn't Claude-reviewed.",
        submitted_at="2026-07-01T00:00:00Z",
    )
    assert _latest(github) == "", "a review nobody read does not spend the read"


def test_the_gate_s_read_still_counts_the_stand_in_approval(github):
    """The two questions differ. `reviewer_reviews_ndjson` answers whether a review
    EXISTS for the review-findings gate and the review-required ruleset, and the
    stand-in approval is exactly that review — the skipped PR strands on a red
    check without it."""
    body = f"{_marker()}\nAutomated approval: this PR type isn't Claude-reviewed."
    github.add_review(state="APPROVED", body=body, submitted_at="2026-07-01T00:00:00Z")
    assert [r["state"] for r in _ndjson(github)] == ["APPROVED"]


def test_a_real_review_after_a_stand_in_approval_still_spends_the_read(github):
    """Dropping the stand-in must not drop a real review that came after it —
    otherwise the marker turns every such PR into an unlimited review budget."""
    github.add_review(
        state="APPROVED",
        body=f"{_marker()}\nAutomated approval.",
        submitted_at="2026-07-01T00:00:00Z",
    )
    github.add_review(
        state="COMMENTED",
        body=_read("the real read"),
        submitted_at="2026-07-02T00:00:00Z",
    )
    assert json.loads(_latest(github))["body"].startswith("the real read")


def test_an_unmarked_bot_review_spends_no_read(github):
    """The filter SELECTS the read marker IN, so a review this bot posted without
    reading a diff costs nothing. A consumer repository posts several under the same
    identity — an approval once the reviewer's hold clears is the one that bites, and
    it carries no marker of its own for this reviewer to exclude. Counted as a read
    it eats one of the reads `max-reviews-per-pr: 2` paid for, silently."""
    github.add_review(
        state="APPROVED",
        body="Automated approval: the reviewer's hold is clear.",
        submitted_at="2026-07-01T00:00:00Z",
    )
    assert _spent(github) == 0
    assert _latest(github) == ""


def test_the_oversized_notice_spends_a_read(github):
    """A run that found the diff too large to read still spent the job — the
    checkout, the Node setup, the sanitizer install and the diff fetch. Excluded
    from the count it would re-run on every push forever, because the notice adds
    nothing the next decide can see."""
    github.add_review(
        state="COMMENTED",
        body=f"too large\n{_lib_marker('OVERSIZED_REVIEW_MARKER')}",
        submitted_at="2026-07-01T00:00:00Z",
    )
    assert _spent(github) == 1


def test_a_review_older_than_the_caller_s_cutover_spends_a_read(github):
    """A consumer that has just bumped its pin holds pull requests reviewed by the
    OLDER reviewer, which stamped nothing. On the stamp alone each of them reads as
    never reviewed, and the next push buys a second whole-diff read — the largest
    single cost this reviewer can incur, and one the caller already paid once.
    READS_MARKED_FROM is that caller's cutover moment."""
    github.add_review(
        state="COMMENTED",
        body="## Review\n\nfindings from the reviewer that stamped nothing",
        submitted_at="2026-07-01T00:00:00Z",
    )
    assert _spent(github, READS_MARKED_FROM="2026-09-10T00:00:00Z") == 1
    assert _spent(github) == 0, "the term is the caller's to set, never a default"


def test_a_review_after_the_cutover_still_needs_its_stamp(github):
    """The term retires itself: past the cutover the stamp decides again, so the
    stand-in approval a consumer posts under this same bot identity keeps costing
    nothing."""
    github.add_review(
        state="APPROVED",
        body=f"{_marker()}\nAutomated approval.",
        submitted_at="2026-09-11T00:00:00Z",
    )
    assert _spent(github, READS_MARKED_FROM="2026-09-10T00:00:00Z") == 0
