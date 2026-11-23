"""Behavioral tests for .github/reviewer/build-review-evidence.py — the file that
shows Agent 5 the earlier fixes, the copies and the open PRs a fix PR's diff hides.

Each case builds a real repository with a PR branch, pushes it to a bare remote,
and runs the real script from a SHALLOW clone of that remote, which is the shape
of the prepare job's checkout. Git is never stubbed. `gh` is: the open-PR list
comes from the GitHub API, which a test cannot reach.
"""

# covers: .github/reviewer/build-review-evidence.py

import json
import os
import time
from pathlib import Path

import pytest

from tests._fake_gh import write_gh_stub
from tests._helpers import (
    REPO_ROOT,
    commit_files,
    git_out,
    init_test_repo,
    load_script,
    run_capture,
)

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "build-review-evidence.py"
COPY = "x = 'the runtime download failed; retry it after the mirror recovers'"
# A doc line long enough to name a copy, in two non-code files and one code file.
DOC = "retry the runtime download once the mirror recovers"


def _open_pr(number: int, created: str, files: list[str], title: str = "") -> dict:
    """One `pullRequests` node of the open-PR GraphQL page."""
    paths = {"nodes": [{"path": path} for path in files]}
    return {"number": number, "title": title, "createdAt": created, "files": paths}


# PR 9 is the PR under review, so it never names itself; PR 8 shares no file.
OPEN_PRS = [
    (
        "graphql",
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            _open_pr(9, "2026-01-01T00:00:00Z", ["m.py"]),
                            _open_pr(8, "2026-01-03T00:00:00Z", ["z.py"]),
                            _open_pr(
                                7, "2026-01-02T00:00:00Z", ["m.py"], "fix: retry `x`"
                            ),
                        ],
                    }
                }
            }
        },
    ),
]


def _module(body: str) -> str:
    return f"def fetch():\n    a = 0\n    {body}\n\n\ndef other():\n    return 0\n"


@pytest.fixture(name="pr")
def _pr(tmp_path: Path) -> tuple[Path, str, str]:
    """(shallow checkout, PR head, earlier fix). `main` fixed `fetch` once with a line
    copy.sh repeats; PR head fixes `fetch` again and removes that line. Only the
    remote holds the head, as only GitHub holds a PR head before the fetch."""
    work = tmp_path / "work"
    init_test_repo(work)
    commit_files(
        work,
        {
            ".gitattributes": "*.py diff=python\n",
            "m.py": _module("return 1"),
            "copy.sh": f"{COPY}\n",
            "lib.py": "def fetch():\n    return `x`\n",
            "run.py": "def go():\n    fetch()\n",
            "util.sh": "fetch() {\n  :\n}\n",
            "unit.conf": "RuntimeDirectory=gate\nself.runtime_dir\nOther=value-x\n",
            "tests/units.txt": "RuntimeDirectory=gate\n",
            "tests/test_m.py": "def test_fetch():\n    assert True\n",
        },
        "feat: add fetch",
    )
    test_fix = "def test_fetch():\n    assert fetch\n"
    commit_files(work, {"tests/test_m.py": test_fix}, "fix(test): name fetch")
    docs = {
        "docs/conf.py": f"{DOC}\n",
        "NOTES.md": f"{DOC}\n",
        "copy.sh": f"{COPY}\n{DOC}\n",
    }
    commit_files(work, docs, "fix(docs): say retry")
    earlier = commit_files(work, {"m.py": _module(COPY)}, "fix(fetch): retry once")
    git_out(work, "checkout", "-qb", "pr")
    head = commit_files(work, {"m.py": _module("return 2")}, "fix(fetch): retry twice")
    remote = tmp_path / "remote.git"
    git_out(tmp_path, "clone", "-q", "--bare", str(work), str(remote))
    checkout = tmp_path / "checkout"
    git_out(
        tmp_path,
        "clone",
        "-q",
        "--depth=1",
        "-b",
        "main",
        f"file://{remote}",
        str(checkout),
    )
    return checkout, head, earlier[:7]


def _build(checkout: Path, head: str, routes=OPEN_PRS):
    """Run the real script in CHECKOUT with a `gh` that answers ROUTES."""
    bindir = checkout.parent / "bin"
    write_gh_stub(bindir, routes)
    out = checkout.parent / "evidence.md"
    proc = run_capture(
        [str(SCRIPT), "--pr", "9", f"--head={head}", "--out", str(out)],
        cwd=checkout,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    return proc, out


def test_the_evidence_names_the_earlier_fix_the_copy_and_the_open_pr(pr) -> None:
    """An auditor's three facts, read from a one-commit checkout the script deepens:
    the prior fix by SHA, the copy by path:line, the overlapping PR by number."""
    checkout, head, earlier = pr
    marker = checkout.parent / "external-diff-ran"
    helper = checkout.parent / "ext.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n", "utf-8")
    helper.chmod(0o755)
    git_out(checkout, "config", "diff.external", str(helper))
    proc, out = _build(checkout, head)
    assert proc.returncode == 0, proc.stderr
    text = out.read_text("utf-8")
    assert f"- m.py `fetch`: {earlier} fix(fetch): retry once" in text
    assert f"- removed `{COPY}` also at copy.sh:1" in text
    assert '- #7 (opened 2026-01-02, "fix: retry ?x?") also changes m.py' in text
    assert "#8" not in text and "#9 " not in text
    assert not marker.exists()
    assert _build(checkout, head)[1].read_text("utf-8") == text


def test_another_definition_is_named_by_path_and_line_and_a_call_is_not(pr) -> None:
    """lib.py and util.sh define `fetch`; run.py only calls it, on a line of its own."""
    checkout, head, _ = pr
    proc, out = _build(checkout, head)
    assert proc.returncode == 0, proc.stderr
    assert "- `fetch` (m.py) is also defined at lib.py:1, util.sh:1\n" in out.read_text(
        "utf-8"
    )


def test_a_definition_and_a_rare_token_the_pr_adds_are_named(pr) -> None:
    """A new file's `fetch` has three owners in the base; its unit key has one
    production holder, since a test file is no holder. A test file's copy, an
    attribute name, a Python call and a minified line are no leads."""
    checkout, _, _ = pr
    added = "def fetch():\n    self.runtime_dir = 'RuntimeDirectory=gate'\n"
    files = {
        "pkg/new.py": added,
        "pkg/call.py": "fetch()\n",
        "pkg/min.py": f"z = '{'a' * 300} Other=value-x'\n",
        "tests/test_new.py": "K = 'Other=value-x'\n",
    }
    head = _pr_on(
        checkout,
        "adds",
        {**files, "tests/test_unit.py": "U = 'RuntimeDirectory=gate'\n"},
    )
    _, out = _build(checkout, head)
    text = out.read_text("utf-8")
    assert (
        "- `fetch` (pkg/new.py) is also defined at lib.py:1, m.py:1, util.sh:1\n"
        in text
    )
    assert (
        "- added `RuntimeDirectory=gate` (pkg/new.py) is already in unit.conf\n" in text
    )
    assert "self.runtime_dir" not in text and "Other=value-x" not in text
    assert "(pkg/call.py)" not in text


def test_a_job_the_pr_writes_twice_is_named_with_both_sites(pr) -> None:
    """A name the PR defines in two files, and a production line it adds in two
    places, are copies inside the diff. Adjacent lines and test lines are not."""
    checkout, _, _ = pr
    loop = "    for store in (objects, *alternates):\n"
    pair = "x = 'one long line of setup text'\n" * 2
    files = {
        "pkg/a.py": f"def helper_one():\n{loop}",
        "pkg/b.sh": "helper_one() {\n  :\n}\n",
        "pkg/c.py": f"def other_two():\n{loop}{pair}",
        "tests/test_d.py": pair + pair,
    }
    _, out = _build(checkout, _pr_on(checkout, "twice", files))
    text = out.read_text("utf-8")
    assert (
        "- `helper_one` is defined 2 times in this PR: pkg/a.py:1, pkg/b.sh:1\n" in text
    )
    assert (
        "- added `for store in (objects, *alternates):` at 2 sites in this PR: "
        "pkg/a.py:2, pkg/c.py:2\n"
    ) in text
    assert "one long line" not in text


def test_a_long_section_cannot_cut_a_short_one() -> None:
    """The open-PR line survives two sections that each fill the budget alone."""
    module = load_script(".github/reviewer/build-review-evidence.py")
    long = [f"- lead {i} " + "x" * 80 for i in range(25)]
    text = module.render([("(a)", long), ("(b)", long), ("(c)", ["- #7 also"])], 1200)
    assert "- #7 also\n" in text
    assert text.count("more cut") == 2 and len(text.encode()) <= 1200


def test_a_short_section_leaves_its_bytes_to_a_long_one() -> None:
    """Section (a) keeps more than a third of the budget when (b) and (c) are short."""
    module = load_script(".github/reviewer/build-review-evidence.py")
    long = [f"- lead {i} " + "x" * 80 for i in range(25)]
    text = module.render([("(a)", long), ("(b)", ["- one"]), ("(c)", ["- two"])], 1200)
    assert text.count("- lead ") * 90 > 1200 // 3 * 2


def test_a_failed_open_pr_list_is_not_known_and_the_git_sections_still_ship(pr) -> None:
    """An API refusal costs section (c) only, and the file says so."""
    checkout, head, earlier = pr
    proc, out = _build(checkout, head, [])
    assert proc.returncode == 0, proc.stderr
    text = out.read_text("utf-8")
    assert "- not known: gh api graphql: gh: Not Found (HTTP 404)" in text
    assert f"{earlier} fix(fetch): retry once" in text
    assert "::warning::review evidence: (c) Open PRs" in proc.stdout


def test_a_pr_too_large_to_list_costs_only_its_own_line(pr) -> None:
    """GitHub answers `files: null` for a PR too large to list; the others still show."""
    checkout, head, _ = pr
    nodes = OPEN_PRS[0][1]["data"]["repository"]["pullRequests"]["nodes"]
    huge = {**_open_pr(6, "2026-01-04T00:00:00Z", []), "files": None}
    page = {"pageInfo": {"hasNextPage": False, "endCursor": None}}
    page["nodes"] = [huge, *nodes]
    routes = [("graphql", {"data": {"repository": {"pullRequests": page}}})]
    proc, out = _build(checkout, head, routes)
    assert proc.returncode == 0, proc.stderr
    text = out.read_text("utf-8")
    assert '- #7 (opened 2026-01-02, "fix: retry ?x?") also changes m.py' in text
    assert "#6" not in text


def test_a_head_the_remote_lacks_writes_a_file_that_says_so(pr) -> None:
    """A failed fetch leaves no merge base; the run still ends 0 with a file."""
    checkout, _, _ = pr
    proc, out = _build(checkout, "ab" * 20)
    assert proc.returncode == 0, proc.stderr
    assert "the history fetch failed" in proc.stdout
    assert "No merge base (git merge-base: " in out.read_text("utf-8")


@pytest.mark.parametrize("head", ["--output=/tmp/x", "-x", "a..b", "HEAD"])
def test_a_head_that_is_not_a_sha_never_reaches_git(pr, head: str) -> None:
    checkout, _, _ = pr
    proc, out = _build(checkout, head)
    assert proc.returncode != 0
    assert "refused head" in proc.stderr
    assert not out.exists()


def _pr_on(checkout: Path, branch: str, files: dict[str, str]) -> str:
    """Push a one-commit PR BRANCH off main that writes FILES; return its head."""
    work = checkout.parent / "work"
    git_out(work, "checkout", "-q", "main")
    git_out(work, "checkout", "-qb", branch)
    head = commit_files(work, files, "fix: again")
    git_out(work, "push", "-q", str(checkout.parent / "remote.git"), branch)
    return head


def _fixes(text: str) -> str:
    return text.split("## (a)")[1].split("## (b)")[0]


def test_a_test_that_main_fixed_before_is_named_again(pr) -> None:
    """A second fix to one flaky test is a repeat like any other, so section (a)
    names the earlier `fix(test)` commit on that test, and (b) a copied test line."""
    checkout, _, _ = pr
    work = checkout.parent / "work"
    earlier = git_out(work, "log", "-1", "--format=%h", "--grep=name fetch", "main")
    test = f"def test_fetch():\n    {COPY}\n"
    head = _pr_on(checkout, "tests-only", {"tests/test_m.py": test})
    _, out = _build(checkout, head)
    text = out.read_text("utf-8")
    fixed = f"- tests/test_m.py `test_fetch`: {earlier.strip()} fix(test): name fetch"
    assert fixed in _fixes(text)
    assert f"- added `{COPY}` also at copy.sh:1, m.py:3" in text


@pytest.mark.parametrize("doc", ["docs/conf.py", "NOTES.md"])
def test_a_doc_that_main_fixed_before_names_no_fix_and_no_copy(pr, doc: str) -> None:
    """A doc holds no code, so its earlier `fix` and its copied line are no leads."""
    checkout, _, _ = pr
    head = _pr_on(checkout, "docs-only", {doc: "\n"})
    _, out = _build(checkout, head)
    text = out.read_text("utf-8")
    assert _fixes(text).strip().endswith("- none found")
    assert text.split("## (b)")[1].split("## (c)")[0].strip().endswith("- none found")


def test_the_report_stops_at_the_size_cap(pr, monkeypatch) -> None:
    """A long report is cut at a line boundary and says so."""
    checkout, head, _ = pr
    _build(checkout, head)
    module = load_script(".github/reviewer/build-review-evidence.py")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(module, "MAX_BYTES", 200)
    text = module.report(9, head)
    assert text.endswith("... cut at the size cap\n")
    assert len(text.encode()) < 260


def test_a_spent_budget_writes_a_file_that_says_so(pr, monkeypatch) -> None:
    """The wall-clock budget stops every later call and names itself as the reason."""
    checkout, head, _ = pr
    _build(checkout, head)
    module = load_script(".github/reviewer/build-review-evidence.py")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(module, "deadline", time.monotonic() - 1)
    assert "evidence budget is spent" in module.report(9, head)


def test_the_open_pr_list_follows_every_page(monkeypatch) -> None:
    """A repository with more open PRs than one page holds still has them all read."""
    module = load_script(".github/reviewer/build-review-evidence.py")
    pages = iter([("c1", True, 1), (None, False, 2)])
    calls: list[list[str]] = []

    def fake_run(argv: list[str], _label: str, **_kw) -> str:
        calls.append(argv)
        cursor, more, number = next(pages)
        info = {"hasNextPage": more, "endCursor": cursor}
        node = _open_pr(number, "2026-01-01T00:00:00Z", ["m.py"])
        body = {"pageInfo": info, "nodes": [node]}
        return json.dumps({"data": {"repository": {"pullRequests": body}}})

    monkeypatch.setattr(module, "run", fake_run)
    assert [pr["number"] for pr in module.open_prs()] == [1, 2]
    assert calls[1][-2:] == ["-f", "cursor=c1"]
    assert len(calls[0]) == len(calls[1]) - 2
