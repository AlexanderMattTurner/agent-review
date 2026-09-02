"""Behavioral tests for .github/reviewer/decide-pr-review-trigger.sh — the gate
that decides whether review.yaml's reviewer runs. The model is the reusable
workflow's own input, so this script never picks one.

Contract:
  * a DRAFT is decided exactly like a ready PR — the script never reads the draft
    state at all. The ready-PR cap drafts most PRs within seconds of `opened`, so
    a reviewer that waited for `ready_for_review` would give its feedback only
    once the work was finished.
  * opened -> run whenever the budget is at least 1 (its first review). GitHub
    fires this action exactly once per pull request, so no review can exist yet
    and the count is 0 by construction — the arm reads the budget, never the API.
  * ready_for_review / synchronize -> run when EITHER
      1. the event is a `synchronize` AND the head commit's TITLE (subject line,
         not body) carries the "[opus-review]" opt-in (matched
         case-insensitively) -> a full re-read. A `ready_for_review` never opts
         in: a toggle carries no new commit, so honoring it would buy one read
         per toggle off a single tagged head; or
      2. the reviewer bot has spent FEWER reads than `max-reviews-per-pr`, which
         is 1 by default — so the first pass re-arms when `opened` produced no
         review, and a higher budget buys the next read on the next push.
    Any verdict — APPROVED, DISMISSED, or a still-outstanding CHANGES_REQUESTED
    or COMMENTED — spends one read; past the budget no repeatable event buys
    another, and only the [opus-review] opt-in and the review label do. Both
    `ready_for_review` and `synchronize` can fire without limit on one PR, so an
    unconditional arm on either costs a whole-diff Opus read per fire.
  * the base branch never decides anything: a PR based on a feature branch is
    reviewed exactly like one based on the default branch. Enforcement matches —
    a second ruleset requires the same checks over `claude/**` — so a merged
    stacked child brings in no unread code and buys no re-read.
  * `recheck` is true for trigger 2 alone — that read can race a review still
    being generated, so the review job re-asks the question from inside its
    concurrency group (see test_recheck_pr_review_owed.py).
  * any other action -> never run.
  * the head commit message is fetched via `gh api .../commits/<sha>` and the
    review state via lib/pr-reviews.bash's shared GraphQL read, read as DATA; a
    `gh` failure yields run=false (no review, no red), never a spurious re-review.

The tests drive the REAL script with a fake `gh` on PATH so the decision logic
(not a re-implementation) is exercised; one test pins that the script actually
head-scopes its API query.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "decide-pr-review-trigger.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review.yaml"
# This repository's own caller of the reusable reviewer. Its `if:` names every
# event that reaches `decide`, which is what the repeatable-action test walks.
CALLER_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"
# The skip predicate the caller runs, which every consumer calls rather than
# copies.
SKIP_ROUTING_WORKFLOW = (
    REPO_ROOT / ".github" / "workflows" / "review-skipped-approval.yaml"
)
HEAD_SHA = "cafef00dcafef00dcafef00dcafef00dcafef00d"
# The commit an existing reviewer verdict was left against.
REVIEWED_SHA = "0ldc0de0ldc0de0ldc0de0ldc0de0ldc0de0ldc0"


def _fake_gh(
    tmp_path: Path,
    *,
    message: str = "",
    review_states: tuple[str, ...] = (),
    fail: bool = False,
    commits_fail: bool = False,
) -> None:
    """A `gh` stub that records each call's argv (appended to $GH_ARGV_FILE) and
    answers the API reads the script makes by branching on the request path:
    `.../commits/<sha>` echoes the head commit `message`, and the `graphql`
    reviews query emits ONE NDJSON node per entry in `review_states`, oldest
    first, exactly as `real_reviewer_reviews`' per-page filter would emit them
    (nothing at all for an empty tuple, which is how "never reviewed" reaches the
    script). The count is what the script compares against MAX_REVIEWS_PER_PR, so
    a test spends a read per entry. Exits non-zero for every call when `fail`.

    `commits_fail` refuses the commit read ALONE, the way the real gh refuses one:
    the HTTP error body goes to STDOUT and the exit status is non-zero. That
    combination is what a caller can get wrong — the body is a string, so a reader
    that keeps it on failure searches the error text for the opt-in keyword."""
    gh = tmp_path / "gh"
    msg = message.replace("\\", "\\\\").replace('"', '\\"')
    # One line per state, dated in order, so the script's latest-by-submittedAt
    # fold reports the LAST entry and its count reports how many were posted.
    nodes = "".join(
        "printf '"
        f'{{"state":"{state}","body":"read {n}",'
        f'"submittedAt":"2024-01-{n + 1:02d}T00:00:00Z",'
        f'"reviewId":"{n + 1}","reviewedSha":"{REVIEWED_SHA}"}}'
        "\\n' ; "
        for n, state in enumerate(
            st.replace("\\", "\\\\").replace('"', '\\"') for st in review_states
        )
    )
    body = (
        "exit 7\n"
        if fail
        else (
            'case "$*" in\n'
            f"*graphql*) {nodes} ;;\n"
            f'*/commits/*) printf "%s" "{msg}" ; exit {7 if commits_fail else 0} ;;\n'
            "*) ;;\n"
            "esac\n"
        )
    )
    gh.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"$GH_ARGV_FILE"\n' + body,
        encoding="utf-8",
    )
    gh.chmod(0o755)


def _run(
    tmp_path: Path,
    action: str,
    *,
    message: str = "",
    review_states: tuple[str, ...] = (),
    fail: bool = False,
    head_sha: str = HEAD_SHA,
    expect_recheck: str | None = None,
    base_ref: str = "main",
    commits_fail: bool = False,
    label: str = "",
    review_label: str | None = None,
    max_reviews: str = "1",
) -> tuple[subprocess.CompletedProcess, str, str]:
    """Run the script with the fake gh on PATH; return (proc, run, argv).
    `expect_recheck` pins the emitted `recheck` — the flag that asks the review
    job to re-read the PR's reviews from inside its concurrency group. `base_ref`
    is put in the environment under both names a base guard would read, so a
    guard added later turns the non-default-base tests red."""
    _fake_gh(
        tmp_path,
        message=message,
        review_states=review_states,
        fail=fail,
        commits_fail=commits_fail,
    )
    out_file = tmp_path / "github_output"
    out_file.write_text("", encoding="utf-8")
    argv_file = tmp_path / "gh_argv"
    argv_file.write_text("", encoding="utf-8")
    # A live draft read needs these three to run at all, and it fails OPEN
    # without them. They are set so that a script which asked the PR for its
    # draft state would actually place the call, which is what
    # test_the_decision_never_asks_whether_the_pr_is_a_draft watches for.
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42}}), encoding="utf-8")
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "GITHUB_OUTPUT": str(out_file),
        "GH_ARGV_FILE": str(argv_file),
        "GH_TOKEN": "fake",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_EVENT_PATH": str(event),
        "RUNNER_TEMP": str(runner_temp),
        "ACTION": action,
        "REPO": "owner/repo",
        "HEAD_SHA": head_sha,
        "PR": "42",
        "BASE_REF": base_ref,
        "GITHUB_BASE_REF": base_ref,
        "LABEL": label,
        # The script takes no default of its own — review.yaml's input owns the
        # number — so every run names one, and the shipped default is pinned
        # against that input by test_the_workflow_owns_the_read_budget.
        "MAX_REVIEWS_PER_PR": max_reviews,
        # The shared reviews read retries; only the delay is test-tuned, so the
        # fail-safe test still exercises the real "ladder exhausted" path.
        "RETRY_BASE_DELAY": "0",
    }
    if review_label is not None:
        env["REVIEW_LABEL"] = review_label
    proc = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env
    )
    outputs = out_file.read_text(encoding="utf-8").splitlines()
    run_lines = [ln.split("=", 1)[1] for ln in outputs if ln.startswith("run=")]
    assert len(run_lines) == 1, f"expected exactly one run= line, got {run_lines}"
    # The model is the reusable workflow's `model` input, so this script must not
    # emit one: an output nothing reads would read as a second, drifting decision.
    assert not [ln for ln in outputs if ln.startswith("model=")], outputs
    recheck = [ln.split("=", 1)[1] for ln in outputs if ln.startswith("recheck=")]
    # Emitted on EVERY decision, not just the racing one: the review job's steps
    # read it as a `!= 'true'` gate, so an absent output is an implicit "no
    # re-check" that a rename would make permanent and invisible.
    assert len(recheck) == 1, f"expected exactly one recheck= line, got {recheck}"
    if expect_recheck is not None:
        assert recheck[0] == expect_recheck
    return proc, run_lines[0], argv_file.read_text(encoding="utf-8")


def test_first_review_always_runs(tmp_path: Path) -> None:
    """A newly opened PR is always reviewed, without consulting gh."""
    proc, run, argv = _run(tmp_path, "opened", expect_recheck="false")
    assert proc.returncode == 0, proc.stderr
    assert run == "true"
    # `opened` makes NO call at all. Gating the first look on the reviews read
    # would put a fallible query in front of the one event every PR depends on.
    assert argv.splitlines() == [], argv


def test_opened_reviews_even_when_a_verdict_already_exists(tmp_path: Path) -> None:
    """`opened` stays unconditional. GitHub fires it once per pull request, so it
    cannot be spent twice — and gating it on the reviews read would put a live
    API call in front of the one event every PR depends on for its first look."""
    _, run, argv = _run(tmp_path, "opened", review_states=("APPROVED",))
    assert run == "true"
    assert "graphql" not in argv


@pytest.mark.parametrize(
    "state", ["CHANGES_REQUESTED", "COMMENTED", "APPROVED", "DISMISSED"]
)
def test_a_draft_ready_toggle_after_a_verdict_is_not_re_read(
    tmp_path: Path, state: str
) -> None:
    """A PR marked ready AFTER it was reviewed buys no second whole-diff read.

    An author can toggle a PR between draft and ready without limit, and each
    toggle re-fires this workflow on the SAME head. PR #3437 paid for three
    whole-diff Opus reads against its one-read budget ($0.79 + $0.74 + $1.19 =
    $2.72) because two `ready_for_review` toggles each emitted run=true."""
    proc, run, argv = _run(tmp_path, "ready_for_review", review_states=(state,))
    assert proc.returncode == 0, proc.stderr
    assert run == "false"
    assert "graphql" in argv, "the toggle must consult the reviews it claims to check"


def test_a_draft_opened_pr_is_reviewed_on_ready_for_review(tmp_path: Path) -> None:
    """The other side of the same arm: a PR opened as a DRAFT spends no `opened`
    review, so its first `ready_for_review` must run the first pass. Reviewing
    only on a spent-read miss yields exactly that."""
    proc, run, argv = _run(
        tmp_path,
        "ready_for_review",
        review_states=(),
        # The same race the never-reviewed push path has: a review still being
        # generated is submitted-review-invisible, so the review job re-asks from
        # inside its concurrency group.
        expect_recheck="true",
    )
    assert proc.returncode == 0, proc.stderr
    assert run == "true"
    assert "graphql" in argv


@pytest.mark.parametrize("head_sha", [HEAD_SHA, REVIEWED_SHA])
def test_a_tagged_toggle_never_buys_a_read(tmp_path: Path, head_sha: str) -> None:
    """The [opus-review] opt-in is bought by a PUSH, never by a toggle.

    A draft->ready toggle carries no new commit, so one tagged head can be
    toggled without limit and would buy a whole-diff read on every toggle — the
    same unbounded spend, through the opt-in instead of the arm. The head is
    unread or already read; neither honors the tag. A push carrying the tag is
    what opts in, on a draft as much as on a ready PR — see
    test_synchronize_keyword_wins_over_a_spent_review and
    test_a_draft_still_opts_in_with_the_keyword."""
    _, run, argv = _run(
        tmp_path,
        "ready_for_review",
        message="[opus-review] big rework",
        review_states=("APPROVED",),
        head_sha=head_sha,
    )
    assert run == "false"
    assert "/commits/" not in argv, "a toggle reads no commit title"


# `opened` is the sole action GitHub fires exactly once per pull request, so it
# is the sole action allowed an unconditional review.
_ONCE_PER_PR_ACTIONS = frozenset({"opened"})


def test_every_repeatable_subscribed_action_respects_the_spent_read(
    tmp_path: Path,
) -> None:
    """The invariant, driven from the caller's own event list: every action the
    reviewer is called on that can fire more than once on one PR must decline
    when the one whole-diff read is already spent. Subscribing to a new
    repeatable action without giving it that check fails here rather than
    silently costing one Opus read per fire.

    The reusable workflow declares no `types:` of its own — the CALLER does — so
    the enumeration comes from this repository's own caller."""
    repeatable = [
        action
        for action in _caller_review_actions()
        if action not in _ONCE_PER_PR_ACTIONS
    ]
    assert repeatable, "expected the reviewer to subscribe to a repeatable action"
    for action in repeatable:
        work = tmp_path / action
        work.mkdir()
        _, run, _ = _run(
            work,
            action,
            message="fix(ci): an ordinary follow-up, no opt-in",
            review_states=("COMMENTED",),
        )
        assert run == "false", f"{action} re-read a PR whose one read is spent"


def test_the_opt_in_label_buys_a_read_the_callers_own_guard_would_skip(
    tmp_path: Path,
) -> None:
    """A caller whose `if:` skips a PR class (a low-risk title, a bot author)
    offers its authors a label instead. The label is the only path such a PR
    has: it reaches no `opened` arm the caller filtered out, and `synchronize`
    is filtered the same way. A review already on the PR does not spend it —
    a human asked for this read."""
    _, run, _ = _run(
        tmp_path, "labeled", label="needs-auto-review", review_states=("COMMENTED",)
    )
    assert run == "true"


def test_an_unrelated_label_buys_nothing(tmp_path: Path) -> None:
    """Every label edit fires the same event, so matching anything but the
    opt-in name would spend a whole-diff read per label a maintainer adds."""
    _, run, _ = _run(tmp_path, "labeled", label="documentation")
    assert run == "false"


def test_the_caller_names_which_label_opens_the_hatch(tmp_path: Path) -> None:
    """The name is the caller's, because the skip notice that advertises it is
    the caller's."""
    _, run, _ = _run(
        tmp_path, "labeled", label="please-review", review_label="please-review"
    )
    assert run == "true"


def test_an_empty_label_name_is_not_a_wildcard(tmp_path: Path) -> None:
    """A caller that passes an empty `review-label` gets the default, never a
    matcher that answers true for the empty LABEL every other event carries."""
    _, run, _ = _run(tmp_path, "labeled", label="", review_label="")
    assert run == "false"


def test_synchronize_runs_on_keyword_in_subject(tmp_path: Path) -> None:
    proc, run, _ = _run(
        tmp_path,
        "synchronize",
        message="[opus-review] revise the fan-out\n\nbody",
        # The opt-in is bought deliberately ON TOP of a spent review, so it must
        # never ask for the re-check that cancels a redundant read.
        expect_recheck="false",
    )
    assert proc.returncode == 0, proc.stderr
    assert run == "true"


def test_synchronize_keyword_is_case_insensitive(tmp_path: Path) -> None:
    _, run, _ = _run(tmp_path, "synchronize", message="[OPUS-REVIEW] please relook")
    assert run == "true"


@pytest.mark.parametrize(
    "state", ["CHANGES_REQUESTED", "COMMENTED", "APPROVED", "DISMISSED"]
)
def test_a_push_after_any_verdict_is_not_re_read(tmp_path: Path, state: str) -> None:
    """ANY existing verdict — including a dismissal or a still-outstanding
    CHANGES_REQUESTED — means the one whole-diff read is spent, so an ordinary
    push gets run=false. The review-findings gate holds the merge on the
    threads the read opened until a later push resolves each; only the
    [opus-review] opt-in buys another read."""
    proc, run, argv = _run(
        tmp_path,
        "synchronize",
        message="fix(ci): address review",
        review_states=(state,),
    )
    assert proc.returncode == 0, proc.stderr
    assert run == "false"
    assert "graphql" in argv
    assert "--paginate" in argv, "a PR can have more reviews than one page holds"


def test_synchronize_keyword_wins_over_a_spent_review(tmp_path: Path) -> None:
    """An explicit [opus-review] opt-in runs a full re-read even though the one
    budgeted read is already spent — the opt-in is the ONLY thing that buys a
    second automated review, so it must fire before the spent-review check."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message="[opus-review] big rework",
        review_states=("CHANGES_REQUESTED",),
    )
    assert run == "true"


@pytest.mark.parametrize(
    "message",
    [
        "Merge pull request #3147 from AlexanderMattTurner/claude/env-prefix-lint",
        "fix(ci): drop the env prefix lint (#3147)",
    ],
)
def test_a_merged_stacked_child_does_not_buy_a_fresh_read(
    tmp_path: Path, message: str
) -> None:
    """A push whose head commit absorbed a merged stacked child gets NO extra
    read: the child's own PR was gated on the same review check, so every commit
    the merge carried in was already read. Pins both subjects GitHub generates
    for a PR merge — the merge-commit one and the squash one — so a re-read
    trigger keyed on either cannot come back as a workaround for enforcement."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message=message,
        review_states=("COMMENTED",),
    )
    assert run == "false"


@pytest.mark.parametrize(
    "message",
    [
        "chore(deps): merge origin/main into claude/install-noise",
        "Merge branch 'main' into claude/install-noise",
    ],
)
def test_a_base_branch_merge_does_not_buy_a_fresh_read(
    tmp_path: Path, message: str
) -> None:
    """Control: merging the base branch INTO the PR brings in code that already
    passed review on its way to the default branch, so it buys no re-read. Both
    subjects a base merge can carry are pinned: the conventional-commit one the
    commit-msg hook shapes for a local `git merge`, and the "Merge branch …" one
    GitHub's Update-branch button writes server-side, which no hook touches and
    which a loosened "Merge " prefix match would wrongly re-read."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message=message,
        review_states=("COMMENTED",),
    )
    assert run == "false"


@pytest.mark.parametrize("action", ["opened", "ready_for_review"])
def test_a_pr_based_on_a_feature_branch_is_still_reviewed(
    tmp_path: Path, action: str
) -> None:
    """A stacked child's base is another feature branch, not the default branch,
    and it is reviewed exactly like any other PR. Nothing in the decision reads
    the base ref, so a base guard added later — a `branches:` filter's
    equivalent — turns this red instead of silently leaving a whole class of PR
    unreviewed."""
    _, run, _ = _run(tmp_path, action, base_ref="claude/ct-guest-app-name-resolution")
    assert run == "true"


def test_a_never_reviewed_push_on_a_feature_branch_base_still_reviews(
    tmp_path: Path,
) -> None:
    """The same for the never-reviewed `synchronize` path: a stacked child that
    opened while the reviewer produced nothing still gets its first pass on the
    next push, whatever its base."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message="fix: ordinary push, no opt-in",
        review_states=(),
        base_ref="claude/ct-guest-app-name-resolution",
        expect_recheck="true",
    )
    assert run == "true"


def test_synchronize_runs_the_first_pass_when_no_review_exists(tmp_path: Path) -> None:
    """A push to a PR the reviewer has NEVER reviewed runs the first pass.

    This is the latch fix. The `opened` trigger is one-shot, so a first pass that
    produced no review (a diff over prepare-pr-review-input.sh's MAX_DIFF_LINES
    takes the oversized branch and posts a comment instead of a review; a
    cancelled job does the same) consumed the only event that ever reviews a PR,
    and nothing else re-arms it. PR #2688 opened at 32,746 diff lines, skipped
    the read, and then decided run=false on every subsequent push even after the
    diff fell to 10,312 lines — permanently unreviewable.

    This is also the arm a draft used to lose: the ready-PR cap drafts most PRs
    within seconds of `opened`, and a reviewer that deferred on a draft left
    this push as the PR's only route to a first pass."""
    proc, run, argv = _run(
        tmp_path,
        "synchronize",
        message="fix: ordinary push, no opt-in",
        review_states=(),
        # This read can race a review that is still generating, so the review job
        # is asked to re-read once its concurrency group has serialized it behind
        # the run that may be producing one.
        expect_recheck="true",
    )
    assert proc.returncode == 0, proc.stderr
    assert run == "true"
    assert "graphql" in argv


def test_synchronize_ignores_keyword_in_body_only(tmp_path: Path) -> None:
    """The opt-in must be in the commit TITLE (subject line); the keyword buried
    in the body does not re-trigger — matching the [breakout-ctf] title scope.

    Pinned against an APPROVED verdict, so the subject scoping is the ONLY thing
    that can decide this run: with no review on record the never-reviewed first
    pass would run regardless of the message, so that setup would pass without
    exercising the title match at all."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message="refactor: tidy things\n\nfollow-up [opus-review] later",
        review_states=("APPROVED",),
    )
    assert run == "false"


def test_a_refused_commit_read_is_discarded_rather_than_searched(
    tmp_path: Path,
) -> None:
    """A refused commit read must yield an EMPTY subject, which is what the arm's
    stated behaviour rests on. gh prints the HTTP error body on stdout, so a
    reader that keeps the capture on failure searches that body for the keyword —
    and GitHub's own 403 for a repository under review policy names it.

    Pinned against an APPROVED verdict, so the discard is the only thing that can
    decide this run."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message='{"message":"[opus-review] is not permitted","status":"403"}',
        review_states=("APPROVED",),
        commits_fail=True,
    )
    assert run == "false"


def test_synchronize_gh_failure_does_not_review(tmp_path: Path) -> None:
    """A transient API failure yields run=false (no red, no spurious review),
    never a crash.

    This is the counterweight to the never-reviewed trigger above: a FAILED
    reviews query produces the same empty state as a successful query over a PR
    with no reviews, but means the opposite — the script cannot tell whether a
    review exists, so it keeps the fail-safe. Keying that trigger on the empty
    state alone (rather than on the query's exit status) would spend a full
    review on every push after any API blip."""
    proc, run, _ = _run(tmp_path, "synchronize", fail=True)
    assert proc.returncode == 0, proc.stderr
    assert run == "false"
    assert "could not read" in proc.stdout, (
        "the fail-safe must be distinguishable from a spent review"
    )


def test_synchronize_fetches_the_head_commit_by_sha(tmp_path: Path) -> None:
    """The lookup fetches the head commit DIRECTLY by SHA, not the PR-commits
    list (which the API caps at 250, dropping the head on a heavily-revised PR —
    the exact case this re-trigger serves). So the [opus-review] opt-in is read
    from exactly the tagged head, cap-immune."""
    _, _, argv = _run(tmp_path, "synchronize", message="[opus-review] x")
    assert f"repos/owner/repo/commits/{HEAD_SHA}" in argv
    assert "/pulls/42/commits" not in argv, "must not use the 250-capped list"


def test_unhandled_action_does_not_review(tmp_path: Path) -> None:
    _, run, argv = _run(tmp_path, "reopened")
    assert run == "false"
    assert "graphql" not in argv, "an unhandled action must not read the reviews"
    assert "/commits/" not in argv, "an unhandled action must not read a commit"


# The `_fake_gh` above hands the script one canned node, so it never runs the real
# filter in lib/pr-reviews.bash. Under `--paginate`, gh applies `--jq` to EACH page
# separately and streams the results, and the shared function's trailing `jq -rs`
# folds that stream to the latest by submittedAt. The stub below reproduces the
# per-page application, so the login match and the cross-page fold are genuinely
# exercised.
_FAKE_GH_REAL_JQ = r"""#!/usr/bin/env bash
printf "%s\n" "$*" >>"$GH_ARGV_FILE"
argv=("$@")
jq_prog=""
for ((i = 0; i < ${#argv[@]}; i++)); do
  [[ "${argv[i]}" == "--jq" ]] && jq_prog="${argv[i + 1]}"
done
case "$*" in
*graphql*)
  # One `--jq` application per page, exactly as gh --paginate does it. Each canned
  # page is a nodes array, wrapped back into the response envelope the real filter
  # indexes into.
  jq -c '.[]' "$REVIEWS_JSON" | while IFS= read -r nodes; do
    printf '%s' "$nodes" |
      jq -c '{data: {repository: {pullRequest: {reviews: {nodes: .}}}}}' |
      jq -r "$jq_prog"
  done
  ;;
*/commits/*) printf "%s" "${HEAD_MSG:-}" ;;
*) ;;
esac
"""


def _run_real_jq(
    tmp_path: Path, *, reviews_pages: list, message: str = "", max_reviews: str = "1"
) -> tuple[str, str]:
    """Run the real script with a gh stub that applies its --jq once per page of a
    canned payload (a list of per-page review-NODE arrays), as gh --paginate does.
    Returns (run, decision-line): the decision line because run=false has
    TWO producers — a spent review and the API fail-safe — and only the reason
    distinguishes them."""
    gh = tmp_path / "gh"
    gh.write_text(_FAKE_GH_REAL_JQ, encoding="utf-8")
    gh.chmod(0o755)
    reviews_json = tmp_path / "reviews.json"
    reviews_json.write_text(json.dumps(reviews_pages), encoding="utf-8")
    out_file = tmp_path / "github_output"
    out_file.write_text("", encoding="utf-8")
    argv_file = tmp_path / "gh_argv"
    argv_file.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(out_file),
            "GH_ARGV_FILE": str(argv_file),
            "GH_TOKEN": "fake",
            "ACTION": "synchronize",
            "REPO": "owner/repo",
            "HEAD_SHA": HEAD_SHA,
            "PR": "42",
            "RETRY_BASE_DELAY": "0",
            "REVIEWS_JSON": str(reviews_json),
            "HEAD_MSG": message,
            "MAX_REVIEWS_PER_PR": max_reviews,
        },
    )
    assert proc.returncode == 0, proc.stderr
    outputs = out_file.read_text(encoding="utf-8").splitlines()
    run = [ln.split("=", 1)[1] for ln in outputs if ln.startswith("run=")][0]
    decision = [ln for ln in proc.stdout.splitlines() if ln.startswith("decision:")][0]
    return run, decision


def _bot_review(
    state: str, body: str = "Automated review.", oid: str = REVIEWED_SHA
) -> dict:
    """A REAL reviewer review: the body is non-empty because the reviewer never
    posts an empty one (post-pr-review.mjs falls back to "Automated review."),
    and the shared read filters empty-bodied reviews out as synthesized shells."""
    return {
        "author": {"login": "github-actions"},
        "state": state,
        "body": body,
        "submittedAt": "2024-01-01T00:00:00Z",
        "fullDatabaseId": "4802416227",
        "commit": {"oid": oid},
    }


def _human_review(state: str) -> dict:
    return {
        "author": {"login": "some-human"},
        "state": state,
        "body": "",
        "submittedAt": "2024-01-02T00:00:00Z",
        "fullDatabaseId": "4802416228",
        "commit": {"oid": "f00df00df00df00df00df00df00df00df00df00d"},
    }


def test_a_reviewer_verdict_in_the_payload_reads_as_spent(tmp_path: Path) -> None:
    """A reviewer verdict in the payload must survive the real jq filter and
    reach the script as a non-empty state, so the spent-review branch fires. The
    DECISION LINE is what proves which branch ran: a filter that dropped the
    node would read as "never reviewed" and run a review on every push."""
    run, decision = _run_real_jq(
        tmp_path, reviews_pages=[[_bot_review("CHANGES_REQUESTED")]]
    )
    assert run == "false", "a push after a verdict is not re-read"
    assert "spent all 1 read(s)" in decision, decision


def test_a_reviewer_verdict_on_a_LATER_page_still_counts(tmp_path: Path) -> None:
    """`--paginate` must walk every page: a reviewer verdict that lands only on a
    later page, behind a page of other people's reviews, is still a verdict. Read
    as absent, it would buy a whole-diff review on every push."""
    run, decision = _run_real_jq(
        tmp_path,
        reviews_pages=[[_human_review("COMMENTED")], [_bot_review("APPROVED")]],
    )
    assert run == "false", "the reviewer's verdict on page 2 is still a verdict"
    assert "spent all 1 read(s)" in decision, decision


def test_an_empty_body_bot_review_still_owes_the_first_pass(tmp_path: Path) -> None:
    """GitHub synthesizes an empty-body COMMENTED review by the same bot around
    every standalone review-comment POST (a merge-delta finding thread, a
    merge-delta audit reply). Neither is the whole-diff read, so it must not
    spend the one-review budget: filtered by the shared read, it leaves the
    PR "never reviewed" and trigger 2 runs the real first pass. Counting it
    would latch the PR permanently unreviewed — the very bug trigger 2 fixes."""
    run, decision = _run_real_jq(
        tmp_path, reviews_pages=[[_bot_review("COMMENTED", body="")]]
    )
    assert run == "true", "an empty-body review is a synthesized shell, not a read"
    assert "spent 0 of 1 read(s)" in decision, decision


def test_a_higher_budget_counts_real_reviews_across_pages(tmp_path: Path) -> None:
    """The count runs through the library's OWN per-page --jq, one page at a
    time, against real jq rather than the stub the other budget cases use. The
    per-page fold it guards against could only live inside that --jq, so this
    case adds the real-jq path, not a comparison the stub cases miss."""
    run, decision = _run_real_jq(
        tmp_path,
        reviews_pages=[[_bot_review("COMMENTED")], [_bot_review("APPROVED")]],
        max_reviews="2",
    )
    assert run == "false", "two reads against a budget of two is spent"
    assert "spent all 2 read(s)" in decision, decision


def test_only_other_peoples_reviews_still_owes_the_first_pass(tmp_path: Path) -> None:
    """The login filter is load-bearing in the other direction: a PR reviewed only
    by humans has had no automated pass, so trigger 2 must still fire."""
    run, _ = _run_real_jq(
        tmp_path, reviews_pages=[[_human_review("CHANGES_REQUESTED")]]
    )
    assert run == "true", "no reviewer verdict exists — the first pass is still owed"


def test_a_higher_budget_buys_the_next_read(tmp_path: Path) -> None:
    """The budget is a COUNT, not a has-been-reviewed flag: a caller that sets
    max-reviews-per-pr to 2 gets a second whole-diff read on the next push."""
    proc, run, _ = _run(
        tmp_path,
        "synchronize",
        review_states=("COMMENTED",),
        max_reviews="2",
        expect_recheck="true",
    )
    assert proc.returncode == 0, proc.stderr
    assert run == "true"


def test_the_budget_stops_the_read_once_every_one_is_spent(tmp_path: Path) -> None:
    """Two reads against a budget of two is spent, so the third push buys
    nothing. A comparison that only asked whether ANY review exists would give
    the same answer here as the one-read budget and hide the counting entirely."""
    proc, run, _ = _run(
        tmp_path,
        "synchronize",
        review_states=("COMMENTED", "CHANGES_REQUESTED"),
        max_reviews="2",
    )
    assert proc.returncode == 0, proc.stderr
    assert run == "false"


def test_a_zero_budget_leaves_even_opened_unreviewed(tmp_path: Path) -> None:
    """0 is how a caller turns the automatic reviewer off for every PR. `opened`
    is the arm that would otherwise fire unconditionally, so it reads the budget
    rather than the review list — no review can exist on a PR being opened."""
    proc, run, argv = _run(tmp_path, "opened", max_reviews="0")
    assert proc.returncode == 0, proc.stderr
    assert run == "false"
    assert argv.splitlines() == [], "the count is 0 by construction, not by query"


def test_the_head_commit_opt_in_outranks_a_spent_budget(tmp_path: Path) -> None:
    """[opus-review] sits ABOVE the budget: it is an explicit request for one
    more read. Under the budget it would be unreachable exactly when a human
    asks for a re-read, which is the case it exists for."""
    _, run, _ = _run(
        tmp_path,
        "synchronize",
        message="fix(gate): rework the closure [opus-review]",
        review_states=("APPROVED",),
        max_reviews="0",
    )
    assert run == "true"


def test_the_review_label_outranks_a_spent_budget(tmp_path: Path) -> None:
    """The label is the other explicit request, so it outranks the budget too —
    a human adding it has asked for this PR to be read."""
    _, run, _ = _run(
        tmp_path,
        "labeled",
        label="needs-auto-review",
        review_states=("APPROVED",),
        max_reviews="0",
    )
    assert run == "true"


def test_an_unreadable_budget_is_refused_rather_than_compared(tmp_path: Path) -> None:
    """`[[ x -lt y ]]` evaluates its operands arithmetically, and a bare word
    that names no variable is 0 there. So an unvalidated typo would compare
    against 0 and silently review NEVER, with a green decide job and no notice."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "MAX_REVIEWS_PER_PR": "one"},
    )
    assert proc.returncode != 0
    assert "must be a whole number" in proc.stderr, proc.stderr


def test_a_leading_zero_budget_is_refused_rather_than_read_as_octal(
    tmp_path: Path,
) -> None:
    """`^[0-9]+$` admits `08`, and bash then reads it as octal with an invalid
    digit: `[[ -lt ]]` errors and answers FALSE, so this script silently reviews
    nothing while the re-check, comparing the other way, reviews everything."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "MAX_REVIEWS_PER_PR": "08"},
    )
    assert proc.returncode != 0
    assert "no leading zero" in proc.stderr, proc.stderr


def test_a_budget_past_the_arithmetic_range_is_refused(tmp_path: Path) -> None:
    """Bash arithmetic is 64-bit and WRAPS: `[[ 0 -lt 9223372036854775808 ]]` is
    false, so a budget past that range would review nothing while decide stayed
    green. The refusal is what keeps every admitted value comparable."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "MAX_REVIEWS_PER_PR": "9223372036854775808"},
    )
    assert proc.returncode != 0
    assert "from 0 to 999" in proc.stderr, proc.stderr


def test_a_zero_budget_decides_a_push_without_reading_the_reviews(
    tmp_path: Path,
) -> None:
    """A budget of 0 is decided by the constant, so a push spends no paginated
    GraphQL walk to reach an answer that cannot depend on it. The absent walk is
    what this test pins: `run == "false"` also holds with the short-circuit gone,
    because a count of 0 is not less than a budget of 0."""
    _, run, argv = _run(tmp_path, "synchronize", max_reviews="0")
    assert run == "false"
    assert "graphql" not in argv, argv


def test_the_budget_must_be_passed_in(tmp_path: Path) -> None:
    """No default in the script: review.yaml's input owns the number, and a
    second default here is a copy that drifts out of sight of the caller."""
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode != 0
    assert "MAX_REVIEWS_PER_PR required" in proc.stderr, proc.stderr


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _caller_review_actions() -> list[str]:
    """The `github.event.action` values this repository's caller runs the
    reusable reviewer on, read out of that job's own `if:` guard."""
    doc = yaml.safe_load(CALLER_WORKFLOW.read_text(encoding="utf-8"))
    guard = doc["jobs"]["review"]["if"]
    actions = json.loads(
        re.search(r"fromJSON\('(?P<events>\[[^']*\])'\)", guard).group("events")
    )
    assert actions, f"no event list in the caller's review guard: {guard}"
    return actions


def test_decide_reviews_every_pr() -> None:
    """Every PR reaches the decide script — no skips by title, author, or draft
    state. A real Claude read is the sign-off for every PR, including
    chore/style/release and bot-authored ones, so none is rubber-stamped unread.

    Drafts included: the ready-PR cap drafts most PRs within seconds of
    `opened`, so a `draft == false` here would hold every review until the work
    was finished."""
    guard = " ".join(_workflow()["jobs"]["decide"]["if"].split())
    assert guard == "github.event_name == 'pull_request_target'"
    for dropped in ("'chore:'", "'style:'", "'release:'", "'Bot'", "'labeled'"):
        assert dropped not in guard, f"decide must not skip on {dropped}"


def test_no_auto_approve_job() -> None:
    """The rubber-stamp auto-approve job is removed: every PR gets a real review,
    so nothing blind-approves a skipped class. Pin its absence so it can't creep
    back."""
    assert "auto-approve-skipped" not in _workflow()["jobs"]


def test_the_workflow_owns_the_read_budget() -> None:
    """Both scripts refuse to run without MAX_REVIEWS_PER_PR, so the workflow is
    the one place the number is written. Both steps must read the SAME input: a
    recheck on a different budget would cancel a read decide had just approved,
    and the PR would go unreviewed with both jobs green."""
    # `on` is YAML 1.1's boolean true, so the parsed key is True, not the string.
    budget = _workflow()[True]["workflow_call"]["inputs"]["max-reviews-per-pr"]
    assert budget["default"] == 1, "one whole-diff read per PR is the shipped budget"
    assert budget["type"] == "number"
    steps = [
        *_workflow()["jobs"]["decide"]["steps"],
        *_workflow()["jobs"]["review"]["steps"],
    ]
    readers = [
        s["env"]["MAX_REVIEWS_PER_PR"]
        for s in steps
        if "max-reviews-per-pr" in str(s.get("env", {}))
    ]
    assert readers == ["${{ inputs.max-reviews-per-pr }}"] * 2, readers


def test_decide_step_passes_the_pr_number() -> None:
    """The script reads the reviews API by PR number; the decide step must feed
    it PR, or the spent-review check can never see an existing verdict and every
    push would buy a fresh whole-diff review."""
    steps = _workflow()["jobs"]["decide"]["steps"]
    decide = next(s for s in steps if s.get("id") == "decide")
    assert decide["env"]["PR"] == "${{ github.event.pull_request.number }}"


def test_a_cancelled_shard_leg_cannot_discard_a_complete_sharded_review() -> None:
    """review_synthesis must run even when a shard leg did not succeed.

    A shard leg uploads its findings and THEN publishes its log, so an expiry or
    cancellation in that tail leaves a complete set of shard reviews on disk under
    a non-success job conclusion. Gating the synthesis on the legs' conclusion
    throws that complete read away, and nothing re-arms it: decide's
    never-reviewed trigger fires only on a later push, and a newer run's recheck
    has already yielded to this run. The coverage invariant the conclusion gate
    stood in for lives in merge-shard-reviews.mjs, which reds this job when a
    shard review is genuinely missing.
    """
    condition = _workflow()["jobs"]["review_synthesis"]["if"]
    assert "!cancelled()" in condition
    # Still gated on the read itself having happened and having been sharded —
    # !cancelled() alone would run the synthesis on every unsharded PR.
    assert "needs.review.result == 'success'" in condition
    assert "needs.review.outputs.sharded == 'true'" in condition
    # The regression stated as the absence it is: ANY gate on the shard LEGS'
    # conclusion discards a complete read when a leg dies after its upload.
    assert "needs.review_shard" not in condition


def test_a_shard_leg_gets_the_whole_diff_read_s_time_budget() -> None:
    """Each shard leg's timeout must be at least the whole-diff review job's.

    A shard slices the DIFF, not the model's thinking, so a leg runs to the same
    measured tail as an unsharded read — and then spends about two more minutes
    on the redacting log upload. A shorter budget expires in that tail and
    CANCELS the leg after its findings are uploaded, which is how PR #3280 lost
    all three of its shard reviews.
    """
    jobs = _workflow()["jobs"]
    assert jobs["review_shard"]["timeout-minutes"] >= jobs["review"]["timeout-minutes"]


@pytest.mark.parametrize("action", ["opened", "ready_for_review", "synchronize"])
def test_the_decision_never_asks_whether_the_pr_is_a_draft(
    tmp_path: Path, action: str
) -> None:
    """A draft is decided exactly like a ready PR, on every event.

    The only way the script could treat one differently is by asking, and
    `repos/owner/repo/pulls/42` is the one request that answers. The ready-PR
    cap drafts most PRs within seconds of `opened`, so a script that asked and
    deferred would hold the reviewer's feedback until the work was finished.
    """
    _, run, argv = _run(tmp_path, action, review_states=())
    assert run == "true"
    assert "pulls/42" not in argv, argv


def _caller_skip_expression() -> str:
    """The event-payload half of the caller's skip predicate — the `PAYLOAD_SKIP`
    expression the `classify` job computes and hands to its decide script."""
    doc = yaml.safe_load(SKIP_ROUTING_WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["classify"]["steps"]
    decide = next(s for s in steps if s.get("id") == "decide")
    return " ".join(decide["env"]["PAYLOAD_SKIP"].split())


def test_only_github_set_fields_decide_the_caller_skip_set() -> None:
    """No field the pull request can CHOOSE may put it in the skip set.

    A skipped PR is routed to the `approve` job, which posts an approving
    review under the reviewer's identity — so an author-written field buys an
    unread approval. The TITLE did: `chore: drop the egress allowlist` was
    reviewed by nobody and approved by the bot. Pinning the enumerated field set
    rather than banning the one spelling fails closed on the next arm of the same
    shape — `head.ref`, `body`, `labels` are all author-picked too.
    """
    skip = _caller_skip_expression()
    fields = set(
        re.findall(r"github\.event\.pull_request\.(?P<field>[A-Za-z_.]+)", skip)
    )
    assert fields == {"draft", "head.repo.full_name", "user.type"}, (
        f"only GitHub-set fields may decide the skip set: {skip}"
    )


def test_the_caller_skip_set_also_reads_the_head_commits() -> None:
    """The payload half alone gates on the OPENER, which never changes, while the
    head does: a same-repo bot PR's branch is pushable by any collaborator, and
    the job re-runs on `synchronize`. So a human commit pushed onto a dependabot
    branch would take the approval its opener bought. The decide step must run the
    script that reads the commits."""
    doc = yaml.safe_load(SKIP_ROUTING_WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["classify"]["steps"]
    decide = next(s for s in steps if s.get("id") == "decide")
    assert "classify-review-skip.sh" in decide["run"], decide["run"]


def _auto_approval_marker() -> str:
    """The marker string out of the ONE definition the producer and the read
    share — never a literal copied into this test."""
    lib = REPO_ROOT / ".github" / "reviewer" / "lib" / "pr-reviews.bash"
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; printf %s "$AUTO_APPROVAL_MARKER"', "_", str(lib)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout, "the library must define AUTO_APPROVAL_MARKER"
    return proc.stdout


def test_the_stand_in_approval_leaves_the_first_pass_owed(tmp_path: Path) -> None:
    """A PR the caller skipped carries an APPROVE from the auto-approve job, posted
    with GITHUB_TOKEN and so under the reviewer's own login with a non-empty body.

    Read as a verdict it latches the PR permanently unread: every later event
    answers "already reviewed", so a PR that leaves the skip class gets no first
    pass, and the `needs-auto-review` label buys a run that reviews nothing.
    """
    run, decision = _run_real_jq(
        tmp_path,
        reviews_pages=[
            [
                _bot_review(
                    "APPROVED",
                    body=f"{_auto_approval_marker()}\nAutomated approval.",
                )
            ]
        ],
    )
    assert run == "true", "an approval nobody read leaves the first pass owed"
    assert "spent 0 of 1 read(s)" in decision, decision
