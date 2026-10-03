#!/usr/bin/env bash
# Install @anthropic-ai/claude-code globally, pinned to the version
# .github/claude-cli/package.json names — one file, so every job that reaches
# for the CLI runs the same build, and Dependabot bumps it.
#
# The pin is its own package.json, outside the pnpm workspace, rather than a
# root devDependency: nothing here imports the CLI, and listing it at the root
# makes `pnpm install` refuse the whole workspace over the package's unapproved
# install scripts (ERR_PNPM_IGNORED_BUILDS).
#
# allow-unsynced: .github/claude-cli/package.json — each repo owns its pin, and
# a missing file fails below naming the path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/retry.bash disable=SC1091
source "$SCRIPT_DIR/lib/retry.bash"

pin_file="$(cd "${SCRIPT_DIR}/.." && pwd)/claude-cli/package.json"
version="$(jq -r '.dependencies["@anthropic-ai/claude-code"]' "$pin_file" 2>/dev/null || true)"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "could not read a pinned @anthropic-ai/claude-code version from ${pin_file}, got '${version}'" >&2
  exit 1
fi
echo "Installing @anthropic-ai/claude-code@${version}"
# Bound + retry: a bare `npm install -g` has no timeout, so a hung registry
# connection (intermittent on GitHub egress) would stall here until the whole
# job's timeout cancels it. `timeout` caps a stuck attempt; retry_cmd rides out a
# transient blip rather than failing the run.
retry_cmd 3 10 timeout --kill-after=10 180 npm install -g "@anthropic-ai/claude-code@${version}"
claude --version
