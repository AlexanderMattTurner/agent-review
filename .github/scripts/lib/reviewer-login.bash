# shellcheck shell=bash
# One source of truth for "is this review / thread / comment the automated
# reviewer's?" — the identity predicate four scripts key their safety on.
#
# PROBLEM CLASS — two API dialects spell the same bot two ways. REST returns an
# app bot's login WITH the `[bot]` suffix (`github-actions[bot]`); GraphQL
# returns it WITHOUT (`github-actions`). A script that compares the configured
# REVIEWER_LOGIN verbatim matches nothing in one of the two dialects, and a
# reviewer filter that matches nothing does not fail loudly — it silently answers
# "no reviewer thread", "no live hold", or "any actor will do". Both directions
# already shipped as live bugs here: the hold-clear script never posted its
# clearing approval, and the thread fetcher always reported zero threads.
#
# The fix each script grew independently was the same three lines — default the
# login, strip a trailing `[bot]`, and compare against a login the jq filter also
# strips. Three copies of a security predicate is three chances for one of them to
# drift; this file is the fourth caller's reason to exist as a library instead.
#
# Usage:
#   source "$SCRIPT_DIR/lib/reviewer-login.bash"
#   reviewer_login_init
#   … --jq "[.data.…nodes[] | ${REVIEWER_MATCH_THREAD_ROOT}] | length"
#
# reviewer_login_init exports REVIEWER_LOGIN_BARE because the select clauses read
# it as `env.REVIEWER_LOGIN_BARE`: jq sees the environment, not the shell's
# unexported variables.

[[ -n "${_REVIEWER_LOGIN_SOURCED:-}" ]] && return 0
_REVIEWER_LOGIN_SOURCED=1

# login_bare_jq <jq-path> — a jq expression reducing the login at <jq-path> to
# the one spelling both dialects agree on: the REST `[bot]` suffix is stripped,
# and case is folded because a GitHub login is unique case-insensitively. Every
# login comparison in these scripts is built from this, so no call site can
# normalize one side of a comparison and not the other.
#
# `// ""` covers a null login (a deleted account, an unresolved thread's
# `resolvedBy`, or a GraphQL node the token cannot see): it becomes the empty
# string, which never equals a real login, so an unattributable review is never
# credited to the reviewer. It does NOT make an unattributable RESOLUTION safe:
# "" does not equal the author either, so a `!=` comparison against the author
# PASSES for a null `resolvedBy`. The caller's separate `.resolvedBy != null`
# conjunct is the check that blocks that one; neither is redundant.
login_bare_jq() {
  local login_path="${1:?login_bare_jq: a jq path to the login is required}"
  printf '%s' "(((${login_path}) // \"\") | ascii_downcase | sub(\"\\\\[bot\\\\]\$\"; \"\"))"
}

# reviewer_login_select <jq-path> — a jq `select(…)` that keeps only the elements
# whose login at <jq-path> is the reviewer's, in either dialect's spelling.
reviewer_login_select() {
  local login_path="${1:?reviewer_login_select: a jq path to the login is required}"
  printf '%s' "select($(login_bare_jq "$login_path") == $(login_bare_jq env.REVIEWER_LOGIN_BARE))"
}

# reviewer_login_init — set and export REVIEWER_LOGIN / REVIEWER_LOGIN_BARE from
# the caller's environment (default: the GITHUB_TOKEN identity every reviewer
# script posts under), then define the three select clauses in use.
reviewer_login_init() {
  REVIEWER_LOGIN="${REVIEWER_LOGIN:-github-actions[bot]}"
  REVIEWER_LOGIN_BARE="${REVIEWER_LOGIN%'[bot]'}"
  export REVIEWER_LOGIN REVIEWER_LOGIN_BARE

  # GraphQL review / comment node.
  REVIEWER_MATCH_AUTHOR="$(reviewer_login_select .author.login)"
  # GraphQL review THREAD: the root comment's author owns the thread.
  REVIEWER_MATCH_THREAD_ROOT="$(reviewer_login_select .comments.nodes[0].author.login)"
  # REST review object.
  REVIEWER_MATCH_USER="$(reviewer_login_select .user.login)"
  export REVIEWER_MATCH_AUTHOR REVIEWER_MATCH_THREAD_ROOT REVIEWER_MATCH_USER
}
