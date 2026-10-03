#!/usr/bin/env bash
# Install @anthropic-ai/claude-code globally at the repo's pinned version, so
# every job that reaches for the CLI runs the same build and Dependabot bumps it.
#
# Two pins, read in order:
#   .github/claude-cli/package.json          the repo's own override, if present
#   .github/claude-cli-default/package.json  the template's default, synced
# Each is its own package.json outside the pnpm workspace: listing the CLI at the
# root makes `pnpm install` refuse the workspace (ERR_PNPM_IGNORED_BUILDS).
#
# allow-unsynced: .github/claude-cli/package.json — the repo-owned override; absent, the default is read.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/retry.bash disable=SC1091
source "$SCRIPT_DIR/lib/retry.bash"

override="$(realpath -m "${SCRIPT_DIR}/../claude-cli/package.json")"
default="$(realpath -m "${SCRIPT_DIR}/../claude-cli-default/package.json")"
if [[ -f "$override" ]]; then
  pin_file="$override"
elif [[ -f "$default" ]]; then
  pin_file="$default"
else
  echo "no Claude CLI pin: create ${override} or ${default}" >&2
  exit 1
fi
if ! version="$(jq -r '.dependencies["@anthropic-ai/claude-code"]' "$pin_file")"; then
  echo "jq could not read ${pin_file}" >&2
  exit 1
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "${pin_file} must pin dependencies[\"@anthropic-ai/claude-code\"] to one exact version, got '${version}'" >&2
  exit 1
fi
echo "Installing @anthropic-ai/claude-code@${version} from ${pin_file}"
# Bound + retry: a bare `npm install -g` has no timeout, so a hung registry
# connection would stall until the job's timeout. `timeout` caps one attempt;
# retry_cmd rides out a transient blip.
retry_cmd 3 10 timeout --kill-after=10 180 npm install -g "@anthropic-ai/claude-code@${version}"
claude --version
