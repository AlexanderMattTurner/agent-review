#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against a stubbed `gh` on PATH, so the branches are asserted but
#   no run is ever traced.
# Report every way the automated reviewer fell short on this run, as ONE message.
#
# A shortfall is a defect in the reviewer, never a reason to hold the pull request for a person:
# the merge gate judges the findings the review has. This only makes the defect visible. With
# REVIEWER_DEFECT_ISSUE set it comments on that issue in GH_REPO; empty, or when GitHub refuses
# the comment, it writes the same text to the job summary and a `::warning::`.
#
# Shortfalls, in the order reported:
#   - the read job delivered nothing to fold (FOLD_OUTCOME failure or skipped);
#   - some shard delivered no review ($PR_INPUT_DIR/incomplete-read.txt, from the fold);
#   - each line post-pr-review.sh appended to REVIEWER_SHORTFALL_FILE;
#   - posting the review failed (POST_OUTCOME failure).
#
# Requires: GH_REPO, PR, HEAD_SHA, RUN_URL. Optional: GH_TOKEN, REVIEWER_DEFECT_ISSUE, PR_INPUT_DIR,
# REVIEWER_SHORTFALL_FILE, READ_RESULT, FOLD_OUTCOME, POST_OUTCOME, GITHUB_STEP_SUMMARY.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR required}"
: "${HEAD_SHA:?HEAD_SHA required}"
: "${RUN_URL:?RUN_URL required}"

# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-ci-retry.sh"

items="$(mktemp)"
body="$(mktemp)"
trap 'rm -f "$items" "$body"' EXIT

case "${FOLD_OUTCOME:-}" in
failure) echo "- No read leg delivered a review (read job: ${READ_RESULT:-unknown}), so no review was posted." >>"$items" ;;
skipped) echo "- The sanitized input could not be fetched, so nothing was read or posted." >>"$items" ;;
success | "") ;;
*) echo "- The step that combines the read legs ended ${FOLD_OUTCOME}, so no review was posted." >>"$items" ;;
esac
incomplete="${PR_INPUT_DIR:-}/incomplete-read.txt"
if [[ -n "${PR_INPUT_DIR:-}" && -s "$incomplete" ]]; then
  # File names come from the PR, so they sit in a fence with no backtick that could close it.
  printf -- "- Some files went unread:\n\n  \`\`\`text\n  %s\n  \`\`\`\n" "$(tr -d '`\n' <"$incomplete")" >>"$items"
fi
if [[ -n "${REVIEWER_SHORTFALL_FILE:-}" && -s "$REVIEWER_SHORTFALL_FILE" ]]; then
  sed 's/^/- /' "$REVIEWER_SHORTFALL_FILE" >>"$items"
fi
if [[ "${POST_OUTCOME:-}" == "failure" ]]; then
  echo "- Posting the review failed; the run log names the refusal." >>"$items"
fi

if [[ ! -s "$items" ]]; then
  echo "the reviewer fell short nowhere on this run" >&2
  exit 0
fi

{
  printf "The automated reviewer fell short on %s#%s at head \`%s\`:\n\n" "$GH_REPO" "$PR" "$HEAD_SHA"
  cat "$items"
  printf '\nThe merge gate judged the findings the review has. Run: %s\n' "$RUN_URL"
} >"$body"

issue="${REVIEWER_DEFECT_ISSUE:-}"
if [[ -n "$issue" ]]; then
  if [[ ! "$issue" =~ ^[1-9][0-9]*$ ]]; then
    echo "::warning::reviewer-defect-issue '${issue}' is not an issue number; reporting to the job summary instead" >&2
  elif retry_stdout gh api -X POST "repos/${GH_REPO}/issues/${issue}/comments" \
    -F body=@"$body" --jq '.html_url'; then
    exit 0
  else
    echo "::warning::GitHub refused the comment on issue #${issue}; reporting to the job summary instead" >&2
  fi
fi

if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
  cat "$body" >>"$GITHUB_STEP_SUMMARY"
fi
# One fixed line, and the detail stays out of the log: it quotes PR file names, which must
# not reach the runner as a workflow command.
echo "::warning::the automated reviewer fell short on PR #${PR} at ${HEAD_SHA:0:7}; the job summary says how"
