#!/usr/bin/env bash
# Check that the GitHub token used for template sync has the 'workflow' scope
# (only enforceable for classic PATs; fine-grained PATs and GITHUB_TOKEN don't
# expose scopes via the API response header).
#
# Inputs (env):
#   TOKEN              GitHub token to inspect
#   TOKEN_SECRET_NAME  the repository secret TOKEN came from, which every message
#                      names: that is the secret the user must fix

set -euo pipefail

: "${TOKEN:?TOKEN must be set}"
: "${TOKEN_SECRET_NAME:?TOKEN_SECRET_NAME must be set}"

# No -f: with -f curl discards the headers of an HTTP error, so the rate-limit
# branch below could never see x-ratelimit-remaining. curl reports the status
# itself (-w): behind an HTTPS proxy the first header line is the CONNECT reply,
# "200 Connection Established", not the API's. Stderr is captured so a network
# error is surfaced rather than yielding empty headers (which would misclassify
# a classic PAT as fine-grained).
hdrfile=$(mktemp)
errfile=$(mktemp)
trap 'rm -f "$hdrfile" "$errfile"' EXIT
if ! status=$(curl --proto '=https' -sS -I --suppress-connect-headers -o /dev/null \
  -D "$hdrfile" -w '%{http_code}' -H "Authorization: token $TOKEN" \
  https://api.github.com/user 2>"$errfile"); then
  echo "::error::Could not query GitHub to validate ${TOKEN_SECRET_NAME} scopes:" >&2
  cat "$errfile" >&2
  exit 1
fi
HEADERS=$(<"$hdrfile")

if ! [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
  # A shared token that has spent its hourly quota gets a 403 here. Name that
  # cause: "could not validate scopes" sends the reader to the token's scopes.
  if grep -qiE '^x-ratelimit-remaining: 0[[:space:]]*$' <<<"$HEADERS"; then
    reset=$(grep -i '^x-ratelimit-reset:' <<<"$HEADERS" | sed 's/^[^:]*: //' | tr -d '\r')
    echo "::error::${TOKEN_SECRET_NAME} has used its whole GitHub API rate limit until $(date -u -d "@${reset}" '+%H:%M UTC'). Another workflow spends the same token's quota; give this one its own token or move its schedule." >&2
    exit 1
  fi
  echo "::error::Could not query GitHub to validate ${TOKEN_SECRET_NAME} scopes (HTTP status '${status}'):" >&2
  echo "$HEADERS" >&2
  exit 1
fi

if grep -qi '^x-oauth-scopes:' <<<"$HEADERS"; then
  SCOPES=$(echo "$HEADERS" | grep -i '^x-oauth-scopes:' | sed 's/^[^:]*: //' | tr -d '\r\n')
  scope_list="$(tr ',' '\n' <<<"$SCOPES" | sed 's/^ *//')"
  if grep -qx 'workflow' <<<"$scope_list"; then
    echo "Classic PAT has 'workflow' scope."
  else
    echo "::error::Classic ${TOKEN_SECRET_NAME} lacks the 'workflow' scope, which GitHub requires to push changes to .github/workflows/ files. Add the 'workflow' scope to your PAT at https://github.com/settings/tokens and update the ${TOKEN_SECRET_NAME} repository secret."
    exit 1
  fi
else
  echo "::warning::Token does not expose OAuth scopes (fine-grained PAT or GITHUB_TOKEN); skipping scope check. Manually verify that 'Workflows: Read and write' is granted on this token."
fi
