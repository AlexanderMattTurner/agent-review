"""The sweep is the only path that re-derives reviewer state for a PR nothing
pushed to: GitHub fires no workflow event when a review thread is resolved, so an
author who resolves every finding and pushes nothing would sit behind a red
`Automated review posted` check until their next push.

What the sweep itself decides is which PRs it covers and what it hands each
per-PR script, so that is what these tests observe.
"""

import json
import shutil
import subprocess
from pathlib import Path

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "sweep-reviewer-holds.sh"
SIBLINGS = ("approve-if-reviewer-hold-clear.sh", "review-gate.sh")


def run_sweep(tmp_path: Path, prs: list[dict]) -> tuple[int, dict[str, list[str]]]:
    """Sweep `prs`; return the exit code and each per-PR script's `PR HEAD_SHA`
    lines, in call order.

    The sweep runs its siblings by path, so it runs here out of a sandbox holding
    recording stands-in for them. They stand in for scripts that would each spend
    real GitHub API calls; the sweep's own contract is which PRs reach them and
    with what, which is what the recordings carry. `gh` is stubbed for the same
    reason — the PR listing is a live API read — and answers only `gh pr list`,
    so a sweep that grew a second API call would come back empty rather than
    silently satisfied.
    """
    sandbox = tmp_path / "scripts"
    sandbox.mkdir()
    shutil.copy(SCRIPT, sandbox / SCRIPT.name)
    for name in SIBLINGS:
        (sandbox / name).write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s %s\\n" "$PR" "${{HEAD_SHA:-}}" >> "{tmp_path}/{name}.log"\n',
            encoding="utf-8",
        )
        (sandbox / name).chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "prs.json").write_text(json.dumps(prs), encoding="utf-8")
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'[[ "$1" == "pr" ]] && cat "{tmp_path}/prs.json"\nexit 0\n',
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
            "GH_REPO": "o/r",
            "GH_TOKEN": "t",
        },
    )
    calls = {}
    for name in SIBLINGS:
        log = tmp_path / f"{name}.log"
        calls[name] = (
            log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        )
    return res.returncode, calls


def pull_request(
    number: int, *, head: str = "sha1", draft: bool = False, bot: bool = False
) -> dict:
    return {
        "number": number,
        "isDraft": draft,
        "author": {"is_bot": bot},
        "headRefOid": head,
    }


def test_the_sweep_re_posts_the_gate_verdict_on_each_swept_head(tmp_path: Path) -> None:
    """Without this the gate never re-runs for a PR whose findings were resolved
    with no follow-up push, and the required check stays red on handled work."""
    code, calls = run_sweep(tmp_path, [pull_request(7, head="deadbeef")])
    assert code == 0
    assert calls["review-gate.sh"] == ["7 deadbeef"]


def test_the_sweep_still_clears_the_reviewer_hold(tmp_path: Path) -> None:
    """The gate re-post is added work, not a replacement: a stale hold blocks the
    merge whatever the gate says."""
    code, calls = run_sweep(tmp_path, [pull_request(7)])
    assert code == 0
    assert calls["approve-if-reviewer-hold-clear.sh"] == ["7 "]


def test_draft_and_bot_pull_requests_are_not_swept(tmp_path: Path) -> None:
    """The reviewer never reads them, so a verdict here would be about a review
    that is not coming."""
    code, calls = run_sweep(
        tmp_path,
        [
            pull_request(1, draft=True),
            pull_request(2, bot=True),
            pull_request(3, head="cafe"),
        ],
    )
    assert code == 0
    assert calls["review-gate.sh"] == ["3 cafe"]
    assert calls["approve-if-reviewer-hold-clear.sh"] == ["3 "]


def test_a_pr_with_no_readable_head_gets_no_verdict_and_reds_the_sweep(
    tmp_path: Path,
) -> None:
    """A status posted on the wrong sha is worse than one not posted: it satisfies
    the required context on a commit nobody reviewed. So the gate is skipped for
    that PR, the rest of the sweep still runs, and the run goes red."""
    code, calls = run_sweep(
        tmp_path, [pull_request(9, head=""), pull_request(10, head="beef")]
    )
    assert code != 0
    assert calls["review-gate.sh"] == ["10 beef"]
    assert calls["approve-if-reviewer-hold-clear.sh"] == ["9 ", "10 "]
