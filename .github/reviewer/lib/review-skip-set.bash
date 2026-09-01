# shellcheck shell=bash
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# PROBLEM CLASS — a merge gate that waits for a review nothing will ever post.
# A reviewer configured to skip some pull requests never reviews them, so a bare
# "wait for the first review" holds every one of them at pending forever. This
# file is the ONE definition of the skipped set, so a consumer's gate and its
# stand-in approver cannot name different pull requests.
#
# THE SET IS EMPTY BY DEFAULT, and that is the important part. A waiver is a
# claim about the CONSUMER'S reviewer, not a judgement that the pull requests
# are safe: a chore or a dependency bump can be as wrong as anything else, and
# the title arm keys on text the pull request AUTHOR writes, on a gate running
# under pull_request_target. So a consumer states its own reviewer's skip set
# explicitly, and one whose reviewer reads everything — this repository's does;
# see review.yaml's decide job — waives nothing and waits for a real read.
#
# A draft is never in the set. A reviewer that skips it for now reads it on
# `ready_for_review`, so a review is still owed, and a draft cannot merge, so
# the wait holds nothing up.
#
# Consumers: review-findings-gate.sh.

# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"

# The Conventional-Commit types the consumer's reviewer skips by title, as a
# JSON array. A `!` breaking marker and a `(scope)` do not change the type, so
# both forms match. EMPTY BY DEFAULT — see the header.
#
# The default is assigned in its own statement, NOT as `: "${VAR:='[...]'}"`.
# Inside those outer double quotes the inner single quotes are literal text and
# the inner double quotes are lost, so that form yields `'[chore, style,
# release]'`, which jq rejects — and the predicate then fails for every pull
# request rather than answering.
if [[ -z "${REVIEW_SKIP_TYPES:-}" ]]; then
  REVIEW_SKIP_TYPES='[]'
fi
# Whether the consumer's reviewer skips a BOT-authored pull request. Off by
# default for the same reason the type list is empty.
: "${REVIEW_SKIP_BOT_AUTHORS:=false}"
# The label that takes a pull request back out of the set. Matches the
# `review-label` input of the reusable review workflow.
: "${REVIEW_LABEL:=needs-auto-review}"

# The types are program TEXT, so a malformed value reshapes the predicate rather
# than failing it. Parse it once here, where the error names this variable.
jq -e 'type == "array" and all(.[]; type == "string")' >/dev/null <<<"$REVIEW_SKIP_TYPES" || {
  echo "REVIEW_SKIP_TYPES must be a JSON array of strings, got: ${REVIEW_SKIP_TYPES}" >&2
  exit 1
}
case "$REVIEW_SKIP_BOT_AUTHORS" in
true | false) ;;
*)
  echo "REVIEW_SKIP_BOT_AUTHORS must be true or false, got: ${REVIEW_SKIP_BOT_AUTHORS}" >&2
  exit 1
  ;;
esac

# pr_review_is_skipped <owner> <name> <pr>
#
# Exit 0 when the consumer's reviewer owes this pull request no review. Reads
# the pull request itself rather than a webhook payload, so a later event (a
# push, a label) gets the same answer as the first one.
#
# An empty set — the default — short-circuits to "reviewed" with no API call, so
# a consumer that waives nothing pays nothing for this file.
pr_review_is_skipped() {
  local owner="$1" name="$2" pr="$3" verdict program
  [[ "$REVIEW_SKIP_BOT_AUTHORS" == true || "$REVIEW_SKIP_TYPES" != "[]" ]] || return 1
  # `gh api` passes no jq variables, so the type list and the bot flag are
  # spliced into the program — both validated above. The flag is spliced BARE,
  # as a jq boolean literal: quoted, it would be the STRING "false", and every
  # non-null string is truthy in jq, so a disabled flag would skip every bot.
  # The LABEL is read out of `env` instead of spliced, because a `"` in it would
  # otherwise close the string literal and reshape the predicate. $title and $t
  # belong to jq, not the shell.
  # shellcheck disable=SC2016
  program='(.title | ascii_downcase) as $title
    | if any(.labels[]?; .name == env.REVIEW_LABEL) then "reviewed"
      elif .user.type == "Bot" and '"$REVIEW_SKIP_BOT_AUTHORS"' then "skipped"
      elif any('"$REVIEW_SKIP_TYPES"'[]; . as $t | $title | test("^" + $t + "(\\(.*\\))?!?:")) then "skipped"
      else "reviewed"
      end'
  verdict="$(REVIEW_LABEL="$REVIEW_LABEL" retry_stdout gh api "repos/${owner}/${name}/pulls/${pr}" --jq "$program")"
  [[ "$verdict" == "skipped" ]]
}
