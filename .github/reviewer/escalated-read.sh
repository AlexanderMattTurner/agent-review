#!/usr/bin/env bash
# Re-read ONE shard with the caller's full-price model, after a cheaper model
# already read it and made a claim that holds the merge.
#
# Its own RUNNER_TEMP: the ladder names each attempt's log by RUNG alone
# (`review-attempt-<n>.json`), so a second read in this job would truncate the
# cheap read's log and both steps would report one path — the shard then priced
# from that log twice, and credited to the model whose findings did not post.
#
# The escalation trigger includes "the cheap read left no readable review", so
# neither copy may assume its source exists: an unguarded `cp` under `set -e`
# reds the shard on exactly the case this read is here to rescue.
#
# The re-read's own failure is absorbed: the cheap verdict is restored and the
# shard still publishes. `checks/claude-execution.py` runs on this read's log in
# the next step, which is where the failure is reported and the spend is billed.
#
# Env: PR_INPUT_DIR, REVIEWER_DIR, RUNNER_TEMP, GITHUB_ENV, plus everything
#      run-review-ladder.py itself requires (MODEL, the rung credentials).
set -euo pipefail

: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"
: "${REVIEWER_DIR:?REVIEWER_DIR required}"

escalated_temp="${RUNNER_TEMP:?RUNNER_TEMP required}/escalated"
mkdir -p "$escalated_temp"
# The post-condition, not the exit status: `mkdir -p` answers 0 on a dangling
# symlink, and the ladder's log would then land somewhere nothing reads.
[[ -d "$escalated_temp" ]] || {
  echo "escalated-read: could not create $escalated_temp" >&2
  exit 1
}
export RUNNER_TEMP="$escalated_temp"

cheap="${PR_INPUT_DIR}/review.cheap.json"
review="${PR_INPUT_DIR}/review.json"
# echo-fallback-ok: the left side tests the cheap read's own output, so the copy
# is that test's then-branch — an absent review is the escalate-on-unreadable case.
[[ ! -s "$review" ]] || cp "$review" "$cheap"

/usr/bin/python3 "${REVIEWER_DIR}/run-review-ladder.py" || true

if [[ ! -s "$review" ]]; then
  if [[ ! -s "$cheap" ]]; then
    echo "::error::the escalated read produced no review and the cheap read left none to restore"
    exit 1
  fi
  echo "::warning::the escalated read produced no review; keeping the cheap one"
  cp "$cheap" "$review"
  echo "ESCALATION_KEPT=false" >>"${GITHUB_ENV:?GITHUB_ENV required}"
fi
