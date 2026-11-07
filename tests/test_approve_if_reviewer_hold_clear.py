"""The hold-clear script decides whether an automated reviewer's hold may be
lifted without a human, so its refusals matter more than its successes.

The `gh` stub here RUNS the script's own `--jq` filters over canned GraphQL
responses rather than returning pre-filtered output. That is deliberate: both
safety properties — never dismiss a review this bot did not write, and never
count a Resolve click with no written reply as a clearing signal — live
entirely inside those filters — as does the scoping that keeps an earlier hold
cycle's resolution from vouching for the current one — and a stub that ignored
`--jq` would report them working while testing nothing.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers import reviewer_marker

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
SCRIPT = REPO_ROOT / ".github" / "scripts" / "approve-if-reviewer-hold-clear.sh"

BOT = "github-actions"
AUTHOR = "pr-author"
OTHER = "a-human"


CYCLE_1 = "2026-01-01T00:00:00Z"
CYCLE_2 = "2026-02-01T00:00:00Z"


def thread(
    author: str = BOT,
    resolved: bool = True,
    resolved_by: str | None = OTHER,
    opened_by_review_at: str = CYCLE_1,
    body: str = "a finding",
    replies: int = 1,
) -> dict:
    """One review thread. `resolved_by` is the login GitHub records on Resolve;
    it is null on an unresolved thread, and on a resolved one whose resolver the
    token cannot see. `opened_by_review_at` is the `submittedAt` of the review
    that opened the thread, which is what ties the thread to one hold cycle.
    `replies` counts the comments after the root, which GitHub reports in
    `comments.totalCount` whatever page size the query asked for."""
    by = {"login": resolved_by} if resolved and resolved_by else None
    return {
        "isResolved": resolved,
        "resolvedBy": by,
        "comments": {
            "totalCount": 1 + replies,
            "nodes": [
                {
                    "author": {"login": author},
                    "body": body,
                    "pullRequestReview": {"submittedAt": opened_by_review_at},
                }
            ],
        },
    }


def review(
    state: str,
    author: str = BOT,
    at: str = CYCLE_1,
    rid: int = 1,
    body: str = "Automated review.",
) -> dict:
    """One review node. `body=""` is the review GitHub synthesizes around a
    standalone review-comment POST, which is how a hold thread is opened."""
    return {
        "databaseId": rid,
        "author": {"login": author},
        "state": state,
        "body": body,
        "submittedAt": at,
    }


def graphql_payloads(threads: list[dict], reviews: list[dict]) -> tuple[str, str]:
    page = {"hasNextPage": False, "endCursor": None}
    t = {
        "data": {
            "repository": {
                "pullRequest": {"reviewThreads": {"pageInfo": page, "nodes": threads}}
            }
        }
    }
    r = {
        "data": {
            "repository": {
                "pullRequest": {"reviews": {"pageInfo": page, "nodes": reviews}}
            }
        }
    }
    return json.dumps(t), json.dumps(r)


ACTIONS_TOKEN = "actions-token"


def run(
    tmp_path: Path,
    *,
    threads: list[dict],
    reviews: list[dict],
    approve_error: str | None = None,
    dismiss_error: str | None = None,
    rungs: dict[str, str] | None = None,
    quotas: dict[str, int] | None = None,
):
    """Drive the script with a `gh` that executes its real --jq filters.

    `rungs` maps ladder env var -> credential; `quotas` maps credential ->
    requests remaining, where a negative value makes the probe itself fail (a
    revoked token) rather than report zero.
    """
    rungs = {"GH_TOKEN_ACTIONS": ACTIONS_TOKEN} if rungs is None else rungs
    quotas = {ACTIONS_TOKEN: 5000} if quotas is None else quotas
    threads_json, reviews_json = graphql_payloads(threads, reviews)
    (tmp_path / "threads.json").write_text(threads_json)
    (tmp_path / "reviews.json").write_text(reviews_json)
    (tmp_path / "quotas.json").write_text(json.dumps(quotas))
    log = tmp_path / "gh-calls.txt"

    def arm(err: str | None) -> str:
        if err is None:
            return "exit 0"
        return 'printf "%s\\n" ' + json.dumps(err) + " >&2; exit 1"

    approve_arm = arm(approve_error)
    dismiss_arm = arm(dismiss_error)

    # The stub picks the fixture by inspecting the query text the script passed,
    # then applies the script's own --jq to it, exactly as `gh api graphql` would.
    stub = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
# The quota probe answers for whichever credential the ladder is currently
# holding, and runs the ladder's real --jq so its core/graphql min is exercised.
if [[ "$1" == "api" && "$2" == "rate_limit" ]]; then
  filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jq) filter="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  left="$(jq -r --arg t "${{GH_TOKEN:-}}" '.[$t] // 0' "{tmp_path}/quotas.json")"
  # A negative budget stands for a credential whose probe fails outright.
  if [[ "$left" -lt 0 ]]; then exit 1; fi
  printf '{{"resources":{{"core":{{"remaining":%s}},"graphql":{{"remaining":%s}}}}}}' "$left" "$left" | jq -r "$filter"
  exit 0
fi
# Any real call spends quota, so a credential with none left fails the way the
# live API fails — this is what took the step down before the ladder existed.
left="$(jq -r --arg t "${{GH_TOKEN:-}}" '.[$t] // 0' "{tmp_path}/quotas.json")"
if [[ "$left" -le 0 ]]; then
  echo "gh: API rate limit already exceeded for user ID 3458070." >&2
  exit 1
fi
if [[ "$1" == "api" && "$2" == "graphql" ]]; then
  query=""; filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -f) [[ "$2" == query=* ]] && query="${{2#query=}}"; shift 2 ;;
      --jq) filter="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [[ "$query" == *reviewThreads* ]]; then src="{tmp_path}/threads.json"; else src="{tmp_path}/reviews.json"; fi
  jq -r "$filter" "$src"
  exit 0
fi
if [[ "$1" == "pr" && "$2" == "review" ]]; then
  {approve_arm}
fi
if [[ "$1" == "api" && "$2" == "--method" && "$3" == "PUT" ]]; then
  {dismiss_arm}
fi
exit 0
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(stub)
    (bin_dir / "gh").chmod(0o755)

    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "GH_REPO": "o/r",
            "PR": "438",
            **rungs,
        },
    )
    calls = log.read_text() if log.exists() else ""
    return res, calls


def dismissed(calls: str) -> bool:
    return "dismissals" in calls


CLEARED = {"threads": [thread(resolved=True)], "reviews": [review("CHANGES_REQUESTED")]}
SELF_APPROVAL = "GraphQL: Can not approve your own pull request (addPullRequestReview)"
ACTIONS_BARRED = "GitHub Actions is not permitted to approve pull requests"


SPENT_PAT = "spent-pat"


def test_a_spent_top_rung_falls_through_to_one_with_quota(tmp_path: Path):
    """THE regression. A configured-but-spent PAT used to take this step down at
    its first API call, with the Actions token below it still holding quota."""
    res, _ = run(
        tmp_path,
        **CLEARED,
        rungs={"GH_TOKEN_ORG_PAT": SPENT_PAT, "GH_TOKEN_ACTIONS": ACTIONS_TOKEN},
        quotas={SPENT_PAT: 0, ACTIONS_TOKEN: 5000},
    )
    assert res.returncode == 0, res.stderr
    assert "out of API quota; trying the next rung" in res.stderr
    assert "approved to satisfy the review gate" in res.stderr, (
        "the rung with quota must do the work the spent one could not"
    )


def test_a_rung_whose_probe_fails_is_stepped_over(tmp_path: Path):
    # A revoked or malformed PAT cannot report a quota at all; it is as unusable
    # as a spent one and must not strand the step either.
    res, _ = run(
        tmp_path,
        **CLEARED,
        rungs={"GH_TOKEN_ORG_PAT": "revoked", "GH_TOKEN_ACTIONS": ACTIONS_TOKEN},
        quotas={"revoked": -1, ACTIONS_TOKEN: 5000},
    )
    assert res.returncode == 0, res.stderr
    assert "its quota could not be read" in res.stderr
    assert "approved to satisfy the review gate" in res.stderr


def test_every_rung_spent_fails_loudly(tmp_path: Path):
    # Nothing left to spend anywhere is a real blocker, not something to swallow:
    # the hold stays up, so the failure has to be visible.
    res, _ = run(
        tmp_path,
        **CLEARED,
        rungs={"GH_TOKEN_ORG_PAT": SPENT_PAT, "GH_TOKEN_ACTIONS": ACTIONS_TOKEN},
        quotas={SPENT_PAT: 0, ACTIONS_TOKEN: 0},
    )
    assert res.returncode == 1
    assert "every configured GitHub credential is out of API quota" in res.stderr


def test_a_self_approval_refusal_dismisses_the_stale_hold(tmp_path: Path):
    res, calls = run(tmp_path, **CLEARED, approve_error=SELF_APPROVAL)
    assert res.returncode == 0, res.stderr
    assert dismissed(calls), (
        "the hold was clear and approval impossible — it must be dismissed"
    )
    assert "dismissed the reviewer's stale CHANGES_REQUESTED" in res.stderr


def test_an_actions_token_refusal_dismisses_the_stale_hold(tmp_path: Path):
    res, calls = run(tmp_path, **CLEARED, approve_error=ACTIONS_BARRED)
    assert res.returncode == 0, res.stderr
    assert dismissed(calls)


def test_a_successful_approval_dismisses_nothing(tmp_path: Path):
    # Approval already cleared the hold; dismissing on top would be gratuitous.
    res, calls = run(tmp_path, **CLEARED)
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls)


def test_a_humans_changes_requested_is_never_dismissed(tmp_path: Path):
    # THE safety case. A human hold must survive, and needs that human to lift it.
    res, calls = run(
        tmp_path,
        threads=[thread(resolved=True)],
        reviews=[
            review("COMMENTED", rid=1),
            review(
                "CHANGES_REQUESTED", author="a-human", at="2026-01-02T00:00:00Z", rid=2
            ),
        ],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls), (
        "a review this bot did not write must never be dismissed"
    )
    assert "no active CHANGES_REQUESTED" in res.stderr


def test_a_comment_only_hold_dismisses_nothing(tmp_path: Path):
    # A COMMENTED review does not block a merge, so there is nothing to dismiss.
    res, calls = run(
        tmp_path,
        threads=[thread(resolved=True)],
        reviews=[review("COMMENTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls)
    assert "does not block a merge" in res.stderr


def test_the_newest_bot_changes_requested_is_the_one_dismissed(tmp_path: Path):
    # A CHANGES_REQUESTED keeps blocking until dismissed or superseded by an
    # APPROVED from the same reviewer — a later COMMENTED does not clear it. So
    # the blocking review is routinely NOT the reviewer's latest review.
    res, calls = run(
        tmp_path,
        threads=[thread(resolved=True, opened_by_review_at="2026-01-04T00:00:00Z")],
        reviews=[
            review("CHANGES_REQUESTED", at="2026-01-01T00:00:00Z", rid=11),
            review("CHANGES_REQUESTED", at="2026-01-03T00:00:00Z", rid=33),
            review("COMMENTED", at="2026-01-04T00:00:00Z", rid=44),
        ],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert "/reviews/33/dismissals" in calls
    assert "/reviews/11/dismissals" not in calls


def test_a_failing_dismissal_exits_non_zero(tmp_path: Path):
    # Unlike the approval refusals, a failed dismissal is not structural — nothing
    # about this PR makes it permanently impossible, so it must be seen.
    res, _ = run(
        tmp_path,
        **CLEARED,
        approve_error=SELF_APPROVAL,
        dismiss_error="HTTP 403: Resource not accessible",
    )
    assert res.returncode != 0
    assert "failed to dismiss" in res.stderr


def test_an_unresolved_thread_blocks_both_approval_and_dismissal(tmp_path: Path):
    # The hold is live. Nothing may clear it — this is the precondition the whole
    # dismissal path rests on.
    res, calls = run(
        tmp_path,
        threads=[thread(resolved=False)],
        reviews=[review("CHANGES_REQUESTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0
    assert not dismissed(calls)
    assert "pr review" not in calls, "a live hold must not even attempt an approval"
    assert "still hold; not approving" in res.stderr


@pytest.mark.parametrize("state", ["APPROVED", "DISMISSED"])
def test_a_reviewer_not_holding_dismisses_nothing(tmp_path: Path, state: str):
    res, calls = run(
        tmp_path,
        threads=[thread(resolved=True)],
        reviews=[review(state)],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0
    assert not dismissed(calls)
    assert "no live hold to clear" in res.stderr


NOT_SETTLED = "was resolved after a reply"


@pytest.mark.parametrize("resolver", [AUTHOR, OTHER, None])
def test_a_silent_resolve_clears_nothing(tmp_path: Path, resolver: str | None):
    """THE written-record case. A Resolve click with no reply leaves no record of
    who read the diff or what they found, whoever clicked it."""
    res, calls = run(
        tmp_path,
        threads=[thread(resolved_by=resolver, replies=0)],
        reviews=[review("CHANGES_REQUESTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls)
    assert "pr review" not in calls, "a silent resolve must mint no approval"
    assert NOT_SETTLED in res.stderr


@pytest.mark.parametrize("resolver", [AUTHOR, OTHER, None])
def test_a_resolve_after_a_reply_clears_whoever_resolved(
    tmp_path: Path, resolver: str | None
):
    """The pair. The agent that drives the PR posts under the author's account,
    so the author's resolve after a reply must clear. GitHub records no resolver
    when the token cannot see it; the reply is the record, so that clears too."""
    res, calls = run(
        tmp_path,
        threads=[thread(resolved_by=resolver, replies=1)],
        reviews=[review("CHANGES_REQUESTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert dismissed(calls)


@pytest.mark.parametrize("resolver", [BOT, "GitHub-Actions", "github-actions[bot]"])
def test_the_reviewer_resolving_its_own_thread_clears_nothing(
    tmp_path: Path, resolver: str
):
    # The reviewer may not settle its own hold, in any spelling of its login.
    res, calls = run(
        tmp_path,
        threads=[thread(resolved_by=resolver, replies=1)],
        reviews=[review("CHANGES_REQUESTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls)
    assert NOT_SETTLED in res.stderr


def test_one_settled_thread_clears_the_hold(tmp_path: Path):
    # The other threads may be resolved in silence; one written verdict on a
    # thread of this hold is the record the approval rests on.
    res, calls = run(
        tmp_path,
        threads=[
            thread(resolved_by=AUTHOR, replies=0),
            thread(resolved_by=AUTHOR, replies=2),
        ],
        reviews=[review("CHANGES_REQUESTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert dismissed(calls)


@pytest.mark.parametrize(
    ("replies", "holds"), [(0, True), (1, False)], ids=["silent", "answered"]
)
def test_a_hold_thread_the_author_resolved_holds_until_answered(
    tmp_path: Path, replies: int, holds: bool
):
    # A hold thread stands in for a read, so even beside a settled finding it
    # holds until somebody replies to it. The merge gate reads the same
    # predicate, so the two agree.
    marker = reviewer_marker("HOLD_THREAD_MARKER")
    res, calls = run(
        tmp_path,
        threads=[
            thread(
                resolved_by=AUTHOR,
                body=f"files went unread\n{marker}",
                replies=replies,
            ),
            thread(resolved_by=AUTHOR, replies=1),
        ],
        reviews=[review("CHANGES_REQUESTED")],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert dismissed(calls) is not holds
    assert ("1 reviewer thread(s) still hold" in res.stderr) is holds


def test_an_earlier_cycles_settled_thread_cannot_clear_a_later_hold(
    tmp_path: Path,
):
    """Threads outlive the review that opened them. Somebody answered and resolved
    a cycle-1 thread; the reviewer then held again and every cycle-2 thread was
    resolved in silence. Counting cycle 1's answer would clear a hold nobody
    answered."""
    res, calls = run(
        tmp_path,
        threads=[
            thread(resolved_by=AUTHOR, opened_by_review_at=CYCLE_1, replies=1),
            thread(resolved_by=AUTHOR, opened_by_review_at=CYCLE_2, replies=0),
        ],
        reviews=[
            review("CHANGES_REQUESTED", at=CYCLE_1, rid=1),
            review("CHANGES_REQUESTED", at=CYCLE_2, rid=2),
        ],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls)
    assert "pr review" not in calls, "a stale answer must mint no approval"
    assert NOT_SETTLED in res.stderr


def test_a_later_hold_that_opened_no_thread_clears_on_no_old_thread(tmp_path: Path):
    # The reviewer held again in a review body alone. Cycle 1's threads are all
    # resolved after a reply, but none of them answers the new finding.
    res, calls = run(
        tmp_path,
        threads=[thread(resolved_by=OTHER, opened_by_review_at=CYCLE_1)],
        reviews=[
            review("CHANGES_REQUESTED", at=CYCLE_1, rid=1),
            review("CHANGES_REQUESTED", at=CYCLE_2, rid=2),
        ],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert not dismissed(calls)
    assert "opened no thread" in res.stderr


@pytest.mark.parametrize("earlier", [[], [CYCLE_1]], ids=["first-hold", "second-hold"])
def test_a_hold_thread_posted_before_its_notice_review_belongs_to_that_hold(
    tmp_path: Path, earlier: list[str]
):
    """The degraded path opens its finding threads FIRST, as standalone
    comments, and posts the summary review after them. GitHub wraps each comment
    in a body-less review that is OLDER than the summary. Timing alone would put
    the thread outside its own hold, and nothing could clear it."""
    thread_at, notice_at = "2026-03-01T00:00:00Z", "2026-03-01T00:00:05Z"
    res, calls = run(
        tmp_path,
        threads=[thread(resolved_by=OTHER, opened_by_review_at=thread_at)],
        reviews=[
            *[review("COMMENTED", at=at, rid=9) for at in earlier],
            review("COMMENTED", at=thread_at, rid=10, body=""),
            review("COMMENTED", at=notice_at, rid=11),
        ],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert "opened no thread" not in res.stderr, res.stderr
    assert "pr review" in calls, "the cleared hold must reach the approval"


def test_the_current_holds_own_thread_still_clears(tmp_path: Path):
    # Non-vacuity for the two refusals above: the same two-cycle shape clears as
    # soon as the answered thread belongs to the LATEST hold.
    res, calls = run(
        tmp_path,
        threads=[
            thread(resolved_by=AUTHOR, opened_by_review_at=CYCLE_1, replies=0),
            thread(resolved_by=AUTHOR, opened_by_review_at=CYCLE_2, replies=1),
        ],
        reviews=[
            review("CHANGES_REQUESTED", at=CYCLE_1, rid=1),
            review("CHANGES_REQUESTED", at=CYCLE_2, rid=2),
        ],
        approve_error=SELF_APPROVAL,
    )
    assert res.returncode == 0, res.stderr
    assert dismissed(calls)
