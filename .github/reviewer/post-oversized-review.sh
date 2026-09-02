#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so the branches are asserted but
#   no run is ever traced.
# A PR too large even to shard gets no automated read — but the review-findings
# gate stays red until SOME completed reviewer review exists, so a bare comment
# (the old shape) would leave an oversized PR permanently unmergeable with no
# thread to resolve. This posts the oversized notice as a real COMMENT review
# (satisfying the gate's reviewed-at-all leg) plus ONE 🔴 file-level finding
# thread telling a human to review the PR themselves and resolve the thread —
# that resolution is what greens the gate, so "too big for the bot" degrades to
# "a human signs off" instead of "nothing can merge".
#
# Only the human (or session) who reviewed resolves this thread — no automated
# read can substitute for the human review this thread demands.
#
# Idempotent per surface: the notice review is posted once per HEAD (skipped when
# a review stamped with this head already exists), and the helper
# re-raises the thread only when no unresolved one exists — so a human's
# resolution is never clobbered by a rerun, while a still-oversized [opus-review]
# re-read after a resolve raises a fresh thread for the new head.
#
# Requires: GH_TOKEN, GH_REPO, PR, PR_INPUT_DIR (oversized-notice.txt), HEAD_SHA.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"
: "${HEAD_SHA:?HEAD_SHA required (the finding thread anchors to the head)}"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$_SCRIPT_DIR/lib-ci-retry.sh"
# shellcheck source=.github/reviewer/lib/review-threads.bash
source "$_SCRIPT_DIR/lib/review-threads.bash"

notice="${PR_INPUT_DIR}/oversized-notice.txt"
[[ -s "$notice" ]] || {
  echo "missing or empty ${notice} — nothing to post" >&2
  exit 1
}

# OVERSIZED_REVIEW_MARKER — stamped on both the notice review and the finding thread.
# Sourced rather than defined here: lib/pr-reviews.bash also reads it back, because an
# oversized run spends the PR's read budget. Exported because the jq programs that look
# for it run in external `gh` processes.
# shellcheck source=.github/reviewer/lib/pr-reviews.bash
source "$_SCRIPT_DIR/lib/pr-reviews.bash"
export OVERSIZED_REVIEW_MARKER

# Both idempotence checks below key on marker AND reviewer authorship — the
# same precision as the gate's own read — so a non-reviewer comment quoting the
# marker cannot fool them into skipping the real post. GraphQL returns an app
# bot's login without the REST `[bot]` suffix (see the lib headers).
# shellcheck disable=SC2031 # false positive across the source-follow: the only
# subshell assignment of this name is settled_merge_delta_shas's deliberate one in
# lib/review-threads.bash, which exists to keep that login off callers like this.
export REVIEWER_LOGIN_BARE="github-actions"

# (1) The notice as a completed COMMENT review — the gate's reviewed-at-all leg.
# Capture then split, never `… | head` (an early-exiting reader SIGPIPEs the
# still-writing gh under pipefail).
# Keyed on the HEAD too, not on the PR alone. The notice is what makes an oversized run
# spend budget, so one notice per PR would pin the count at 1 forever: every later push
# would decide "budget not spent", re-run the checkout, the Node setup and the sanitizer
# install, find the diff still oversized, and post nothing — a loop with no end. One
# notice per head makes the count grow per attempt, so `max-reviews-per-pr` bounds it.
export OVERSIZED_HEAD_MARKER="<!-- oversized-head: ${HEAD_SHA} -->"
review_list="$(retry_stdout gh api --paginate "repos/${GH_REPO}/pulls/${PR}/reviews" \
  --jq '.[] | select((.user.login // "" | sub("\\[bot\\]$"; "")) == env.REVIEWER_LOGIN_BARE)
            | select((.body // "") | contains(env.OVERSIZED_HEAD_MARKER)) | .id')"
existing_review="${review_list%%$'\n'*}"
if [[ -n "$existing_review" ]]; then
  echo "oversized notice review already posted for head ${HEAD_SHA} (review ${existing_review}); not re-posting" >&2
else
  review_body="$(mktemp)"
  {
    cat "$notice"
    printf '\n%s\n%s\n' "$OVERSIZED_REVIEW_MARKER" "$OVERSIZED_HEAD_MARKER"
  } >"$review_body"
  retry gh api -X POST "repos/${GH_REPO}/pulls/${PR}/reviews" \
    -f "event=COMMENT" \
    -F body=@"$review_body" >/dev/null
  rm -f "$review_body"
  echo "posted the oversized-PR notice as a COMMENT review" >&2
fi

# (2) The 🔴 finding thread a human resolves after reviewing.
finding_prose="$(mktemp)"
{
  printf '🔴 This PR is too large for the automated reviewer — it needs a HUMAN review.\n\n'
  printf 'The diff exceeds what the reviewer can read even sharded, so no automated pass has covered it. Review the PR yourself, then resolve this thread; the review-findings gate stays red until it is resolved.\n'
} >"$finding_prose"
raise_human_review_finding "$OVERSIZED_REVIEW_MARKER" "$finding_prose"
rm -f "$finding_prose"
