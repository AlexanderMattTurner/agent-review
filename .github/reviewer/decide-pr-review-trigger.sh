#!/usr/bin/env bash
# kcov-exclude: a step body with a behavioral suite that runs the real script
#   against stubbed CLIs on PATH, asserting branches without tracing a run.
# Decide whether the PR reviewer (the `review` job in review.yaml) runs for this
# pull_request_target event. Writes run=true/false to GITHUB_OUTPUT. The model is
# the reusable workflow's `model` input and is not decided here.
#
# Budget: `max-reviews-per-pr` whole-diff reads per PR, 1 by default. A later push
# is not re-read once they are spent. The review-findings gate holds the merge on
# the threads that read opened; resolving an addressed one is the session's own
# job, not an automated re-read. [opus-review] is the escape valve for a head that
# needs a fresh look.
#
# Runs for every PR whatever its base, so a stacked child cannot merge with the
# review gate red.
#
# A DRAFT is reviewed like any other PR. The ready-PR cap drafts most PRs within
# seconds of `opened`, so a reviewer that waited for `ready_for_review` gave feedback
# only once the work was finished — when it is worth the least.
#
# Triggers: `opened` fires whenever the budget is at least 1. `ready_for_review` /
# `synchronize` fire only when: (1) "[opus-review]" is in the head commit TITLE,
# bounded to once per tagged commit, opted in by a PUSH alone; (2) the PR has spent
# fewer reads than the budget, which re-arms `opened` after an oversized diff or a
# cancelled job.
#
# Security: read under pull_request_target, so the untrusted head is never
# checked out or executed, and matched only as fixed DATA strings (never eval).
#
# Env: GH_TOKEN, ACTION, REPO, HEAD_SHA, PR, LABEL, REVIEW_LABEL,
#      MAX_REVIEWS_PER_PR.
set -euo pipefail

KEYWORD="[opus-review]"
# The label that asks for a review the caller's own `if:` would skip. A caller
# that passes an empty one gets this default rather than a wildcard, because an
# empty name matches the empty LABEL every other event carries.
REVIEW_LABEL="${REVIEW_LABEL:-needs-auto-review}"
LABEL="${LABEL:-}"
# How many whole-diff reads this PR may spend on the AUTOMATIC arms. The
# `[opus-review]` head-commit opt-in and the review label sit above it: each is
# an explicit request for one more read, the way the label already sits above
# every automatic arm. A caller that wants no automatic read at all passes 0.
#
# REQUIRED, with no default here: review.yaml's `max-reviews-per-pr` input owns
# the number, and a second default in this file is a copy that drifts. Validated
# rather than compared raw, because `[[ -lt ]]` reads an unreadable value as 0 —
# so a typo would silently review forever, or never.
MAX_REVIEWS_PER_PR="${MAX_REVIEWS_PER_PR:?MAX_REVIEWS_PER_PR required — review.yaml passes its max-reviews-per-pr input}"
[[ "$MAX_REVIEWS_PER_PR" =~ ^[0-9]+$ ]] ||
  {
    echo "max-reviews-per-pr must be a whole number, not '$MAX_REVIEWS_PER_PR'" >&2
    exit 1
  }
REVIEWER="github-actions[bot]"                   # posts with GITHUB_TOKEN, so every review from this bot is one whole-diff read of this PR
export REVIEWER_LOGIN_BARE="${REVIEWER%'[bot]'}" # bare, since GraphQL omits the REST `[bot]` suffix

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/reviewer/lib/pr-reviews.bash
source "$_SCRIPT_DIR/lib/pr-reviews.bash"
REPO="${REPO:?REPO (owner/name) required}"
owner="${REPO%%/*}"
name="${REPO##*/}"

emit() { # $1 run, $2 reason, $3 recheck (default false)
  local run="$1" reason="$2" recheck="${3:-false}"
  {
    echo "run=$run"
    echo "recheck=$recheck"
  } >>"$GITHUB_OUTPUT"
  echo "decision: run=$run recheck=$recheck ($reason)"
}

case "$ACTION" in # `opened` is the ONLY unconditional arm — fires once per PR; the other two fire without limit
opened)
  # GitHub fires `opened` exactly once per PR, so no review can exist yet and the
  # count is 0 by construction — the cap is read here, never the review list.
  if [[ "$MAX_REVIEWS_PER_PR" -eq 0 ]]; then
    emit false "max-reviews-per-pr is 0, so no automatic review runs on this PR"
  else
    emit true "first review on opened"
  fi
  exit 0
  ;;
labeled)
  # The on-demand hatch, for a caller whose own `if:` skips some PRs (a low-risk
  # title, a bot author). Such a PR reaches no other arm, so without this the
  # label a caller's skip notice offers starts nothing. A human adds the label,
  # so it buys one read per add and needs no head-commit bound.
  if [[ "$LABEL" == "$REVIEW_LABEL" ]]; then
    emit true "the $REVIEW_LABEL label asks for a review"
  else
    emit false "'$LABEL' is not the $REVIEW_LABEL opt-in label"
  fi
  exit 0
  ;;
ready_for_review | synchronize) ;;
*)
  emit false "no automatic review on '$ACTION'"
  exit 0
  ;;
esac

# Trigger 1: full re-read on [opus-review] in the head commit title, on a PUSH alone — the
# push carries the tagged head, so one tagged commit buys exactly one read, where a toggle
# carrying no new commit would buy one per toggle off a single head. Fetch that commit
# DIRECTLY by SHA, not the PR-commits list, which the API caps at 250 even with --paginate: on
# a heavily-revised PR the head falls off it and the opt-in silently fails.
if [[ "$ACTION" == "synchronize" ]]; then
  # A failed read is DISCARDED, not matched: empty the capture rather than
  # suppressing the status. A non-2xx prints the API's own error body on STDOUT, so
  # a bare `|| true` would leave that body in `message` and search it as the "commit
  # subject", matching the keyword against a complaint. Only an empty subject makes
  # the stated behaviour true: a transient commit-fetch failure opts nothing in, so
  # no spurious re-review fires. Captured into a variable rather than piped to grep,
  # whose early exit SIGPIPEs the still-writing `gh` under pipefail.
  message="$(gh api "repos/$REPO/commits/$HEAD_SHA" --jq '.commit.message' 2>/dev/null)" || message=""
  subject="${message%%$'\n'*}"
  if grep -qiF "$KEYWORD" <<<"$subject"; then
    emit true "$KEYWORD in head commit title"
    exit 0
  fi
fi

# Consumed only by trigger 2, and run AFTER trigger 1 so a tagged push pays no paginated
# GraphQL read it never uses. The exit STATUS is captured separately from the state, because
# the two empty results mean opposite things: a successful "" is the strongest reason to
# review (nobody ever looked), while a failed "" must keep the fail-safe of not reviewing.
# Folded together they would review on every API blip, and a malformed jq filter would read as
# "never reviewed" and review forever. The shared library owns the pagination and the
# latest-by-`submittedAt` fold, so this is one call and not a page walk written twice.
reviews_rc=0
spent="$(real_reviewer_reviews "$owner" "$name" "${PR:-}" 2>/dev/null)" || reviews_rc=$?
# One walk answers both questions: how many reads are spent, and what the last
# one said. `grep -c` would count the empty string as a line, so the count comes
# from jq's own slurp over the same NDJSON the fold reads.
count="$(jq -rs 'length' <<<"$spent")"
latest="$(latest_of_reviews <<<"$spent")"
state="$(jq -r '.state // ""' <<<"$latest")"
reviewed_sha="$(jq -r '.reviewedSha // ""' <<<"$latest")"

# Trigger 2: any state — APPROVED, DISMISSED, or a still-live CHANGES_REQUESTED / COMMENTED —
# means the reviewer looked, so it spends one of the budgeted reads; past the budget only
# `$KEYWORD` buys another pass. A count of 0 after a SUCCESSFUL query means no first pass ever
# ran, so this event is it, which is what re-arms `opened` when `opened` produced no review (an
# oversized diff, a cancelled job). Without that re-arm such a PR is never reviewed again.
if [[ "$reviews_rc" -ne 0 ]]; then
  emit false "could not read $REPO#${PR:-} reviews (exhausted the retry ladder, rc=$reviews_rc) — not reviewing rather than guessing"
elif [[ "$count" -lt "$MAX_REVIEWS_PER_PR" ]]; then
  # recheck=true: a still-generating review is invisible here; the review job re-asks after the
  # concurrency group serializes behind it. An event inside the reviewer's own window would
  # otherwise buy a second whole-diff read of the same head.
  emit true "$REVIEWER has spent $count of $MAX_REVIEWS_PER_PR read(s) on this PR — running one on this $ACTION" true
else
  emit false "$REVIEWER has spent all $MAX_REVIEWS_PER_PR read(s) on this PR (latest: $state, at ${reviewed_sha:-an unrecorded commit}) — a $ACTION is not re-read; push a commit titled $KEYWORD for a full re-read"
fi
