# agent-review

**A reusable GitHub Actions workflow that reviews a pull request with Claude.** It reads the whole diff once, then posts one review with inline, line-anchored comments.

The read runs in two shapes:

1. **A diff that fits one model context is read whole**, by one agent, and posted as one review.
2. **A larger diff is split per file** and read by parallel agents, whose findings are folded into one review before it posts.

A diff too large even to split gets no automated read. The workflow then posts a notice and opens one thread a human resolves after reviewing, so the pull request is never blocked with no way out.

You call it from your own repository by `uses:`, pinned to a commit with the release version in a trailing comment.

## Call it

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
      # Pass the same sha the `uses:` line above names. The workflow falls back
      # to `github.job_workflow_sha`, which is EMPTY in the expression context,
      # and it then refuses to clone rather than run unpinned code. Move both
      # shas in the same edit.
      reviewer-ref: <the sha the uses: line pins>
      review-prompt: .github/prompts/claude-pr-review.md
    secrets:
      rung_1: ${{ secrets.FAR_ANTHROPIC_API_KEY }}
      # ... the remaining 7; see `.github/workflows/claude-review.yaml` in this
      # repository for a block a consumer can copy verbatim.
```

Pin a commit sha rather than `main` once this repository cuts its first release: a branch ref runs whatever landed on it since you last read it. `rung_1` is required and is a metered Anthropic API key: every run spends it first and reaches a subscription token only once it errors. Rungs 2 to 8 are Claude Code OAuth tokens, and an empty rung is skipped rather than fatal.

Two secrets are what the reviewer costs; everything else is a knob:

| Input                   | Default               | What it does                                                                                                                         |
| ----------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `reviewer-repository`   | required              | The repository the reviewer's own code is cloned from, so the reviewed repository cannot rewrite what reviews it.                    |
| `reviewer-ref`          | required in practice  | The commit of `reviewer-repository` to run. Pass the sha your `uses:` line pins. An empty value stops the run at the clone step.     |
| `model`                 | `claude-opus-5`       | The model behind every verdict.                                                                                                      |
| `review-prompt`         | the reviewer's own    | A path in YOUR repository to the review instructions, so a reviewer of your tree holds it to your conventions.                       |
| `setup-command`         | none                  | A dependency sync run in your base checkout before the model call.                                                                   |
| `setup-cache-path`      | none                  | Paths `actions/cache` restores before the setup command runs — the directory a pinned toolchain installs into.                       |
| `setup-cache-key-files` | none                  | A `hashFiles` pattern naming the file that holds the pin. This workflow hashes it into the cache key, so a bump refreshes the entry. |
| `elide-command`         | none                  | A command that drops generated output from the raw diff. The reviewer's budget is diff lines, so name one if yours are build files.  |
| `post-review-command`   | none                  | Run after the review lands, with `GH_TOKEN`/`GH_REPO`/`PR`/`REPORT_SHA` set — how a merge gate hears about a review it must read.    |
| `log-redactor`          | none, publishing none | A path in YOUR repository to a redactor for the agent's log. Empty publishes no logs rather than publishing raw ones.                |
| `max-diff-lines`        | `12000`               | Above this the read splits per file.                                                                                                 |
| `max-shardable-lines`   | `192000`              | Above this the pull request gets the human-review notice and no read.                                                                |

## Budget

Each pull request gets ONE whole-diff read. A later push is not re-read, because the threads the first read opened are what hold the merge. Push a commit whose title carries `[opus-review]` to buy another read.

## What the reviewer never does

It posts a review and nothing else. The job that reads the untrusted diff holds `contents: read` alone, so it can write to no pull-request surface at all; the diff passes an input sanitizer before the agent sees it; and the checkout is your default branch, never the pull request's head. A successful prompt injection can post a comment. It cannot push code, merge, or reach any other scope.
