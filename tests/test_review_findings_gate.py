"""The exported gate decides whether a consumer's required review check goes
green, so the two things that matter are WHICH reviews it credits and WHICH
unresolved threads hold the merge.

The `gh` stub here RUNS the script's own `--jq` filters over canned GraphQL
payloads instead of returning a pre-filtered answer. The whole safety property
lives inside those filters — the whole-line severity marker, the icon
`startswith`, the reviewer-identity select — so a stub that ignored `--jq` would
report the gate working while testing nothing. Both GraphQL reads go through one
stub, routed by which node set their filter names.

Every green case is paired with a red one over a payload that differs by one
field, so no test can pass against a gate that answers `success` unconditionally.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "review-findings-gate.sh"
SEVERITIES = REPO_ROOT / "config" / "review-severities.json"
BOT = "github-actions"
HEAD_SHA = "cafebabe"
CONTEXT = "Review findings resolved"


def review(state: str, *, author: str = BOT, body: str = "Automated review.") -> dict:
    return {
        "state": state,
        "body": body,
        "author": {"login": author},
        "submittedAt": "2026-01-01T00:00:00Z",
    }


def thread(
    body: str, *, resolved: bool = False, author: str = BOT, path: str = "a.py"
) -> dict:
    return {
        "isResolved": resolved,
        "path": path,
        "line": 1,
        "comments": {"nodes": [{"body": body, "author": {"login": author}}]},
    }


def _gate_proc(
    tmp_path: Path,
    reviews: list[dict],
    threads: list[dict] | None = None,
    *,
    thread_pages: list[list[dict]] | None = None,
    unreviewed_state: str = "pending",
    max_delta_reviews: str | None = None,
    live_head: str = HEAD_SHA,
    severity_config: Path = SEVERITIES,
    report_sha: str | None = HEAD_SHA,
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the gate against a `gh` stub; return the process and its call log.

    `thread_pages` is one GraphQL PAGE of review threads per element. `gh api
    graphql --paginate --jq` applies the filter to each page separately and
    concatenates the results, so the stub does the same — a single-payload stub
    never exercises the gate's cross-page sum.

    `report_sha=None` is the merge-queue leg, where the exit code IS the verdict.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews.json").write_text(
        json.dumps(
            {"data": {"repository": {"pullRequest": {"reviews": {"nodes": reviews}}}}}
        ),
        encoding="utf-8",
    )
    for index, page in enumerate(thread_pages or [threads or []]):
        (tmp_path / f"threads-{index}.json").write_text(
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {"reviewThreads": {"nodes": page}}
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    log = tmp_path / "gh-calls.txt"
    # The two GraphQL reads are told apart by the node set their filter names,
    # which is the only thing that differs between them at the `gh` boundary.
    stub = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
filter=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq) filter="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$filter" in
  *reviewThreads.nodes*)
    for page in "{tmp_path}"/threads-*.json; do jq -r "$filter" "$page"; done
    exit 0 ;;
  *reviews.nodes*)       jq -r "$filter" "{tmp_path}/reviews.json"; exit 0 ;;
  .headRefOid)           printf '%s\\n' "{live_head}"; exit 0 ;;
esac
exit 0
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(stub, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)

    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "GH_TOKEN": "t",
            "GH_REPO": "o/r",
            "PR": "18",
            **({} if report_sha is None else {"REPORT_SHA": report_sha}),
            "GATE_CONTEXT": CONTEXT,
            "SEVERITY_CONFIG": str(severity_config),
            "UNREVIEWED_STATE": unreviewed_state,
            # Left OUT when None, which is how a consumer that runs no
            # accumulated read keeps the PR-scoped predicate it had.
            **(
                {}
                if max_delta_reviews is None
                else {"MAX_DELTA_REVIEWS_PER_PR": max_delta_reviews}
            ),
        },
    )
    return res, log


def gate_calls(tmp_path: Path, *args, **kwargs) -> str:
    """Run the gate in its status-posting mode and return the `gh` calls it made.
    The verdict's DESCRIPTION is in them, for the cases whose whole content is
    what the status says."""
    res, log = _gate_proc(tmp_path, *args, **kwargs)
    assert res.returncode == 0, res.stderr
    return log.read_text(encoding="utf-8")


def queue_leg_exit(tmp_path: Path, *args, **kwargs) -> int:
    """The merge-queue leg's verdict: no REPORT_SHA, so the exit code is it."""
    res, _ = _gate_proc(tmp_path, *args, report_sha=None, **kwargs)
    assert res.returncode in (0, 1), res.stderr
    return res.returncode


def _state_of(calls: str) -> str:
    """The one verdict a gate run posted, out of the `gh` calls it made."""
    assert f"statuses/{HEAD_SHA}" in calls, f"the gate posted no status: {calls}"
    assert f"context={CONTEXT}" in calls, f"posted under the wrong context: {calls}"
    states = [
        s for s in ("state=success", "state=failure", "state=pending") if s in calls
    ]
    assert len(states) == 1, f"expected exactly one verdict, got {states}: {calls}"
    return states[0].removeprefix("state=")


def run_gate(*args, **kwargs) -> str:
    """The state a gate run posted — what nearly every case here asserts on."""
    return _state_of(gate_calls(*args, **kwargs))


def test_the_gate_context_defaults_to_the_severity_ssot(tmp_path: Path) -> None:
    """A caller that passes no GATE_CONTEXT posts under the SSOT's `gate_context`,
    so the context is not restated at each call site."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews.json").write_text(
        json.dumps(
            {"data": {"repository": {"pullRequest": {"reviews": {"nodes": []}}}}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "threads-0.json").write_text(
        json.dumps(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
        ),
        encoding="utf-8",
    )
    log = tmp_path / "calls.log"
    stub = f"""#!/usr/bin/env bash
echo "$*" >>"{log}"
[[ "$1" == graphql ]] || exit 0
filter=""
for ((i = 1; i <= $#; i++)); do
  [[ "${{!i}}" == --jq ]] && {{ j=$((i + 1)); filter="${{!j}}"; }}
done
case "$*" in
  *reviewThreads*) for page in "{tmp_path}"/threads-*.json; do jq -r "$filter" "$page"; done; exit 0 ;;
  *reviews.nodes*) jq -r "$filter" "{tmp_path}/reviews.json"; exit 0 ;;
esac
exit 0
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(stub, encoding="utf-8")
    (bin_dir / "gh").chmod(0o755)
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "GH_TOKEN": "t",
            "GH_REPO": "o/r",
            "PR": "18",
            "REPORT_SHA": HEAD_SHA,
            "SEVERITY_CONFIG": str(SEVERITIES),
        },
    )
    assert res.returncode == 0, res.stderr
    expected = json.loads(SEVERITIES.read_text(encoding="utf-8"))["gate_context"]
    assert f"context={expected}" in log.read_text(encoding="utf-8")


def test_a_reviewed_pr_with_no_findings_is_green(tmp_path: Path) -> None:
    """The clean path: no fix may green-lock or red-lock the gate."""
    assert run_gate(tmp_path, [review("COMMENTED")]) == "success"


def test_an_unreviewed_pr_never_greens(tmp_path: Path) -> None:
    """Clause (a). Zero findings from zero reviews is vacuous, and the skip set
    is empty by default, so nothing waives the wait."""
    assert run_gate(tmp_path, []) == "pending"


def test_the_unreviewed_state_is_allowlisted(tmp_path: Path) -> None:
    """`failure` is a consumer's choice; `success` is the one answer that must
    never reach the wire, and anything unrecognized fails closed."""
    assert run_gate(tmp_path / "chosen", [], unreviewed_state="failure") == "failure"
    assert run_gate(tmp_path / "bogus", [], unreviewed_state="success") == "failure"


def test_a_dismissed_review_still_counts_as_a_read(tmp_path: Path) -> None:
    """A dismissal retracts the HOLD, not the reading. The hold sweeper dismisses
    the reviewer's CHANGES_REQUESTED on the routine path, so dropping dismissed
    reviews would strand every cleared hold at pending forever."""
    assert run_gate(tmp_path, [review("DISMISSED")]) == "success"


@pytest.mark.parametrize("author", ["pr-author", "outside-contributor", "dependabot"])
def test_a_non_reviewer_review_never_clears_the_gate(
    tmp_path: Path, author: str
) -> None:
    """Any actor's review counting would make the gate self-clearing: an author
    submits a one-word COMMENT review on their own PR and a required merge lever
    goes green with no reviewer having run."""
    assert run_gate(tmp_path, [review("COMMENTED", author=author)]) == "pending"


def test_a_body_less_reviewer_review_never_clears_the_gate(tmp_path: Path) -> None:
    """GitHub synthesizes a body-less COMMENTED review around every standalone
    review comment, and this repo posts those under the reviewer's identity when
    it replies in-thread. Crediting one greens a PR the reviewer still holds."""
    assert run_gate(tmp_path, [review("COMMENTED", body="")]) == "pending"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<!-- severity: blocking -->\nthis breaks", "failure"),
        ("<!-- severity: warning -->\nrisky", "failure"),
        ("\U0001f534 this breaks", "failure"),
        ("\U0001f7e1 risky", "failure"),
        ("<!-- severity: nit -->\ntiny", "success"),
        ("\U0001f535 tiny", "success"),
        ("we saw a <!-- severity: blocking --> inline", "success"),
        ("a plain reply with no severity at all", "success"),
    ],
)
def test_which_thread_bodies_gate(tmp_path: Path, body: str, expected: str) -> None:
    """The severity predicate, member by member against the live SSOT. The marker
    match is WHOLE-LINE on purpose: a finding that merely quotes a marker in
    prose or inside a suggestion block must not hold a merge."""
    assert run_gate(tmp_path, [review("COMMENTED")], [thread(body)]) == expected


DELTA = "\n<!-- read: delta -->"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("risky\n\n<!-- severity: warning -->" + DELTA, "success"),
        ("\U0001f7e1 risky\n\n<!-- severity: warning -->" + DELTA, "success"),
        ("this breaks\n\n<!-- severity: blocking -->" + DELTA, "failure"),
        ("\U0001f534 this breaks" + DELTA, "failure"),
        ("quotes <!-- read: delta --> inline\n<!-- severity: warning -->", "failure"),
    ],
)
def test_a_finding_of_the_accumulated_read_gates_by_delta_gating(
    tmp_path: Path, body: str, expected: str
) -> None:
    """The live SSOT lists only `blocking` under `delta_gating`, so a warning the
    accumulated read found informs and does not hold the merge. The read marker is
    matched whole-line, like the severity marker: a body quoting it stays gated."""
    assert run_gate(tmp_path, [review("COMMENTED")], [thread(body)]) == expected


def test_with_no_delta_gating_key_an_accumulated_warning_still_gates(
    tmp_path: Path,
) -> None:
    """A consumer whose config names no `delta_gating` keeps the gate it had."""
    config = json.loads(SEVERITIES.read_text(encoding="utf-8"))
    del config["delta_gating"]
    path = tmp_path / "severities.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    body = "risky\n\n<!-- severity: warning -->" + DELTA
    assert (
        run_gate(tmp_path, [review("COMMENTED")], [thread(body)], severity_config=path)
        == "failure"
    )


def test_a_resolved_gating_thread_stops_gating(tmp_path: Path) -> None:
    """Resolving the last gating thread is the whole clearing ceremony."""
    gating = "<!-- severity: blocking -->\nthis breaks"
    # Separate directories: run_gate asserts on ONE verdict in the call log, and
    # a shared log would carry both runs'.
    assert (
        run_gate(tmp_path / "open", [review("COMMENTED")], [thread(gating)])
        == "failure"
    )
    assert (
        run_gate(
            tmp_path / "done", [review("COMMENTED")], [thread(gating, resolved=True)]
        )
        == "success"
    )


def test_a_gating_thread_rooted_by_someone_else_does_not_gate(tmp_path: Path) -> None:
    """Only the reviewer's own findings are this gate's lever, so a human cannot
    hold a merge by pasting the marker — their CHANGES_REQUESTED does that."""
    body = "<!-- severity: blocking -->\nthis breaks"
    assert (
        run_gate(tmp_path, [review("COMMENTED")], [thread(body, author="a-human")])
        == "success"
    )


def test_a_gating_thread_on_an_EARLIER_page_still_holds_the_gate(
    tmp_path: Path,
) -> None:
    """The gate counts per page and sums the counts in the shell, because a `--jq`
    reducer would answer from the LAST page alone. That under-read greens a gate
    that should be red: the gating thread sits on page 1 and page 2 carries only a
    nit, so a reducer answers 0 and the merge goes through with the finding open."""
    pages = [
        [thread("Race on the shared file.\n\n<!-- severity: warning -->")],
        [thread("Rename this.\n\n<!-- severity: nit -->")],
    ]
    assert run_gate(tmp_path, [review("COMMENTED")], thread_pages=pages) == "failure"
    assert (
        run_gate(tmp_path / "b", [review("COMMENTED")], thread_pages=pages[1:])
        == "success"
    )


# ── Clause (c): a push the reviewer has not read ──────────────────────────────
#
# Clause (a) is PR-scoped on purpose, so a reviewed PR whose head then moves
# stays green. Clause (c) is what keeps that honest once an accumulated read
# exists to wait for: while one is still owed the gate holds, and once the
# budget is spent it greens again and SAYS where the reading stopped.

COVERED = "1111111111111111111111111111111111111111"
PUSHED = "2222222222222222222222222222222222222222"


def _covered_review(
    head: str,
    *,
    read: str = "first",
    scope: str = "whole",
    submitted_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    """A reviewer review carrying both stamps post-pr-review.sh writes: the read
    marker that spends the budget, and the coverage stamp naming what it read.
    Built through the library's own producer so a format change reds here."""
    marker = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s\\n" "$WHOLE_DIFF_READ_MARKER";'
            ' coverage_stamp "$2" "$4" "$5" "$3" "$6"',
            "_",
            str(REPO_ROOT / ".github" / "reviewer" / "lib" / "pr-reviews.bash"),
            head,
            read,
            "ba5eba5e",
            "0e0e0e0e",
            scope,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {
        **review("COMMENTED", body=f"Automated review.\n{marker}"),
        "submittedAt": submitted_at,
    }


def test_a_push_after_the_covered_head_holds_the_merge(tmp_path: Path) -> None:
    """The gap this closes: the reviewer read `COVERED`, the author pushed, and
    nothing has read what they pushed. Without clause (c) the gate is green and
    the PR merges on a review of older code."""
    state = run_gate(
        tmp_path,
        [_covered_review(COVERED)],
        max_delta_reviews="1",
        live_head=PUSHED,
        unreviewed_state="failure",
    )
    assert state == "failure"


def test_the_same_pr_is_green_once_its_head_is_the_covered_one(tmp_path: Path) -> None:
    """The pair for the case above, differing only in the live head: clause (c)
    holds an UNREAD push, never a read one."""
    state = run_gate(
        tmp_path,
        [_covered_review(COVERED)],
        max_delta_reviews="1",
        live_head=COVERED,
        unreviewed_state="failure",
    )
    assert state == "success"


def test_a_spent_accumulated_budget_greens_and_names_where_reading_stopped(
    tmp_path: Path,
) -> None:
    """Budget exhausted is the one case that must never read as full coverage.
    No further read is coming, so the merge is not held — but the description
    says which commit the reading stopped at instead of claiming the head."""
    calls = gate_calls(
        tmp_path,
        [
            _covered_review(COVERED),
            _covered_review(PUSHED, read="delta", submitted_at="2026-02-01T00:00:00Z"),
        ],
        max_delta_reviews="1",
        live_head="3333333333333333333333333333333333333333",
    )
    assert _state_of(calls) == "success"
    assert "the accumulated read is spent" in calls, calls
    # The accumulated read is the newest coverage, so the reading stopped at the
    # head IT covered, not at the first read's.
    assert PUSHED[:7] in calls, calls


def _severities_with_budget(tmp_path: Path, budget: int | None) -> Path:
    """A copy of the real severity SSOT whose `max_delta_reviews_per_pr` is
    BUDGET, or absent when BUDGET is None."""
    config = json.loads(SEVERITIES.read_text(encoding="utf-8"))
    config.pop("max_delta_reviews_per_pr", None)
    if budget is not None:
        config["max_delta_reviews_per_pr"] = budget
    path = tmp_path / f"severities-{budget}.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_the_clause_is_off_for_a_consumer_that_runs_no_accumulated_read(
    tmp_path: Path,
) -> None:
    """A consumer passing no budget, whose severity config names none either,
    runs no accumulated read, so no head can be waiting for one and the gate
    stays the predicate it was."""
    state = run_gate(
        tmp_path,
        [_covered_review(COVERED)],
        live_head=PUSHED,
        severity_config=_severities_with_budget(tmp_path, None),
    )
    assert state == "success"


def test_the_severity_config_budget_turns_the_clause_on_with_no_env(
    tmp_path: Path,
) -> None:
    """The pair for the case above, differing only in the config key. Both gate
    legs pass no budget and read this one, which is what keeps them agreeing."""
    state = run_gate(
        tmp_path,
        [_covered_review(COVERED)],
        live_head=PUSHED,
        severity_config=_severities_with_budget(tmp_path, 1),
        unreviewed_state="failure",
    )
    assert state == "failure"


def test_an_explicit_budget_outranks_the_severity_config(tmp_path: Path) -> None:
    """A caller's own number still wins: config 1 with one read spent would say
    `spent`, so only the env's 2 can make this pull request wait."""
    spent_once = [
        _covered_review(COVERED),
        _covered_review(PUSHED, read="delta", submitted_at="2026-02-01T00:00:00Z"),
    ]
    state = run_gate(
        tmp_path,
        spent_once,
        live_head="3333333333333333333333333333333333333333",
        severity_config=_severities_with_budget(tmp_path, 1),
        max_delta_reviews="2",
        unreviewed_state="failure",
    )
    assert state == "failure"


@pytest.mark.parametrize(("budget", "exit_code"), [(1, 0), (2, 1)])
def test_the_merge_queue_leg_answers_from_the_same_config_number(
    tmp_path: Path, budget: int, exit_code: int
) -> None:
    """The merge-queue leg passes no budget either. One read spent and a head
    past it: at 1 the read is spent and the batch may merge, at 2 it is still
    owed. Two legs reading two numbers could disagree here, and a queue leg
    that says 1 where the pull-request leg let the head in at 2 fails a merge
    group that entered green."""
    code = queue_leg_exit(
        tmp_path,
        [
            _covered_review(COVERED),
            _covered_review(PUSHED, read="delta", submitted_at="2026-02-01T00:00:00Z"),
        ],
        live_head="3333333333333333333333333333333333333333",
        severity_config=_severities_with_budget(tmp_path, budget),
    )
    assert code == exit_code


@pytest.mark.parametrize("bad", ["01", 1000, "x", -1])
def test_a_malformed_config_budget_fails_the_gate_loud(tmp_path: Path, bad) -> None:
    """A budget the gate cannot read is a can't-verify, and can't-verify is red:
    reading it as "off" would green a head nobody read."""
    config = json.loads(SEVERITIES.read_text(encoding="utf-8"))
    config["max_delta_reviews_per_pr"] = bad
    path = tmp_path / "bad.json"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")
    res, _ = _gate_proc(
        tmp_path, [_covered_review(COVERED)], live_head=PUSHED, severity_config=path
    )
    assert res.returncode != 0
    assert "max-delta-reviews-per-pr must be a whole number" in res.stderr


def test_an_unreadable_live_head_does_not_invent_a_hold(tmp_path: Path) -> None:
    """A can't-verify is not evidence. The reviewed-at fact is already true, so
    an empty head read leaves the verdict alone rather than holding a PR on an
    API blip."""
    state = run_gate(
        tmp_path,
        [_covered_review(COVERED)],
        max_delta_reviews="1",
        live_head="",
        unreviewed_state="failure",
    )
    assert state == "success"


def test_an_unread_push_never_outranks_an_open_gating_finding(tmp_path: Path) -> None:
    """Clause (b) still decides first: an unresolved blocking thread is a red the
    author must act on, and reporting it as "waiting for a read" would tell them
    to wait instead."""
    calls = gate_calls(
        tmp_path,
        [_covered_review(COVERED)],
        [thread("<!-- severity: blocking -->\nleaks")],
        max_delta_reviews="1",
        live_head=PUSHED,
        unreviewed_state="failure",
    )
    assert _state_of(calls) == "failure"
    assert "unresolved reviewer finding" in calls, calls


def test_an_abandoned_accumulated_read_stops_the_gate_waiting_at_any_budget(
    tmp_path: Path,
) -> None:
    """The give-up notice spends ONE read, so at a budget of 2 the spent-count
    test still leaves room and the gate waits — for a read the dispatcher has
    already refused to ask for again. The gate therefore asks whether the read
    was abandoned, not how many were spent."""
    calls = gate_calls(
        tmp_path,
        [
            _covered_review(COVERED),
            _covered_review(
                COVERED,
                read="delta",
                scope="failed",
                submitted_at="2026-02-01T00:00:00Z",
            ),
        ],
        max_delta_reviews="2",
        live_head=PUSHED,
        unreviewed_state="failure",
    )
    assert _state_of(calls) == "success"
    assert "the accumulated read was abandoned" in calls, calls
    # The head the last REAL review covered, not the live one nobody read.
    assert COVERED[:7] in calls, calls


def test_a_budget_with_room_and_no_notice_still_holds_the_merge(
    tmp_path: Path,
) -> None:
    """The pair for the case above: the abandonment is what greens it, not the
    budget of 2."""
    state = run_gate(
        tmp_path,
        [_covered_review(COVERED)],
        max_delta_reviews="2",
        live_head=PUSHED,
        unreviewed_state="failure",
    )
    assert state == "failure"
