#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against a stubbed `gh` on PATH, so the branches are asserted but
#   no run is ever traced.
# Ask for the ACCUMULATED review of the pushes a pull request has collected since
# the head its last review covered — and ask only when the pull request is
# otherwise ready to merge. A sweep calls this per open pull request; it starts a
# `workflow_dispatch` run of the caller's reviewer workflow, or explains why not.
#
# PROBLEM CLASS — a reviewer that reads once and a branch that keeps moving. The
# first read covers the head it ran on; every later push is unread, and the merge
# gate greens on the review of older code. Reading every push is what the read
# budget exists to prevent, so the reads are BATCHED instead: one read, of
# everything since the covered head, once the pull request stops moving.
#
# READINESS is what makes "stops moving" checkable, and it is defined by the
# checks the pull request already runs:
#   * every check and status on the live head has concluded successfully, EXCEPT
#     the review gate itself — waiting for the gate would deadlock, because the
#     gate is red precisely while this read is owed;
#   * no unresolved reviewer thread still holds the merge — those name work the
#     author has yet to push, so reading now would read a head about to change;
#   * the pull request is open and not a draft.
# A burst of pushes therefore coalesces with no timer: each push reds the checks
# again, and only the head that survives long enough to go green is read.
#
# DUPLICATE dispatches cost a queued job, not a read: the review job's own
# concurrency group serializes them and its owed-review re-check answers skip
# once this budget is spent. This script is the cheap filter, not the lock.
#
# BOUNDING a read that keeps failing needs the runs to be attributable to a pull
# request, and only the caller's `run-name:` can carry that. The run name must END
# with `PR <number>` (override the whole suffix with RUN_NAME_MATCH), matched as an
# exact suffix so PR 1 never claims PR 12's runs. A caller whose run-name does not
# end that way gets no bound and re-dispatches a failing read each sweep — the
# README says so beside the input. Hitting the bound POSTS a stamped notice and
# re-posts the gate verdict: the stamp ENDS the accumulated read, so the gate
# stops waiting, and the notice says which commits went unread. A later run reads
# that stamp and asks for nothing, whatever the budget says.
#
# Env: GH_TOKEN, GH_REPO (owner/name), PR, REVIEW_WORKFLOW (the caller's workflow
# file name), SEVERITY_CONFIG, MAX_DELTA_REVIEWS_PER_PR.
# Optional: DISPATCH_REF (default branch by default), GATE_CONTEXT,
# REVIEWER_LOGIN, RUN_NAME_MATCH, MAX_FAILED_DELTA_RUNS (2).
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/reviewer/lib/pr-reviews.bash
source "$_SCRIPT_DIR/lib/pr-reviews.bash"
# shellcheck source=.github/reviewer/lib/review-threads.bash
source "$_SCRIPT_DIR/lib/review-threads.bash"

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${REVIEW_WORKFLOW:?REVIEW_WORKFLOW required — the caller workflow file the dispatch starts}"
: "${SEVERITY_CONFIG:?SEVERITY_CONFIG required — the severity SSOT of the consumer}"
require_delta_review_budget

owner="${GH_REPO%%/*}"
name="${GH_REPO##*/}"
# GraphQL omits the REST `[bot]` suffix; both shared reads compare the bare login.
REVIEWER_LOGIN_BARE="${REVIEWER_LOGIN:-github-actions}"
REVIEWER_LOGIN_BARE="${REVIEWER_LOGIN_BARE%'[bot]'}"
export REVIEWER_LOGIN_BARE
# The one check whose red state must NOT hold this dispatch back, from the same
# SSOT the gate posts it under.
GATE_CONTEXT="${GATE_CONTEXT:-$(jq -er '.gate_context' "$SEVERITY_CONFIG")}"
MAX_FAILED_DELTA_RUNS="${MAX_FAILED_DELTA_RUNS:-2}"

skip() { # $1 reason
  echo "no accumulated review of ${GH_REPO}#${PR}: $1" >&2
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "dispatched=false" >>"$GITHUB_OUTPUT"
  fi
  exit 0
}

if [[ "$MAX_DELTA_REVIEWS_PER_PR" -eq 0 ]]; then
  skip "max-delta-reviews-per-pr is 0, so no accumulated review runs"
fi

pr_json="$(retry_stdout gh pr view "$PR" --repo "$GH_REPO" \
  --json state,isDraft,headRefOid,statusCheckRollup)"
[[ "$(jq -r '.state' <<<"$pr_json")" == "OPEN" ]] || skip "it is not open"
[[ "$(jq -r '.isDraft' <<<"$pr_json")" == "false" ]] || skip "it is a draft"
head_sha="$(jq -r '.headRefOid // ""' <<<"$pr_json")"
[[ -n "$head_sha" ]] || skip "GitHub reported no head commit for it"

reviews="$(reviewer_reviews_ndjson "$owner" "$name" "$PR")"
coverage="$(coverage_of_reviews <<<"$reviews")"
[[ -n "$coverage" ]] || skip "no review covers it yet — its first read is the push-driven reviewer's"
covered="$(jq -r '.head // ""' <<<"$coverage")"
covered_at="$(jq -r '.submittedAt // ""' <<<"$coverage")"
[[ "$covered" != "$head_sha" ]] || skip "its head ${head_sha:0:7} is the head the last review read"

# Abandonment is terminal, and this is where that binds on the asking side: the
# notice below is a decision, so a later run must not make it again. Counted as
# one spent read instead, a budget above 1 posts one notice per sweep until the
# budget runs out.
[[ "$(delta_read_abandoned <<<"$reviews")" != "true" ]] ||
  skip "its accumulated read was abandoned — a notice on the pull request says which commits went unread"

deltas="$(delta_reviews_count <<<"$reviews")"
[[ "$deltas" -lt "$MAX_DELTA_REVIEWS_PER_PR" ]] ||
  skip "it has spent all ${MAX_DELTA_REVIEWS_PER_PR} accumulated read(s)"

gating="$(unresolved_gating_findings "$owner" "$name" "$PR" "$SEVERITY_CONFIG")"
gating_count="$(jq 'length' <<<"$gating")"
[[ "$gating_count" -eq 0 ]] ||
  skip "${gating_count} unresolved reviewer finding(s) still hold it — the author has work to push"

# Readiness, off the rollup already in hand. A check run must have COMPLETED with
# a conclusion that does not block a merge; a commit status must be SUCCESS. The
# review gate is excluded by name, because it is red exactly while this read is
# owed and waiting for it would deadlock.
not_ready="$(jq -r --arg gate "$GATE_CONTEXT" '
  [ .statusCheckRollup[]?
    | select((.name // .context // "") != $gate)
    | select(
        if .__typename == "StatusContext"
        then ((.state // "") | ascii_upcase) != "SUCCESS"
        else ((.status // "") | ascii_upcase) != "COMPLETED"
             or (((.conclusion // "") | ascii_upcase) as $c
                 | ["SUCCESS", "NEUTRAL", "SKIPPED"] | index($c)) == null
        end)
    | (.name // .context // "an unnamed check") ]
  | unique | join(", ")' <<<"$pr_json")"
[[ -z "$not_ready" ]] || skip "it is not ready to merge yet (${not_ready})"

# A read that keeps dying must not be re-dispatched forever. Runs are attributed
# by the caller's run-name, and only those started after the covered review count
# — a new push re-arms the read by moving the head, and the review that covered
# it is the boundary this window starts at.
match="${RUN_NAME_MATCH:-PR ${PR}}"
# The bound is a COUNT, so it needs every run in the window, not the newest page
# of them: a repository running 50 newer dispatches between two sweeps drops this
# PR's failures off page one, the count resets to zero, and the read it exists to
# stop is dispatched again. `created` asks the API for the same window the count
# is over, which is what keeps the paginated walk bounded. `%3E%3D` is `>=`.
failed="$(retry_stdout gh api --paginate \
  "repos/${GH_REPO}/actions/workflows/${REVIEW_WORKFLOW}/runs?event=workflow_dispatch&per_page=100&created=%3E%3D${covered_at}" \
  --jq ".workflow_runs[]
        | select((.display_title // \"\") | endswith(\"${match}\"))
        | select((.created_at // \"\") > \"${covered_at}\")
        | select(.status == \"completed\" and .conclusion != \"success\")
        | .id" | wc -l | tr -d '[:space:]')"
if [[ "$failed" -ge "$MAX_FAILED_DELTA_RUNS" ]]; then
  # Giving up is a decision, and a decision that stamps nothing holds the merge
  # forever: the gate waits while the live head is past the covered one and the
  # accumulated budget has room, and a crashed read moves neither. So say it out
  # loud, keeping the OLD covered head because nothing read the new one. The
  # scope marks the read ABANDONED, which the gate reads as terminal.
  # A read that landed while this script was deciding covers a head nobody had
  # read, and the notice would then abandon a pull request that was just
  # reviewed. Coverage is read again against the head this decision was made
  # from. The window narrows; it does not close.
  fresh="$(reviewer_reviews_ndjson "$owner" "$name" "$PR")"
  [[ "$(coverage_of_reviews <<<"$fresh" | jq -r '.head // ""')" == "$covered" ]] ||
    skip "a review landed while this was deciding, so the read it was about to abandon is done"
  body="$(
    printf '%s\n\n%s\n' \
      "$(coverage_stamp "$covered" "" "" delta "$ABANDONED_COVERAGE_SCOPE")" \
      "The accumulated review of this pull request was started ${failed} time(s) since the review of ${covered:0:7} and did not complete. Nothing will ask for it again, so the commits pushed after ${covered:0:7} are NOT reviewed. Fix the reviewer rather than re-running it; a human should read those commits before merging."
  )"
  retry gh api -X POST "repos/${GH_REPO}/pulls/${PR}/reviews" \
    -f "event=COMMENT" -f "body=${body}" >/dev/null
  # A review posted with the workflow GITHUB_TOKEN starts no workflow run, so
  # nothing re-reads the gate on its own and the hold this notice just released
  # would stand until the next sweep. Re-evaluate it here, from the same head the
  # gate's own clause (c) reads.
  PR="$PR" REPORT_SHA="$head_sha" SEVERITY_CONFIG="$SEVERITY_CONFIG" \
    bash "$_SCRIPT_DIR/review-findings-gate.sh"
  skip "${failed} accumulated read(s) of it have failed since the last review — said so on the pull request and ended the read; fix the reviewer rather than re-running it"
fi

dispatch_ref="${DISPATCH_REF:-$(retry_stdout gh api "repos/${GH_REPO}" --jq .default_branch)}"
retry gh workflow run "$REVIEW_WORKFLOW" --repo "$GH_REPO" --ref "$dispatch_ref" -f "pr=${PR}"
echo "asked for the accumulated review of ${GH_REPO}#${PR}: covered to ${covered:0:7}, head is ${head_sha:0:7}" >&2
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "dispatched=true" >>"$GITHUB_OUTPUT"
fi
