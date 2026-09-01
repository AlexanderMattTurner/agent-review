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
# non-draft, bot-opened pull request). This adds the head half: every commit on
# the pull request must be authored by a bot account.
#
# Fails CLOSED — a truncated commit list, an unmapped commit author, or an
# unreadable API all print skip=false, which buys a real review rather than an
# unread approval.
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR, PAYLOAD_SKIP, GITHUB_OUTPUT.
set -euo pipefail

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"
: "${PAYLOAD_SKIP:?PAYLOAD_SKIP required}"

emit() {
  echo "decision: $2" >&2
  echo "skip=$1" >>"$GITHUB_OUTPUT"
  exit 0
}

[[ "$PAYLOAD_SKIP" == "true" ]] ||
  emit false "the event payload puts this PR outside the skip class"

# --paginate walks past the 250-commit cap the `pulls/N/commits` list carries, so
# a long PR is answered rather than silently truncated to its first page.
authors="$(gh api --paginate "repos/$GH_REPO/pulls/$PR/commits" \
  --jq '.[] | .author.type // "unmapped"')" ||
  emit false "could not read $GH_REPO#$PR's commits — reviewing rather than approving unread"

[[ -n "$authors" ]] ||
  emit false "$GH_REPO#$PR reported no commits — reviewing rather than approving unread"

while IFS= read -r type; do
  [[ "$type" == "Bot" ]] ||
    emit false "a commit on $GH_REPO#$PR is authored by $type, not a bot — this PR needs a real read"
done <<<"$authors"

emit true "every commit on $GH_REPO#$PR is bot-authored"
