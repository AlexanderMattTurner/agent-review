#!/usr/bin/env bash
# kcov-exclude: a GitHub Actions step body that no test runs: it provisions the
#   runner itself, so it has no entry point off a runner.
# Install @anthropic-ai/claude-code at the RESOLVER's pin. The shared installer
# resolves the pin from its own location, never the working directory: the review
# job's working directory is the REVIEWED repository, and a pin read from there
# would let the repository under review choose which CLI binary reads its diff.
set -euo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../scripts/install-claude-cli.sh"
