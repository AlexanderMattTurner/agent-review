"""review-gate.sh decides whether the "Automated review posted" required check
goes green, so the two things that matter about it are WHICH reviews it credits
and WHICH unresolved threads hold the merge.

The `gh` stub here RUNS the script's own `--jq` filters over canned reviews and
review-thread payloads instead of returning a pre-filtered answer. The whole
safety property lives inside those filters, so a stub that ignored `--jq` would
report the gate working while testing nothing.

Two review-filter invariants, both of which the pre-fix filter
(`select(.state != "DISMISSED") | .user.login`) violated — each test that pins one
also runs the pre-fix filter over the same payload and asserts it answered the
other way, so the test cannot pass vacuously against a gate that never changed.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "review-gate.sh"
LIB_REL = ".github/scripts/lib/reviewer-login.bash"
REVIEWER_SCRIPTS = (
    "review-gate.sh",
    "approve-if-reviewer-hold-clear.sh",
)

BOT = "github-actions[bot]"
HEAD_SHA = "cafebabe"

# The filter this gate shipped with. Kept verbatim so every invariant below can
# show the fixture it rejects is one the old gate accepted.
PRE_FIX_JQ = '.[] | select(.state != "DISMISSED") | .user.login // ""'


def review(state: str, *, author: str = BOT, body: str = "Automated review.") -> dict:
    return {"state": state, "body": body, "user": {"login": author}}


def thread(body: str, *, resolved: bool = False, author: str = BOT) -> dict:
    """One review thread, shaped as the shared GraphQL walker returns it."""
    return {
        "isResolved": resolved,
        "comments": {"nodes": [{"body": body, "author": {"login": author}}]},
    }


def run_gate(
    tmp_path: Path, reviews: list[dict], threads: list[dict] | None = None
) -> str:
    """Run the gate over `reviews` and `threads`; return the status state it posted."""
    (tmp_path / "reviews.json").write_text(json.dumps(reviews), encoding="utf-8")
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
    stub = f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
if [[ "$2" == "--paginate" || "$2" == "graphql" ]]; then
  payload="{tmp_path}/reviews.json"
  [[ "$2" == "graphql" ]] && payload="{tmp_path}/threads.json"
  filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --jq) filter="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  jq -r "$filter" "$payload"
  exit 0
fi
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
            "PR": "438",
            "HEAD_SHA": HEAD_SHA,
        },
    )
    assert res.returncode == 0, res.stderr
    calls = log.read_text(encoding="utf-8")
    assert f"statuses/{HEAD_SHA}" in calls, f"the gate posted no status: {calls}"
    states = [
        s for s in ("state=success", "state=failure", "state=pending") if s in calls
    ]
    assert len(states) == 1, f"expected exactly one verdict, got {states}: {calls}"
    return states[0].removeprefix("state=")


def pre_fix_verdict(reviews: list[dict]) -> str:
    """What the gate's original review filter answered for the same payload."""
    out = subprocess.run(
        ["jq", "-r", PRE_FIX_JQ],
        input=json.dumps(reviews),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return "success" if out else "failure"


def test_the_reviewers_own_review_clears_the_gate(tmp_path: Path) -> None:
    """The clean path: no fix may green a gate that stopped working."""
    assert run_gate(tmp_path, [review("COMMENTED")]) == "success"


def test_an_approval_clears_the_gate(tmp_path: Path) -> None:
    """auto-approve-skipped-pr.sh and approve-if-reviewer-hold-clear.sh both post
    an APPROVE under the reviewer's identity; both must still clear the gate."""
    assert run_gate(tmp_path, [review("APPROVED")]) == "success"


def test_a_dismissed_review_reddens_the_gate(tmp_path: Path) -> None:
    assert run_gate(tmp_path, [review("DISMISSED")]) == "failure"


@pytest.mark.parametrize(
    "author", ["pr-author", "outside-contributor", "dependabot[bot]"]
)
def test_a_non_reviewer_review_never_clears_the_gate(
    tmp_path: Path, author: str
) -> None:
    """INVARIANT 1. The gate claims an AUTOMATED review exists. Any actor's review
    counting made it self-clearing: a PR author submits a one-word COMMENT review
    on their own PR and a required merge lever goes green with no reviewer run."""
    payload = [review("COMMENTED", author=author)]
    assert run_gate(tmp_path, payload) == "failure"
    assert pre_fix_verdict(payload) == "success", (
        "fixture no longer exercises the bug: the pre-fix filter must have "
        "credited this review, or this test proves nothing"
    )


def test_a_body_less_reviewer_review_never_clears_the_gate(tmp_path: Path) -> None:
    """INVARIANT 2. GitHub synthesizes a body-less COMMENTED review around every
    standalone review comment, and this repo posts such comments under
    the reviewer's own identity on each auto-resolved thread. Crediting it greens
    the gate for a PR the reviewer is still holding."""
    payload = [review("COMMENTED", body="")]
    assert run_gate(tmp_path, payload) == "failure"
    assert pre_fix_verdict(payload) == "success", (
        "fixture no longer exercises the bug: the pre-fix filter must have "
        "credited this body-less review, or this test proves nothing"
    )


def test_a_real_review_still_clears_a_gate_full_of_noise(tmp_path: Path) -> None:
    """Both filters at once, in the order a live PR accumulates them."""
    payload = [
        review("COMMENTED", author="pr-author", body="looks fine to me"),
        review("COMMENTED", body=""),
        review("CHANGES_REQUESTED"),
    ]
    assert run_gate(tmp_path, payload) == "success"


# ── An unresolved finding holds the merge ────────────────────────────────────
#
# The reviewer posts every review as a COMMENT, so before this the gate went
# green the moment a review landed — findings and all — and auto-merge took the
# PR with its blocking findings unread. These pin the second half of the
# predicate: which unresolved threads keep the required check red.

REVIEWED = [review("COMMENTED")]


def test_an_unresolved_blocking_finding_holds_the_gate_red(tmp_path: Path) -> None:
    payload = [thread("Null deref here.\n\n<!-- severity: blocking -->")]
    assert run_gate(tmp_path, REVIEWED, payload) == "failure"


def test_resolving_the_last_gating_thread_greens_the_gate(tmp_path: Path) -> None:
    """The clearing path, and the only one that does not need a push."""
    payload = [thread("Null deref here.\n\n<!-- severity: blocking -->", resolved=True)]
    assert run_gate(tmp_path, REVIEWED, payload) == "success"


def test_an_unresolved_nit_never_holds_the_gate(tmp_path: Path) -> None:
    """config/review-severities.json lists `blocking` and `warning` as gating, so a
    🔵 nit is advice: a gate a nit could hold would teach authors to resolve
    threads to merge rather than to fix what they name."""
    payload = [thread("Rename this.\n\n<!-- severity: nit -->")]
    assert run_gate(tmp_path, REVIEWED, payload) == "success"


def test_an_icon_led_finding_holds_the_gate_without_a_marker(tmp_path: Path) -> None:
    """A thread posted before the hidden marker existed carries only its icon."""
    payload = [thread("\U0001f7e1 The retry swallows the exit code.")]
    assert run_gate(tmp_path, REVIEWED, payload) == "failure"


def test_a_marker_quoted_in_prose_does_not_gate(tmp_path: Path) -> None:
    """The marker counts as a whole LINE only. Matching it anywhere in the body
    would let a finding that quotes the marker — in prose, or in a suggestion
    block proposing one — relabel itself into or out of the gating set."""
    payload = [thread("Stamp it with `<!-- severity: blocking -->` next time.")]
    assert run_gate(tmp_path, REVIEWED, payload) == "success"


def test_another_actors_unresolved_thread_never_holds_the_gate(tmp_path: Path) -> None:
    """This gate reports on the AUTOMATED reviewer. A human's unresolved thread is
    the review-required ruleset's business; holding this check on one would report
    a reviewer finding that does not exist."""
    payload = [thread("I disagree with this design.", author="pr-author")]
    assert run_gate(tmp_path, REVIEWED, payload) == "success"


def test_one_unresolved_finding_among_resolved_ones_still_holds(tmp_path: Path) -> None:
    payload = [
        thread("Fixed.\n\n<!-- severity: blocking -->", resolved=True),
        thread("Rename this.\n\n<!-- severity: nit -->"),
        thread("Race on the shared file.\n\n<!-- severity: warning -->"),
    ]
    assert run_gate(tmp_path, REVIEWED, payload) == "failure"


def test_findings_are_not_read_before_a_review_exists(tmp_path: Path) -> None:
    """Term (a) first: zero unresolved findings from zero reviews is vacuous, and
    a green there is exactly the merge-past-the-reviewer this gate exists to stop."""
    assert run_gate(tmp_path, [], []) == "failure"


# ── Every file a reviewer script reads must reach the runner that runs it ────

# What each reviewer script needs on disk, itself included. A sparse checkout
# naming individual FILES gets exactly those files, so a job that runs the script
# without one of these dies at its `source` line under `set -e` — or, for the
# severity SSOT, at the `jq` that reads which findings gate. The gate's own
# bootstrap arm cannot tell either apart from a repo that has not adopted it yet.
GATE_DEPS = (
    ".github/scripts/review-gate.sh",
    LIB_REL,
    ".github/reviewer/lib/review-threads.bash",
    ".github/reviewer/lib-ci-retry.sh",
    "config/review-severities.json",
)
HOLD_CLEAR_DEPS = (
    ".github/scripts/approve-if-reviewer-hold-clear.sh",
    LIB_REL,
    ".github/scripts/lib/github-token-ladder.bash",
)
# The sweep runs BOTH per PR, so its runner needs the union.
REVIEWER_SCRIPT_DEPS = {
    "review-gate.sh": GATE_DEPS,
    "approve-if-reviewer-hold-clear.sh": HOLD_CLEAR_DEPS,
    "sweep-reviewer-holds.sh": (
        ".github/scripts/sweep-reviewer-holds.sh",
        *GATE_DEPS,
        *HOLD_CLEAR_DEPS,
    ),
}


def _covered(entries: list[str], path: str) -> bool:
    """Whether a sparse-checkout list brings PATH in — named outright, or inside a
    directory it names."""
    return any(e == path or path.startswith(e.rstrip("/") + "/") for e in entries)


def _reviewer_jobs():
    """(workflow, job id, scripts it runs, sparse entries) per job that runs a
    reviewer script. A job whose checkout takes the whole repo yields nothing:
    every dependency is already there."""
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            steps = (job or {}).get("steps") or []
            run_text = "\n".join(str(step.get("run") or "") for step in steps)
            scripts = [s for s in REVIEWER_SCRIPT_DEPS if s in run_text]
            if not scripts:
                continue
            entries: list[str] = []
            full_checkout = False
            for step in steps:
                if not str(step.get("uses") or "").startswith("actions/checkout@"):
                    continue
                sparse = (step.get("with") or {}).get("sparse-checkout")
                if not sparse:
                    full_checkout = True
                    continue
                entries += [e.strip() for e in str(sparse).split("\n") if e.strip()]
            if full_checkout:
                continue
            yield path.name, job_id, scripts, entries


def test_every_job_running_a_reviewer_script_checks_out_what_it_reads():
    checked = []
    for workflow, job_id, scripts, entries in _reviewer_jobs():
        for script in scripts:
            for dep in REVIEWER_SCRIPT_DEPS[script]:
                checked.append((workflow, job_id, dep))
                assert _covered(entries, dep), (
                    f"{workflow} / job {job_id} runs {script} but its sparse "
                    f"checkout ({entries}) does not bring in {dep}"
                )
    assert checked, (
        "no job runs a reviewer script any more — this guard would now pass "
        "without checking anything; re-point it or delete it"
    )


def test_every_reviewer_script_uses_the_shared_login_library():
    """The identity predicate was copied into four scripts; the point of the
    library is that it stops being four. A script that re-derives the bare login
    locally has forked the predicate again."""
    for name in REVIEWER_SCRIPTS:
        text = (REPO_ROOT / ".github" / "scripts" / name).read_text(encoding="utf-8")
        assert "lib/reviewer-login.bash" in text and "reviewer_login_init" in text, (
            f"{name} does not source the shared reviewer-login library"
        )
        assert "REVIEWER_LOGIN%" not in text, (
            f"{name} re-derives REVIEWER_LOGIN_BARE itself instead of calling "
            "reviewer_login_init"
        )
        assert 'sub("\\\\[bot\\\\]$"' not in text, (
            f"{name} hand-writes the [bot]-stripping jq instead of using the "
            "library's select clause"
        )
