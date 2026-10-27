# shellcheck shell=bash
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
#
# The hidden HTML-comment markers the reviewer stamps on its reviews and threads,
# bound as bash variables from lib/shared-names.json.
#
# PROBLEM CLASS — a review marker spelled once in bash and again in JavaScript.
# post-pr-review.mjs writes the severity and delta-read markers, and the bash
# readers match them. Both sides read the one table, so a rename cannot leave a
# reader looking for a string no writer emits.
#
# The values are fixed data, so a missing file or key fails loudly here rather than
# binding an empty marker that every `contains` test would match.

_REVIEW_MARKERS_JSON="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/shared-names.json"

# _review_marker <key> — one value from `.review_markers`, or exit non-zero.
_review_marker() {
  jq -er --arg k "$1" '.review_markers[$k] | select(type == "string" and length > 0)' \
    "$_REVIEW_MARKERS_JSON"
}

# shellcheck disable=SC2034 # read by the sourcing scripts
{
  WHOLE_DIFF_READ_MARKER="$(_review_marker whole_diff_read)"
  OVERSIZED_REVIEW_MARKER="$(_review_marker oversized_review)"
  DEGRADED_REVIEW_MARKER="$(_review_marker degraded_review)"
  INCOMPLETE_READ_MARKER="$(_review_marker incomplete_read)"
  AUTO_APPROVAL_MARKER="$(_review_marker automated_approval)"
  COVERAGE_MARKER_PREFIX="$(_review_marker coverage_prefix)"
  DELTA_READ_FINDING_MARKER="$(_review_marker delta_read_finding)"
  SEVERITY_MARKER_FORMAT="$(_review_marker severity_marker)"
  HUMAN_HOLD_MARKER="$(_review_marker human_hold)"
}

# severity_marker <severity> — the marker line a finding of that severity carries.
severity_marker() {
  printf '%s\n' "${SEVERITY_MARKER_FORMAT//%s/$1}"
}
