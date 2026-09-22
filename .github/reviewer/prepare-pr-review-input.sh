#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body with a behavioral suite: the suite runs the real
#   script as `bash <script>` against stubbed CLIs on PATH, so the branches are asserted but
#   no run is ever traced.
# Fetch the untrusted PR diff + metadata and run them through the
# agent-sanitizer (sanitize-pr-input.mjs) BEFORE the review agent sees them.
# The agent reads only the sanitized files this writes, never the raw
# `gh pr diff`, so an injection payload hidden in the diff cannot reach it intact.
#
# Above MAX_DIFF_LINES the diff no longer fits one model context, so this
# SHARDS it per-file and emits the shard list for parallel reads. Only a diff
# too large even to shard falls back to the human-review notice. The >300-file
# case (GitHub refuses the diff media type outright) is rebuilt from the files
# API first, then routed by size like any other.
#
# A READ=delta run covers the pushes after SINCE, which an earlier review already
# read: the compare API names those files and the diff is narrowed to them. A
# rebase or a force-push leaves SINCE off the head's history, so compare reports
# `diverged` and the whole diff is read instead — the one disposition that cannot
# claim coverage it does not have.
#
# Requires: gh authenticated (GH_TOKEN/GH_REPO), node + `pnpm install` done.
# Env: PR, PR_INPUT_DIR, HEAD_SHA; READ (first|delta, default first), SINCE (the
# covered head a delta read starts after), REVIEWER_SHA, ELIDE_COMMAND and the
# size bounds below are optional.
# Emits to GITHUB_OUTPUT: diff_lines, sharded, unreviewable, stale, shards,
# shard_count (the last two written by shard-pr-diff.py). Writes into
# $PR_INPUT_DIR: diff.txt/meta.txt, context.txt, coverage.json,
# sanitizer-report.txt, shards/, oversized-notice.txt.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Every `gh` read below is one GitHub API call away from a 5xx, and this script is the
# producing step of the required `Review findings resolved` gate: one transient fault
# reds that gate and blocks the PR until a human re-runs the workflow. An `HTTP 504` from
# api.github.com/graphql on a `gh pr view` is the observed shape.
# shellcheck source=.github/reviewer/lib-ci-retry.sh disable=SC1091
source "$here/lib-ci-retry.sh"

: "${PR:?PR number required}"
: "${PR_INPUT_DIR:?PR_INPUT_DIR required}"
# The head this run was dispatched for. Every coverage claim below names it, so a
# run that cannot say which commit it read must not run at all.
: "${HEAD_SHA:?HEAD_SHA required — the head this read covers}"
READ="${READ:-first}"
SINCE="${SINCE:-}"
REVIEWER_SHA="${REVIEWER_SHA:-}"

# The single-context cap. A diff line costs about 13.6 tokens, so 12k lines is roughly
# 163k tokens against a 200k window. The review job checks out the BASE only, so diff.txt
# is the sole source of the PR's changes — a reviewer that overruns cannot reconstruct
# them from the trusted tree.
MAX_DIFF_LINES="${MAX_DIFF_LINES:-12000}"
# The legs run in parallel, so the review's wall-clock is one leg's read; a
# smaller shard trades that for a finding likelier to fall across a boundary.
SHARD_MAX_LINES="${SHARD_MAX_LINES:-4000}"
# The largest diff that can be sharded AT ALL; above it a PR gets the human-review notice
# and no read. 192k keeps a wide margin over the largest PR in this repo's history (81,731
# lines). INVARIANT — DERIVED, never a second constant: the ceiling is stated once here, so
# changing SHARD_MAX_LINES cannot lower it by omission, and an operator overriding
# SHARD_MAX_LINES still gets a consistent fan-out.
MAX_SHARDABLE_LINES="${MAX_SHARDABLE_LINES:-192000}"
# The fan-out bound proper: a pathological diff must not spawn unbounded runners.
MAX_SHARDS="${MAX_SHARDS:-$((MAX_SHARDABLE_LINES / SHARD_MAX_LINES))}"

mkdir -p "$PR_INPUT_DIR" # bare-mkdir-ok: Linux CI runner (no BSD mkdir -p symlink semantics)

emit_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1" >>"$GITHUB_OUTPUT"
  fi
}

# Materialize the raw diff OUTSIDE the agent-readable input dir, so only the
# SANITIZED diff.txt ever reaches the reviewer.
raw_diff="$(mktemp)"
gh_err="$(mktemp)"
meta_pr="$(mktemp)"
meta_pages="$(mktemp)"
trap 'rm -f "$raw_diff" "$gh_err" "$meta_pr" "$meta_pages"' EXIT

# `gh pr diff` asks for the REST diff media type, which GitHub refuses with HTTP
# 406 above 300 CHANGED FILES — a cap on file count, not diff size, so even a
# small-diff wide PR gets no read at all. The files endpoint has no such cap,
# so rebuild the diff from it. Every OTHER gh failure stays red.

# `gh` exits 1 for every API refusal, so the 406 above is indistinguishable by
# exit code from a 5xx blip. This splits them: 3 is GitHub's ANSWER about this
# PR's width, which re-running only reproduces, so the retry below excludes it
# and reaches the rebuild immediately.
gh_pr_diff_once() {
  # `--allow-escape-sequences` is safe here: these bytes reach only the
  # sanitizer below, never a real terminal.
  gh pr diff "$PR" --allow-escape-sequences 2>"$gh_err" && return 0
  # Echoed per attempt, not once at the end: a retry that eventually succeeds
  # would otherwise leave the job log with the ci-retry counter and no trace of
  # WHAT GitHub answered, which is the one thing a later reader needs.
  cat "$gh_err" >&2
  grep -qiE 'exceeded the maximum number of files|too_large' "$gh_err" && return 3
  return 1
}

fetch_whole_pr_diff() {
  local out="$1" diff_rc=0
  RETRY_EXIT_CODES=1 RETRY_GH_BUDGET=1 retry_stdout gh_pr_diff_once >"$out" || diff_rc=$?
  ((diff_rc == 0)) && return 0
  ((diff_rc == 3)) || return "$diff_rc"
  echo "gh pr diff refused the >300-file diff; rebuilding it from the files API" >&2
  local files
  files="$(retry_stdout gh api --paginate "repos/{owner}/{repo}/pulls/${PR}/files")"
  # INVARIANT: the rebuilt diff is the WHOLE diff or no diff. The files endpoint
  # stops at 3000 entries with no cursor past it, so a wider PR arrives silently
  # short — and a partial diff sanitized and sharded reads as a complete review of
  # a PR whose later files nobody looked at. Refusing routes it to the
  # human-review notice instead, through the sharder's own over-budget code.
  local fetched changed
  # `--paginate` concatenates one ARRAY PER PAGE, so the count flattens them.
  fetched="$(jq -s 'flatten | length' <<<"$files")"
  changed="$(retry_stdout gh pr view "$PR" --json changedFiles --jq .changedFiles)"
  if [[ -n "$changed" ]] && ((fetched < changed)); then
    echo "the files API served ${fetched} of ${changed} changed files; a rebuilt diff would be partial" >&2
    return 3
  fi
  python3 "$here/pr/files-to-diff.py" <<<"$files" >"$out"
}

# 3 is the rebuild's refusal to serve a partial diff. It is a verdict about this
# PR's width, not a fault: the read is skipped and a human is asked, the same
# ending an unshardable diff takes.
fetch_rc=0
fetch_whole_pr_diff "$raw_diff" || fetch_rc=$?
if ((fetch_rc == 3)); then
  emit_output "sharded=false"
  emit_output "unreviewable=true"
  printf '%s\n' \
    "Automated review skipped: this PR changes more files than GitHub's own files API will serve, so no complete diff can be built for a review. A change this large should get a human review — please review it manually." \
    >"${PR_INPUT_DIR}/oversized-notice.txt"
  exit 0
fi
((fetch_rc == 0)) || exit "$fetch_rc"

# The head, re-read AFTER the diff fetch. A push inside the fetch window would
# otherwise be stamped as covered by a read that never saw it, which is the one
# error this whole record exists to prevent — every later reader trusts the stamp.
# The base comes from the same call, so the record names what the diff was taken
# against.
pr_state="$(retry_stdout gh pr view "$PR" --json headRefOid,baseRefOid)"
live_head="$(jq -r '.headRefOid // ""' <<<"$pr_state")"
base_sha="$(jq -r '.baseRefOid // ""' <<<"$pr_state")"
if [[ -z "$live_head" ]]; then
  echo "the live-head read returned nothing; refusing to claim coverage of a commit it cannot name" >&2
  exit 1
fi
if [[ "$live_head" != "$HEAD_SHA" ]]; then
  emit_output "stale=true"
  emit_output "sharded=false"
  emit_output "unreviewable=false"
  echo "the head moved ($HEAD_SHA -> $live_head) during the fetch; this run reads nothing" >&2
  exit 0
fi
emit_output "stale=false"

sanitize() { node "$here/sanitize-pr-input.mjs"; }

# The caller's own elider, run BEFORE the sanitizer, whose cost is per byte: a
# 14.7 MB diff that was 97% generated output spent 29 minutes there and hit the
# review job's 30-minute timeout, so no review posted. ELIDE_COMMAND names a
# command in the CALLER's checkout; it reads the raw diff at $1 and rewrites it
# in place. An empty ELIDE_COMMAND elides nothing, which reviews the whole diff.
if [[ -n "${ELIDE_COMMAND:-}" ]]; then
  # Run as a command LINE with the diff as `$1`, never word-split: splitting drops
  # the caller's own quoting, and appending the path silently ignores a command that
  # names `$1` in the middle of its arguments.
  bash -c "$ELIDE_COMMAND" -- "$raw_diff"
fi

sanitize <"$raw_diff" >"${PR_INPUT_DIR}/diff.txt" 2>"${PR_INPUT_DIR}/diff.report.txt"

# A delta read covers the pushes after SINCE. GitHub's compare API is the
# authority on which files those pushes touched, and the narrowing runs on the
# SANITIZED diff, so a shard of it is still a slice of the bytes a whole read
# would have shown the model.
#
# `ahead` is the only status that licenses a narrowed read: it says SINCE is on
# this head's history, so everything before it is what the earlier review saw. A
# rebase or a force-push answers `diverged`, where no commit range describes the
# change, and a compare of 300 files is GitHub's own page limit rather than the
# whole set. Both read the WHOLE diff again, and the stamp below says so.
COVERAGE_SCOPE=whole
if [[ "$READ" == "delta" && -n "$SINCE" ]]; then
  compare_rc=0
  compare="$(retry_stdout gh api "repos/{owner}/{repo}/compare/${SINCE}...${HEAD_SHA}" 2>/dev/null)" || compare_rc=$?
  compare_status=""
  compare_files=0
  if ((compare_rc == 0)); then
    compare_status="$(jq -r '.status // ""' <<<"$compare")"
    compare_files="$(jq -r '[.files[]? | .filename] | length' <<<"$compare")"
  fi
  if ((compare_rc == 0)) && [[ "$compare_status" == "ahead" ]] && ((compare_files > 0 && compare_files < 300)); then
    changed_paths="$(mktemp)"
    narrowed="$(mktemp)"
    jq -r '.files[]? | .filename' <<<"$compare" >"$changed_paths"
    python3 "$here/narrow-diff-to-paths.py" \
      --diff "${PR_INPUT_DIR}/diff.txt" --paths "$changed_paths" --out "$narrowed"
    # A narrowing that kept no file is NOT a delta of those commits. A revert
    # reaches this: compare names the reverted file, and the base...head diff has
    # no section for it, because it is in neither end. Keeping the empty result
    # would hand the model nothing and stamp `since:` over it, which spends the
    # accumulated budget and reports the pushes as read. Read the whole diff.
    if grep -q '^diff --git ' "$narrowed"; then
      mv "$narrowed" "${PR_INPUT_DIR}/diff.txt"
      COVERAGE_SCOPE="since:${SINCE}"
      echo "delta read: ${compare_files} file(s) changed since ${SINCE}" >&2
    else
      echo "delta read: no file changed since ${SINCE} has a section in this diff, so this reads the whole diff again" >&2
    fi
    rm -f "$changed_paths" "$narrowed"
  else
    echo "delta read: compare says '${compare_status:-unreadable}' over ${compare_files} file(s), so this reads the whole diff again" >&2
  fi
fi

# The coverage record every later reader trusts: which commits this read covered,
# which budget paid for it, and which reviewer commit did the reading. It is
# stamped onto the posted review body, and it is what tells the agent whether its
# diff.txt is the whole pull request or only the pushes after an earlier read.
jq -n --arg head "$HEAD_SHA" --arg base "$base_sha" --arg reviewer "$REVIEWER_SHA" \
  --arg read "$READ" --arg scope "$COVERAGE_SCOPE" --arg since "$SINCE" \
  '{head: $head, base: $base, reviewer: $reviewer, read: $read, scope: $scope, since: $since}' \
  >"${PR_INPUT_DIR}/coverage.json"

# The base tree's OTHER mentions of the identifiers this diff changes, so a
# sibling caller of a changed contract is in front of the model rather than one
# grep away. The workspace is the caller's trusted default branch, so this reads
# no PR-authored content and needs no sanitizer pass.
python3 "$here/build-review-context.py" \
  --diff "${PR_INPUT_DIR}/diff.txt" --out "${PR_INPUT_DIR}/context.txt" --repo-dir .

# Counted on the SANITIZED diff, and after the elision, so every downstream
# budget is spent on lines a review can act on.
diff_lines="$(wc -l <"${PR_INPUT_DIR}/diff.txt" | tr -d '[:space:]')"
emit_output "diff_lines=$diff_lines"

# meta.txt names every path the diff contains: the reviewer reads both halves and must not
# find them disagreeing. gh refuses `--jq` alongside `--slurp`, so the projection is a
# separate jq. Both payloads go to FILES, not jq's argv: `--argjson` puts the whole value
# on the command line, and a wide PR's files payload dies with E2BIG above roughly 2 MB —
# exactly the PRs the files-API fallback serves. `--slurpfile` wraps each in an array.
retry_stdout gh pr view "$PR" --json title,body,author >"$meta_pr"
retry_stdout gh api --paginate --slurp "repos/{owner}/{repo}/pulls/${PR}/files" >"$meta_pages"
# Per-file churn stays in the projection: a reviewer sizes a change by it and uses it to
# find the hunk that dominates the diff. `status` keeps the REST spelling rather than
# mapping to the GraphQL `changeType` enum, which would invent values GitHub never emits
# for the members that do not correspond.
jq -n --slurpfile pr "$meta_pr" --slurpfile pages "$meta_pages" \
  '$pr[0] + {files: [$pages[0][][] | {path: .filename, additions, deletions, status}]}' |
  sanitize >"${PR_INPUT_DIR}/meta.txt" 2>"${PR_INPUT_DIR}/meta.report.txt"

report="${PR_INPUT_DIR}/sanitizer-report.txt"
{
  if [[ -s "${PR_INPUT_DIR}/diff.report.txt" ]]; then
    echo "## Diff"
    cat "${PR_INPUT_DIR}/diff.report.txt"
  fi
  if [[ -s "${PR_INPUT_DIR}/meta.report.txt" ]]; then
    echo "## Metadata"
    cat "${PR_INPUT_DIR}/meta.report.txt"
  fi
} >"$report"

if [[ -s "$report" ]]; then
  echo "sanitizer neutralized injection-shaped content; see ${report}" >&2
else
  echo "(sanitizer found no injection-shaped content in the diff or metadata)" >"$report"
fi

# Size routing happens AFTER sanitization, so the shards are slices of exactly
# what the reviewer would otherwise have read.

if ((diff_lines <= MAX_DIFF_LINES)); then
  emit_output "sharded=false"
  emit_output "unreviewable=false"
  exit 0
fi

# Over the single-context cap. The ceiling is checked HERE, on the line count: a
# diff dominated by one file splits into one shard however large it is, so the
# shard cap below would admit a million-line file as a single reviewable slice.
if ((diff_lines > MAX_SHARDABLE_LINES)); then
  emit_output "sharded=false"
  emit_output "unreviewable=true"
  printf '%s\n' \
    "Automated review skipped: this PR's diff is ${diff_lines} lines, over the ${MAX_SHARDABLE_LINES}-line ceiling for an automated read. A change this large should get a human review — please review it manually." \
    >"${PR_INPUT_DIR}/oversized-notice.txt"
  echo "diff ${diff_lines} lines exceeds MAX_SHARDABLE_LINES=${MAX_SHARDABLE_LINES}; asking for a human review" >&2
  exit 0
fi

# Shard, and give up when even the sharded fan-out would be unbounded.
shard_rc=0
python3 "$here/shard-pr-diff.py" \
  --diff "${PR_INPUT_DIR}/diff.txt" \
  --out-dir "${PR_INPUT_DIR}/shards" \
  --max-lines "$SHARD_MAX_LINES" \
  --max-shards "$MAX_SHARDS" \
  --model "${MODEL:-}" \
  --model-low "${MODEL_LOW:-}" \
  --low-tier-paths "${LOW_TIER_PATHS:-}" \
  --bulk-lines "${BULK_DIFF_LINES:-0}" || shard_rc=$?

# 3 is the sharder's over-budget refusal; any other non-zero stays a red job.
if ((shard_rc == 3)); then
  emit_output "sharded=false"
  emit_output "unreviewable=true"
  printf '%s\n' \
    "Automated review skipped: this PR's diff is ${diff_lines} lines, which needs more than the ${MAX_SHARDS}-shard fan-out limit even after splitting it per file. A change this large should get a human review — please review it manually." \
    >"${PR_INPUT_DIR}/oversized-notice.txt"
  echo "diff ${diff_lines} lines needs more than MAX_SHARDS=${MAX_SHARDS} shards; asking for a human review" >&2
  exit 0
fi
((shard_rc == 0)) || exit "$shard_rc"

emit_output "sharded=true"
emit_output "unreviewable=false"
echo "diff ${diff_lines} lines exceeds MAX_DIFF_LINES=${MAX_DIFF_LINES}; sharded for a fan-out review" >&2
