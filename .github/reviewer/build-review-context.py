#!/usr/bin/env python3
"""Collect the trusted tree's OTHER mentions of the identifiers a diff changes.

PROBLEM CLASS — a fix applied to the site that failed rather than to the class.
The reviewer reads the diff and the diff alone, so a change to one caller of a
helper looks complete: the sibling caller with the same defect is in the base
tree, which nothing puts in front of the model. Observed shape: a probe whose
non-zero status one caller learned to report while the caller beside it kept
reading every status as "nothing there".

This runs in the BASE checkout, so every line it emits is trusted repository
content, not PR-authored text. It is a PRE-FILTER: it lists where each name is
used and judges nothing. The caps keep a common name (and a very wide diff) from
spending the model's context on noise.

Env/flags: --diff (the sanitized diff), --out (context.txt), --repo-dir (the
checkout to search, default `.`).
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from _diff_sections import file_path_of, split_into_files  # noqa: E402

# An identifier worth searching for: a name specific enough that its other
# mentions are about the same thing. A short lowercase word (`path`, `name`) is
# not — it matches every file in the tree and buys the reader nothing.
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MIN_LENGTH = 8

# How many identifiers are searched, most interesting first.
MAX_IDENTIFIERS = 40
# Past this many hits a name is too common to be about one thing, so it is
# dropped rather than truncated: a truncated list reads as the whole set.
MAX_HITS_PER_IDENTIFIER = 12
# The whole file's budget. A diff touching hundreds of names must not push the
# diff itself out of the model's context.
MAX_TOTAL_LINES = 1500
# A hit longer than this is truncated: one minified line can be the whole budget.
MAX_HIT_CHARS = 300
# The search is an optimization, so a tree too large to walk in this many seconds
# yields a stated gap instead of a red review job.
SEARCH_TIMEOUT_SECONDS = 120


def changed_paths(diff_text: str) -> set[str]:
    """The diff's file paths, which the search excludes: the whole question is
    where ELSE the tree mentions these names."""
    _, files = split_into_files(diff_text)
    return {file_path_of(section) for section in files}


def identifiers_of(diff_text: str) -> list[str]:
    """The identifiers to search, most interesting first.

    A name on BOTH an added and a removed line is a contract the diff CHANGED,
    which is where a sibling caller matters most, so it ranks above a name the
    diff only adds or only removes. Frequency in the diff breaks the tie.
    """
    _, files = split_into_files(diff_text)
    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    for section in files:
        for line in section:
            if line.startswith(("+++", "---")):
                continue
            if line.startswith("+"):
                bucket = added
            elif line.startswith("-"):
                bucket = removed
            else:
                continue
            for name in IDENTIFIER.findall(line[1:]):
                if len(name) < MIN_LENGTH and "_" not in name:
                    continue
                bucket[name] = bucket.get(name, 0) + 1
    names = set(added) | set(removed)
    ranked = sorted(
        names,
        key=lambda name: (
            0 if (name in added and name in removed) else 1,
            -(added.get(name, 0) + removed.get(name, 0)),
            name,
        ),
    )
    return ranked[:MAX_IDENTIFIERS]


def search(repo_dir: Path, names: list[str], exclude: set[str]) -> tuple[str, bool]:
    """Every tracked line outside `exclude` that mentions one of `names`.

    ONE `git grep` for the whole set: a per-name walk of a large tree costs a
    process and a full traversal each. `--fixed-strings --word-regexp` because
    these are names, not patterns — a name holding a regex metacharacter would
    otherwise search for something else. Returns (output, complete).
    """
    if not names:
        return "", True
    command = ["git", "-C", str(repo_dir), "grep", "-n", "-I", "-F", "-w"]
    for name in names:
        command += ["-e", name]
    command += ["--", ".", *(f":(exclude){path}" for path in sorted(exclude))]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "", False
    # 1 is `git grep`'s "no lines matched", which is an answer, not a fault.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"git grep failed ({proc.returncode}): {proc.stderr}")
    return proc.stdout, True


def group_hits(output: str, names: list[str]) -> dict[str, list[str]]:
    """Each name's hits, dropping a name with more than the per-name cap.

    One grep answered every name, so a line is attributed to each queried name it
    contains — a line mentioning two of them is about both.
    """
    hits: dict[str, list[str]] = {name: [] for name in names}
    dropped: set[str] = set()
    for line in output.splitlines():
        for name in names:
            if name in dropped or name not in line:
                continue
            bucket = hits[name]
            if len(bucket) >= MAX_HITS_PER_IDENTIFIER:
                dropped.add(name)
                continue
            bucket.append(line[:MAX_HIT_CHARS])
    return {
        name: bucket for name, bucket in hits.items() if bucket and name not in dropped
    }


def render(hits: dict[str, list[str]], tree: str, complete: bool) -> str:
    """context.txt: one section per name, inside the whole-file line budget."""
    out = [
        f"# Other mentions of this diff's identifiers in the base tree ({tree}).",
        "# Trusted repository content: these lines are NOT from the pull request.",
        "# The diff's own files are excluded, so each hit is a site the diff did",
        "# not change. A name mentioned nowhere else, or in more places than are",
        "# worth listing, has no section here.",
    ]
    if not complete:
        out.append(
            "# INCOMPLETE: the search timed out, so this file lists nothing. Read the"
        )
        out.append("# base tree yourself for the identifiers the diff changes.")
    budget = MAX_TOTAL_LINES - len(out)
    for name, lines in hits.items():
        if budget <= len(lines) + 2:
            out.append(f"# Budget reached; {len(hits)} name(s) in total were searched.")
            break
        out.append("")
        out.append(f"## {name}")
        out.extend(lines)
        budget -= len(lines) + 2
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo-dir", default=".")
    args = parser.parse_args()

    repo_dir = Path(args.repo_dir)
    diff_text = Path(args.diff).read_text(encoding="utf-8", errors="replace")
    paths = changed_paths(diff_text)
    names = identifiers_of(diff_text)
    output, complete = search(repo_dir, names, paths)
    hits = group_hits(output, names)
    tree = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    Path(args.out).write_text(
        render(hits, tree or "unknown", complete), encoding="utf-8"
    )
    print(
        f"context: {len(hits)} of {len(names)} identifier(s) have other mentions"
        + ("" if complete else " (search timed out)"),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
