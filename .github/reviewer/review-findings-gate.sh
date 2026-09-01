#!/usr/bin/env bash
#
# The review merge gate, as ONE stateless predicate: a pull request is clear to
# merge when
#   (a) the automated reviewer has read it at least once, or owes it no read at
#       all, AND
#   (b) no unresolved reviewer-rooted review thread still carries a merge-gating
#       finding.
# Resolving the last gating thread is what flips the gate — there is no approval
# to mint, no sticky verdict to supersede, and no state beyond what the pull
# request itself shows.
#
# PROBLEM CLASS — auto-merge landing a pull request past the reviewer. The cheap
# checks finish in about ninety seconds while a model review takes minutes, so a
# pull request gated only on those merges before the reviewer has read it, and
# the findings arrive on a merged pull request. Clause (a) closes that window.
#
# Clause (a) is PR-SCOPED, not head-scoped, and that is load-bearing. The
# reviewer does not read every push (decide-pr-review-trigger.sh answers
# run=false for a plain `synchronize`), so a head-scoped clause would hold a
# reviewed pull request at unreviewed forever the moment a push produced a head
# nothing will review.
#
# A DISMISSED review counts as a read, because a dismissal retracts the HOLD and
# not the reading. A consumer's hold sweeper dismisses the reviewer's
# CHANGES_REQUESTED on the routine path — GitHub refuses approvals from an
# Actions token, so dismissal is how a cleared hold gets cleared. Dropping it
# would turn that clearing into a permanent `pending` on every reviewed pull
# request. Clause (b) is the merge lever, and a dismissal moves none of its
# threads.
#
# The SKIP SET is clause (a)'s explicit complement. The reviewer reads no
# bot-authored, chore, style or release pull request, so no review of one ever
# arrives and a bare "wait for the first review" holds every Dependabot and
# every machine-cut release at pending forever. lib/review-skip-set.bash is the
# one definition of that set. Clause (b) still runs: the review label takes a
# pull request back out of the set, and any finding it then collects gates as
# usual.
#
# WHICH THREADS GATE comes from the SEVERITY_CONFIG the CONSUMER passes — the
# same file its reviewer stamps each finding from. A thread holds the merge when
# its root comment carries a gating severity's hidden `<!-- severity: … -->`
# marker on a line of its own (a whole-line match, so a finding that merely
# QUOTES a marker in prose or a suggestion block does not gate), or — the
# pre-marker fallback — when the body starts with that severity's icon. The
# reviewer renders findings from ITS copy of that model; the consumer's copy
# decides only what holds a merge in the consumer's repository.
#
# Two modes, one predicate:
#   * REPORT_SHA set — post the verdict as a COMMIT STATUS under GATE_CONTEXT on
#     that sha, and exit 0 whatever the verdict. The status is the output.
#   * REPORT_SHA unset — exit 0 when green, 1 otherwise. The merge queue leg
#     uses this: the job's own conclusion IS the report there.
#
# A STATUS, not a check run, and that distinction is load-bearing: a check run
# POSTed for a bare sha lands in a check suite of the app's own making, whose
# `pull_requests` array is empty, and the PR-scoped merge box counts only the
# suites tied to the pull request. Such a run shows green on the commit and in
# the Checks tab while the required context sits at "Expected — Waiting for
# status to be reported" forever. A status carries no suite and is read on the
# sha itself.
#
# GATE_UNREPORTED set skips the predicate entirely and posts a RED verdict on
# REPORT_SHA — the caller's `always()` arm for a run that died before it could
# evaluate, so a required check is never left unreported.
#
# Can't-verify is RED, never green: an API failure exhausting the retry ladder
# propagates as a non-zero exit (set -e), because a gate that fails open lets a
# pull request merge past findings nobody read.
#
# tests/test_review_findings_gate.py drives this script through a `gh` stub that
# runs its own `--jq` filters over canned payloads. The safety property lives
# inside those filters, so that is where it is pinned.
#
# Env: GH_TOKEN, GH_REPO (owner/name), PR, GATE_CONTEXT, SEVERITY_CONFIG.
# Optional: REPORT_SHA, RUN_URL, GATE_UNREPORTED, UNREVIEWED_STATE (pending or
# failure), RECHECK_LABEL, REVIEWER_LOGIN, REVIEW_LABEL, REVIEW_SKIP_TYPES,
# REVIEW_SKIP_BOT_AUTHORS.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.github/reviewer/lib/review-threads.bash
source "$SCRIPT_DIR/lib/review-threads.bash"
# shellcheck source=.github/reviewer/lib/pr-reviews.bash
source "$SCRIPT_DIR/lib/pr-reviews.bash"
# shellcheck source=.github/reviewer/lib/review-skip-set.bash
source "$SCRIPT_DIR/lib/review-skip-set.bash"

: "${GH_REPO:?GH_REPO required}"
: "${PR:?PR number required}"
: "${GH_TOKEN:?GH_TOKEN required}"
: "${SEVERITY_CONFIG:?SEVERITY_CONFIG required — the severity SSOT of the consumer}"
[[ -f "$SEVERITY_CONFIG" ]] || {
  echo "missing $SEVERITY_CONFIG — the gate cannot know which severities gate; failing closed" >&2
  exit 1
}
# MUST stay byte-identical to the consumer's required-check context: its
# merge-queue job carries that name, and a status posted under any other one
# leaves the head satisfying nothing. So it comes from the SSOT rather than a
# caller's literal; a caller may still override for a second gate of its own.
GATE_CONTEXT="${GATE_CONTEXT:-$(jq -er '.gate_context' "$SEVERITY_CONFIG")}"
: "${GATE_CONTEXT:?no gate_context in $SEVERITY_CONFIG and none passed}"

# The reviewer posts with the workflow GITHUB_TOKEN, so its reviews and threads
# are authored by github-actions[bot]. A consumer whose reviewer runs under a
# GitHub App or a PAT passes REVIEWER_LOGIN instead; leave it unset and this
# gate reads zero reviews and holds every pull request at unreviewed. GraphQL
# omits the REST `[bot]` suffix, and the shared jq predicates read this out of
# `env`.
#
# ONE login, not a set. lib/review-threads.bash also ships
# REVIEW_THREAD_ROOT_IS_A_GATING_REVIEWER, which walks a list so an external
# review bot's threads gate too. That predicate needs a per-login severity model
# — an external bot marks priority with its own badge, not with this reviewer's
# `<!-- severity: … -->` marker — and no consumer config here carries one, so
# wiring the walk without it would read those threads and gate on none of them.
# Single-login is the honest shape until a config defines that model.
REVIEWER_LOGIN_BARE="${REVIEWER_LOGIN:-github-actions}"
REVIEWER_LOGIN_BARE="${REVIEWER_LOGIN_BARE%'[bot]'}"
export REVIEWER_LOGIN_BARE

# GitHub caps a status description at 140 characters, so the reason a reader
# acts on lives in this run's log and behind target_url; the merge box gets as
# much of its head as fits.
#
# It also REJECTS any non-BMP code point outright ("Description doesn't accept
# 4-byte Unicode"), and a rejected POST is a hard red that hangs the pull
# request at "Expected". A red reason names the offending paths, so an emoji in
# a filename is enough to make the gate unreportable; the code points are
# dropped here rather than trusted to be absent upstream. The full reason still
# reaches the log line above and target_url intact.
post_verdict() {
  local state="$1" description="$2"
  local stripped
  stripped="$(jq -rn --arg d "$description" '$d | explode | map(select(. <= 65535)) | implode')"
  if [[ "$stripped" != "$description" ]]; then
    echo "stripped non-BMP characters from the status description; the log line above carries the full reason" >&2
    description="$stripped"
  fi
  if ((${#description} > 140)); then
    description="${description:0:137}..."
  fi
  retry gh api --method POST "repos/${GH_REPO}/statuses/${REPORT_SHA}" \
    -f "state=${state}" \
    -f "context=${GATE_CONTEXT}" \
    -f "description=${description}" \
    -f "target_url=${RUN_URL:-}" >/dev/null
}

# GATE_UNREPORTED mode: the evaluation never reached its POST, so report red
# here instead of leaving the head with no verdict at all. An unposted REQUIRED
# context reads as "Expected — Waiting for status to be reported", which blocks
# the merge on a check that never arrives: thread resolution fires no workflow
# event, so nothing re-derives this gate until the next push or a re-check
# label, and the merge box offers nothing to act on meanwhile. Red is the same
# state said out loud, and it keeps the retry path.
if [[ -n "${GATE_UNREPORTED:-}" ]]; then
  : "${REPORT_SHA:?REPORT_SHA required to report an unevaluated gate}"
  post_verdict failure \
    "the gate evaluation did not complete — re-run it by removing and re-adding the ${RECHECK_LABEL:-recheck-review-gate} label"
  echo "posted failure status '${GATE_CONTEXT}' on ${REPORT_SHA}: evaluation did not complete" >&2
  exit 0
fi

owner="${GH_REPO%%/*}"
name="${GH_REPO##*/}"

# Captured before iterating so a jq failure (malformed config, a gating severity
# with no icon) fails the gate loudly instead of dissolving into an empty loop.
severity_rows="$(jq -r '.gating[] as $s | [$s, (.icons[$s] // error("no icon for gating severity \($s)"))] | @tsv' "$SEVERITY_CONFIG")"
gating_predicate=""
while IFS=$'\t' read -r sev sev_icon; do
  # An empty `gating` list makes the herestring yield ONE blank line, and a
  # blank row would append `startswith("")` — true of every body — so the gate
  # would red every thread while the can-never-gate guard below stayed
  # satisfied. Skipping blanks is what lets that guard actually see an empty
  # predicate.
  [[ -n "$sev" && -n "$sev_icon" ]] || continue
  [[ -n "$gating_predicate" ]] && gating_predicate+=" or "
  gating_predicate+="(\$body | split(\"\\n\") | any(. == \"<!-- severity: ${sev} -->\"))"
  gating_predicate+=" or (\$body | startswith(\"${sev_icon}\"))"
done <<<"$severity_rows"
[[ -n "$gating_predicate" ]] || {
  echo "no gating severities in $SEVERITY_CONFIG — refusing to run a gate that can never gate" >&2
  exit 1
}

reviews="$(reviewer_reviews_ndjson "$owner" "$name" "$PR")"
# Clause (a): has the reviewer read this, or does it owe no read? `read_owed`
# false means clause (b) still decides — a pull request in the skip set can
# carry a finding thread anyway, because the oversized and degraded review paths
# open one without posting a review, so greening on the waiver alone would
# publish success over an open hold.
read_owed=false
if [[ -z "$reviews" ]] && ! pr_review_is_skipped "$owner" "$name" "$PR"; then
  read_owed=true
fi
if [[ "$read_owed" == true ]]; then
  verdict=unreviewed
  reason="waiting for the automated review of this pull request"
else
  gating="$(fetch_review_threads "$owner" "$name" "$PR" \
    "[.[] | select(.isResolved == false)
          | $REVIEW_THREAD_ROOT_IS_REVIEWER
          | . + {rootBody: (.comments.nodes[0].body // \"\")}
          | select(.rootBody as \$body | ${gating_predicate})
          | {path, line}]" |
    jq -s 'add // []')"
  count="$(jq 'length' <<<"$gating")"
  if [[ "$count" -eq 0 ]]; then
    verdict=green
    if [[ -z "$reviews" ]]; then
      reason="the reviewer owes this pull request no review, and no unresolved thread carries a gating finding"
    else
      reason="the reviewer has reviewed this PR and no unresolved thread carries a gating finding"
    fi
  else
    verdict=red
    where="$(jq -r '[.[] | (.path // "(general)") + (if .line then ":" + (.line|tostring) else "" end)] | join(", ")' <<<"$gating")"
    reason="${count} unresolved reviewer finding(s) still gate the merge: ${where} — resolve each thread (fix and let the resolver judge it, or resolve it with a reply) to clear"
  fi
fi

echo "review-findings gate on ${GH_REPO}#${PR}: ${verdict} — ${reason}" >&2

if [[ -z "${REPORT_SHA:-}" ]]; then
  [[ "$verdict" == "green" ]] || exit 1
  exit 0
fi

case "$verdict" in
green) state=success ;;
# The state for "nothing has read this yet" is the consumer's call. `pending`
# says "waiting" in the merge box; `failure` is louder — a pending status does
# not appear in `gh pr checks` at all, so a reader cannot tell "the reviewer has
# not spoken" from "no gate here". Both block a required context.
unreviewed)
  # ALLOWLISTED, never spliced: this is the one state that must never be
  # `success`, and a consumer input reaching `state=` unchecked is how a gate
  # posts green over a pull request nothing has read. Anything else fails
  # CLOSED to `failure`.
  case "${UNREVIEWED_STATE:-pending}" in
  pending | failure) state="${UNREVIEWED_STATE:-pending}" ;;
  *)
    echo "::warning::unreviewed-state '${UNREVIEWED_STATE}' is not pending or failure — reporting failure" >&2
    state=failure
    ;;
  esac
  ;;
*) state=failure ;;
esac
post_verdict "$state" "$reason"
echo "posted ${state} status '${GATE_CONTEXT}' on ${REPORT_SHA}" >&2
