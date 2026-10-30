#!/usr/bin/env bash
# The two ends of review.yaml's read-leg hand-off, around a REAL artifact upload
# and download: review-handoff-smoke.yaml runs `leg` in each matrix leg, uploads
# what it staged, downloads it the way the post job does, then runs `fold`.
#
# `leg <leg>` writes a stub review with one finding and stages it.
# `fold <case>` folds the downloaded legs and checks the result:
#   one      the unsharded `whole` leg alone        -> 1 finding, complete
#   two      both planned shards delivered          -> 2 findings, complete
#   partial  one of two planned shards delivered    -> 1 finding, INCOMPLETE
set -euo pipefail

input="${RUNNER_TEMP}/pr-input"

case "$1" in
leg)
  mkdir -p "$input"
  printf '{"summary":"stub","findings":[{"path":"%s.py","line":1,"severity":"blocking","title":"t"}]}\n' "$2" >"${input}/review.json"
  printf '{"type":"result","total_cost_usd":1,"model":"opus"}\n' >"${input}/cost.json"
  bash .github/reviewer/stage-leg-review.sh "$2" "$input" "${RUNNER_TEMP}/leg-out"
  ;;
fold)
  mkdir -p "$input"
  case "$2" in
  one) want_findings=1 want_complete=true ;;
  two) want_findings=2 want_complete=true ;;
  partial) want_findings=1 want_complete=false ;;
  *)
    echo "unknown case: $2" >&2
    exit 2
    ;;
  esac
  if [[ "$2" != one ]]; then
    mkdir -p "${input}/shards"
    printf '%s\n' '{"shards":[{"name":"shard-a","lines":1,"files":["a.py"]},{"name":"shard-b","lines":1,"files":["b.py"]}],"total_lines":2,"total_files":2}' >"${input}/shards/manifest.json"
  fi
  out="${RUNNER_TEMP}/fold-output"
  : >"$out"
  PR_INPUT_DIR="$input" SHARD_REVIEW_DIR="${RUNNER_TEMP}/read-legs" GITHUB_OUTPUT="$out" \
    node .github/reviewer/merge-shard-reviews.mjs
  got_findings="$(jq '.findings | length' "${input}/review.json")"
  got_complete="$(sed -n 's/^complete=//p' "$out")"
  if [[ "$got_findings" != "$want_findings" || "$got_complete" != "$want_complete" ]]; then
    echo "case $2: folded ${got_findings} finding(s), complete=${got_complete}; want ${want_findings}, complete=${want_complete}" >&2
    exit 1
  fi
  echo "case $2: folded ${got_findings} finding(s), complete=${got_complete}"
  ;;
*)
  echo "usage: review-handoff-smoke.sh leg <leg> | fold <case>" >&2
  exit 2
  ;;
esac
