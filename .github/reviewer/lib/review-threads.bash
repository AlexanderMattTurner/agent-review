# shellcheck shell=bash
# kcov-exclude: a GitHub Actions step body no test runs — it reads runner-only context or provisions the runner itself.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
# The PR review-thread read, in ONE place: the GraphQL document plus the
# `gh api graphql --paginate` call that walks it. INVARIANT: every step that needs a PR's review threads goes through fetch_review_threads, so no caller can ship a `reviewThreads(first: 100)` with no cursor — a query that silently drops every thread past the first page and reports the truncated slice as the whole set. Callers differ only in the jq they project each page's nodes through.
#
# Consumers: review-findings-gate.sh, dispatch-delta-review.sh, post-pr-review.sh, post-oversized-review.sh, and .github/scripts/approve-if-reviewer-hold-clear.sh.
#
# API:
#   fetch_review_threads <owner> <name> <pr> <jq> [comments-per-thread] — walk EVERY page, applying <jq> to each page's nodes ARRAY. Non-zero once retries exhaust.
#   raise_human_review_finding <marker> <prose-file> — open ONE file-level finding thread stamped <marker>, unless an open thread already carries it. Requires GH_REPO, PR, HEAD_SHA and an exported REVIEWER_LOGIN_BARE.
#   REVIEW_THREAD_CLEARED — jq predicate: this thread holds nothing any more. Reads an exported PR_AUTHOR.
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

# PROBLEM CLASS — the reviewer read the PR but could not report its findings as inline
# threads, so the gate would go green with nothing for anyone to resolve. Two consumers raise
# the same hold: post-oversized-review.sh (the diff is too large to read at all) and
# post-pr-review.sh (GitHub rejected the structured review, so some finding has no thread of
# its own). The caller writes the prose and names its MARKER; this appends that marker, the
# HUMAN_HOLD_MARKER and the blocking severity, so no caller can raise a hold the gate ignores or
# the PR's author may clear alone (see REVIEW_THREAD_CLEARED). It anchors to the PR's first
# changed file with subject_type=file: the concern is PR-wide, and all the gate needs is a
# RESOLVABLE thread.
raise_human_review_finding() {
  local marker="$1" prose="$2"
  local owner="${GH_REPO%%/*}" name="${GH_REPO##*/}"
  # The subshell keeps the export off the caller: an external `gh` process reads it.
  (
    export HUMAN_REVIEW_FINDING_MARKER="$marker"
    local threads open_thread anchor_path body
    threads="$(fetch_review_threads "$owner" "$name" "$PR" \
      ".[] | select(.isResolved == false)
           | $REVIEW_THREAD_ROOT_IS_REVIEWER
           | select((.comments.nodes[0].body // \"\") | contains(env.HUMAN_REVIEW_FINDING_MARKER))
           | .id")"
    open_thread="${threads%%$'\n'*}"
    if [[ -n "$open_thread" ]]; then
      echo "an unresolved ${marker} finding thread already exists (${open_thread}); not re-raising" >&2
      exit 0
    fi
    anchor_path="$(retry_stdout gh api "repos/${GH_REPO}/pulls/${PR}/files?per_page=1" --jq '.[0].filename')"
    body="$(mktemp)"
    {
      cat "$prose"
      printf '\n<sub>PR-wide finding: anchored to this file only to open a resolvable thread.</sub>\n\n'
      printf '%s\n%s\n' "$marker" "$HUMAN_HOLD_MARKER"
      severity_marker blocking
    } >"$body"
    retry gh api -X POST "repos/${GH_REPO}/pulls/${PR}/comments" \
      -f "commit_id=${HEAD_SHA}" \
      -f "path=${anchor_path}" \
      -f "subject_type=file" \
      -F body=@"$body" >/dev/null
    rm -f "$body"
    echo "raised the ${marker} finding thread on ${anchor_path} (head ${HEAD_SHA})" >&2
  )
}

# jq projection: the thread's ROOT comment's author login, `""` when absent. GraphQL returns an app bot's login WITHOUT the REST `[bot]` suffix (`github-actions`, not `github-actions[bot]`), so the suffix is stripped here and every consumer compares the BARE login. One normalization, spliced by the predicate below.
# shellcheck disable=SC2034 # spliced into the sourcing scripts' projections
REVIEW_THREAD_ROOT_LOGIN='(.comments.nodes[0].author.login // "" | sub("\\[bot\\]$"; ""))'

# jq predicate: the thread's ROOT comment was authored by the automated reviewer. Requires the caller to have EXPORTED REVIEWER_LOGIN_BARE, since jq reads it out of `env`.
# shellcheck disable=SC2034 # spliced into the sourcing scripts' projections
REVIEW_THREAD_ROOT_IS_REVIEWER="select($REVIEW_THREAD_ROOT_LOGIN == env.REVIEWER_LOGIN_BARE)"

# The hidden markers a needs-a-human hold thread carries. HUMAN_HOLD_MARKER is on every hold
# raise_human_review_finding opens; the two older markers cover a hold opened before it existed.
_HUMAN_HOLD_MARKERS_JSON="$(jq -cn --arg a "$HUMAN_HOLD_MARKER" --arg b "$OVERSIZED_REVIEW_MARKER" \
  --arg c "$DEGRADED_REVIEW_MARKER" '[$a, $b, $c]')"

# PROBLEM CLASS — a pull request's author clearing a hold that needs somebody else. GitHub lets
# the author resolve any thread on their own pull request. A finding thread is theirs to settle,
# but a needs-a-human hold (the diff was too large to read, or GitHub refused the findings) asks
# for a reader the author is not. Both the merge gate and the hold-clear approver ask "does this
# thread still hold?" through this one predicate, so the two cannot disagree.
#
# jq predicate over one thread: it is resolved, and when its root comment carries a hold marker,
# the resolver is somebody other than the PR's author, the reviewer, or any bot. INVARIANT — this
# is what stops the author from greening the gate on a hold alone. It reads PR_AUTHOR from `env`;
# an empty one, or a resolution GitHub attributes to nobody, leaves the hold in place.
# shellcheck disable=SC2034,SC2016 # spliced into jq programs; `$b`, `$r` and `$a` belong to jq
REVIEW_THREAD_CLEARED='(.isResolved == true
  and ((((.comments.nodes[0].body // "") as $b | '"$_HUMAN_HOLD_MARKERS_JSON"'
          | any(.[]; . as $m | $b | contains($m))) | not)
       or ((.resolvedBy.login // "" | ascii_downcase) as $r
           | (env.PR_AUTHOR // "" | ascii_downcase | sub("\\[bot\\]$"; "")) as $a
           | $r != "" and $a != "" and ($r | endswith("[bot]") | not)
             and $r != $a and $r != (env.REVIEWER_LOGIN_BARE // "" | ascii_downcase))))'

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
# The caller must have EXPORTED REVIEWER_LOGIN_BARE. The PR's author is read here, for
# REVIEW_THREAD_CLEARED; a read that returns nobody leaves every hold thread holding.
unresolved_gating_findings() {
  local owner="$1" name="$2" pr="$3" config="$4" predicate delta_predicate author
  predicate="$(gating_severity_predicate "$config")"
  delta_predicate="$(gating_severity_predicate "$config" '(.delta_gating // .gating)')"
  author="$(retry_stdout gh api "repos/${owner}/${name}/pulls/${pr}" --jq '.user.login // ""')"
  PR_AUTHOR="$author" fetch_review_threads "$owner" "$name" "$pr" \
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
