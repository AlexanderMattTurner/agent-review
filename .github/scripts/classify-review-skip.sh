#!/usr/bin/env bash
# Decide whether the Claude reviewer skips this pull request, by reading its
# COMMITS rather than the account that opened it.
#
# The skip class routes a pull request to auto-approve-skipped-pr.sh, which posts
# an approving review under the reviewer's identity. So every field the class
# reads must be one the pull request cannot choose. The pull request's TITLE is
# not, which is why no title reaches here. Nor is the OPENER on its own: the head
# branch of a same-repo bot pull request is pushable by any collaborator, and the
# caller re-runs this on `synchronize`, so a human commit pushed onto a
# dependabot branch would take the approval its opener bought.
#
# PAYLOAD_SKIP carries the event-payload half of the class (a same-repo,
# non-draft, bot-opened pull request). This adds two halves the PR cannot forge:
#   * SENDER_TYPE — the account that fired THIS event is a bot. On `synchronize`
#     that is the account that pushed, which no commit field can claim.
#   * every commit on the pull request is authored by a bot account, carries a
#     signature GitHub verified, and was committed by `web-flow`, the account
#     GitHub signs its own server-side commits with. A commit's author is mapped
#     from its author EMAIL, which `git commit --author` sets to any bot's address.
#     A signature GitHub made itself is what a local commit cannot produce.
#
# Fails CLOSED — a truncated commit list, an unmapped or unsigned commit, or an
# unreadable API all print skip=false, which buys a real review rather than an
# unread approval.
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR, PAYLOAD_SKIP, SENDER_TYPE,
# GITHUB_OUTPUT.
set -euo pipefail

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"
: "${PAYLOAD_SKIP:?PAYLOAD_SKIP required}"
: "${SENDER_TYPE?SENDER_TYPE required — the type of the account that fired the event}"

emit() {
  echo "decision: $2" >&2
  echo "skip=$1" >>"$GITHUB_OUTPUT"
  exit 0
}

[[ "$PAYLOAD_SKIP" == "true" ]] ||
  emit false "the event payload puts this PR outside the skip class"

[[ "$SENDER_TYPE" == "Bot" ]] ||
  emit false "this event was fired by a ${SENDER_TYPE:-unknown} account, not a bot — this PR needs a real read"

# --paginate walks past the 250-commit cap the `pulls/N/commits` list carries, so
# a long PR is answered rather than silently truncated to its first page.
# One verdict per commit: `ok`, or the first reason it is outside the class.
verdicts="$(gh api --paginate "repos/$GH_REPO/pulls/$PR/commits" \
  --jq '.[] | if (.author.type // "unmapped") != "Bot"
              then "authored by \(.author.type // "an unmapped account"), not a bot"
              elif .commit.verification.verified != true
              then "not signed with a signature GitHub verified"
              elif (.committer.login // "") != "web-flow"
              then "committed by \(.committer.login // "an unmapped account"), not signed by GitHub"
              else "ok" end')" ||
  emit false "could not read $GH_REPO#$PR's commits — reviewing rather than approving unread"

[[ -n "$verdicts" ]] ||
  emit false "$GH_REPO#$PR reported no commits — reviewing rather than approving unread"

while IFS= read -r verdict; do
  [[ "$verdict" == "ok" ]] ||
    emit false "a commit on $GH_REPO#$PR is $verdict — this PR needs a real read"
done <<<"$verdicts"

emit true "every commit on $GH_REPO#$PR is bot-authored and signed by GitHub"
