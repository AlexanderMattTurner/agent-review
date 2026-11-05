#!/usr/bin/env bash
# Run the pinned lychee release with the given arguments, downloading it once
# into a per-user cache. The pre-commit `lychee` hook calls this.
#
# The download uses the release URL on github.com and never api.github.com.
# lychee's own pre-commit installer resolves releases through api.github.com,
# and a sandboxed session whose proxy refuses that host could not push at all.
# The version and the per-platform tarball digests live in
# .github/tool-versions.sh; the digest there is the anchor.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pins="${here}/../tool-versions.sh"
[[ -f "$pins" ]] || {
  echo "lychee.sh: ${pins} does not exist, so no pinned version or digest can be read." >&2
  exit 1
}
# shellcheck source=/dev/null
source "$pins"

case "$(uname -s)-$(uname -m)" in
Linux-x86_64) target=x86_64-unknown-linux-gnu digest="${LYCHEE_SHA256_linux_amd64:-}" ;;
Linux-aarch64) target=aarch64-unknown-linux-gnu digest="${LYCHEE_SHA256_linux_arm64:-}" ;;
Darwin-x86_64) target=x86_64-apple-darwin digest="${LYCHEE_SHA256_darwin_amd64:-}" ;;
Darwin-arm64) target=aarch64-apple-darwin digest="${LYCHEE_SHA256_darwin_arm64:-}" ;;
*)
  echo "lychee.sh: no pinned lychee build for $(uname -s)-$(uname -m)." >&2
  exit 1
  ;;
esac
# An empty pin must never degrade into running an unverified binary.
[[ -n "${LYCHEE_VERSION:-}" && -n "$digest" ]] || {
  echo "lychee.sh: LYCHEE_VERSION or the ${target} digest is unset in .github/tool-versions.sh." >&2
  exit 1
}

cache="${XDG_CACHE_HOME:-${HOME}/.cache}/lychee-pinned/${LYCHEE_VERSION}-${digest}"
if [[ ! -x "${cache}/lychee" ]]; then
  workdir="$(mktemp -d)"
  trap 'rm -rf "$workdir"' EXIT
  tarball="lychee-${target}.tar.gz"
  curl -fsSL --retry 4 --retry-all-errors --connect-timeout 30 -o "${workdir}/${tarball}" \
    "https://github.com/lycheeverse/lychee/releases/download/lychee-v${LYCHEE_VERSION}/${tarball}"
  # This refusal is what keeps a swapped or corrupted release asset out of the
  # cache. macOS ships `shasum`, not `sha256sum`.
  if command -v sha256sum >/dev/null; then
    actual="$(sha256sum "${workdir}/${tarball}")"
  else
    actual="$(shasum -a 256 "${workdir}/${tarball}")"
  fi
  [[ "${actual%% *}" == "$digest" ]] || {
    echo "lychee.sh: ${tarball} has sha256 ${actual%% *}, but .github/tool-versions.sh pins ${digest}." >&2
    exit 1
  }
  tar xzf "${workdir}/${tarball}" -C "$workdir" "lychee-${target}/lychee"
  mkdir -p "$cache"
  # Rename into place, so a concurrent run never executes a half-written file.
  install -m 0755 "${workdir}/lychee-${target}/lychee" "${cache}/lychee.partial.$$"
  mv -f "${cache}/lychee.partial.$$" "${cache}/lychee"
  # `exec` below replaces this shell, so the EXIT trap would never fire.
  rm -rf "$workdir"
  trap - EXIT
fi
exec "${cache}/lychee" "$@"
