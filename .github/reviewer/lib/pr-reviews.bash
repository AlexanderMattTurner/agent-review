# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body that no test runs: it reads the runner's context
#   — GITHUB_*, a job-scoped GH_TOKEN, an actions/ working directory — or provisions the
#   runner itself, so it has no entry point off a runner.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# The PR review read, in ONE place: the GraphQL document, the reviewer filter, and
# the latest-by-submittedAt fold that together answer "what has the automated
# reviewer posted on this PR?". Every step that asks that question goes through
# these helpers, so no caller can ship a `reviews(first: 100)` with no cursor — a
# query that returns the OLDEST 100 reviews and reports a stale state as the live
# one — nor a fold that picks by array order instead of submittedAt.
#
# Consumers: decide-pr-review-trigger.sh, recheck-pr-review-owed.sh,
# review_findings_gate.py.

# retry_stdout: sourced here rather than assumed, so a consumer gets the retry
# ladder by sourcing this file alone. lib-ci-retry.sh guards against double-source.
# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"

# $endCursor + pageInfo are what make `gh api graphql --paginate` able to walk:
# gh feeds the previous page's endCursor back in and stops on hasNextPage=false.
# Drop either and gh has no cursor to advance, so it returns page one forever —
# and page one of `reviews` is the OLDEST page, so an unpaginated query on a
# long-lived PR reports a superseded review as the current state.
REVIEWS_QUERY=$(
  cat <<'GRAPHQL'
query($owner: String!, $name: String!, $pr: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviews(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login } state body submittedAt fullDatabaseId commit { oid } }
      }
    }
  }
}
GRAPHQL
)

# reviewer_reviews_ndjson <owner> <name> <pr> — every one of the reviewer's REAL reviews as
# NDJSON objects {state, body, submittedAt, reviewId, reviewedSha}.
#
# Empty-body reviews are excluded HERE, in the one shared read: GitHub synthesizes a
# body-less COMMENTED review around every standalone review-comment POST, and counting one
# as a review both spends a read from this PR's budget and satisfies the review-findings
# gate's reviewed-at-all condition vacuously. The real reviewer never posts an empty body.
#
# reviewId is fullDatabaseId AS A STRING ("" when GitHub omits it), not databaseId: review
# database ids exceed Int32, which GraphQL's Int-typed databaseId errors on.
#
# The caller must EXPORT REVIEWER_LOGIN_BARE: the jq reads it out of `env`, and GraphQL
# returns an app bot's login WITHOUT the REST `[bot]` suffix.
reviewer_reviews_ndjson() {
  local owner="$1" name="$2" pr="$3"
  retry_stdout gh api graphql --paginate \
    -f query="$REVIEWS_QUERY" -f owner="$owner" -f name="$name" -F pr="$pr" \
    --jq '.data.repository.pullRequest.reviews.nodes[]
          | select((.author.login // "" | sub("\\[bot\\]$"; "")) == env.REVIEWER_LOGIN_BARE)
          | select((.body // "") != "")
          | {state, body, submittedAt,
             reviewId: (.fullDatabaseId // "" | tostring),
             reviewedSha: (.commit.oid // "")}'
}

# The stand-in approval auto-approve-skipped-pr.sh posts carries this marker in its
# body. It is the SSOT for that string: the producer sources this file for it, and
# `real_reviewer_reviews` below drops any review carrying it.
#
# An HTML comment, so GitHub renders nothing and a human reading the review sees the
# prose alone.
AUTO_APPROVAL_MARKER='<!-- automated-approval-no-read -->'

# real_reviewer_reviews <owner> <name> <pr> — the reviews that SPEND this PR's
# read budget, as NDJSON, one object per line, oldest page first. A caller that
# wants both a COUNT and the latest verdict reads this once and folds the result
# locally, so two questions cost one paginated walk. Non-zero only once the retry
# ladder is exhausted.
#
# The stand-in approval is excluded HERE, because every consumer asks the same
# question: how many whole-diff reads has this PR spent? That approval is posted by
# a job that skipped the read, under the reviewer's identity because it posts with
# GITHUB_TOKEN. Counted as a read it latches the PR permanently unread — the
# `needs-auto-review` label then buys a run whose own decide answers "already
# reviewed", and a PR that leaves the skip class never gets its first pass.
# `reviewer_reviews_ndjson` above keeps counting it: the review-findings gate asks
# whether a review EXISTS for the ruleset, and the stand-in is exactly that.
real_reviewer_reviews() {
  reviewer_reviews_ndjson "$@" |
    jq -rc --arg marker "$AUTO_APPROVAL_MARKER" \
      'select((.body // "") | contains($marker) | not)'
}

# require_review_budget — bind MAX_REVIEWS_PER_PR from the environment, or refuse.
# REQUIRED, with no default: review.yaml's `max-reviews-per-pr` input owns the
# number, and a per-script default is a copy that drifts out of sight of the
# caller. Both consumers call this, so neither can read the value the other's
# rejects.
#
# The pattern refuses every spelling `[[ -lt ]]` reads as something other than
# the number written. A LEADING ZERO, which `^[0-9]+$` admits, is read as octal:
# `08` errors on the invalid digit and evaluates FALSE. A value past 63 bits
# wraps NEGATIVE: `[[ 0 -lt 9223372036854775808 ]]` is false, so decide would
# never review with a green job and no notice. Three digits is the ceiling,
# which is far past any budget a PR can spend.
require_review_budget() {
  MAX_REVIEWS_PER_PR="${MAX_REVIEWS_PER_PR:?MAX_REVIEWS_PER_PR required — review.yaml passes its max-reviews-per-pr input}"
  [[ "$MAX_REVIEWS_PER_PR" =~ ^(0|[1-9][0-9]{0,2})$ ]] || {
    echo "max-reviews-per-pr must be a whole number from 0 to 999 with no leading zero, not '$MAX_REVIEWS_PER_PR'" >&2
    exit 1
  }
}

# latest_of_reviews — the newest of the NDJSON reviews on stdin as one JSON
# object, or NOTHING when there are none, which a caller reads with `[[ -n … ]]`.
#
# The fold spans the whole walk rather than one page: gh emits each page's --jq
# output after the last, and page one of `reviews` is the OLDEST, so a fold that
# picked by array order would answer with the first review ever posted.
latest_of_reviews() {
  jq -rs 'if length == 0 then empty else (sort_by(.submittedAt) | last) end'
}
