# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body no test runs — it reads runner-only context or provisions the runner itself.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
# The PR review-thread read, in ONE place: the GraphQL document plus the
# `gh api graphql --paginate` call that walks it. INVARIANT: every step that needs a PR's review threads goes through fetch_review_threads, so no caller can ship a `reviewThreads(first: 100)` with no cursor — a query that silently drops every thread past the first page and reports the truncated slice as the whole set. Callers differ only in the jq they project each page's nodes through.
#
# Consumers: prepare-pr-review-input.sh, review-findings-gate.sh, dispatch-delta-review.sh, and .github/scripts/approve-if-reviewer-hold-clear.sh.
#
# API:
#   fetch_review_threads <owner> <name> <pr> <jq> [comments-per-thread] — walk EVERY page, applying <jq> to each page's nodes ARRAY. Non-zero once retries exhaust.
#   REVIEW_THREAD_CLEARED — jq predicate: this thread holds nothing any more.
#   unresolved_gating_findings <owner> <name> <pr> <severity-config> — the reviewer's threads that still HOLD a merge, as a JSON array of {path, line}.

# retry_stdout, sourced here rather than assumed, so a consumer gets the retry ladder by
# sourcing this file alone. lib-ci-retry.sh guards against a double source.
# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib-ci-retry.sh"
# shellcheck source=.github/reviewer/lib/review-markers.bash
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/review-markers.bash"

# `$endCursor` and `pageInfo` are what let `gh api graphql --paginate` walk: gh feeds endCursor back in and stops on hasNextPage=false, so drop either and it has no cursor to advance and returns page one forever, reporting a truncated thread set as the whole one. That set feeds the review-findings merge gate, so an under-read greens a gate that should be red.
# The node selection is the UNION of what the consumers project, because one shared document is the point. `resolvedBy` is what tells a hold cleared by somebody else from one its author cleared. The root comment's `pullRequestReview.submittedAt` ties a thread to the review that opened it. `comments` is the one field whose per-page cost grows with its size, so it is a variable: a caller keying on who OPENED each thread takes the root comment alone.
REVIEW_THREADS_QUERY=$(
  cat <<'GRAPHQL'
query($owner: String!, $name: String!, $pr: Int!, $comments: Int!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewThreads(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          resolvedBy { login }
          path
          line
          comments(first: $comments) { nodes { author { login } body pullRequestReview { submittedAt } } }
        }
      }
    }
  }
}
GRAPHQL
)

# jq projection: the thread's ROOT comment's author login, `""` when absent. GraphQL returns an app bot's login WITHOUT the REST `[bot]` suffix (`github-actions`, not `github-actions[bot]`), so the suffix is stripped here and every consumer compares the BARE login. One normalization, spliced by the predicate below.
# shellcheck disable=SC2034 # spliced into the sourcing scripts' projections
REVIEW_THREAD_ROOT_LOGIN='(.comments.nodes[0].author.login // "" | sub("\\[bot\\]$"; ""))'

# jq predicate: the thread's ROOT comment was authored by the automated reviewer. Requires the caller to have EXPORTED REVIEWER_LOGIN_BARE, since jq reads it out of `env`.
# shellcheck disable=SC2034 # spliced into the sourcing scripts' projections
REVIEW_THREAD_ROOT_IS_REVIEWER="select($REVIEW_THREAD_ROOT_LOGIN == env.REVIEWER_LOGIN_BARE)"

# jq predicate over one thread: it holds nothing any more. Both the merge gate and the
# hold-clear approver ask "does this thread still hold?" through this one predicate, so the
# two cannot disagree. A resolution by anyone, the PR's author included, settles the thread.
# shellcheck disable=SC2034 # spliced into jq programs
REVIEW_THREAD_CLEARED='(.isResolved == true)'

# Which severities HOLD a merge, as a jq predicate over `$body`. <list> is the jq
# path in the consumer's config that names them (`.gating` by default). Each
# gates two ways: a hidden `<!-- severity: … -->` marker on a line of its own (a whole-line match, so a
# body that merely QUOTES a marker in prose or a suggestion block does not gate),
# or — the pre-marker fallback — a body starting with that severity's icon.
#
# A gating list with no severities is refused rather than compiled: an empty
# predicate is `false` for every thread, so the gate would green over every
# finding, and the dispatcher would read a held PR as clear.
gating_severity_predicate() {
  local config="$1" list="${2:-.gating}" rows predicate="" sev sev_icon
  rows="$(jq -r "${list}[] as \$s | [\$s, (.icons[\$s] // error(\"no icon for gating severity \\(\$s)\"))] | @tsv" "$config")"
  while IFS=$'\t' read -r sev sev_icon; do
    # An empty `gating` list makes the herestring yield ONE blank line, and a
    # blank row would append `startswith("")` — true of every body.
    [[ -n "$sev" && -n "$sev_icon" ]] || continue
    [[ -n "$predicate" ]] && predicate+=" or "
    predicate+="(\$body | split(\"\\n\") | any(. == \"$(severity_marker "$sev")\"))"
    predicate+=" or (\$body | startswith(\"${sev_icon}\"))"
  done <<<"$rows"
  [[ -n "$predicate" ]] || {
    echo "no gating severities at ${list} in $config — refusing to answer a question that can never gate" >&2
    return 1
  }
  printf '%s' "$predicate"
}

# DELTA_READ_FINDING_MARKER (review-markers.bash) is the line post-pr-review.mjs stamps on each
# finding of an accumulated read. The poster defuses every HTML comment in model-written text, so
# only the poster can write this line.

# unresolved_gating_findings <owner> <name> <pr> <severity-config> — the
# reviewer's threads that still hold a gating finding (not REVIEW_THREAD_CLEARED),
# as a JSON array of {path, line}. The merge gate reds on a non-empty answer and
# the delta-read dispatcher declines on one, so both read the same threads through
# the same predicate: a PR still holding findings is one whose next read would be
# of a head its author is about to change.
#
# A finding of the accumulated read gates by `delta_gating` (else `gating`). That
# read runs when the pull request is otherwise ready to merge, so a severity it
# leaves out posts a thread that informs and does not hold the merge a second time.
#
# The caller must have EXPORTED REVIEWER_LOGIN_BARE.
unresolved_gating_findings() {
  local owner="$1" name="$2" pr="$3" config="$4" predicate delta_predicate
  predicate="$(gating_severity_predicate "$config")"
  delta_predicate="$(gating_severity_predicate "$config" '(.delta_gating // .gating)')"
  fetch_review_threads "$owner" "$name" "$pr" \
    "[.[] | select($REVIEW_THREAD_CLEARED | not)
          | $REVIEW_THREAD_ROOT_IS_REVIEWER
          | . + {rootBody: (.comments.nodes[0].body // \"\")}
          | select(.rootBody as \$body
                   | if (\$body | split(\"\\n\") | any(. == \"${DELTA_READ_FINDING_MARKER}\"))
                     then (${delta_predicate}) else (${predicate}) end)
          | {path, line}]" |
    jq -s 'add // []'
}

# fetch_review_threads <owner> <name> <pr> <jq> [comments-per-thread] — walks every page, applying <jq> to each page's nodes ARRAY.
fetch_review_threads() {
  local owner="$1" name="$2" pr="$3" projection="$4" comments="${5:-1}"
  retry_stdout gh api graphql --paginate \
    -f query="$REVIEW_THREADS_QUERY" \
    -f owner="$owner" -f name="$name" -F pr="$pr" -F comments="$comments" \
    --jq ".data.repository.pullRequest.reviewThreads.nodes | $projection"
}
