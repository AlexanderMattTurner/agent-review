# agent-review-gate

**A reusable GitHub Actions workflow that reviews a pull request with Claude and gates the merge on the findings.** The read itself is Anthropic's `code-review` plugin, run through [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action). This repository is the layer around that read: it decides when a pull request gets a read, feeds the plugin a sanitized diff, posts the findings as inline review threads, and holds the merge while a serious finding is unresolved.

The read runs in one of three shapes, and the size of the diff picks the shape:

1. **A diff of at most `max-diff-lines` lines gets one read.** The plugin's agents review it, and the workflow posts their findings as one review. Each finding is anchored to a line of the diff, so you read it beside the code that caused it.
2. **A larger diff is split per file, and each part gets its own read.** The parts run at the same time with the same prompt and the same models. The workflow folds their findings into one review. If a part delivers no review, the review says that the read is incomplete, and a thread asks a human to read the files that part held.
3. **A diff too large even to split gets no automated read.** The workflow posts a notice and opens one thread. A human reviews the code and resolves that thread. The pull request is never blocked with no way out.

Diff size is the usual reason for the third shape, and not the only one. A pull request touching more than 3,000 files also lands there, because GitHub's files API cannot hand back a complete diff for one. So can a diff that needs more parts than the limit allows, because a part never splits a file. So does a pull request that targets a branch other than your default branch: `claude-code-action` loads `CLAUDE.md` and `.claude/` from the pull request's target branch, so the reviewer reads only pull requests whose target is the default branch.

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
    uses: AlexanderMattTurner/agent-review-gate/.github/workflows/review.yaml@main
    with:
      reviewer-repository: AlexanderMattTurner/agent-review-gate
      # Name the SAME ref the `uses:` line above names, and change both
      # together. The workflow falls back to `github.job_workflow_sha`, which is
      # EMPTY in the expression context, so it refuses to clone rather than run
      # unpinned code.
      reviewer-ref: main
      extra-instructions: .github/prompts/review-extra-instructions.md
    secrets:
      rung_1: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN_FALLBACK }}
      # ... the other 6; see `.github/workflows/claude-review.yaml` in this
      # repository for a block a consumer can copy verbatim.
```

Three parts of that block need a word of explanation before you copy it.

**The event, `pull_request_target`.** GitHub can start a workflow on a pull request in two ways. The ordinary `pull_request` event runs the code sitting on the pull request's branch, and a fork's pull request gets no secrets there. `pull_request_target` instead takes the workflow file from the BASE branch and gives it the base repository's secrets. The reviewer needs those secrets. The pull request may come from a stranger, so the pull request's own code must never execute. The event alone does not hold that line: a step could still check the head out. What holds it is the checkout, and this reviewer checks out your DEFAULT branch, never the author-chosen base. Nothing stops a pull request from targeting a branch whose machinery was rewritten.

**The `permissions:` block, which is a ceiling.** A called workflow may request only what the calling job already holds. The block above is therefore the maximum, and the jobs inside narrow themselves from it. Granting less than this list does not merely limit the run. It ends the run with the status `startup_failure` before any job starts, and you get no red check to read.

**The `rung_` secrets, which are a fallback chain.** Each rung holds one Claude Code OAuth token, and the workflow tries them in order. An empty rung is skipped rather than fatal, so you can pass as few as one. A metered Anthropic API key is optional and bills real credits. Whatever rung holds one, the review tries it only after every subscription token has failed. The workflow tells the two apart by the token's prefix: a subscription token starts `sk-ant-oat`.

Both `uses:` and `reviewer-ref:` end in a ref, which is the branch name, tag or commit sha the reviewer runs at. Pin a commit sha rather than `main` once this repository cuts its first release: a branch ref runs whatever landed on it since you last read it. Two lines carry that ref, `uses:` and `reviewer-ref:`, and they must name the same one.

Two secrets are what the reviewer costs. Everything else is a knob:

| Input                      | Default               | What it does                                                                                                                                                                                                                        |
| -------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reviewer-repository`      | required              | The repository the reviewer's own code is cloned from, so the reviewed repository cannot rewrite what reviews it.                                                                                                                   |
| `reviewer-ref`             | required in practice  | The commit of `reviewer-repository` to run. Pass the sha your `uses:` line pins. An empty value stops the run at the clone step.                                                                                                    |
| `model`                    | `opus`                | The top model. It runs the review session, the two bug reviewers and the agents that confirm their findings.                                                                                                                        |
| `model-low`                | none, so `sonnet`     | The helper model. It runs the plugin's lighter agents: the CLAUDE.md search, the change summary, the two CLAUDE.md reviewers and the agents that confirm their findings.                                                            |
| `review-prompt`            | the reviewer's own    | A path in YOUR repository to review instructions that REPLACE the reviewer's own. Prefer `extra-instructions`: a replaced prompt misses every later change to the reviewer's. The findings schema still binds it.                   |
| `extra-instructions`       | none                  | A path in YOUR repository to instructions the reviewer follows after its prompt, winning where they conflict. Put your tree's conventions here. A path that names no file stops the read.                                           |
| `setup-command`            | none                  | A dependency sync run in your base checkout before the elide command, the log redactor and the post-review command.                                                                                                                 |
| `setup-cache-path`         | none                  | Paths `actions/cache` restores before the setup command runs — the directory a pinned toolchain installs into.                                                                                                                      |
| `setup-cache-key-files`    | none                  | A `hashFiles` pattern naming the file that holds the pin. This workflow hashes it into the cache key, so a bump refreshes the entry.                                                                                                |
| `elide-command`            | none                  | A command that drops generated files from the raw diff before the reviewer reads it. The reviewer's budget is diff lines, so a generated file spends budget on code nobody wrote. Name one if your diffs are mostly build files.    |
| `post-review-command`      | none                  | Run after the review step, with `GH_TOKEN`/`GH_REPO`/`PR`/`REPORT_SHA` set. It asks a required check to re-evaluate its gate. It runs after a failed review too, so the check reports the missing review.                           |
| `log-redactor`             | none, publishing none | A path in YOUR repository to a redactor for the agent's log. Empty publishes no logs rather than publishing raw ones.                                                                                                               |
| `reads-marked-from`        | none                  | An RFC3339 timestamp: when YOU started running a reviewer that stamps a review as a read. An older review counts as a read with no stamp. Set it when you bump your pin.                                                            |
| `max-reviews-per-pr`       | `1`                   | How many whole-diff reads one pull request may spend, from 0 to 999. It bounds the automatic triggers; `[opus-review]` and the review label fire whatever the count says. `0` turns the automatic reviewer off.                     |
| `max-delta-reviews-per-pr` | `1`                   | How many ACCUMULATED reads one pull request may spend, from 0 to 999. An accumulated read covers the pushes made since the head the last review read, and runs once the pull request is otherwise ready to merge. `0` turns it off. |
| `pr-number`                | none                  | The pull request an accumulated read runs on. A `workflow_dispatch` run carries no pull request payload, so the caller passes its own `inputs.pr` here. Leave it empty on a `pull_request_target` run.                              |
| `max-diff-lines`           | `12000`               | Above this many diff lines the read splits per file.                                                                                                                                                                                |
| `shard-max-lines`          | `4000`                | The most diff lines one part of a split read holds. A part never splits a file, so one large file gets a part of its own.                                                                                                           |
| `max-shardable-lines`      | `192000`              | Above this many diff lines the pull request gets the human-review notice and no read.                                                                                                                                               |
| `review-timeout-seconds`   | `1500`                | The wall clock one credential attempt may spend on the read, rounded up to whole minutes.                                                                                                                                           |

## The read

The read is the `code-review` plugin's own command, vendored at a pinned commit into `.github/reviewer/prompts/code-review.md`. The file names its source commit at the top. It keeps the plugin's agents and its bar for what counts as a finding:

1. A helper agent lists the CLAUDE.md files that apply to the changed paths, and another summarizes the change.
2. Four agents review the change in parallel. Two check it against those CLAUDE.md files, and two look for bugs and security problems in the changed code.
3. A separate agent confirms each issue and gives it a confidence score from 0 to 100. An issue that fails confirmation, or scores under 80, is dropped.

The vendored copy differs from the plugin in four ways, and each one keeps a property this repository already had:

- The plugin fetches the pull request with `gh`. Here the agent has no shell and no network tool, and reads only the diff and metadata the sanitizer already cleaned.
- The plugin stops on a draft, a trivial pull request, or one Claude already commented on. Here that step is removed, because the budget below decides when a pull request gets a read.
- The plugin posts its own comments. Here it returns its findings as structured output, and a separate job posts them.
- Each finding carries a category and a confidence score, so the workflow can set its severity.

### How a finding gets its severity

The plugin gives each finding a category and a confidence score. The workflow, not the model, turns that pair into a severity, in `.github/reviewer/record-review.mjs`:

| Category            | Confidence | Severity    | Holds the merge                      |
| ------------------- | ---------- | ----------- | ------------------------------------ |
| `bug` or `security` | 90 to 100  | 🔴 blocking | yes, on a first read and a delta one |
| `bug` or `security` | 80 to 89   | 🟡 warning  | yes, on a first read                 |
| `claude-md`         | 90 to 100  | 🟡 warning  | yes, on a first read                 |
| `claude-md`         | 80 to 89   | 🔵 nit      | no                                   |
| any                 | under 80   | dropped     | no                                   |

Which severities hold the merge is set by `gating` and `delta_gating` in `config/review-severities.json`. The table shows the values this repository ships. Each posted finding ends with its category and score, so a reader can see why it got its severity.

## Budget

Each pull request gets `max-reviews-per-pr` whole-diff reads, and one by default. Two things start a read whatever the count says: a commit whose title carries `[opus-review]`, and the `needs-auto-review` label. Each is somebody asking for this pull request to be read, so both still work at `max-reviews-per-pr: 0` while no push starts a review on its own. A read either one posts still counts, because the number bounds what one pull request costs. A run that finds the diff too large to read spends one as well: it paid for the job, and counting it is what stops an oversized pull request re-running on every push. Reviews this bot posts without reading a diff — the stand-in approval on a skipped pull request, an approval once a hold clears — spend nothing. Do not pair `0` with the review-findings gate: that gate holds the merge until a review exists, and it takes only `pending` or `failure` for a pull request with none, so every pull request would stay blocked until somebody added the label.

A push after that read is not read again on its own. Reading every push is what the budget exists to prevent, so the later pushes are BATCHED into one accumulated read: `max-delta-reviews-per-pr` of them, and one by default. That read covers only the files pushed since the head the last review read, and it is charged to its own budget, so an accumulated read never spends a whole-diff read.

An accumulated read is asked for only when the pull request has stopped moving. `dispatch-delta-review.sh` decides that, and two callers offer it a pull request. `claude-delta-review-dispatch.yaml` runs it the moment one of the pull request's check workflows finishes, which is what keeps the wait down to the review's own runtime. The twice-hourly sweep in `claude-reviewer-hold-clear.yaml` runs it per open pull request as well, because a reviewer thread resolved with no push fires no event at all. It starts a read when all of these hold: the pull request is open and not a draft; a review already covers an older head; the accumulated budget has room; no unresolved reviewer finding still holds the merge; and every check and status on the live head has concluded successfully. The review gate itself is excluded from that last test, because the gate is red exactly while this read is owed, and waiting for it would deadlock. A burst of pushes therefore coalesces with no timer: each push reds the checks again, and only the head that survives long enough to go green is read.

Either caller needs three things from your repository to start that read. Give its job `actions: write` and `pull-requests: write`, set `REVIEW_WORKFLOW` to your caller's workflow file name, and end your caller's `run-name:` with `PR <number>`. The run name is the only attribution an accumulated read has, so a caller whose name does not end that way gets no retry bound and re-dispatches a failing read on every event. `check-review-caller-run-name.py` refuses a caller in THIS repository whose name does not end that way; a caller in your own tree is yours to check, and copying that hook is one way.

Every review the reviewer posts carries a hidden coverage stamp naming the head and base it read, the reviewer commit that read them, and whether the read was the first or the accumulated one. The stamp is what lets a later run tell "findings nobody resolved" from "pushes nobody read". A review that was never posted stamps nothing, so a failed or skipped read advances no coverage. The gate reads the stamp too: while the live head is past the covered head and an accumulated read is still affordable, the gate stays red and says it is waiting for it. Once the accumulated budget is spent, the gate goes green with a description naming the head it was last read at, so it never claims coverage it does not have. The gate and both callers above read that budget from `max_delta_reviews_per_pr` in your severity config, so a merge-queue leg and a pull-request leg give the same answer. With two numbers, a queue leg holding the higher one fails the merge group of a pull request that entered the queue green. Set the reviewer's `max-delta-reviews-per-pr` to the same number: a reviewer budget above the gate's lets a head merge while its read is still running. A finding of the accumulated read holds the merge only when its severity is in `delta_gating` in the same config, which is `["blocking"]` here. That read runs once everything else is green, so its warnings post as threads that inform without holding the merge. A config with no `delta_gating` gates those findings by `gating`, as before.

Only reviews the reviewer posted itself carry a stamp. A pull request reviewed BEFORE you moved to a stamping reviewer therefore reads as never reviewed, and its next push buys a second read; `reads-marked-from` is the date that says those older reviews are reads. Remove it once no open pull request predates the date.

## Cost

A read is priced by the models that run it. `model` runs the review session, the two bug reviewers and the agents that confirm bug findings, so it carries most of the cost. `model-low` runs the lighter agents. Each posted review ends with a footer that names what the read cost.

## What the reviewer never does

It posts a review and nothing else. Six things hold that line:

- The only job that runs a model, `read`, holds `contents: read` alone in each of its parts. It can write to no pull-request surface at all. Separate jobs, `post` and `gate`, hold the write scopes and run no model.
- The agent can read only the checkout, the sanitized input and its own prompt. It cannot read its own credential from the runner.
- The diff passes an input sanitizer before the agent sees it.
- The agent has no shell and no network tool, so it cannot fetch the pull request past the sanitizer or run a command.
- The checkout is your default branch, never the pull request's head.
- The agent's log is published only through a redactor you name. No `log-redactor` means no published log.

A successful prompt injection can change what the findings say. It cannot push code, merge, or reach any other scope.
