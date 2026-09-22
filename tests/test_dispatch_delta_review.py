"""Behavioral tests for .github/reviewer/dispatch-delta-review.sh — the filter
that decides when a pull request has collected enough unread pushes to be worth
ONE more model read.

It is the only place a review is started outside a push event, so what matters
is when it refuses. The `gh` stub here runs the script's own `--jq` filters over
canned GraphQL payloads (the reviews walk and the review threads), because the
coverage stamp and the gating-severity predicate both live inside those filters.
Everything else is asserted from what the script actually did: whether
`gh workflow run` was called, and with which arguments.

Every dispatching case is paired with a refusing one over inputs that differ by
one field, so no test passes against a script that dispatches unconditionally.
"""

# covers: .github/reviewer/dispatch-delta-review.sh

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "dispatch-delta-review.sh"
LIB = REPO_ROOT / ".github" / "reviewer" / "lib" / "pr-reviews.bash"
SEVERITIES = REPO_ROOT / "config" / "review-severities.json"

PR = 42
COVERED = "1111111111111111111111111111111111111111"
PUSHED = "2222222222222222222222222222222222222222"
GATE = "Automated review posted"
REVIEW_WORKFLOW = "claude-review.yaml"
COVERED_AT = "2026-01-01T00:00:00Z"


def _stamped(head: str, *, read: str = "first", scope: str = "whole") -> str:
    """A review body as post-pr-review.sh leaves it, built by the library's own
    producer so a stamp-format change reds here instead of passing silently."""
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1";'
            ' printf "%s\\n" "$WHOLE_DIFF_READ_MARKER";'
            ' coverage_stamp "$2" ba5eba5e 0e0e0e0e "$3" "$4"',
            "_",
            str(LIB),
            head,
            read,
            scope,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _review(
    head: str, *, read: str = "first", at: str = COVERED_AT, scope: str = "whole"
) -> dict:
    return {
        "state": "COMMENTED",
        "body": f"Automated review.\n{_stamped(head, read=read, scope=scope)}",
        "author": {"login": "github-actions"},
        "submittedAt": at,
    }


def check(name: str, *, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
    }


def status_context(name: str, *, state: str = "SUCCESS") -> dict:
    return {"__typename": "StatusContext", "context": name, "state": state}


def thread(body: str, *, resolved: bool = False) -> dict:
    return {
        "isResolved": resolved,
        "path": "a.py",
        "line": 1,
        "comments": {"nodes": [{"body": body, "author": {"login": "github-actions"}}]},
    }


def dispatch(
    tmp_path: Path,
    *,
    reviews: list[dict] | None = None,
    reviews_after: list[dict] | None = None,
    threads: list[dict] | None = None,
    rollup: list[dict] | None = None,
    head: str = PUSHED,
    state: str = "OPEN",
    draft: bool = False,
    runs: list[dict] | None = None,
    max_delta_reviews: str = "1",
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the real script against a `gh` stub; return (proc, its argv lines).

    The rollup defaults to one green check, so a case that does not name checks
    is a ready pull request and the refusals below are the one thing it varies.

    `reviews_after` answers every read of the reviews but the first, which is how
    a review landing mid-decision is driven.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)

    def _reviews_payload(nodes: list[dict]) -> str:
        return json.dumps(
            {"data": {"repository": {"pullRequest": {"reviews": {"nodes": nodes}}}}}
        )

    (tmp_path / "reviews.json").write_text(
        _reviews_payload(reviews or []), encoding="utf-8"
    )
    if reviews_after is not None:
        (tmp_path / "reviews-after.json").write_text(
            _reviews_payload(reviews_after), encoding="utf-8"
        )
    (tmp_path / "threads.json").write_text(
        json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {"reviewThreads": {"nodes": threads or []}}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pr.json").write_text(
        json.dumps(
            {
                "state": state,
                "isDraft": draft,
                "headRefOid": head,
                "statusCheckRollup": (
                    [check("Unit tests")] if rollup is None else rollup
                ),
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "runs.json").write_text(
        json.dumps({"workflow_runs": runs or []}), encoding="utf-8"
    )
    log = tmp_path / "gh-calls.txt"
    log.write_text("", encoding="utf-8")
    stub = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>"{log}"
subcommand="$1"
filter=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq) filter="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$filter" in
  *reviewThreads.nodes*) jq -r "$filter" "{tmp_path}/threads.json"; exit 0 ;;
  *reviews.nodes*)
    reads="$(cat "{tmp_path}/reviews-reads.txt" 2>/dev/null || printf 0)"
    printf '%s' "$((reads + 1))" >"{tmp_path}/reviews-reads.txt"
    src="{tmp_path}/reviews.json"
    if [[ "$reads" -ge 1 && -f "{tmp_path}/reviews-after.json" ]]; then
      src="{tmp_path}/reviews-after.json"
    fi
    jq -r "$filter" "$src"; exit 0 ;;
  .default_branch)       printf 'main\\n'; exit 0 ;;
esac
case "$subcommand" in
  pr)       cat "{tmp_path}/pr.json"; exit 0 ;;
  api)      jq -r "${{filter:-.}}" "{tmp_path}/runs.json" 2>/dev/null; exit 0 ;;
  workflow) exit 0 ;;
esac
exit 0
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(stub, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "GH_TOKEN": "t",
            "GH_REPO": "o/r",
            "PR": str(PR),
            "REVIEW_WORKFLOW": REVIEW_WORKFLOW,
            "SEVERITY_CONFIG": str(SEVERITIES),
            "GATE_CONTEXT": GATE,
            "MAX_DELTA_REVIEWS_PER_PR": max_delta_reviews,
            "RETRY_BASE_DELAY": "0",
            **(env or {}),
        },
    )
    assert proc.returncode == 0, proc.stderr
    return proc, log.read_text(encoding="utf-8").splitlines()


def _dispatched(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("workflow run")]


# ── The dispatching case, and the exact call it makes ─────────────────────────


def test_a_ready_pr_with_unread_pushes_asks_for_one_read(tmp_path: Path) -> None:
    """The case the script exists for: a review covers an older head, the PR is
    open and green, and nothing has read what the author pushed since."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)])
    assert _dispatched(calls) == [
        f"workflow run {REVIEW_WORKFLOW} --repo o/r --ref main -f pr={PR}"
    ], calls


def test_the_reviewed_head_is_never_re_read(tmp_path: Path) -> None:
    """The pair for the case above, differing only in the live head."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)], head=COVERED)
    assert _dispatched(calls) == []


# ── Readiness ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rollup",
    [
        pytest.param([check("Unit tests", status="IN_PROGRESS")], id="running"),
        pytest.param([check("Unit tests", conclusion="FAILURE")], id="failed-check"),
        pytest.param([check("Unit tests", status="QUEUED")], id="queued"),
        pytest.param(
            [status_context("coverage", state="PENDING")], id="pending-status"
        ),
    ],
)
def test_a_pr_still_moving_is_not_read(tmp_path: Path, rollup: list[dict]) -> None:
    """Readiness is how a burst of pushes coalesces with no timer: each push reds
    or re-queues the checks, so only the head that survives long enough to go
    green is ever read."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)], rollup=rollup)
    assert _dispatched(calls) == []


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED"], ids=str.lower)
def test_a_concluded_check_that_blocks_no_merge_is_ready(
    tmp_path: Path, conclusion: str
) -> None:
    """A skipped path-gated job and a neutral advisory check are both green as far
    as the merge box is concerned, so neither may hold the read back forever."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        rollup=[check("Unit tests", conclusion=conclusion)],
    )
    assert len(_dispatched(calls)) == 1, calls


def test_the_review_gate_being_red_never_blocks_the_read(tmp_path: Path) -> None:
    """THE deadlock this excludes: the gate is red exactly while this read is
    owed, so waiting for it would mean the read never starts and the gate never
    clears. Its red is the signal to read, not a reason to wait."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        rollup=[check("Unit tests"), check(GATE, conclusion="FAILURE")],
    )
    assert len(_dispatched(calls)) == 1, calls


# ── The other refusals ────────────────────────────────────────────────────────


def test_an_unresolved_gating_finding_holds_the_read_back(tmp_path: Path) -> None:
    """An open blocking thread names work the author has yet to push, so reading
    now would spend the one accumulated read on a head about to change."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        threads=[thread("<!-- severity: blocking -->\nleaks")],
    )
    assert _dispatched(calls) == []


def test_a_resolved_finding_leaves_the_read_owed(tmp_path: Path) -> None:
    """The pair: the same thread, resolved."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        threads=[thread("<!-- severity: blocking -->\nleaks", resolved=True)],
    )
    assert len(_dispatched(calls)) == 1, calls


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"state": "MERGED"}, id="merged"),
        pytest.param({"state": "CLOSED"}, id="closed"),
        pytest.param({"draft": True}, id="draft"),
    ],
)
def test_a_pr_nobody_is_about_to_merge_is_not_read(
    tmp_path: Path, kwargs: dict
) -> None:
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)], **kwargs)
    assert _dispatched(calls) == []


def test_a_pr_no_review_covers_waits_for_its_first_pass(tmp_path: Path) -> None:
    """The first read belongs to the push-driven reviewer and its own budget.
    Dispatching here would spend the accumulated budget on a pull request that
    has never been read at all."""
    _, calls = dispatch(tmp_path, reviews=[])
    assert _dispatched(calls) == []


def test_the_accumulated_budget_is_spent_exactly_once(tmp_path: Path) -> None:
    """One extra read per pull request, for its whole life: a second push after
    the accumulated read buys nothing, and the gate says so rather than claiming
    the head was read."""
    _, calls = dispatch(
        tmp_path,
        reviews=[
            _review(COVERED),
            _review(PUSHED, read="delta", at="2026-02-01T00:00:00Z"),
        ],
        head="3333333333333333333333333333333333333333",
    )
    assert _dispatched(calls) == []


def test_a_zero_budget_asks_for_nothing_and_reads_no_api(tmp_path: Path) -> None:
    """Turning the accumulated read off must cost nothing per sweep per PR."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)], max_delta_reviews="0")
    assert calls == []


def _failed_run() -> dict:
    return {
        "display_title": f"Claude reviewers — PR {PR}",
        "created_at": "2026-03-01T00:00:00Z",
        "status": "completed",
        "conclusion": "failure",
    }


def _posted_reviews(calls: list[str]) -> list[str]:
    return [c for c in calls if "pulls/42/reviews" in c and "-X POST" in c]


def test_giving_up_on_a_dying_read_spends_the_budget_out_loud(tmp_path: Path) -> None:
    """The deadlock, closed. The merge gate holds while the live head is past the
    covered one AND the accumulated budget still has room. A crashed read posts
    no review, so it advances no coverage and spends no budget, and the bound
    above then stops the retries — leaving the pull request unmergeable with
    nothing red to fix. So the give-up spends the budget itself.

    The stamp keeps the OLD covered head on purpose: nothing read the new one, so
    the gate must say the later pushes went unread rather than claim it read
    them."""
    _, calls = dispatch(
        tmp_path, reviews=[_review(COVERED)], runs=[_failed_run(), _failed_run()]
    )
    assert _dispatched(calls) == []
    posted = _posted_reviews(calls)
    assert len(posted) == 1, calls
    assert "read=delta" in posted[0], posted[0]
    assert "scope=failed" in posted[0], posted[0]
    assert f"head={COVERED}" in posted[0], posted[0]
    assert PUSHED not in posted[0], posted[0]
    # The notice releases the gate's hold, and a review posted with the workflow
    # GITHUB_TOKEN starts no workflow run — so nothing else would re-read it, and
    # the hold would stand until the next sweep.
    assert [c for c in calls if f"statuses/{PUSHED}" in c], calls


def _abandoned_notice(at: str = "2026-04-01T00:00:00Z") -> dict:
    return _review(COVERED, read="delta", at=at, scope="failed")


def test_a_read_already_abandoned_is_never_abandoned_a_second_time(
    tmp_path: Path,
) -> None:
    """The notice is a decision, so a later run must not make it again. Counted
    as one spent read, a budget of 2 leaves room, the failures are still in the
    window, and the script posts a second notice saying the same thing."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED), _abandoned_notice()],
        runs=[_failed_run(), _failed_run()],
        max_delta_reviews="2",
    )
    assert _dispatched(calls) == []
    assert _posted_reviews(calls) == [], calls


def test_a_budget_with_room_and_no_notice_still_abandons_the_read(
    tmp_path: Path,
) -> None:
    """The pair for the case above, differing only in whether a notice exists."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        runs=[_failed_run(), _failed_run()],
        max_delta_reviews="2",
    )
    assert len(_posted_reviews(calls)) == 1, calls


def test_a_review_landing_mid_decision_stops_the_notice(tmp_path: Path) -> None:
    """The state this decision rests on is minutes old by the time it posts. A
    read that landed meanwhile covers the live head, so the notice would abandon
    a pull request that was just reviewed — and abandonment is terminal."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        reviews_after=[
            _review(COVERED),
            _review(PUSHED, read="delta", at="2026-03-02T00:00:00Z"),
        ],
        runs=[_failed_run(), _failed_run()],
    )
    assert _posted_reviews(calls) == [], calls
    assert _dispatched(calls) == []


def test_a_read_that_has_failed_once_is_retried_and_spends_nothing(
    tmp_path: Path,
) -> None:
    """The pair for the case above, differing only in how many runs died. Below
    the bound the read is still coming, so spending its budget here would strand
    the pushes the retry is about to read."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)], runs=[_failed_run()])
    assert len(_dispatched(calls)) == 1, calls
    assert _posted_reviews(calls) == []


def test_another_prs_failures_do_not_stop_this_ones_read(tmp_path: Path) -> None:
    """The suffix match is what makes that bound per-PR. `PR 4` is a suffix of no
    run named `PR 42`, and `PR 42` is a suffix of no run named `PR 421`."""
    others = [
        {**_failed_run(), "display_title": f"Claude reviewers — PR {PR}{tail}"}
        for tail in ("1", "7")
    ]
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)], runs=others)
    assert len(_dispatched(calls)) == 1, calls


def test_a_failure_from_before_the_last_review_does_not_count(
    tmp_path: Path,
) -> None:
    """A new push re-arms the read by moving the head, so the window starts at
    the review that covered the old one — an older failure is spent history."""
    _, calls = dispatch(
        tmp_path,
        reviews=[_review(COVERED)],
        runs=[
            {
                "display_title": f"Claude reviewers — PR {PR}",
                "created_at": "2025-01-01T00:00:00Z",
                "status": "completed",
                "conclusion": "failure",
            }
        ]
        * 3,
    )
    assert len(_dispatched(calls)) == 1, calls


def _unstamped_notice() -> dict:
    """An oversized notice carrying its markers and no coverage stamp."""
    return {
        "state": "COMMENTED",
        "body": "This PR's diff is too large.\n<!-- oversized-review -->",
        "author": {"login": "github-actions"},
        "submittedAt": "2026-06-01T00:00:00Z",
    }


def test_an_oversized_notice_that_stamps_nothing_is_asked_for_again(
    tmp_path: Path,
) -> None:
    """The loop, reproduced. A dispatched read whose diff is too large posts a
    notice and SUCCEEDS, so the failed-run bound never fires. Stamping nothing
    leaves the covered head where it was, and the next sweep reads the same
    state and asks for the same read."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED), _unstamped_notice()])
    assert len(_dispatched(calls)) == 1, calls


def test_an_oversized_notice_on_the_live_head_stops_the_re_dispatch(
    tmp_path: Path,
) -> None:
    """The pair for the case above, differing only in whether the notice stamps
    the head it decided. It read no diff, and it still cost a run, so the record
    advances and the sweep stands down."""
    _, calls = dispatch(
        tmp_path,
        reviews=[
            _review(COVERED),
            _review(PUSHED, read="delta", at="2026-06-01T00:00:00Z", scope="oversized"),
        ],
    )
    assert _dispatched(calls) == []


def test_the_failed_run_window_is_asked_for_in_full(tmp_path: Path) -> None:
    """The retry bound counts runs, so a first page is not an answer: a
    repository running 100 newer dispatches between sweeps drops this pull
    request's failures off it, the count resets, and the read the bound exists to
    stop is dispatched again. Asserted on the call the script made, because
    `gh` paginates inside itself and a stub cannot show the later pages."""
    _, calls = dispatch(tmp_path, reviews=[_review(COVERED)])
    runs = [c for c in calls if "/runs?" in c]
    assert len(runs) == 1, calls
    assert "--paginate" in runs[0], runs[0]
    assert f"created=%3E%3D{COVERED_AT}" in runs[0], runs[0]
