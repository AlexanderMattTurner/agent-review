#!/usr/bin/env bash
# Post an approving review on a PR the Claude reviewer deliberately SKIPS — a
# bot author on a same-repo head, which is the whole skip set. No TITLE reaches
# this class: a title is author-written, so a title in the skip set would be an
# approval a pull request writes for itself.
#
# A review-required ruleset needs an approving review, and the `Automated review
# posted` gate needs a review by the reviewer with a body. A skipped PR gets
# neither, so it strands on both. This review supplies both at once, because it
# posts with GITHUB_TOKEN and so carries the reviewer identity review-gate.sh
# counts. The caller (claude-review.yaml's `auto_approve_skipped` job `if:`) has
# already decided this PR is in the skip set, and that job re-runs
# review-gate.sh afterwards to post the cleared verdict on the head.
#
# The post goes through the shared retry-as-COMMENT helper
# (lib-post-review-with-retry.sh), because an APPROVE can 422 here the same way
# it does in the reviewer's post-pr-review.sh.
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR.
set -euo pipefail

# shellcheck source=.github/scripts/lib-post-review-with-retry.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-post-review-with-retry.sh"

: "${PR:?PR number required}"
: "${GH_REPO:?GH_REPO required}"

BODY="Automated approval: this PR type isn't Claude-reviewed (low-risk change or bot-authored), so it's approved here to satisfy a review-required ruleset. Add the \`needs-auto-review\` label to have Claude review it anyway."

payload="$(mktemp)"
fallback="$(mktemp)"
trap 'rm -f "$payload" "$fallback"' EXIT
jq -n --arg body "$BODY" '{event: "APPROVE", body: $body}' >"$payload"
printf '%s\n' "$BODY" >"$fallback"

post_review_with_retry "$PR" "$payload" "$fallback"
