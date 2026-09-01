#!/usr/bin/env bash
# Print every pull request number a merge-group batch carries, one per line,
# newest ref first.
#
# PROBLEM CLASS — the queue's ref names only the LAST pull request of a batch
# (refs/heads/gh-readonly-queue/<base>/pr-<N>-<sha>). A check that reads the ref
# alone evaluates that one and no other, so with `max_entries_to_merge` above 1
# every other pull request in the batch reaches the default branch with no
# verdict of its own.
#
# The batch's own commits are the authority. Each commit's associated pull
# requests come from the API, so nothing here parses a commit message.
#
# THE RANGE STARTS AT THE BASE BRANCH, never at merge_group.base_sha. Under
# batching GitHub sets that payload field to the PREVIOUS QUEUE ENTRY, so a
# comparison from it covers only the final entry — which is the very failure
# this script exists to prevent. `.github/scripts/lib-decide-range.sh` documents
# the same trap for path-gated checks. The REST compare's `...` is merge-base
# semantics, so `<base branch>...<queue head>` is every commit the batch adds.
#
# Env: GH_TOKEN, GH_REPO, MG_BASE_REF, MG_HEAD_SHA, MG_REF.
set -euo pipefail

# shellcheck source=.github/reviewer/lib-ci-retry.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib-ci-retry.sh"

: "${GH_REPO:?GH_REPO required}"
: "${MG_BASE_REF:?MG_BASE_REF required — the BASE BRANCH of the batch; merge_group.base_sha is the previous queue entry}"
: "${MG_HEAD_SHA:?MG_HEAD_SHA required}"
: "${MG_REF:?MG_REF required}"

# The ref's own number is the floor. The queue builds the head commit itself, so
# the API may associate no pull request with it, and losing the very pull
# request the queue names would be worse than the batch problem above.
# ANCHORED to the ref's final component. Bash `=~` takes the LEFTMOST match, so
# an unanchored `/pr-([0-9]+)-` reads `feature/pr-123-work` out of a queue ref
# for pull request 456 and seeds the wrong number — a branch name is free text
# and may carry that shape. The queue appends `/pr-<N>-<sha>` last.
if [[ ! "$MG_REF" =~ /pr-([0-9]+)-[0-9a-fA-F]+$ ]]; then
  echo "cannot parse a pull request number from merge-group ref '${MG_REF}'" >&2
  exit 1
fi
numbers="${BASH_REMATCH[1]}"

# Both reads go through the shared retry ladder. Failing here is fail-closed,
# but in the merge queue a failed required job ejects the whole batch and
# re-queues it, so one transient 502 costs a full re-run of every check for
# every pull request in the group.
base_branch="${MG_BASE_REF#refs/heads/}"
shas="$(retry_stdout gh api "repos/${GH_REPO}/compare/${base_branch}...${MG_HEAD_SHA}" \
  --paginate --jq '.commits[].sha')"
while IFS= read -r sha; do
  [[ -n "$sha" ]] || continue
  # --paginate, like the compare above: a commit reachable from many branches
  # can carry more than one page of associated pull requests, and losing one is
  # losing its verdict. A batch is small, so the extra page costs nothing when
  # there is none.
  found="$(retry_stdout gh api --paginate "repos/${GH_REPO}/commits/${sha}/pulls" --jq '.[].number')"
  [[ -z "$found" ]] || numbers+=$'\n'"$found"
done <<<"$shas"

# Dedupe while KEEPING insertion order, which the header promises: the ref's own
# number is seeded first on purpose, and `sort -u` would both reorder that and
# sort the numbers as text, putting 7 after 100.
awk '!seen[$0]++' <<<"$numbers"
