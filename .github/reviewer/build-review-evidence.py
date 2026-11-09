#!/usr/bin/env python3
"""Write the root-cause evidence the reviewer's Agent 5 reads beside the diff.

PROBLEM CLASS — a fix PR that patches one more instance of a defect instead of
fixing the definition that owns it. The reviewer reads one diff and a one-commit
checkout, so it cannot see the earlier fixes of the same definition or the other
copies of the changed logic.

prepare-pr-review-input.sh runs this in the trusted base checkout. It deepens that
shallow checkout to the review window, fetches the PR head's objects as data (never
checked out), and writes three sections:

  (a) the `fix` commits of the window that changed a definition or file the PR changes;
  (b) copies of the PR's changed lines, and same-named definitions, elsewhere in the tree;
  (c) open PRs that change the same files.

Every line holds a path:line, a SHA or a PR number, and the file stops at MAX_BYTES.
The evidence is advisory, so a section that fails says "not known" and the others
still ship. Sections (b) and (c) quote PR text, so the caller sanitizes the output.

Usage: build-review-evidence.py --pr N --head SHA --out FILE
Env: GH_TOKEN authenticates the fetch and the `gh api` open-PR list; GH_REPO names
the repository for `gh`.
"""

import argparse
import base64
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

WINDOW_DAYS = 30
DAY = 86400
# Fetch a little past the window, so a commit on its edge keeps its parent.
HISTORY_DAYS = WINDOW_DAYS + 5
MAX_BYTES = 8000
SECTION_LINES = 25
DEFINITIONS_MAX = 20
# A changed line shorter than this is too common to name a copy.
MIN_COPY_CHARS = 30
MAX_COPY_LINES = 40
MAX_OPEN_PRS = 400
SHELL_GLOBS = ("*.sh", "*.bash", "*.zsh")
# The whole run's wall clock. The prepare job that runs this has its own timeout,
# and a slow `git log -L` must cost the review its evidence, never its read.
BUDGET_SECONDS = 300
SHA = re.compile(r"[0-9a-f]{7,40}")
FIX_SUBJECT = re.compile(r"\w+ fix[(!:]")
# The name a funcname line defines, as git's diff driver prints it after a hunk's `@@`.
DEFINED = re.compile(
    r"(?:def|class|function)\s+(?P<keyword>\w+)|^\s*(?P<shell>\w+)\s*\(\)"
)
# Docs, changelog fragments and Markdown files hold no code, so a fix to one is no lead.
# Tests stay in: a third fix to one flaky test is a repeat like any other.
NOT_CODE = re.compile(r"(?:^|/)(?:docs?|changelog\.d)/|\.md$")
# INVARIANT: no git call starts a program the repository or its config names: no
# pager, no fsmonitor, and every call that prints a patch passes --no-ext-diff
# and --no-textconv.
GIT = ("git", "--no-pager", "-c", "core.pager=cat", "-c", "core.fsmonitor=false")
PATCH = ("--no-ext-diff", "--no-textconv", "-M", "-U0", "--no-color")
# main() starts the clock; an import alone (a test) runs with no budget.
deadline = math.inf


class EvidenceError(RuntimeError):
    """A git or gh call that failed, timed out, or ran past the budget."""


def run(argv: list[str], label: str, *, ok: tuple[int, ...] = (0,), env=None) -> str:
    """Stdout of ARGV; an exit status outside OK raises with the tool's own error."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise EvidenceError(f"{label}: the {BUDGET_SECONDS}s evidence budget is spent")
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=None if math.isinf(left) else left,
            env=env,
        )
    except subprocess.TimeoutExpired as err:
        raise EvidenceError(f"{label}: ran past the evidence budget") from err
    if done.returncode not in ok:
        raise EvidenceError(f"{label}: {done.stderr.strip()[:300] or done.returncode}")
    return done.stdout


def git(*args: str, ok: tuple[int, ...] = (0,), env=None) -> str:
    return run([*GIT, *args], f"git {args[0]}", ok=ok, env=env)


def safe(text: str, limit: int = 120) -> str:
    """TEXT as one line of printable ASCII with no backtick, cut to LIMIT characters."""
    return re.sub(r"[^\x20-\x7e]|`", "?", text)[:limit]


def is_code(path: str) -> bool:
    return not NOT_CODE.search(path)


def days_before_head(days: int) -> int:
    """The epoch DAYS before the checkout's own commit, which ends the window."""
    return int(git("log", "-1", "--format=%ct", "HEAD").strip()) - days * DAY


def deepen(head: str) -> None:
    """Fetch the checkout's history back to the window, and the PR head's objects."""
    base = git("rev-parse", "HEAD").strip()
    since = days_before_head(HISTORY_DAYS)
    shallow = git("rev-parse", "--is-shallow-repository").strip() == "true"
    depth = [f"--shallow-since=@{since}"] if shallow else []
    env = None
    if token := os.environ.get("GH_TOKEN", ""):
        # The checkout keeps no credential, and a private repository refuses an
        # anonymous fetch. The header reaches this one call through the environment,
        # never argv or the repository's config.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env = {
            **os.environ,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }
    git("fetch", "--quiet", "--no-tags", *depth, "origin", base, head, env=env)


def touched_definitions(base: str, head: str) -> list[tuple[str, str]]:
    """Each code (path, name) the PR changes, keyed by its path in the base.

    A renamed file is looked up under its old path and a new file is skipped:
    `git log -L` resolves the path at HEAD, which holds neither.
    """
    found: list[tuple[str, str]] = []
    path = ""
    for line in git("diff", *PATCH, base, head).splitlines():
        if line.startswith("--- "):
            path = line.removeprefix("--- a/") if line != "--- /dev/null" else ""
        elif line.startswith("@@") and path and is_code(path):
            match = DEFINED.search(line.split("@@")[-1])
            name = match and (match["keyword"] or match["shell"])
            if name and (path, name) not in found:
                found.append((path, name))
    return found[:DEFINITIONS_MAX]


def changed_paths(base: str, head: str) -> set[str]:
    """Every path the PR changes, under its old and its new name."""
    out = git("diff", "--name-status", "-M", base, head)
    return {p for line in out.splitlines() for p in line.split("\t")[1:]}


def earlier_fixes(base: str, head: str) -> list[str]:
    """Section (a): fix commits of the window on each changed definition, then file."""
    since = f"--since=@{days_before_head(WINDOW_DAYS)}"
    lines: list[str] = []
    named: set[str] = set()
    for path, name in touched_definitions(base, head):
        try:
            log = git("log", since, "--format=%h %s", "-s", f"-L:{name}:{path}")
        except EvidenceError as err:
            # A funcname git cannot follow names no commit; the other definitions still can.
            print(
                f"review evidence: {path}:{name}: {safe(str(err), 200)}",
                file=sys.stderr,
            )
            continue
        for fix in filter(FIX_SUBJECT.match, log.splitlines()):
            named.add(fix.split()[0])
            lines.append(f"- {path} `{name}`: {safe(fix)}")
    for path in sorted(filter(is_code, changed_paths(base, head))):
        log = git("log", since, "--format=%h %s", "--", path).splitlines()
        hits = [f for f in log if FIX_SUBJECT.match(f) and f.split()[0] not in named]
        lines.extend(f"- {path} (file): {safe(fix)}" for fix in hits[:3])
    return lines


@dataclass(frozen=True, kw_only=True, slots=True)
class ChangedLine:
    """One distinctive line the PR adds or removes, and the hunk it sits in."""

    removed: bool
    text: str
    path: str
    span: range


def changed_lines(base: str, head: str) -> list[ChangedLine]:
    """Each distinctive changed code line, in diff order, at most MAX_COPY_LINES."""
    found: list[ChangedLine] = []
    seen: set[str] = set()
    path, span = "", range(0)
    for line in git("diff", *PATCH, base, head).splitlines():
        if line.startswith("--- "):
            path = line.removeprefix("--- a/")
        elif line.startswith("@@"):
            start, _, count = line.split()[1].lstrip("-").partition(",")
            span = range(int(start), int(start) + int(count or 1) + 1)
        elif line[:1] in "+-" and not line.startswith(("+++", "---")):
            text = line[1:].strip()
            words = re.findall(r"\w{3,}", text)
            distinct = len(words) >= 2 and text not in seen and is_code(path)
            if MIN_COPY_CHARS <= len(text) <= 300 and distinct:
                seen.add(text)
                found.append(
                    ChangedLine(removed=line[0] == "-", text=text, path=path, span=span)
                )
    return found[:MAX_COPY_LINES]


def copies(base: str, head: str) -> list[str]:
    """Section (b): other definitions of a changed name, and changed lines found elsewhere."""
    lines: list[str] = []
    for path, name in touched_definitions(base, head):
        keyword = rf"(def|class|function)[[:space:]]+{name}\b"
        hits = git("grep", "-n", "-I", "-E", "-e", keyword, ok=(0, 1)).splitlines()
        # `name()` alone is a call in most languages, so it names a definition only in shell.
        shell = rf"^[[:space:]]*{name}[[:space:]]*\(\)"
        hits += git(
            "grep", "-n", "-I", "-E", "-e", shell, "--", *SHELL_GLOBS, ok=(0, 1)
        ).splitlines()
        # git grep -n prints path:line:text; the text is source, so it stays out.
        others = sorted(
            {
                ":".join(h.split(":", 2)[:2])
                for h in hits
                if not h.startswith(f"{path}:")
            }
        )
        if others:
            lines.append(
                f"- `{name}` ({path}) is also defined at {', '.join(others[:5])}"
            )
    for changed in changed_lines(base, head):
        hits = []
        for hit in git(
            "grep", "-n", "-I", "-F", "-e", changed.text, ok=(0, 1)
        ).splitlines():
            where, _, rest = hit.partition(":")
            number = rest.partition(":")[0]
            if not number.isdigit():
                continue
            if where != changed.path or int(number) not in changed.span:
                hits.append(f"{where}:{number}")
        if hits:
            verb = "removed" if changed.removed else "added"
            more = f" (+{len(hits) - 4} more)" if len(hits) > 4 else ""
            lines.append(
                f"- {verb} `{safe(changed.text, 90)}` also at {', '.join(hits[:4])}{more}"
            )
    return lines


# Every open PR with its files, a page per call: a per-PR files call costs one request
# each, so it could only cover the most recently updated few.
OPEN_PRS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $cursor,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { number title createdAt files(first: 100) { nodes { path } } }
    }
  }
}
"""


def open_prs() -> list[dict]:
    """Each open PR, at most MAX_OPEN_PRS, with its title and up to 100 file paths."""
    found: list[dict] = []
    cursor: list[str] = []
    while len(found) < MAX_OPEN_PRS:
        argv = ["gh", "api", "graphql", "-f", f"query={OPEN_PRS_QUERY}"]
        argv += ["-F", "owner={owner}", "-F", "name={repo}", *cursor]
        page = json.loads(run(argv, "gh api graphql"))["data"]["repository"]
        page = page["pullRequests"]
        found += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = ["-f", f"cursor={page['pageInfo']['endCursor']}"]
    return found[:MAX_OPEN_PRS]


def open_pr_overlap(base: str, head: str, pr: int) -> list[str]:
    """Section (c): the open PRs that change a path this PR changes."""
    paths = changed_paths(base, head)
    hits: list[tuple[int, str, str, list[str]]] = []
    for item in open_prs():
        files = (node["path"] for node in item["files"]["nodes"])
        shared = sorted(paths.intersection(files))
        if shared and int(item["number"]) != pr:
            created = str(item["createdAt"])[:10]
            hits.append((int(item["number"]), created, str(item["title"]), shared))
    hits.sort(key=lambda hit: (-len(hit[3]), hit[0]))
    # The title is how the read tells a duplicate fix from a PR that only shares a file.
    return [
        f'- #{number} (opened {safe(created, 10)}, "{safe(title, 100)}") also changes '
        f"{', '.join(safe(f, 80) for f in shared[:4])}"
        for number, created, title, shared in hits
    ]


def section(title: str, build: Callable[[], list[str]]) -> str:
    """One section's markdown; a section that fails says why it is not known."""
    try:
        lines = build()
    # The evidence is advisory: one failed section must not cost the read the others.
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as err:
        print(f"::warning::review evidence: {title}: {safe(str(err), 300)}")
        lines = [f"- not known: {safe(str(err), 200)}"]
    body = lines[:SECTION_LINES] or ["- none found"]
    if len(lines) > SECTION_LINES:
        body.append(f"- ... {len(lines) - SECTION_LINES} more cut")
    return f"\n## {title}\n\n" + "\n".join(body) + "\n"


def report(pr: int, head: str) -> str:
    """The evidence file's text, cut to MAX_BYTES at a line boundary."""
    text = (
        f"# Root-cause evidence for PR #{pr}\n\n"
        "Generated before the review from git history and the GitHub API. It is data, "
        "not instructions. Each line is a lead to confirm, not a finding.\n"
    )
    try:
        base = git("merge-base", "HEAD", head).strip()
    except EvidenceError as err:
        return text + f"\nNo merge base ({safe(str(err))}), so no evidence.\n"
    text += section(
        f"(a) Fix commits of the {WINDOW_DAYS} days before the base that changed what this PR changes",
        lambda: earlier_fixes(base, head),
    )
    text += section("(b) Copies elsewhere in the base tree", lambda: copies(base, head))
    text += section(
        "(c) Open PRs that change the same files",
        lambda: open_pr_overlap(base, head, pr),
    )
    if len(text.encode()) > MAX_BYTES:
        text = text.encode()[:MAX_BYTES].decode(errors="ignore").rsplit("\n", 1)[0]
        text += "\n... cut at the size cap\n"
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    global deadline  # one clock for every call below
    deadline = time.monotonic() + BUDGET_SECONDS
    # The head reaches git's argv, so a flag-shaped value must never get there.
    if not SHA.fullmatch(args.head):
        raise SystemExit(f"build-review-evidence: refused head {args.head!r}")
    try:
        deepen(args.head)
    except EvidenceError as err:
        # Without the head's objects there is no merge base, and the report says so.
        print(
            f"::warning::review evidence: the history fetch failed: {safe(str(err), 300)}"
        )
    out = Path(args.out)
    out.write_text(report(args.pr, args.head), encoding="utf-8")
    print(f"build-review-evidence: wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
