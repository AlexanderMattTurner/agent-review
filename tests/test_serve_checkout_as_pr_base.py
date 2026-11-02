"""serve-checkout-as-pr-base.sh — after it runs, claude-code-action's config
restore (`git fetch origin <base>` then `git checkout origin/<base> -- <paths>`)
copies the trusted checkout onto itself, even when the base branch no longer
exists on GitHub. Real git throughout. A global `insteadOf` sends every GitHub
URL the script does not redirect to an empty local repository, so a fetch that
escaped finds no branch instead of reaching the network.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "serve-checkout-as-pr-base.sh"
REPO = "example-owner/example-repo"
DELETED_BASE = "claude/stacked-parent"


def git(cwd: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, capture_output=True, text=True
    ).stdout


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shallow default-branch checkout shaped like actions/checkout's, whose
    `origin` is the GitHub URL and whose `.claude/x` holds the trusted text."""
    github = tmp_path / "github"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(github / f"{REPO}.git")], check=True
    )
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[url "file://{github}/"]\n\tinsteadOf = https://github.com/\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    upstream = tmp_path / "upstream"
    (upstream / ".claude").mkdir(parents=True)
    (upstream / ".claude" / "x").write_text("trusted\n", encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main", str(upstream))
    git(upstream, "add", "-A")
    git(upstream, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")

    ws = tmp_path / "ws"
    git(tmp_path, "init", "-q", str(ws))
    git(
        ws,
        "fetch",
        "-q",
        "--depth=1",
        f"file://{upstream}",
        "+main:refs/remotes/origin/main",
    )
    git(ws, "checkout", "-q", "-B", "main", "origin/main")
    git(ws, "remote", "add", "origin", f"https://github.com/{REPO}")
    return ws


def run_script(
    ws: Path, repository: str = REPO, payload: dict[str, object] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the script with an event file shaped like a pull_request_target one,
    or like `payload` when given."""
    event = ws.parent / "event.json"
    if payload is None:
        payload = {"pull_request": {"base": {"ref": DELETED_BASE}}}
    event.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ws,
        env={
            **os.environ,
            "GITHUB_EVENT_PATH": str(event),
            "RUNNER_TEMP": str(ws.parent),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": repository,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_actions_restore_copies_the_checkout_onto_itself(checkout: Path) -> None:
    assert run_script(checkout).returncode == 0
    # What the action does: re-point origin at its own URL, then restore.
    git(checkout, "remote", "set-url", "origin", f"https://github.com/{REPO}.git")
    (checkout / ".claude" / "x").write_text("from the PR\n", encoding="utf-8")
    git(
        checkout,
        "fetch",
        "origin",
        DELETED_BASE,
        "--depth=1",
        "--no-recurse-submodules",
    )
    git(checkout, "checkout", f"origin/{DELETED_BASE}", "--", ".claude")

    assert (checkout / ".claude" / "x").read_text(encoding="utf-8") == "trusted\n"


def test_refuses_when_the_rule_does_not_cover_origin(checkout: Path) -> None:
    result = run_script(checkout, repository="other-owner/other-repo")

    assert result.returncode == 1
    assert f"origin serves '' for {DELETED_BASE}, not HEAD" in result.stderr


def test_a_payload_without_a_pr_base_changes_nothing(checkout: Path) -> None:
    result = run_script(checkout, payload={"inputs": {"pr-number": "7"}})

    assert result.returncode == 0
    assert (
        git(checkout, "config", "--local", "--get-regexp", "^url\\.", check=False) == ""
    )
