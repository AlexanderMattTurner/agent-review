"""Behavioral tests for .github/reviewer/build-review-evidence.py — the file that
shows Agent 5 the earlier fixes, the copies and the open PRs a fix PR's diff hides.

Each case builds a real repository with a PR branch, pushes it to a bare remote,
and runs the real script from a SHALLOW clone of that remote, which is the shape
of the prepare job's checkout. Git is never stubbed. `gh` is: the open-PR list
comes from the GitHub API, which a test cannot reach.
"""

# covers: .github/reviewer/build-review-evidence.py

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
PULLS = "repos/{owner}/{repo}/pulls"
OPEN_PRS = [
    (f"{PULLS}/7/files", [{"filename": "m.py"}]),
    (f"{PULLS}/8/files", [{"filename": "z.py"}]),
    (f"{PULLS}/9/files", [{"filename": "m.py"}]),
    (
        f"{PULLS}?",
        [
            {"number": 9, "created_at": "2026-01-01T00:00:00Z"},
            {"number": 8, "created_at": "2026-01-03T00:00:00Z"},
            {"number": 7, "created_at": "2026-01-02T00:00:00Z"},
        ],
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
            "tests/test_m.py": "def test_fetch():\n    assert True\n",
        },
        "feat: add fetch",
    )
    test_fix = "def test_fetch():\n    assert fetch\n"
    commit_files(work, {"tests/test_m.py": test_fix}, "fix(test): name fetch")
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
    assert "- #7 (opened 2026-01-02) also changes m.py" in text
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


def test_a_failed_open_pr_list_is_not_known_and_the_git_sections_still_ship(pr) -> None:
    """An API refusal costs section (c) only, and the file says so."""
    checkout, head, earlier = pr
    proc, out = _build(checkout, head, [])
    assert proc.returncode == 0, proc.stderr
    text = out.read_text("utf-8")
    assert f"- not known: gh api {PULLS}: gh: Not Found (HTTP 404)" in text
    assert f"{earlier} fix(fetch): retry once" in text
    assert "::warning::review evidence: (c) Open PRs" in proc.stdout


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


def test_a_test_file_change_names_no_fix(pr) -> None:
    """A test that `main` fixed before is not a product definition, so a PR that
    edits it again leaves section (a) empty."""
    checkout, _, _ = pr
    work = checkout.parent / "work"
    git_out(work, "checkout", "-q", "main")
    git_out(work, "checkout", "-qb", "tests-only")
    head = commit_files(
        work, {"tests/test_m.py": "def test_fetch():\n    assert 1\n"}, "fix(test): x"
    )
    git_out(work, "push", "-q", str(checkout.parent / "remote.git"), "tests-only")
    _, out = _build(checkout, head)
    text = out.read_text("utf-8")
    fixes = text.split("## (a)")[1].split("## (b)")[0]
    assert fixes.strip().endswith("- none found")


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
