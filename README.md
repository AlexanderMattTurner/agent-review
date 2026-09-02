# agent-review

**A reusable GitHub Actions workflow that reviews a pull request with Claude.** It reads the whole diff once, then posts a review. The comments are inline: each one is anchored to a line of the diff, so you read the finding beside the code that caused it.

The read runs in three shapes, and the size of the diff picks the shape:

1. **A diff small enough for the model to read in one go is read whole**, by one agent, and posted as one review.
2. **A larger diff is sharded per file.** Parallel agents read the files at the same time. The workflow folds their findings into one review before it posts.
3. **A diff too large even to split gets no automated read.** The workflow posts a notice and opens one thread. A human reviews the code and resolves that thread. The pull request is never blocked with no way out.

Diff size is the usual reason for the third shape, and not the only one. A pull request touching more than 3,000 files also lands there, because GitHub's files API cannot hand back a complete diff for one. So can a diff under both line limits that still needs more shards than the limit allows, because a shard never splits a file.

You call it from your own repository by `uses:`, pinned to a commit with the release version in a trailing comment.

## Call it

Put this in a workflow file in your own repository, such as `.github/workflows/claude-review.yaml`.

```yaml
on:
  pull_request_target: # the reviewer never checks the pull request's own code out
    types: [opened, ready_for_review, synchronize]

jobs:
  review:
    # The ceiling this workflow's own jobs are narrowed from. A called workflow
    # may request only what the calling job holds, so granting less ends the run
    # in `startup_failure` before any job starts.
    permissions:
      contents: read
      pull-requests: write
      statuses: write
      actions: read
      checks: read
    uses: AlexanderMattTurner/agent-review/.github/workflows/review.yaml@main
    with:
      reviewer-repository: AlexanderMattTurner/agent-review
      # Name the SAME ref the `uses:` line above names, and change both
      # together. The workflow falls back to `github.job_workflow_sha`, which is
      # EMPTY in the expression context, so it refuses to clone rather than run
      # unpinned code.
      reviewer-ref: main
      review-prompt: .github/prompts/claude-pr-review.md
    secrets:
      rung_1: ${{ secrets.FAR_ANTHROPIC_API_KEY }}
      # ... the remaining 7; see `.github/workflows/claude-review.yaml` in this
      # repository for a block a consumer can copy verbatim.
```

Three parts of that block need a word of explanation before you copy it.

**The event, `pull_request_target`.** GitHub can start a workflow on a pull request in two ways. The ordinary `pull_request` event runs the code sitting on the pull request's branch, and a fork's pull request gets no secrets there. `pull_request_target` instead takes the workflow file from the BASE branch and gives it the base repository's secrets. The reviewer needs those secrets. The pull request may come from a stranger, so the pull request's own code must never execute. The event alone does not hold that line: a step could still check the head out. What holds it is the checkout, and this reviewer checks out your DEFAULT branch, never the author-chosen base. Nothing stops a pull request from targeting a branch whose machinery was rewritten.

**The `permissions:` block, which is a ceiling.** A called workflow may request only what the calling job already holds. The block above is therefore the maximum, and the jobs inside narrow themselves from it. Granting less than this list does not merely limit the run. It ends the run with the status `startup_failure` before any job starts, and you get no red check to read.

**The eight `rung_` secrets, which are a fallback chain.** Each rung holds one credential, and the workflow tries them in order. `rung_1` is required and is a metered Anthropic API key: every run spends it first and reaches a subscription token only once it errors. Rungs 2 to 8 are Claude Code OAuth tokens, and an empty rung is skipped rather than fatal.

Both `uses:` and `reviewer-ref:` end in a ref, which is the branch name, tag or commit sha the reviewer runs at. Pin a commit sha rather than `main` once this repository cuts its first release: a branch ref runs whatever landed on it since you last read it. Two lines carry that ref, `uses:` and `reviewer-ref:`, and they must name the same one.

Two secrets are what the reviewer costs. Everything else is a knob:

| Input                   | Default               | What it does                                                                                                                                                                                                                     |
| ----------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reviewer-repository`   | required              | The repository the reviewer's own code is cloned from, so the reviewed repository cannot rewrite what reviews it.                                                                                                                |
| `reviewer-ref`          | required in practice  | The commit of `reviewer-repository` to run. Pass the sha your `uses:` line pins. An empty value stops the run at the clone step.                                                                                                 |
| `model`                 | `claude-opus-5`       | The model behind every verdict.                                                                                                                                                                                                  |
| `review-prompt`         | the reviewer's own    | A path in YOUR repository to the review instructions, so a reviewer of your tree holds it to your conventions.                                                                                                                   |
| `setup-command`         | none                  | A dependency sync run in your base checkout before the model call.                                                                                                                                                               |
| `setup-cache-path`      | none                  | Paths `actions/cache` restores before the setup command runs — the directory a pinned toolchain installs into.                                                                                                                   |
| `setup-cache-key-files` | none                  | A `hashFiles` pattern naming the file that holds the pin. This workflow hashes it into the cache key, so a bump refreshes the entry.                                                                                             |
| `elide-command`         | none                  | A command that drops generated files from the raw diff before the reviewer reads it. The reviewer's budget is diff lines, so a generated file spends budget on code nobody wrote. Name one if your diffs are mostly build files. |
| `post-review-command`   | none                  | Run after the review step, with `GH_TOKEN`/`GH_REPO`/`PR`/`REPORT_SHA` set. It asks a required check to re-evaluate its gate. It runs after a failed review too, so the check reports the missing review.                        |
| `log-redactor`          | none, publishing none | A path in YOUR repository to a redactor for the agent's log. Empty publishes no logs rather than publishing raw ones.                                                                                                            |
| `max-reviews-per-pr`    | `1`                   | How many whole-diff reads one pull request may spend. It bounds the automatic triggers; `[opus-review]` and the review label fire whatever the count says. `0` turns the automatic reviewer off.                                 |
| `max-diff-lines`        | `12000`               | Above this many diff lines the read splits per file.                                                                                                                                                                             |
| `max-shardable-lines`   | `192000`              | Above this many diff lines the pull request gets the human-review notice and no read.                                                                                                                                            |

## Budget

Each pull request gets `max-reviews-per-pr` whole-diff reads, and one by default. A later push is not re-read once they are spent, because the threads the first read opened are what hold the merge. Two things start a read whatever the count says: a commit whose title carries `[opus-review]`, and the `needs-auto-review` label. Each is somebody asking for this pull request to be read, so both still work at `max-reviews-per-pr: 0` while no push starts a review on its own. A read either one posts still counts, because the number bounds what one pull request costs. Do not pair `0` with the review-findings gate: that gate holds the merge until a review exists, and it takes only `pending` or `failure` for a pull request with none, so every pull request would stay blocked until somebody added the label.

## What the reviewer never does

It posts a review and nothing else. Four things hold that line:

- The job that reads the untrusted diff holds `contents: read` alone. It can write to no pull-request surface at all.
- The diff passes an input sanitizer before the agent sees it.
- The checkout is your default branch, never the pull request's head.
- The agent's log is published only through a redactor you name. No `log-redactor` means no published log.

A successful prompt injection can post a comment. It cannot push code, merge, or reach any other scope.
