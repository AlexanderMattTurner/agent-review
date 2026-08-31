# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body that no test runs — it reads a job-scoped GH_TOKEN.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# PROBLEM CLASS — the automated reviewer never reads some pull requests, so no
# review of them ever lands. Anything that waits for one waits forever unless it
# waives the wait for exactly this set. This file is the ONE definition of the
# set, so a consumer's waiver and its stand-in approval cannot name different
# pull requests.
#
# The set: a bot-authored pull request, or a title whose Conventional-Commit
# type is listed in REVIEW_SKIP_TYPES. The review label takes a pull request
# back OUT of the set: the reviewer reads it on demand, so a review is owed
# again.
#
# A draft is NOT in the set. The reviewer skips it for now and reads it on
# `ready_for_review`, so a review is still owed. A draft cannot merge, so a
# waiting gate holds nothing up.
#
# A consumer repository states the same predicate as job `if:` expressions on
# its own review workflow. Those decide only whether a runner boots. This file
# decides the outcome, so a drift there costs one skipped job and never a wrong
# verdict.
#
# Consumers: review-findings-gate.sh.

# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"

# The Conventional-Commit types the reviewer skips by title, as a jq array. A
# `!` breaking marker and a `(scope)` do not change the type, so both forms
# match. A caller may override it before sourcing to widen or narrow the set.
: "${REVIEW_SKIP_TYPES:='["chore", "style", "release"]'}"
# The label that takes a pull request back out of the set. Matches the
# `review-label` input of the reusable review workflow.
: "${REVIEW_LABEL:=needs-auto-review}"

# pr_review_is_skipped <owner> <name> <pr>
#
# Exit 0 when the reviewer owes this pull request no review. Reads the pull
# request itself rather than a webhook payload, so a later event (a push, a
# label) gets the same answer as the first one.
pr_review_is_skipped() {
  local owner="$1" name="$2" pr="$3" verdict program
  # `gh api` passes no jq variables, so the type list and the label are spliced
  # into the program. $title and $t belong to jq, not the shell.
  # shellcheck disable=SC2016
  program='(.title | ascii_downcase) as $title
    | if any(.labels[]?; .name == "'"$REVIEW_LABEL"'") then "reviewed"
      elif .user.type == "Bot" then "skipped"
      elif any('"$REVIEW_SKIP_TYPES"'[]; . as $t | $title | test("^" + $t + "(\\(.*\\))?!?:")) then "skipped"
      else "reviewed"
      end'
  verdict="$(retry_stdout gh api "repos/${owner}/${name}/pulls/${pr}" --jq "$program")"
  [[ "$verdict" == "skipped" ]]
}
