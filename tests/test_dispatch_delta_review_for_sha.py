"""The event path into the accumulated read.

`check_suite: completed` fires the moment a head's checks finish, which is the
same condition `dispatch-delta-review.sh` waits for, so this script is what turns
that event into the read the twice-hourly sweep would otherwise ask for up to
half an hour later.

What it decides is WHICH pull requests a commit's event covers. Every readiness
question stays in the dispatcher, so that is the only contract observed here.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "dispatch-delta-review-for-sha.sh"
# The dispatcher, at the path this script runs it from — relative to its own
# directory, because that is how it resolves it.
DISPATCHER_REL = "../reviewer/dispatch-delta-review.sh"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def associated(
    number: int,
    *,
    head: str = SHA,
    state: str = "open",
    draft: bool = False,
    bot: bool = False,
) -> dict:
    """One entry of `GET /repos/{repo}/commits/{sha}/pulls`."""
    return {
        "number": number,
        "state": state,
        "draft": draft,
        "user": {"type": "Bot" if bot else "User"},
        "head": {"sha": head},
    }


def run_dispatch(
    tmp_path: Path, pulls: list[dict], *, dispatcher_rc: int = 0, **env: str
) -> tuple[subprocess.CompletedProcess, list[str]]:
    """Run the real script against `pulls`; return the process and one
    `<PR> <SEVERITY_CONFIG basename>` line per dispatcher call, in call order.

    The dispatcher is a recording stand-in: it would spend real API calls and
    start a real review, and its own readiness contract has its own suite. `gh`
    is stubbed because the association read is a live API call, and it answers
    only `gh api`, so a script that grew a second kind of call comes back empty
    rather than silently satisfied.
    """
    sandbox = tmp_path / "scripts"
    sandbox.mkdir()
    shutil.copy(SCRIPT, sandbox / SCRIPT.name)
    log = tmp_path / "dispatcher.log"
    stub = (sandbox / DISPATCHER_REL).resolve()
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s %s\\n" "$PR" "$(basename "${{SEVERITY_CONFIG:-}}")" >> "{log}"\n'
        f"exit {dispatcher_rc}\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "pulls.json").write_text(json.dumps(pulls), encoding="utf-8")
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'[[ "$1" == "api" ]] && cat "{tmp_path}/pulls.json"\nexit 0\n',
        encoding="utf-8",
    )
    (bin_dir / "gh").chmod(0o755)

    res = subprocess.run(
        ["bash", str(sandbox / SCRIPT.name)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "GH_REPO": "owner/name",
            "SHA": SHA,
            "REVIEW_WORKFLOW": "claude-review.yaml",
            **env,
        },
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return res, calls


def test_a_pull_request_headed_by_the_commit_gets_the_read(tmp_path: Path) -> None:
    res, calls = run_dispatch(tmp_path, [associated(42)])
    assert res.returncode == 0, res.stderr
    assert calls == ["42 review-severities.json"]


def test_every_pull_request_on_one_head_gets_its_own_read(tmp_path: Path) -> None:
    """A commit can head more than one open pull request — a stack's lower layer
    and a branch pointed at the same tip. Each carries its own coverage and its
    own budget, so each is offered the read."""
    res, calls = run_dispatch(tmp_path, [associated(42), associated(43)])
    assert res.returncode == 0, res.stderr
    assert calls == ["42 review-severities.json", "43 review-severities.json"]


@pytest.mark.parametrize(
    "pull",
    [
        associated(42, head=OTHER_SHA),
        associated(42, state="closed"),
        associated(42, draft=True),
        associated(42, bot=True),
    ],
    ids=["a-head-that-moved-on", "closed", "draft", "bot-authored"],
)
def test_a_pull_request_the_sweep_would_skip_is_skipped_here(
    tmp_path: Path, pull: dict
) -> None:
    """The event path must select exactly what the sweep selects, or the two
    callers disagree about which pull requests the reviewer reads.

    The head test is the one this endpoint forces: GitHub keeps a commit
    associated with its pull request for that request's whole life, so a push
    leaves this event naming a head nobody is on. Reading it would spend the one
    accumulated read on code already superseded.
    """
    res, calls = run_dispatch(tmp_path, [pull])
    assert res.returncode == 0, res.stderr
    assert calls == []
    assert SHA[:7] in res.stderr


def test_a_commit_heading_nothing_open_asks_for_no_read(tmp_path: Path) -> None:
    res, calls = run_dispatch(tmp_path, [])
    assert res.returncode == 0, res.stderr
    assert calls == []


def test_one_failed_evaluation_reds_the_run_without_stopping_the_rest(
    tmp_path: Path,
) -> None:
    """The dispatcher exits 0 for every ordinary "nothing to do" branch, so a
    non-zero exit is a fault — an unreadable API, a missing library. Reporting it
    is the point, and it must not cost the other pull requests their read."""
    res, calls = run_dispatch(
        tmp_path, [associated(42), associated(43)], dispatcher_rc=1
    )
    assert res.returncode == 1, res.stdout
    assert calls == ["42 review-severities.json", "43 review-severities.json"]
