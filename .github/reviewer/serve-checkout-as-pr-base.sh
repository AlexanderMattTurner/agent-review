#!/usr/bin/env bash
# Make `git fetch origin <base-ref>` in the current checkout return the checkout's
# own HEAD. Args: <base-ref>, the PR base the event payload names.
#
# PROBLEM CLASS — claude-code-action restores CLAUDE.md and .claude/ from the
# PR's base branch on every PR event. It reads `pull_request.base.ref` from the
# event file, ignores its `base_branch` input, and runs
# `git fetch origin <base.ref>`. A job whose checkout is already the trusted
# default branch then has two faults:
#   - a base deleted while the job waited for a runner fails every attempt with
#     "couldn't find remote ref";
#   - a stacked PR's author-chosen base overwrites the trusted config.
# The runner rewrites the event file before each action step, and the action
# re-points `origin` itself, so neither can be changed from outside. A local
# `url.<mirror>.insteadOf` rule survives both. The mirror holds HEAD under the
# base's name, so the restore copies the checkout onto itself.
#
# Call it after the last step that fetches from origin for real, since every
# later fetch of this repository reads the mirror.
set -euo pipefail

base_ref="${1:?usage: serve-checkout-as-pr-base.sh <base-ref>}"
mirror="${RUNNER_TEMP:?}/trusted-origin"

[[ -d "${mirror}.git" ]] || git init -q --bare "${mirror}.git"
git -C "${mirror}.git" fetch -q --depth=1 "file://$(git rev-parse --show-toplevel)" "+HEAD:refs/heads/${base_ref}"
# No `.git` on either side: the prefix then matches the checkout's URL and the
# action's `.git` one, and git finds `${mirror}.git` for both.
git config --local "url.file://${mirror}.insteadOf" "${GITHUB_SERVER_URL:?}/${GITHUB_REPOSITORY:?}"

# This check is what proves the rule matched the URL origin answers to now.
served="$(git ls-remote origin "refs/heads/${base_ref}")"
[[ "${served%%[[:space:]]*}" == "$(git rev-parse HEAD)" ]] || {
  echo "serve-checkout-as-pr-base: origin serves '${served}' for ${base_ref}, not HEAD $(git rev-parse HEAD)." >&2
  exit 1
}
