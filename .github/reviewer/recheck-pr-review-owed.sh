#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so the branches are asserted but
#   no run is ever traced.
# Re-ask, from inside the review job's per-PR concurrency group, whether this PR
# still owes a whole-diff review under `max-reviews-per-pr`; emits
# skip=true/false to GITHUB_OUTPUT.
# The group serializes review JOBS only, and a sharded review is posted later by
# review_synthesis (its own group) — so a submitted-reviews read alone still
# races the sharded path, and an earlier run of this workflow with a live
# shard/synthesis job on this PR also counts as a read being spent — that job, not
# the umbrella run around it. Fails toward REVIEWING: an exhausted query emits
# skip=false — losing a read the PR still owes is worse than one duplicate.
#
# CALLER CONTRACT — review.yaml runs this ONLY on decide's budget arm, which is
# the sole arm that emits recheck=true. This script judges the BUDGET alone, so
# it answers skip=true whenever the spent reads already fill it; at
# `max-reviews-per-pr: 0` that is every PR. Widening the step's `if:` to the
# `[opus-review]` keyword or the review label would therefore cancel exactly the
# two reads that are meant to fire whatever the count says.
#
# Env: GH_TOKEN, REPO, PR, GITHUB_RUN_ID, GITHUB_WORKFLOW_REF,
#      MAX_REVIEWS_PER_PR.
set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/reviewer/lib/pr-reviews.bash
source "$_SCRIPT_DIR/lib/pr-reviews.bash"
REPO="${REPO:?REPO (owner/name) required}"
PR="${PR:?PR (number) required}"
RUN_ID="${GITHUB_RUN_ID:?GITHUB_RUN_ID required}"
# owner/repo/.github/workflows/<file>@ref -> <file>, scoping the runs query to
# this workflow alone.
WORKFLOW_FILE="${GITHUB_WORKFLOW_REF:?GITHUB_WORKFLOW_REF required}"
WORKFLOW_FILE="${WORKFLOW_FILE%%@*}"
WORKFLOW_FILE="${WORKFLOW_FILE##*/}"
# GraphQL omits the REST `[bot]` suffix; the shared read's jq reads this from env.
export REVIEWER_LOGIN_BARE="github-actions"
# The same budget decide-pr-review-trigger.sh reads, through the same helper, so
# the two cannot disagree about whether this PR still owes a read.
require_review_budget

emit() {
  # $1 skip, $2 reason
  echo "skip=$1" >>"$GITHUB_OUTPUT"
  echo "recheck: skip=$1 ($2)"
}

reviews_rc=0
count=0
spent="$(real_reviewer_reviews "${REPO%%/*}" "${REPO##*/}" "$PR" 2>/dev/null)" || reviews_rc=$?
# Folded into the same status capture, for the reason decide's own read gives: a
# jq failure leaves the count as unknown as a failed walk does.
[[ "$reviews_rc" -ne 0 ]] || count="$(jq -rs 'length' <<<"$spent")" || reviews_rc=$?
if [[ "$reviews_rc" -ne 0 ]]; then
  emit false "could not read $REPO#$PR reviews (exhausted the retry ladder, rc=$reviews_rc) — reviewing rather than risking a read the PR still owes"
  exit 0
fi
if [[ "$count" -ge "$MAX_REVIEWS_PER_PR" ]]; then
  # Any state — including DISMISSED — counts as spent, matching decide's trigger 2.
  state="$(latest_of_reviews <<<"$spent" | jq -r '.state // ""')"
  emit true "$REPO#$PR has spent all $MAX_REVIEWS_PER_PR read(s) (latest: $state) — this run buys none"
  exit 0
fi

# The sharded path: the earlier run's review job released the group while its
# shard/synthesis jobs are still generating the review, so no submitted review
# exists yet. Those jobs are what this run must yield to. A run whose
# pull_requests list is empty (a fork PR, or a PR that has since closed) never
# matches, which fails toward reviewing.
#
# Candidates are filtered client-side on "not completed" rather than with the
# API's status=in_progress: a run whose shard legs still wait for runners reports
# `queued`. Only runs OLDER than this one count — a newer run is waiting on us.
runs_rc=0
candidates="$(
  retry_stdout gh api \
    "repos/$REPO/actions/workflows/$WORKFLOW_FILE/runs?event=pull_request_target&per_page=100" 2>/dev/null |
    jq -r --argjson run_id "$RUN_ID" --argjson pr "$PR" \
      '.workflow_runs[]
        | select(.status != "completed")
        | select(.id < $run_id)
        | select(any(.pull_requests[]?.number; . == $pr))
        | .id'
)" || runs_rc=$?
if [[ "$runs_rc" -ne 0 ]]; then
  emit false "could not read $REPO in-flight runs (exhausted the retry ladder, rc=$runs_rc) — reviewing rather than risking a read the PR still owes"
  exit 0
fi

# A run is the read in flight only while a SHARDED-review job of it is live.
# The workflow reports as in flight while ANY of its four jobs runs, and most of
# them are not a review anybody is waiting on — an older run still in `decide`,
# or one whose review job already failed. Counting one would emit skip=true and
# leave this head with no whole-diff read at all, the permanently-unreviewed
# latch this script exists to prevent.
# INVARIANT: matched with contains/endswith, never a prefix. A reusable workflow's
# jobs are named "<caller job> / <job name>" in the runs API, so an anchored match
# finds no live shard, this run reviews anyway, and the PR buys a second paid read.
_sharded_review_live() {
  local jobs_rc=0 live
  live="$(
    retry_stdout gh api "repos/$REPO/actions/runs/$1/jobs?per_page=100" 2>/dev/null |
      jq '[.jobs[]
            | select(.status != "completed")
            | select(.name | contains("Claude PR review (shard ") or endswith("Post the sharded PR review"))]
          | length'
  )" || jobs_rc=$?
  # A jobs list we could not read is not evidence of a review in flight, so it
  # falls through to reviewing — the direction every other failure here takes.
  [[ "$jobs_rc" -eq 0 ]] && [[ "$live" -gt 0 ]]
}

inflight=0
while IFS= read -r run; do
  [[ -n "$run" ]] || continue # the herestring feeds one empty line when there are no candidates
  if _sharded_review_live "$run"; then inflight=$((inflight + 1)); fi
done <<<"$candidates"

# A read in flight only cancels this one once it fills the budget. Counting it as
# a stop whatever the budget says would drop this event at a budget of 3 with one
# read spent and one generating, leaving the third owed and no event to buy it.
if [[ $((count + inflight)) -ge "$MAX_REVIEWS_PER_PR" ]]; then
  emit true "an earlier run of this workflow is still reviewing $REPO#$PR ($inflight with a live sharded-review job) — those fill the $MAX_REVIEWS_PER_PR read(s) this PR may spend"
else
  emit false "$REPO#$PR has spent $count of $MAX_REVIEWS_PER_PR read(s), with $inflight in flight — running this one"
fi
