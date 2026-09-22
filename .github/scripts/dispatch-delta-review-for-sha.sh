#!/usr/bin/env bash
# Ask for the ACCUMULATED review of every open pull request whose head is $SHA,
# the moment that head's checks finish.
#
# PROBLEM CLASS — a merge gate that waits on a TIMER rather than on the condition
# it gates. `dispatch-delta-review.sh` asks for the read once a pull request is
# otherwise ready to merge, and a twice-hourly sweep is the slowest way to notice
# that: a head green at :24 sits behind the gate until :53. A check workflow
# finishing is the same condition, delivered as an event.
#
# This script SELECTS; it decides nothing. Every readiness question — budget,
# draft state, unresolved findings, the rollup — stays in dispatch-delta-review.sh,
# so the event path and the sweep reach the same verdict.
#
# The pull requests come from the API rather than the event payload, which a
# `workflow_run` leaves empty for a run on a merged branch. GitHub keeps a commit
# associated with its pull request for that request's whole life, so the head
# comparison below is what stops a read of a head nobody is on.
#
# Env: GH_TOKEN, GH_REPO (owner/name), SHA. REVIEW_WORKFLOW and
#      MAX_DELTA_REVIEWS_PER_PR enable the read, as they do for the sweep.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${SHA:?SHA required — the head the completed checks ran on}"
: "${REVIEW_WORKFLOW:?REVIEW_WORKFLOW required — the caller workflow file the dispatch starts}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
severity_config="$(cd "$here/../.." && pwd)/config/review-severities.json"

# Open, non-draft, human-authored, and still headed by this sha — the same
# selection the sweep applies, so neither path offers the dispatcher a pull
# request the other would have filtered out.
associated="$(gh api "repos/${GH_REPO}/commits/${SHA}/pulls" --paginate)"
pr_numbers="$(
  jq -r --arg sha "$SHA" '.[]
    | select(.state == "open")
    | select(.draft == false)
    | select(.user.type != "Bot")
    | select(.head.sha == $sha)
    | .number' <<<"$associated"
)"
prs=()
if [[ -n "$pr_numbers" ]]; then
  mapfile -t prs <<<"$pr_numbers"
fi
if [[ "${#prs[@]}" -eq 0 ]]; then
  echo "no open pull request is headed by ${SHA:0:7}; no accumulated review asked for" >&2
  exit 0
fi

status=0
for pr in "${prs[@]}"; do
  echo "::group::PR #${pr}"
  # One pull request failing to evaluate must not stop the rest, but the run must
  # still go red — a dispatcher that cannot read the API is a fault, not a quiet
  # "nothing to do" (every such branch exits 0 inside the dispatcher itself).
  if ! PR="$pr" SEVERITY_CONFIG="$severity_config" \
    bash "$here/../reviewer/dispatch-delta-review.sh"; then
    echo "dispatch-delta-review-for-sha: PR #${pr} accumulated review could not be evaluated" >&2
    status=1
  fi
  echo "::endgroup::"
done

exit "$status"
