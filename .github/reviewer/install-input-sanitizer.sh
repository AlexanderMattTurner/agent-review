#!/usr/bin/env bash
# Install the agent-sanitizer package the reviewer's node scripts import
# (sanitize-pr-input.mjs, post-pr-review.mjs).
#
# It installs into `.github/reviewer/node_modules`, beside the scripts that import
# it, so ESM resolution finds it without a package.json or a lockfile in the
# repository under review — a consumer needs no sanitizer dependency of its own.
# The pin comes from THIS script, in the reviewer's own repository, never from the
# reviewed repository's tree.
set -euo pipefail

SANITIZER_VERSION="2.47.3"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# --ignore-scripts: the published package ships a postinstall, and this install
# runs in a job holding the review credentials.
npm install --prefix "$here" --no-save --no-package-lock \
  --ignore-scripts --no-audit --no-fund \
  "agent-sanitizer@${SANITIZER_VERSION}"
