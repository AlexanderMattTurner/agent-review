#!/usr/bin/env python3
"""Split a unified diff into per-file shards small enough for one model read.

The PR reviewer reads the whole diff in one context, so above roughly 12,000
lines (~13.6 tokens/line, measured) it no longer fits and the review is skipped
entirely.

Shards never split a file. A reviewer handed half a hunk cannot tell an unsafe
change from a truncation, so a file bigger than the shard budget gets its own
oversize shard instead: still a complete file, just a larger read. The caller
sees `oversize` in the manifest and can decide.

Each shard also carries the MODEL that reads it. A caller that names no
--model-low leaves every shard on --model, which is the default and the whole
behaviour until it opts in. A caller that names one buys the cheaper read for
the shards it declares low-risk by path, and for every shard of a diff past
--bulk-lines, where the read is worth having but not at the top model's price.
A shard is low only when EVERY file in it qualifies, so a doc change bundled
with source stays on --model.

Emits into --out-dir: `shard-NN.diff` per shard plus `manifest.json` carrying the
per-shard line counts, file lists and model, and the totals a review's coverage
line is built from. With GITHUB_OUTPUT set, also emits `shard_count` and `shards` (a JSON
array of shard basenames, for a workflow matrix).

Exit codes: 0 on success, OVER_BUDGET_EXIT when the diff needs more than
--max-shards shards (the caller falls back to a human-review notice), 1 on an
input that is not a diff at all.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from _diff_sections import FILE_HEADER, file_path_of, split_into_files  # noqa: E402

# Distinct from 1 (a broken/absent input) so the caller can tell "this diff is too
# big even to shard — post the human-review notice" from "the sharder itself
# failed", which must stay a red job rather than a quiet notice.
OVER_BUDGET_EXIT = 3


def pack(files: list[list[str]], max_lines: int) -> list[list[list[str]]]:
    """Greedily group whole file sections into shards of at most max_lines.

    Greedy, not optimal bin-packing: shard count is what matters and greedy is
    within one shard of optimal here, while preserving the diff's own file order
    so a reviewer reads related files together.
    """
    shards: list[list[list[str]]] = []
    current: list[list[str]] = []
    current_lines = 0
    for section in files:
        n = len(section)
        if current and current_lines + n > max_lines:
            shards.append(current)
            current, current_lines = [], 0
        current.append(section)
        current_lines += n
    if current:
        shards.append(current)
    return shards


def model_for(
    paths: list[str], models: tuple[str, str], low_paths: str, bulk: bool
) -> str:
    """The model that reads a shard holding `paths`.

    INVARIANT — the low model is reachable only when the caller named one, so a
    caller that opts out of tiering reads every shard at the price it pays today.
    A path the pattern does not match keeps the high model; a pattern that does
    not compile raises here, which reds the job rather than downgrading a read.
    """
    high, low = models
    if not low:
        return high
    if bulk:
        return low
    # A shard is low only when every file in it qualifies: a shard packs whole
    # files, so one source file beside a doc change would otherwise take the
    # cheaper read with it.
    if low_paths and paths and all(re.search(low_paths, p) for p in paths):
        return low
    return high


def main() -> None:
    """Split the diff at --diff into shards under --out-dir, with a manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff", required=True, type=Path, help="the unified diff to split"
    )
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="where to write shards"
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=8000,
        help="shard budget in lines (default 8000, ~109k tokens)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="the model each shard is read with unless it qualifies for --model-low",
    )
    parser.add_argument(
        "--model-low",
        default="",
        help="the cheaper model for a low-risk shard; empty (default) keeps every shard on --model",
    )
    parser.add_argument(
        "--low-tier-paths",
        default="",
        help="regex a file path must match to qualify for --model-low; a shard qualifies only when every file in it does",
    )
    parser.add_argument(
        "--bulk-lines",
        type=int,
        default=0,
        help="whole-diff line count past which every shard takes --model-low; 0 (default) never does",
    )
    parser.add_argument(
        "--max-shards",
        type=int,
        default=0,
        help="refuse (exit 3) above this many shards; 0 (default) means no bound",
    )
    args = parser.parse_args()

    diff_text = args.diff.read_text(encoding="utf-8", errors="replace")
    preamble, files = split_into_files(diff_text)
    if not files:
        raise SystemExit(
            f"{args.diff}: no `{FILE_HEADER}` sections — not a unified diff"
        )

    shards = pack(files, args.max_lines)
    # Refuse BEFORE writing anything, so an over-budget run leaves no half-written
    # shard set a later step could mistake for a complete one.
    if args.max_shards and len(shards) > args.max_shards:
        print(
            f"{args.diff}: needs {len(shards)} shards, over --max-shards={args.max_shards}",
            file=sys.stderr,
        )
        raise SystemExit(OVER_BUDGET_EXIT)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # Read once, so a diff whose size decides the tier decides it for every shard.
    bulk = bool(args.bulk_lines) and diff_text.count("\n") > args.bulk_lines
    manifest = []
    for index, shard in enumerate(shards):
        name = f"shard-{index:02d}.diff"
        body = "".join("".join(section) for section in shard)
        # The preamble carries `gh pr diff` context that precedes the first file;
        # it belongs with the first shard so nothing is dropped from the reunion.
        if index == 0 and preamble:
            body = "".join(preamble) + body
        (args.out_dir / name).write_text(body, encoding="utf-8")
        line_count = body.count("\n")
        paths = [file_path_of(section) for section in shard]
        manifest.append(
            {
                "name": name,
                "lines": line_count,
                "files": paths,
                "model": model_for(
                    paths,
                    (args.model, args.model_low),
                    args.low_tier_paths,
                    bulk,
                ),
                # One file that alone exceeds the budget: kept whole on purpose.
                "oversize": line_count > args.max_lines,
            }
        )

    (args.out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "shards": manifest,
                "total_lines": diff_text.count("\n"),
                "total_files": len(files),
                "max_lines": args.max_lines,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if github_output := os.environ.get("GITHUB_OUTPUT"):
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"shard_count={len(manifest)}\n")
            handle.write(f"shards={json.dumps([s['name'] for s in manifest])}\n")

    oversize = sum(1 for s in manifest if s["oversize"])
    print(
        f"split {diff_text.count(chr(10))} lines across {len(files)} files "
        f"into {len(manifest)} shards (max {args.max_lines} lines"
        + (f", {oversize} oversize" if oversize else "")
        + ")"
    )


if __name__ == "__main__":
    main()
