#!/usr/bin/env bash
# Sweep every open, non-draft, human-authored PR and re-evaluate the two pieces of
# automated-reviewer state that no workflow event re-evaluates:
#   * the reviewer's hold — approve-if-reviewer-hold-clear.sh, the one source of
#     truth for "the hold is cleared -> approve";
#   * the `Automated review posted` merge gate — review-gate.sh, which stays RED
#     while one of the reviewer's finding threads is unresolved.
# This is the no-push safety net: GitHub fires no event when a review thread is
# resolved, so a PR whose author resolves every finding without pushing gets
# neither the push-time approve nor a fresh gate verdict, and the gate would hold
# it red after the findings were handled. Enumerating open PRs here and re-running
# both state-based evaluations closes that gap. This script only SELECTS PRs; each
# verdict stays in the script that owns it, so every caller reaches the same one.
#
# Env: GH_TOKEN, GH_REPO (owner/name); REVIEWER_LOGIN optional (passed through).
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Open, non-draft PRs authored by a real user (skip bot-authored PRs — Dependabot
# et al. are handled elsewhere and never Claude-reviewed), mirroring the per-event
# workflows' draft/bot guard. Capture into a variable first so a `gh` failure trips
# set -e loudly rather than silently sweeping nothing.
readonly SWEEP_PR_LIMIT=200
prs_json="$(gh pr list --repo "$GH_REPO" --state open --limit "$SWEEP_PR_LIMIT" \
  --json number,isDraft,author,headRefOid)"
# A full page means the repo may have more open PRs than this sweep can see, so the
# excess would be silently never swept. Fail loud (warn) rather than quietly
# under-sweep — no silent caps.
if [[ "$(jq 'length' <<<"$prs_json")" -ge "$SWEEP_PR_LIMIT" ]]; then
  echo "::warning::sweep-reviewer-holds: open-PR page hit the ${SWEEP_PR_LIMIT} cap; PRs beyond this are not swept. Raise SWEEP_PR_LIMIT or paginate." >&2
fi
# One TAB-separated line per swept PR: its number and the head the gate verdict
# is posted on. The head comes from this listing rather than a second read per PR,
# so the sweep spends one request on selection however many PRs it covers.
pr_rows="$(
  jq -r '.[] | select(.isDraft == false) | select(.author.is_bot == false)
         | [.number, .headRefOid] | @tsv' \
    <<<"$prs_json"
)" || {
  echo "::error::sweep-reviewer-holds: jq failed to filter the open-PR list" >&2
  exit 1
}
prs=()
if [[ -n "$pr_rows" ]]; then
  mapfile -t prs <<<"$pr_rows"
fi

status=0
for row in "${prs[@]}"; do
  IFS=$'\t' read -r pr head_sha <<<"$row"
  echo "::group::PR #${pr}"
  # One PR failing to evaluate must not abort the sweep of the rest; record it and
  # keep going, but exit non-zero at the end so a real API/token fault is surfaced
  # (the approval script exits 0 for every normal "nothing to do" branch).
  if ! PR="$pr" bash "$here/approve-if-reviewer-hold-clear.sh"; then
    echo "sweep: PR #${pr} could not be evaluated" >&2
    status=1
  fi
  # Re-posted even when the approval step above failed: the gate is a separate
  # verdict, and a PR whose hold could not be evaluated still deserves a current
  # one. A head this listing could not report is skipped loudly — posting a status
  # on the wrong sha is worse than posting none.
  if [[ -z "$head_sha" ]]; then
    echo "::warning::sweep-reviewer-holds: PR #${pr} reported no head sha; its review gate is not re-evaluated" >&2
    status=1
  elif ! PR="$pr" HEAD_SHA="$head_sha" bash "$here/review-gate.sh"; then
    echo "sweep: PR #${pr} review gate could not be re-evaluated" >&2
    status=1
  fi
  echo "::endgroup::"
done

exit "$status"
