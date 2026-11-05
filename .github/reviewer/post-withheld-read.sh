#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against a localhost GitHub, so the branches are asserted but
#   no run is ever traced.
# Record a read that prepare-pr-review-input.sh WITHHELD, as one COMMENT review carrying
# READ_WITHHELD_MARKER and the reason prepare wrote. review-findings-gate.sh holds the merge on
# that marker until a later complete read, so the review must exist for the hold to say why.
#
# The review carries no read marker and no coverage stamp, so it spends no budget and claims no
# head. A later push therefore asks for the read again and is withheld again; this posts nothing
# when the reviewer's newest review is already a withheld notice, so the pull request gets one.
#
# Requires: gh authenticated (GH_TOKEN), GH_REPO, PR, PR_INPUT_DIR holding withheld.txt.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"

# shellcheck source=.github/reviewer/lib/pr-reviews.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/pr-reviews.bash"

reason_file="${PR_INPUT_DIR}/withheld.txt"
[[ -s "$reason_file" ]] || {
  echo "::error::missing ${reason_file} — prepare withheld the read but wrote no reason" >&2
  exit 1
}

# REST lists reviews oldest first, so the last body the reviewer posted is its newest review.
export READ_WITHHELD_MARKER
latest="$(retry_stdout gh api --paginate "repos/${GH_REPO}/pulls/${PR}/reviews" \
  --jq '.[] | select((.user.login // "" | sub("\\[bot\\]$"; "")) == "github-actions")
            | select((.body // "") != "") | .body | contains(env.READ_WITHHELD_MARKER)')"
if [[ "${latest##*$'\n'}" == "true" ]]; then
  echo "the reviewer's newest review on PR ${PR} already records the withheld read; not re-posting" >&2
  exit 0
fi

body="$(mktemp)"
trap 'rm -f "$body"' EXIT
{
  printf '%s\n\n' "$READ_WITHHELD_MARKER"
  printf '**The automated review of this pull request was withheld, so the merge is held.**\n\n'
  cat "$reason_file"
} >"$body"
retry gh api -X POST "repos/${GH_REPO}/pulls/${PR}/reviews" -f "event=COMMENT" -F body=@"$body" >/dev/null
echo "recorded the withheld read on PR ${PR}" >&2
