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


def run_gate(
    tmp_path: Path,
    reviews: list[dict],
    threads: list[dict] | None = None,
    *,
    unreviewed_state: str = "pending",
) -> str:
    """Run the gate and return the single status state it posted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "reviews.json").write_text(
        json.dumps(
            {"data": {"repository": {"pullRequest": {"reviews": {"nodes": reviews}}}}}
        ),
        encoding="utf-8",
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
  *reviewThreads.nodes*) jq -r "$filter" "{tmp_path}/threads.json"; exit 0 ;;
  *reviews.nodes*)       jq -r "$filter" "{tmp_path}/reviews.json"; exit 0 ;;
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
            "GATE_CONTEXT": CONTEXT,
            "SEVERITY_CONFIG": str(SEVERITIES),
            "UNREVIEWED_STATE": unreviewed_state,
        },
    )
    assert res.returncode == 0, res.stderr
    calls = log.read_text(encoding="utf-8")
    assert f"statuses/{HEAD_SHA}" in calls, f"the gate posted no status: {calls}"
    assert f"context={CONTEXT}" in calls, f"posted under the wrong context: {calls}"
    states = [
        s for s in ("state=success", "state=failure", "state=pending") if s in calls
    ]
    assert len(states) == 1, f"expected exactly one verdict, got {states}: {calls}"
    return states[0].removeprefix("state=")


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
