# shellcheck shell=bash
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set shell options.
# Preflight and host-inventory helpers for the Docker sbx microVM backend (needs the sbx CLI plus
# KVM on Linux). Must stay bash 3.2-compatible: setup.bash sources this before it re-execs under
# bash 5, so macOS's stock /bin/bash parses every byte here.
#
# Two rules bind every probe here. A runtime that ANSWERS is not one that is RESPONSIVE: a lost
# Docker sign-in answers, yet every caller responds to a true verdict by proceeding into work the
# dead credential will fail. And a host this walk cannot diagnose is REFUSED, never waved through.
#
# Every preflight check fails loud with the action that unblocks it — there is no software
# fallback when virtualization is missing. The inventory helpers are the one place the
# sandbox naming shapes are recognized — the gb-<hex> one minted by sbx_session_base and
# sbx_sandbox_name (sbx/launch.bash), and the warm-spare pool's — so gc, panic and the
# purge can never drift on them.

[[ -n "${_GLOVEBOX_SBX_DETECT_SOURCED:-}" ]] && return 0
_GLOVEBOX_SBX_DETECT_SOURCED=1

# shellcheck source=/dev/null
source "${BASH_SOURCE[0]%/*}/../abspath.bash"
_SBX_DETECT_DIR="${BASH_SOURCE[0]%/*}"
gb_abs_dir_into _SBX_DETECT_DIR "$_SBX_DETECT_DIR"
# shellcheck source=/dev/null
source "$_SBX_DETECT_DIR/../msg.bash"

# eval fixture excerpt: bin/lib/sbx/detect.bash, the auth.bash re-export block
    fi
    printf 'joined'
    return 0
  fi
  printf 'failed'
}

# The Docker sign-in probes, the host-credential re-auth and the lost-sign-in classifier:
# sbx/auth.bash owns them, and this lib re-exports them by sourcing it so every consumer of
# detect.bash reaches them under their existing names.
