"""Behavioral tests for .github/scripts/classify-review-skip.sh — the head half of
the caller's skip class.

A skipped pull request is APPROVED without a read, so the class must rest only on
what the pull request cannot choose. The payload half (a same-repo, non-draft,
bot-opened PR) is an expression in the workflow and is pinned in
test_decide_pr_review_trigger.py. This script adds the half the payload cannot
answer: every commit on the pull request is bot-authored.

The real script runs against a fake `gh` on PATH, so the decision logic is
exercised rather than re-implemented.
"""

# covers: .github/scripts/classify-review-skip.sh

import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "classify-review-skip.sh"

# A `gh` that prints one line per canned commit-author type, and fails when the
# canned file says to — the shape `--jq '.[] | .author.type // "unmapped"'` yields.
FAKE_GH = """#!/usr/bin/env bash
[[ -s "$GH_FAIL" ]] && exit 1
cat "$GH_AUTHORS"
"""


def _run(
    tmp_path: Path,
    *,
    authors: list[str],
    payload_skip: str = "true",
    fail: bool = False,
) -> tuple[str, str]:
    """Run the real script and return (skip, decision line)."""
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "authors").write_text("\n".join(authors) + ("\n" if authors else ""))
    (tmp_path / "fail").write_text("x" if fail else "")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{tmp_path}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(out),
            "GH_TOKEN": "fake",
            "GH_REPO": "owner/repo",
            "PR": "42",
            "PAYLOAD_SKIP": payload_skip,
            "GH_AUTHORS": str(tmp_path / "authors"),
            "GH_FAIL": str(tmp_path / "fail"),
        },
    )
    assert proc.returncode == 0, proc.stderr
    lines = out.read_text(encoding="utf-8").splitlines()
    skip = [ln.split("=", 1)[1] for ln in lines if ln.startswith("skip=")][0]
    decision = [ln for ln in proc.stderr.splitlines() if ln.startswith("decision:")][0]
    return skip, decision


def test_an_all_bot_pr_is_skipped(tmp_path: Path) -> None:
    """The class this job exists for: a Dependabot PR whose commits are all its
    own. It gets the stand-in approval instead of a paid whole-diff read."""
    skip, _ = _run(tmp_path, authors=["Bot", "Bot"])
    assert skip == "true"


def test_a_human_commit_on_a_bot_branch_forces_a_real_read(tmp_path: Path) -> None:
    """The defect the head half closes. `user.type` names the account that OPENED
    the PR and never changes, while a same-repo bot branch is pushable by any
    collaborator — and the caller re-runs this on `synchronize`. Reading the
    opener alone, a human diff pushed onto a dependabot branch takes the approval
    its opener bought, with nobody reading it."""
    skip, decision = _run(tmp_path, authors=["Bot", "User"])
    assert skip == "false"
    assert "not a bot" in decision, decision


def test_a_commit_with_no_github_account_forces_a_real_read(tmp_path: Path) -> None:
    """A commit whose author GitHub maps to no account answers `unmapped`. Nothing
    proves a bot wrote it, so it is not in the class."""
    skip, _ = _run(tmp_path, authors=["Bot", "unmapped"])
    assert skip == "false"


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"fail": True}, "an unreadable commit list"),
        ({"authors": []}, "a PR that reported no commits"),
    ],
)
def test_the_script_fails_closed(tmp_path: Path, kwargs: dict, why: str) -> None:
    """Fail CLOSED: every uncertainty buys a real review, never an unread
    approval. The opposite default turns one API blip into a rubber stamp."""
    skip, _ = _run(tmp_path, **{"authors": ["Bot"], **kwargs})
    assert skip == "false", why


def test_the_payload_half_still_gates_the_whole_class(tmp_path: Path) -> None:
    """A PR the payload already excludes — a fork head, a draft, a human opener —
    is not in the class whatever its commits say, and the script spends no API
    read on it."""
    skip, decision = _run(tmp_path, authors=["Bot"], payload_skip="false")
    assert skip == "false"
    assert "event payload" in decision, decision
