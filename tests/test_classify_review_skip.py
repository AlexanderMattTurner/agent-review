"""Behavioral tests for .github/scripts/classify-review-skip.sh — the head half of
the caller's skip class.

A skipped pull request is APPROVED without a read, so the class must rest only on
what the pull request cannot choose. The payload half (a same-repo, non-draft,
bot-opened PR) is an expression in the workflow and is pinned in
test_decide_pr_review_trigger.py. This script adds the half the payload cannot
answer: the event was fired by a bot, and every commit on the pull request is
bot-authored and signed by GitHub.

The real script runs against a fake `gh` on PATH that applies the script's OWN
`--jq` to canned commit objects, so the decision logic is exercised rather than
re-implemented.
"""

# covers: .github/scripts/classify-review-skip.sh

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "classify-review-skip.sh"

# A `gh` that runs the caller's --jq over the canned `pulls/N/commits` page, and
# fails when the canned file says to.
FAKE_GH = """#!/usr/bin/env bash
[[ -s "$GH_FAIL" ]] && exit 1
while [[ $# -gt 0 ]]; do
  [[ "$1" == --jq ]] && { jq -r "$2" "$GH_COMMITS"; exit; }
  shift
done
exit 2
"""


def commit(type_: str | None = "Bot", *, verified: bool = True, committer="web-flow"):
    """One `pulls/N/commits` entry. `type_=None` is an author email GitHub maps to
    no account; `committer` is the GitHub login the committer email maps to."""
    return {
        "author": None if type_ is None else {"type": type_},
        "committer": None if committer is None else {"login": committer},
        "commit": {"verification": {"verified": verified}},
    }


def _run(
    tmp_path: Path,
    *,
    commits: list[dict],
    payload_skip: str = "true",
    sender_type: str = "Bot",
    fail: bool = False,
) -> tuple[str, str]:
    """Run the real script and return (skip, decision line)."""
    gh = tmp_path / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "commits.json").write_text(json.dumps(commits), encoding="utf-8")
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
            "SENDER_TYPE": sender_type,
            "GH_COMMITS": str(tmp_path / "commits.json"),
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
    skip, _ = _run(tmp_path, commits=[commit(), commit()])
    assert skip == "true"


def test_a_human_commit_on_a_bot_branch_forces_a_real_read(tmp_path: Path) -> None:
    """The defect the head half closes. `user.type` names the account that OPENED
    the PR and never changes, while a same-repo bot branch is pushable by any
    collaborator — and the caller re-runs this on `synchronize`. Reading the
    opener alone, a human diff pushed onto a dependabot branch takes the approval
    its opener bought, with nobody reading it."""
    skip, decision = _run(tmp_path, commits=[commit(), commit("User")])
    assert skip == "false"
    assert "not a bot" in decision, decision


def test_a_commit_with_no_github_account_forces_a_real_read(tmp_path: Path) -> None:
    """A commit whose author GitHub maps to no account answers `unmapped`. Nothing
    proves a bot wrote it, so it is not in the class."""
    skip, _ = _run(tmp_path, commits=[commit(), commit(None)])
    assert skip == "false"


@pytest.mark.parametrize(
    ("spoof", "why"),
    [
        (commit(verified=False, committer="a-human"), "an unsigned local commit"),
        (commit(committer="a-human"), "a commit the human signed with their own key"),
        (commit(verified=False), "a web-flow committer with no valid signature"),
    ],
)
def test_a_bot_author_set_by_email_alone_forces_a_real_read(
    tmp_path: Path, spoof: dict, why: str
) -> None:
    """GitHub maps a commit's author from its author EMAIL, and `git commit
    --author` sets that to any bot's address. So `author.type == "Bot"` alone is
    a field the pusher chooses. A signature GitHub made itself is not."""
    skip, decision = _run(tmp_path, commits=[commit(), spoof])
    assert skip == "false", why
    assert "real read" in decision, decision


def test_an_event_a_human_fired_forces_a_real_read(tmp_path: Path) -> None:
    """On `synchronize` the sender is the account that pushed. A human push onto a
    bot branch is out of the class whatever the commits claim."""
    skip, decision = _run(tmp_path, commits=[commit()], sender_type="User")
    assert skip == "false"
    assert "fired by a User account" in decision, decision


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"fail": True}, "an unreadable commit list"),
        ({"commits": []}, "a PR that reported no commits"),
    ],
)
def test_the_script_fails_closed(tmp_path: Path, kwargs: dict, why: str) -> None:
    """Fail CLOSED: every uncertainty buys a real review, never an unread
    approval. The opposite default turns one API blip into a rubber stamp."""
    skip, _ = _run(tmp_path, **{"commits": [commit()], **kwargs})
    assert skip == "false", why


def test_the_payload_half_still_gates_the_whole_class(tmp_path: Path) -> None:
    """A PR the payload already excludes — a fork head, a draft, a human opener —
    is not in the class whatever its commits say, and the script spends no API
    read on it."""
    skip, decision = _run(tmp_path, commits=[commit()], payload_skip="false")
    assert skip == "false"
    assert "event payload" in decision, decision
