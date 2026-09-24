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
      rung_8: ${{ secrets.FAR_ANTHROPIC_API_KEY }}
      # ... the other 7; see `.github/workflows/claude-review.yaml` in this
      # repository for a block a consumer can copy verbatim.
```

Three parts of that block need a word of explanation before you copy it.

**The event, `pull_request_target`.** GitHub can start a workflow on a pull request in two ways. The ordinary `pull_request` event runs the code sitting on the pull request's branch, and a fork's pull request gets no secrets there. `pull_request_target` instead takes the workflow file from the BASE branch and gives it the base repository's secrets. The reviewer needs those secrets. The pull request may come from a stranger, so the pull request's own code must never execute. The event alone does not hold that line: a step could still check the head out. What holds it is the checkout, and this reviewer checks out your DEFAULT branch, never the author-chosen base. Nothing stops a pull request from targeting a branch whose machinery was rewritten.

**The `permissions:` block, which is a ceiling.** A called workflow may request only what the calling job already holds. The block above is therefore the maximum, and the jobs inside narrow themselves from it. Granting less than this list does not merely limit the run. It ends the run with the status `startup_failure` before any job starts, and you get no red check to read.

**The eight `rung_` secrets, which are a fallback chain.** Each rung holds one credential, and the workflow tries them in order. Rungs 1 to 7 are Claude Code OAuth tokens, and an empty rung is skipped rather than fatal. `rung_8` is a metered Anthropic API key: a run reaches it only once every subscription token has errored. A metered key in any other rung is also tried last. The review refuses to run when no rung holds a metered key.

Both `uses:` and `reviewer-ref:` end in a ref, which is the branch name, tag or commit sha the reviewer runs at. Pin a commit sha rather than `main` once this repository cuts its first release: a branch ref runs whatever landed on it since you last read it. Two lines carry that ref, `uses:` and `reviewer-ref:`, and they must name the same one.

Two secrets are what the reviewer costs. Everything else is a knob:

| Input                      | Default               | What it does                                                                                                                                                                                                                        |
| -------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reviewer-repository`      | required              | The repository the reviewer's own code is cloned from, so the reviewed repository cannot rewrite what reviews it.                                                                                                                   |
| `reviewer-ref`             | required in practice  | The commit of `reviewer-repository` to run. Pass the sha your `uses:` line pins. An empty value stops the run at the clone step.                                                                                                    |
| `model`                    | `claude-opus-5`       | The model behind every verdict.                                                                                                                                                                                                     |
| `review-prompt`            | the reviewer's own    | A path in YOUR repository to the review instructions, so a reviewer of your tree holds it to your conventions.                                                                                                                      |
| `setup-command`            | none                  | A dependency sync run in your base checkout before the model call.                                                                                                                                                                  |
| `setup-cache-path`         | none                  | Paths `actions/cache` restores before the setup command runs — the directory a pinned toolchain installs into.                                                                                                                      |
| `setup-cache-key-files`    | none                  | A `hashFiles` pattern naming the file that holds the pin. This workflow hashes it into the cache key, so a bump refreshes the entry.                                                                                                |
| `elide-command`            | none                  | A command that drops generated files from the raw diff before the reviewer reads it. The reviewer's budget is diff lines, so a generated file spends budget on code nobody wrote. Name one if your diffs are mostly build files.    |
| `post-review-command`      | none                  | Run after the review step, with `GH_TOKEN`/`GH_REPO`/`PR`/`REPORT_SHA` set. It asks a required check to re-evaluate its gate. It runs after a failed review too, so the check reports the missing review.                           |
| `log-redactor`             | none, publishing none | A path in YOUR repository to a redactor for the agent's log. Empty publishes no logs rather than publishing raw ones.                                                                                                               |
| `reads-marked-from`        | none                  | An RFC3339 timestamp: when YOU started running a reviewer that stamps a review as a read. An older review counts as a read with no stamp. Set it when you bump your pin.                                                            |
| `model-low`                | none                  | A cheaper model for the shards of a sharded read. Empty reads every shard with `model`.                                                                                                                                             |
| `low-tier-paths`           | none                  | Regex a path must match for its shard to take `model-low`. Every file in the shard must match.                                                                                                                                      |
| `bulk-diff-lines`          | `0`                   | Diff lines past which every shard takes `model-low`. `0` never does.                                                                                                                                                                |
| `escalate-blocking`        | `false`               | Re-read a shard with `model` when the cheap read's own verdict or a finding is `blocking`.                                                                                                                                          |
| `max-reviews-per-pr`       | `1`                   | How many whole-diff reads one pull request may spend, from 0 to 999. It bounds the automatic triggers; `[opus-review]` and the review label fire whatever the count says. `0` turns the automatic reviewer off.                     |
| `max-delta-reviews-per-pr` | `1`                   | How many ACCUMULATED reads one pull request may spend, from 0 to 999. An accumulated read covers the pushes made since the head the last review read, and runs once the pull request is otherwise ready to merge. `0` turns it off. |
| `pr-number`                | none                  | The pull request an accumulated read runs on. A `workflow_dispatch` run carries no pull request payload, so the caller passes its own `inputs.pr` here. Leave it empty on a `pull_request_target` run.                              |
| `max-diff-lines`           | `12000`               | Above this many diff lines the read splits per file.                                                                                                                                                                                |
| `max-shardable-lines`      | `192000`              | Above this many diff lines the pull request gets the human-review notice and no read.                                                                                                                                               |

## Budget

Each pull request gets `max-reviews-per-pr` whole-diff reads, and one by default. Two things start a read whatever the count says: a commit whose title carries `[opus-review]`, and the `needs-auto-review` label. Each is somebody asking for this pull request to be read, so both still work at `max-reviews-per-pr: 0` while no push starts a review on its own. A read either one posts still counts, because the number bounds what one pull request costs. A run that finds the diff too large to read spends one as well: it paid for the job, and counting it is what stops an oversized pull request re-running on every push. Reviews this bot posts without reading a diff — the stand-in approval on a skipped pull request, an approval once a hold clears — spend nothing. Do not pair `0` with the review-findings gate: that gate holds the merge until a review exists, and it takes only `pending` or `failure` for a pull request with none, so every pull request would stay blocked until somebody added the label.

A push after that read is not read again on its own. Reading every push is what the budget exists to prevent, so the later pushes are BATCHED into one accumulated read: `max-delta-reviews-per-pr` of them, and one by default. That read covers everything since the head the last review read, and it is charged to its own budget, so an accumulated read never spends a whole-diff read.

An accumulated read is asked for only when the pull request has stopped moving. `dispatch-delta-review.sh` decides that, and two callers offer it a pull request. `claude-delta-review-dispatch.yaml` runs it the moment one of the pull request's check workflows finishes, which is what keeps the wait down to the review's own runtime. The twice-hourly sweep in `claude-reviewer-hold-clear.yaml` runs it per open pull request as well, because a reviewer thread resolved with no push fires no event at all. It starts a read when all of these hold: the pull request is open and not a draft; a review already covers an older head; the accumulated budget has room; no unresolved reviewer finding still holds the merge; and every check and status on the live head has concluded successfully. The review gate itself is excluded from that last test, because the gate is red exactly while this read is owed, and waiting for it would deadlock. A burst of pushes therefore coalesces with no timer: each push reds the checks again, and only the head that survives long enough to go green is read.

Either caller needs three things from your repository to start that read. Give its job `actions: write` and `pull-requests: write`, set `REVIEW_WORKFLOW` to your caller's workflow file name, and end your caller's `run-name:` with `PR <number>`. The run name is the only attribution an accumulated read has, so a caller whose name does not end that way gets no retry bound and re-dispatches a failing read on every event. `check-review-caller-run-name.py` refuses a caller in THIS repository whose name does not end that way; a caller in your own tree is yours to check, and copying that hook is one way.

Every review the reviewer posts carries a hidden coverage stamp naming the head and base it read, the reviewer commit that read them, and whether the read was the first or the accumulated one. The stamp is what lets a later run tell "findings nobody resolved" from "pushes nobody read". A review that was never posted stamps nothing, so a failed or skipped read advances no coverage. The gate reads the stamp too: while the live head is past the covered head and an accumulated read is still affordable, the gate stays red and says it is waiting for it. Once the accumulated budget is spent, the gate goes green with a description naming the head it was last read at, so it never claims coverage it does not have. The gate and both callers above read that budget from `max_delta_reviews_per_pr` in your severity config, so a merge-queue leg and a pull-request leg give the same answer. With two numbers, a queue leg holding the higher one fails the merge group of a pull request that entered the queue green. Set the reviewer's `max-delta-reviews-per-pr` to the same number: a reviewer budget above the gate's lets a head merge while its read is still running. A finding of the accumulated read holds the merge only when its severity is in `delta_gating` in the same config, which is `["blocking"]` here. That read runs once everything else is green, so its warnings post as threads that inform without holding the merge. A config with no `delta_gating` gates those findings by `gating`, as before.

Only reviews the reviewer posted itself carry a stamp. A pull request reviewed BEFORE you moved to a stamping reviewer therefore reads as never reviewed, and its next push buys a second read; `reads-marked-from` is the date that says those older reviews are reads. Remove it once no open pull request predates the date.

## Cost

A read is priced by the model that runs it, so the reviewer lets one pull request use two. `model` reads everything by default. `model-low`, once you name one, reads the shards you declare low-risk by path and every shard of a diff past `bulk-diff-lines` — the mechanical sweep, where the read is still worth having and the top model's price is not. Both are off until you set them, and a shard takes the cheaper model only when EVERY file in it qualifies, so a source file bundled with a doc change keeps the full-price read.

Sizing it: one 136,480-line sweep read with `claude-opus-5` cost $103.84. Claude Sonnet 5 is priced 2.5x under Opus 5 on both input and output, so the same read on the low model costs about $42.

`escalate-blocking` sends one claim back up. A shard the cheap model read, whose own output holds the merge — verdict `blocking`, or a finding of severity `blocking` — is read again by `model`, and that second read is the one that posts. Warnings, nits and a `needs_changes` verdict stay as the cheap model wrote them: they are the bulk of what a review says, and escalating them buys the diff at full price twice.

It confirms a claim; it cannot find one nobody made. What the cheap read never noticed, the full-price read never sees, so a path whose misses are expensive belongs on `model` from the start rather than in the cheap tier with escalation behind it.

The arithmetic: a shard that escalates pays both reads. With the low model at 40% of the high one, a review escalating a fraction k of its shards costs `0.4 + k` of the untiered price — cheaper while under 60% of shards escalate, and 1.4x in the worst case, where every shard is blocking.

## What the reviewer never does

It posts a review and nothing else. Four things hold that line:

- The job that reads the untrusted diff holds `contents: read` alone. It can write to no pull-request surface at all.
- The diff passes an input sanitizer before the agent sees it.
- The checkout is your default branch, never the pull request's head.
- The agent's log is published only through a redactor you name. No `log-redactor` means no published log.

A successful prompt injection can post a comment. It cannot push code, merge, or reach any other scope.
