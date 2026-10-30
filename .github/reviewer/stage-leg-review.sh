#!/usr/bin/env bash
# Stage one read leg's review.json and cost.json under OUT_DIR/<leg>/ for upload.
#
# INVARIANT: the leg name rides INSIDE the artifact. download-artifact unpacks a
# lone pattern match flat but several into per-artifact directories, so the post
# job downloads with merge-multiple and finds each leg at <path>/<leg>/ only
# because this directory carries the name.
#
# Usage: stage-leg-review.sh <leg> <src-dir> <out-dir>
set -euo pipefail

leg="$1" src="$2" out="$3"
mkdir -p "${out}/${leg}"
cp "${src}/review.json" "${src}/cost.json" "${out}/${leg}/"
