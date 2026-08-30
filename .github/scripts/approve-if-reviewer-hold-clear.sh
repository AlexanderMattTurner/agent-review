#!/usr/bin/env bash
# Approve the PR when the automated reviewer's hold is fully cleared. This is the
# single source of truth for "the reviewer requested changes (or commented),
# every one of its threads is resolved, and somebody other than the pull
# request's author resolved one of THIS hold's threads, so post the APPROVE that
# supersedes the hold and satisfies a review-required ruleset."
#
# It is state-based and idempotent: it reads the CURRENT thread and review state
# through the API and decides from that alone. A periodic sweep of open PRs
# (claude-reviewer-hold-clear.yaml) runs it, so a resolution that fires no
# workflow event is caught too.
#
# Approves ONLY when the reviewer's LATEST review is a live hold — CHANGES_REQUESTED
# or COMMENTED; any other latest state means nothing to clear, so an unrelated
# thread-resolved event mints no approval — AND all three thread conditions hold:
# no reviewer thread is unresolved, that latest hold opened at least one thread,
# and somebody other than the author resolved one of those. A hold whose concern
# lived only in the review body opens no thread, so it clears on the reviewer's
# own re-review instead.
#
# Env: the GH_TOKEN_* ladder rungs (see lib/github-token-ladder.bash), GH_REPO
# (owner/name), PR; REVIEWER_LOGIN optional.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/github-token-ladder.bash disable=SC1091
source "$SCRIPT_DIR/lib/github-token-ladder.bash"

# Every call below spends API quota, so pick a credential that has some before
# the first one rather than discovering it mid-flight. Reddening only once EVERY
# rung is spent is the point: a spent top rung is not a reason to fail a step
# whose posture is to degrade, but nothing left to spend anywhere is a real
# blocker a human has to clear, so it is not swallowed either.
GH_TOKEN="$(github_token_with_quota)" || {
  echo "every configured GitHub credential is out of API quota; cannot read this PR's review state, so the reviewer's hold is left in place. Re-run once quota resets, or provision another TEMPLATE_SYNC_TOKEN." >&2
  exit 1
}
export GH_TOKEN
# Both reviewer lookups below run through `gh api graphql`, which spells an app
# bot's login WITHOUT the `[bot]` suffix the REST API appends (REST
# `github-actions[bot]` ↔ GraphQL `github-actions`). Comparing the REST-shaped
# value against GraphQL's matched zero reviews, so this script always concluded
# "no live hold" and never posted the clearing approval; reviewer_login_init owns
# that normalization now, for every reviewer script (lib/reviewer-login.bash).
# shellcheck source=lib/reviewer-login.bash disable=SC1091
source "$SCRIPT_DIR/lib/reviewer-login.bash"
reviewer_login_init

owner="${GH_REPO%%/*}"
name="${GH_REPO##*/}"

# WHO resolved a thread decides whether that resolution counts, so the author's
# login has to be known before any thread is counted. Empty is fatal rather than
# permissive: an unknown author matches nobody, which would credit the author's
# own resolutions to somebody else and clear the hold this script guards.
PR_AUTHOR="$(gh api "repos/${GH_REPO}/pulls/${PR}" --jq '.user.login // ""')"
if [[ -z "$PR_AUTHOR" ]]; then
  echo "could not read the author of PR #${PR}; no resolution can be attributed, so the reviewer's hold stays in place." >&2
  exit 1
fi
# jq reads it from the environment, and login_bare_jq folds both spellings the
# two API dialects use (REST returns `x[bot]` where GraphQL returns `x`).
export PR_AUTHOR
resolver_is_not_author="$(login_bare_jq .resolvedBy.login) != $(login_bare_jq env.PR_AUTHOR)"

# What is the reviewer's latest review, and when did it land? Paginated (a
# long-lived PR can accrue >100 reviews, and an unpaginated first:100 returns the
# OLDEST 100 and would pick a stale state): the per-page --jq emits the reviewer's
# reviews as NDJSON and the slurp picks the globally latest by submittedAt.
# shellcheck disable=SC2016 # GraphQL query + jq program are literal, not shell
reviews_query='query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviews(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId author { login } state submittedAt }
      }
    }
  }
}'
latest_review="$(gh api graphql --paginate \
  -f query="$reviews_query" -f owner="$owner" -f name="$name" -F pr="$PR" \
  --jq ".data.repository.pullRequest.reviews.nodes[]
        | ${REVIEWER_MATCH_AUTHOR}
        | {state, submittedAt}" |
  jq -cs 'if length == 0 then {state: "", submittedAt: ""}
          else (sort_by(.submittedAt) | last) end')"
latest_state="$(jq -r '.state' <<<"$latest_review")"

if [[ "$latest_state" != "CHANGES_REQUESTED" && "$latest_state" != "COMMENTED" ]]; then
  echo "reviewer's latest review is '${latest_state:-<none>}' — no live hold to clear; nothing to do" >&2
  exit 0
fi

# HOLD_SINCE scopes every thread count below to the hold this run may clear. jq
# reads it from the environment, so it is exported rather than shell-local.
HOLD_SINCE="$(jq -r '.submittedAt // ""' <<<"$latest_review")"
export HOLD_SINCE
if [[ -z "$HOLD_SINCE" || "$HOLD_SINCE" == "null" ]]; then
  echo "the reviewer's ${latest_state} review carries no submittedAt, so no thread can be scoped to it; the hold stays in place." >&2
  exit 1
fi

# Count the reviewer's threads three ways. Paginated: a PR can accrue >100
# threads, and an unpaginated first:100 would miss a thread on a later page. The
# per-page --jq emits one {unresolved, in_hold, cleared_by_other} object; the
# trailing reduce sums them.
# shellcheck disable=SC2016 # GraphQL query + jq program are literal, not shell
remaining_query='query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          resolvedBy { login }
          comments(first: 1) { nodes { author { login } pullRequestReview { submittedAt } } }
        }
      }
    }
  }
}'
# A thread belongs to the current hold when the review that opened its root
# comment is that hold or newer. The root comment's own createdAt cannot answer
# this: a comment drafted into a pending review is created BEFORE that review is
# submitted, so it can predate the very hold it belongs to.
thread_in_hold='((.comments.nodes[0].pullRequestReview.submittedAt // "") >= env.HOLD_SINCE)'

# `unresolved` spans the whole PR — a thread left open in any cycle blocks. The
# other two are scoped to this hold, so an earlier cycle cannot vouch for it.
# shellcheck disable=SC2016 # jq program is literal, not shell ($p is a jq var)
counts="$(gh api graphql --paginate \
  -f query="$remaining_query" -f owner="$owner" -f name="$name" -F pr="$PR" \
  --jq "[.data.repository.pullRequest.reviewThreads.nodes[]
         | ${REVIEWER_MATCH_THREAD_ROOT}]
        | {unresolved: (map(select(.isResolved == false)) | length),
           in_hold: (map(select(${thread_in_hold})) | length),
           cleared_by_other: (map(select(${thread_in_hold}
                                         and .isResolved == true
                                         and .resolvedBy != null
                                         and ${resolver_is_not_author})) | length)}" |
  jq -s 'reduce .[] as $p ({unresolved: 0, in_hold: 0, cleared_by_other: 0};
           {unresolved: (.unresolved + $p.unresolved), in_hold: (.in_hold + $p.in_hold),
            cleared_by_other: (.cleared_by_other + $p.cleared_by_other)})')"
unresolved="$(jq -r '.unresolved' <<<"$counts")"
in_hold="$(jq -r '.in_hold' <<<"$counts")"
cleared_by_other="$(jq -r '.cleared_by_other' <<<"$counts")"

if [[ "${unresolved:-0}" -ne 0 ]]; then
  echo "${unresolved} reviewer thread(s) still open; not approving" >&2
  exit 0
fi

# INVARIANT — an approval needs a resolved thread FROM THIS HOLD to rest on. A
# hold whose concern lived only in the review body opens no thread, so nothing
# here can clear it: the reviewer's own re-check on the next push supersedes it.
if [[ "${in_hold:-0}" -eq 0 ]]; then
  echo "the reviewer's latest hold (${latest_state} at ${HOLD_SINCE}) opened no thread, so no resolution signal exists; a thread-less hold clears on the reviewer's own re-review" >&2
  exit 0
fi

# INVARIANT — this refusal is what stops a pull request's author from clearing
# the reviewer's hold alone. GitHub lets the author resolve any conversation on
# their pull request, so the hold clears only on a resolution by somebody else,
# of a thread THIS hold opened: a resolution attributed to nobody, or one from an
# earlier review cycle, vouches for nothing the latest hold raised.
if [[ "${cleared_by_other:-0}" -eq 0 ]]; then
  echo "no thread from the reviewer's latest hold on PR #${PR} names a resolver other than its author (${PR_AUTHOR}); the hold stays until somebody else resolves one or the reviewer re-reviews" >&2
  exit 0
fi

cleared_by="every review conversation from the automated reviewer has been resolved, and somebody other than the pull request's author resolved one that its latest hold opened"

# Dismiss the REVIEWER'S OWN stale CHANGES_REQUESTED. Reached only when the hold
# is already proven clear above and the approval was structurally refused, so it
# is the fallback lever for a hold nothing else can clear.
#
# Dismissal is not approval: it needs write access rather than a different actor,
# so it succeeds exactly where the approval cannot — including for GITHUB_TOKEN,
# which GitHub bars from approving at all, so the periodic sweep gains it too.
#
# The selection is what makes this safe: it filters on the reviewer's own login,
# so a HUMAN's CHANGES_REQUESTED is never a candidate. A human hold still blocks
# and still needs that human. Dismissing is also idempotent — a dismissed review's
# state stops being CHANGES_REQUESTED, so a re-run finds nothing and says so.
dismiss_stale_hold() {
  local reason="$1" review_id dismiss_err
  # The most recent CHANGES_REQUESTED specifically, NOT the latest review: a
  # CHANGES_REQUESTED keeps blocking until dismissed or superseded by an APPROVED
  # from the same reviewer, and a later COMMENTED review does not clear it. So the
  # blocking review is routinely not the latest one.
  review_id="$(gh api graphql --paginate \
    -f query="$reviews_query" -f owner="$owner" -f name="$name" -F pr="$PR" \
    --jq ".data.repository.pullRequest.reviews.nodes[]
          | ${REVIEWER_MATCH_AUTHOR}
          | select(.state == \"CHANGES_REQUESTED\")
          | {databaseId, submittedAt}" |
    jq -rs 'if length == 0 then "" else (sort_by(.submittedAt) | last | .databaseId) end')"

  if [[ -z "$review_id" ]]; then
    echo "no active CHANGES_REQUESTED from ${REVIEWER_LOGIN} to dismiss — its hold was a COMMENTED review, which does not block a merge." >&2
    return 0
  fi

  # Unlike the approval refusals above, a failed dismissal is NOT structural:
  # nothing about this PR makes it permanently impossible, so it is a real error
  # and must be seen rather than logged past.
  if ! dismiss_err="$(gh api --method PUT \
    "repos/${GH_REPO}/pulls/${PR}/reviews/${review_id}/dismissals" \
    -f message="$reason" -f event=DISMISS 2>&1)"; then
    echo "failed to dismiss the reviewer's stale hold (review ${review_id}): ${dismiss_err}" >&2
    return 1
  fi
  echo "dismissed the reviewer's stale CHANGES_REQUESTED (review ${review_id}) — ${reason}" >&2
}
# Two refusals here are STRUCTURAL — no permission, retry or configuration on
# this PR makes them succeed, so failing the job on either would red every PR
# whose hold clears, forever, and a check that can only fail teaches nothing.
# GitHub refuses `addPullRequestReview` for an Actions token regardless of
# permissions ("GitHub Actions is not permitted to approve pull requests"), and
# it refuses any approval of a PR the token's own actor authored. Stand down
# LOUDLY on both, naming the remedy. Any OTHER failure is real and exits
# non-zero.
approve_err=""
if ! approve_err="$(gh pr review "$PR" --repo "$GH_REPO" --approve --body \
  "Automated approval: ${cleared_by}, so this satisfies the review-required ruleset. Re-request review if a human should take a closer look." 2>&1)"; then
  if [[ "$approve_err" == *"not permitted to approve pull requests"* ]]; then
    echo "hold is clear, but this token cannot approve: GitHub blocks approvals from GitHub Actions." >&2
    dismiss_stale_hold "${cleared_by}, so this hold no longer reflects the pull request's state." || exit 1
    exit 0
  fi
  if [[ "$approve_err" == *"Can not approve your own pull request"* ]]; then
    echo "hold is clear, but this token's actor authored PR #${PR}, and GitHub refuses a self-approval." >&2
    dismiss_stale_hold "${cleared_by}, so this hold no longer reflects the pull request's state." || exit 1
    exit 0
  fi
  echo "failed to post the clearing approval: ${approve_err}" >&2
  exit 1
fi
echo "${cleared_by} and reviewer was holding (${latest_state}); approved to satisfy the review gate" >&2
