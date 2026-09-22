#!/usr/bin/env python3
"""Keep only the named files' sections of a unified diff.

An accumulated-delta read covers the pushes after a commit an earlier review
already read, so its diff.txt must hold those files alone. The path set comes
from GitHub's own compare API, which is the authority on what those pushes
touched; this only slices the diff the reviewer would otherwise have read, so a
shard of it is still a slice of the sanitized bytes.

A path the diff does not contain is dropped silently: compare names files by
their state at each end, and a file renamed since then appears under another
name in this diff. The counts go to stderr so the caller can report them.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from _diff_sections import file_path_of, split_into_files  # noqa: E402


def narrow(diff_text: str, keep: set[str]) -> tuple[str, int, int]:
    """The diff holding only `keep`'s files, with (kept, total) section counts."""
    preamble, files = split_into_files(diff_text)
    kept = [section for section in files if file_path_of(section) in keep]
    out = "".join(preamble) + "".join("".join(section) for section in kept)
    return out, len(kept), len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", required=True, help="the unified diff to narrow")
    parser.add_argument(
        "--paths",
        required=True,
        help="a file holding one path to keep per line",
    )
    parser.add_argument("--out", required=True, help="where to write the narrowed diff")
    args = parser.parse_args()

    keep = {
        line.strip()
        for line in Path(args.paths).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    text, kept, total = narrow(
        Path(args.diff).read_text(encoding="utf-8", errors="replace"), keep
    )
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"narrowed the diff to {kept} of {total} file section(s)", file=sys.stderr)


if __name__ == "__main__":
    main()
