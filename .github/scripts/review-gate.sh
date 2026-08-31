#!/usr/bin/env bash
# Post the automated-review gate's verdict as a COMMIT STATUS on the PR head.
#
# PROBLEM CLASS — auto-merge landing a pull request past the reviewer. The cheap
# checks finish in about ninety seconds while an LLM review takes minutes, so a PR
# whose ruleset lists only the cheap checks merges before the reviewer has read
# it — and, once the review lands, merges with its findings unresolved, because
# the reviewer's own findings carry no merge consequence. Nothing is red either
# time; the review simply was not part of the merge gate.
#
# The predicate is stateless, and has two halves. A pull request is clear when
#   1. at least one undismissed review of it was written BY THE REVIEWER and
#      carries a body, and
#   2. none of the reviewer's finding threads is both unresolved and gating.
# It needs no memory of which reviews have been seen, and it re-derives the same
# answer on every event. Both halves of "by the reviewer, with a body" are load-
# bearing — see the filter below.
#
# RED IS THE DEFAULT, and it is the second half that makes the gate mean
# something. The reviewer posts every review as a COMMENT (post-pr-review.mjs:
# the merge consequence lives here, never in an APPROVE/REQUEST_CHANGES verdict),
# so a gate that asked only "did a review land?" went green the moment the
# reviewer reported a blocking finding, and auto-merge landed the pull request
# with the finding unread. The gate now stays `failure` until every gating
# finding thread is resolved. `failure` rather than `pending` before the first
# review, too: a pending status is invisible in a PR's check list and in
# `gh pr checks`, so a reader cannot tell "the reviewer has not spoken" from
# "no gate here at all".
#
# WHICH THREADS GATE comes from config/review-severities.json, the same SSOT
# post-pr-review.mjs stamps each finding from: a thread holds the merge when its
# root comment carries a gating severity's hidden marker, or leads with that
# severity's icon. A nit does not, and neither does a thread carrying no severity
# signal at all.
#
# PR-SCOPED, NOT HEAD-SCOPED, and that is load-bearing. Requiring a review OF THE
# CURRENT HEAD looks stricter and strands the pull request instead:
# .github/reviewer/decide-pr-review-trigger.sh answers run=false for a plain
# `synchronize`, so once the reviewer has approved, the next push produces a head
# nothing will ever review, and a head-scoped gate would hold that pull request
# red forever with no event able to clear it. Whether a later push satisfies
# the reviewer is a question the reviewer already owns: a non-approving verdict
# makes every push re-run the cheap recheck, and the review-required ruleset
# holds the merge meanwhile. This gate answers only what nothing else did — has
# the reviewer read this pull request, and does it still hold a finding open?
#
# A COMMIT STATUS, not this job's own check run. Under `pull_request_target` the
# job's check run is reported against the BASE commit, so it never satisfies a
# requirement evaluated on the pull request's head. A status posted explicitly on
# `HEAD_SHA` does.
#
# Can't-verify is RED, never green: an API failure propagates through `set -e`,
# because a gate that fails open lets a PR merge past a review nobody read.
#
# NOTHING CLEARS A RESOLVED THREAD BY ITSELF. GitHub fires no workflow event when
# a review thread is resolved, so a pull request whose author resolves every
# finding without pushing would sit red with no event able to re-run this script.
# The twice-hourly sweep (claude-reviewer-hold-clear.yaml) re-posts this verdict
# for every open pull request, which is the path that clears such a head.
#
# Env: GH_TOKEN, GH_REPO (owner/name), PR, HEAD_SHA, RUN_URL; REVIEWER_LOGIN
# optional.
set -euo pipefail

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${HEAD_SHA:?HEAD_SHA required}"
: "${GH_TOKEN:?GH_TOKEN required}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib/reviewer-login.bash disable=SC1091
source "$SCRIPT_DIR/lib/reviewer-login.bash"
reviewer_login_init
# The PR's review threads are read through the shared walker, never a query
# written a second time here: an unpaginated `reviewThreads(first: 100)` reports
# the first page as the whole set, and an under-read greens this gate.
# shellcheck source=../reviewer/lib/review-threads.bash disable=SC1091
source "$REPO_ROOT/.github/reviewer/lib/review-threads.bash"

# MUST stay byte-identical to the `name:` of the job in review-gate.yaml: that
# job name is what sync-required-checks registers as the ruleset's required
# context, and the status posted here has to carry the same context or the head
# never satisfies it.
GATE_CONTEXT="Automated review posted"

# Every review that still stands, paginated: a long-lived PR accumulates more
# than one page. A DISMISSED review is dropped here, which is what makes the
# workflow's `dismissed` trigger do something — dismissing the only review
# reddens the PR again.
#
# The filter is per-element (`.[] | select(…)`), never a reducer: `gh api
# --paginate --jq` applies the filter to EACH page, so a `first`/`max_by` would
# silently run once per page and answer from the last one.
#
# ONLY THE REVIEWER'S OWN reviews count, and only ones carrying a body. The
# gate's whole claim is "an automated review of this pull request exists", so
# every actor it credits has to be one that actually reviews:
#
#   * Any actor at all is a self-clearing gate. The PR author can open their own
#     pull request, submit a COMMENT review on it with one word, and the required
#     "Automated review posted" context goes green with no reviewer having run.
#     The reviewer identity filter closes that: the author's review is not the
#     reviewer's, so it credits nothing.
#   * A body-less review is not a review. GitHub SYNTHESIZES a body-less
#     COMMENTED review around a standalone review comment, and this repo posts
#     those under the reviewer's own identity whenever something replies
#     in-thread with addPullRequestReviewThreadReply. Without the body filter,
#     that reply alone greens the gate for a pull request the reviewer is still
#     holding. Every writer of a
#     REAL review here sends a non-empty body: the reviewer's post-pr-review.mjs
#     falls back to "Automated review." when the model returns nothing, and
#     auto-approve-skipped-pr.sh and approve-if-reviewer-hold-clear.sh hardcode theirs.
#
# The approval that auto-approve-skipped posts for a PR the reviewer skips by
# title or author still clears the gate: it is posted with GITHUB_TOKEN, so it
# carries the reviewer identity. Reading that OUTCOME beats re-deriving the skip
# predicate, which would be a second copy of the reviewer's own trigger rules.
reviewers="$(gh api --paginate "repos/${GH_REPO}/pulls/${PR}/reviews" \
  --jq ".[] | select(.state != \"DISMISSED\") | ${REVIEWER_MATCH_USER} | select((.body // \"\") != \"\") | .user.login // \"\"")"
reviewer="$(head -n 1 <<<"$reviewers")"

if [[ -z "$reviewer" ]]; then
  state=failure
  description="No automated review of this pull request yet"
else
  # WHICH FINDINGS HOLD A MERGE comes from config/review-severities.json, the SSOT
  # post-pr-review.mjs stamps each finding from. Both signals it can leave are read:
  # the hidden marker, and the icon the finding leads with, which is all a thread
  # posted before the stamper existed carries. A gating severity with no icon is
  # fatal — the gate must know every signal it is meant to see.
  severities="$REPO_ROOT/config/review-severities.json"
  GATING_MARKERS="$(jq -r '.gating[] | "<!-- severity: \(.) -->"' "$severities")"
  GATING_ICONS="$(jq -er '.gating[] as $s | (.icons[$s] // error("no icon for gating severity \($s)"))' "$severities")"
  export GATING_MARKERS GATING_ICONS

  # Count the reviewer's unresolved GATING threads. A marker counts only as a WHOLE
  # LINE of the root comment, so a finding that quotes one in its prose or in a
  # suggestion block cannot re-label itself; an icon counts only where the body
  # starts with it. A thread carrying neither signal — a nit, a reply thread — does
  # not hold the merge. The per-page filter emits one count per page, so the sum is
  # taken here rather than in a `--jq` reducer that would answer from the last page.
  unresolved_gating="$(fetch_review_threads "${GH_REPO%%/*}" "${GH_REPO##*/}" "$PR" \
    "[.[] | select(.isResolved == false)
          | ${REVIEWER_MATCH_THREAD_ROOT}
          | (.comments.nodes[0].body // \"\") as \$b
          | select(([env.GATING_MARKERS | split(\"\n\")[] | select(length > 0)]
                    | any(IN(\$b | split(\"\n\")[])))
                   or ([env.GATING_ICONS | split(\"\n\")[] | select(length > 0)]
                       | any(. as \$i | \$b | startswith(\$i))))] | length" |
    jq -s 'add // 0')"

  if [[ "$unresolved_gating" -gt 0 ]]; then
    state=failure
    description="${unresolved_gating} unresolved reviewer finding(s) — resolve each thread to clear"
  else
    state=success
    description="Reviewed by ${reviewer}; no unresolved findings"
  fi
fi

gh api -X POST "repos/${GH_REPO}/statuses/${HEAD_SHA}" \
  -f "state=${state}" \
  -f "context=${GATE_CONTEXT}" \
  -f "description=${description}" \
  -f "target_url=${RUN_URL:-}" >/dev/null

echo "posted ${state} status '${GATE_CONTEXT}' on ${HEAD_SHA}: ${description}" >&2
