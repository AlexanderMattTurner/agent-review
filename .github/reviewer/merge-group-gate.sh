#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against a stubbed `gh` on PATH, so no run is ever traced.
# The merge-queue leg of the review gate: run review-findings-gate.sh for EVERY pull
# request a merge-group batch carries, and exit non-zero when any of them is red.
#
# PROBLEM CLASS — a merge-queue job that gates only the pull request its ref names.
# The queue ref names only the LAST pull request of a batch, so a job that reads the
# ref alone lets every other pull request in the batch reach the default branch with
# no verdict. This repository's own queue job and the job a consumer copies both call
# this script, so the two cannot drift apart.
#
# Every pull request is evaluated even after a red one, so the log names all of them.
# Any non-zero gate exit counts as red: a gate that could not decide must not merge.
#
# Env: GH_TOKEN, GH_REPO, MG_BASE_REF, MG_HEAD_SHA, MG_REF, SEVERITY_CONFIG, and the
# gate's own optional settings. REPORT_SHA must be unset: in the queue the job's own
# conclusion is the report.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ -z "${REPORT_SHA:-}" ]] || {
  echo "REPORT_SHA is set, but the merge-queue leg reports through its exit code" >&2
  exit 1
}

prs="$(bash "$here/merge-group-prs.sh")"
red=()
while IFS= read -r pr; do
  [[ -n "$pr" ]] || continue
  if ! PR="$pr" bash "$here/review-findings-gate.sh" </dev/null; then
    red+=("$pr")
  fi
done <<<"$prs"

if ((${#red[@]} > 0)); then
  echo "::error::the review gate is red for pull request(s): ${red[*]}" >&2
  exit 1
fi
echo "the review gate is green for every pull request in the batch" >&2
