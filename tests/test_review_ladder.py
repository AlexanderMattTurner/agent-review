"""Behavioral tests for .github/reviewer/review-ladder.py — the loop body that
review.yaml's repeated attempt and judge steps share.

Contract:
  * The walk is the CONFIGURED rungs, in order. An unset rung is skipped, never
    the end of the ladder: a repository holding rungs 1, 5 and 8 walks all three.
  * A metered key is spent LAST, whichever slot holds it, and its attempt is
    armed with kind `metered`, which is what routes it to ANTHROPIC_API_KEY.
  * The free same-credential retry runs only when one credential is configured,
    and only on a proven zero-cost error.
  * An attempt that answered without the structured findings did not answer.
  * A run the step timeout killed is NOT proof the attempt was free, so it
    neither buys the free retry nor advances, and the gate reports the timeout.

The tests drive the real `plan` and `next` over the files GitHub Actions hands a
step (GITHUB_ENV, GITHUB_OUTPUT, the action's execution log). The action step
itself cannot run here, so `_walk` stands in for it by writing the log a real
attempt leaves at the action's fixed path.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, load_script

REVIEWER = REPO_ROOT / ".github" / "reviewer"
ACTION_LOG = "claude-execution-output.json"


def _module():
    """The ladder, loaded fresh. `sys.path` is restored: the module puts the
    reviewer directory on it at import, and an entry left behind makes every
    later test in this worker import `_ladder` from there."""
    saved = list(sys.path)
    try:
        return load_script(".github/reviewer/review-ladder.py")
    finally:
        sys.path[:] = saved


def _ladder_env(path: Path) -> dict[str, str]:
    """GITHUB_ENV as the runner applies it: the last write of a name wins."""
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        env[key] = value
    return env


def _walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kinds: dict[int, str],
    *,
    log_for,
    killed=(),
) -> tuple[list[tuple[int, str]], list[float], dict[str, str]]:
    """Run plan, then attempt-and-judge until the ladder stops, as review.yaml
    does. `log_for(slot)` is the execution log that attempt leaves; a slot in
    `killed` leaves none, and the step timeout is shown as already spent. Returns
    the (rung, kind) of every attempt, every backoff wait, and the final env."""
    module = _module()
    waits: list[float] = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    pr_input = tmp_path / "pr-input"
    pr_input.mkdir(exist_ok=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    monkeypatch.chdir(checkout)
    github_env = tmp_path / "github-env"
    github_env.touch()
    base = {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_ENV": str(github_env),
        "GITHUB_OUTPUT": str(tmp_path / "github-output"),
        "PR_INPUT_DIR": str(pr_input),
        "PROMPT_FILE": str(REVIEWER / "prompts" / "code-review.md"),
        "MODEL": "opus",
        "EXTRA_INSTRUCTIONS_FILE": os.environ.get("EXTRA_INSTRUCTIONS_FILE", ""),
    }
    for key, value in base.items():
        monkeypatch.setenv(key, value)
    for index in range(1, 9):
        monkeypatch.setenv(f"RUNG_{index}_KIND", kinds.get(index, ""))
    module.plan()
    attempts: list[tuple[int, str]] = []
    while (state := _ladder_env(github_env)).get("LADDER_GO") == "true":
        slot = int(state["LADDER_SLOT"])
        attempts.append((int(state["LADDER_RUNG"]), state["LADDER_KIND"]))
        if slot in killed:
            state["LADDER_STARTED"] = "0"
        else:
            (tmp_path / ACTION_LOG).write_text(
                json.dumps(log_for(slot)), encoding="utf-8"
            )
        for key, value in state.items():
            monkeypatch.setenv(key, value)
        module.next_slot()
    return attempts, waits, _ladder_env(github_env)


def _errored(*, cost: float):
    return [{"type": "result", "is_error": True, "total_cost_usd": cost}]


def _answered():
    return [
        {
            "type": "result",
            "is_error": False,
            "total_cost_usd": 1.5,
            "structured_output": {"summary": "fine", "findings": []},
        }
    ]


def test_an_unset_rung_is_skipped_not_the_end_of_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walk that stopped at the first gap would leave a repository holding rungs
    1, 5 and 8 spending only rung 1. The paid key also waits for the end."""
    attempts, waits, _ = _walk(
        tmp_path,
        monkeypatch,
        {1: "metered", 5: "oauth", 8: "oauth"},
        log_for=lambda _: _errored(cost=0),
    )
    assert attempts == [(5, "oauth"), (8, "oauth"), (1, "metered")]
    module = _module()
    assert waits == list(module.BACKOFF_SECONDS[:2])


@pytest.mark.parametrize(
    ("cost", "expected"),
    [(0, [(8, "metered"), (8, "metered")]), (0.42, [(8, "metered")])],
    ids=["free-error-retries", "paid-error-stops"],
)
def test_the_free_retry_re_spends_the_only_credential_only_when_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cost: float, expected: list
) -> None:
    """With one credential, a zero-cost error buys one retry on it, armed with the
    same kind. A failure that billed buys the same wall again at full price."""
    attempts, _, _ = _walk(
        tmp_path, monkeypatch, {8: "metered"}, log_for=lambda _: _errored(cost=cost)
    )
    assert attempts == expected


def test_an_answer_stops_the_ladder_and_hands_over_the_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts, _, env = _walk(
        tmp_path, monkeypatch, {1: "oauth", 8: "metered"}, log_for=lambda _: _answered()
    )
    assert attempts == [(1, "oauth")]
    findings = json.loads((tmp_path / "pr-input" / "findings.json").read_text())
    assert findings == {"summary": "fine", "findings": []}
    assert env["REVIEW_EXECUTION_FILE"] == str(tmp_path / "review-attempt-1.json")


def test_a_clean_result_without_the_findings_is_not_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """is_error false with no structured output posts nothing, so the next rung
    must still be allowed to try, and no findings.json may appear."""
    no_findings = [{"type": "result", "is_error": False, "total_cost_usd": 0.2}]
    logs = {1: no_findings, 2: _answered()}
    attempts, _, _ = _walk(
        tmp_path, monkeypatch, {1: "oauth", 2: "oauth"}, log_for=logs.__getitem__
    )
    assert attempts == [(1, "oauth"), (2, "oauth")]


def test_a_timed_out_attempt_stops_the_ladder_and_the_gate_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed attempt may already have billed a whole read, and a fresh
    credential faces the identical wall. The killed action writes no log, so the
    judge writes the record the gate reads, rather than leaving the gate to report
    an earlier rung's failure or none."""
    attempts, _, env = _walk(
        tmp_path,
        monkeypatch,
        {1: "oauth", 8: "metered"},
        log_for=lambda _: pytest.fail("a killed attempt leaves no log"),
        killed=(1,),
    )
    assert attempts == [(1, "oauth")]
    gate_out = tmp_path / "gate-out"
    gate = subprocess.run(
        [sys.executable, str(REVIEWER / "checks" / "claude-execution.py")],
        env={
            "PATH": os.environ["PATH"],
            "EXECUTION_FILE": env["REVIEW_EXECUTION_FILE"],
            "GITHUB_OUTPUT": str(gate_out),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert gate.returncode == 1
    assert "hit its wall clock after 1500s" in gate.stderr
    # A killed read may have billed, so the gate must not grant a free retry.
    assert gate_out.read_text(encoding="utf-8") == "execution_reached_model=true\n"


def test_no_credential_refuses_before_any_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(SystemExit, match="every rung_ secret is empty"):
        _walk(tmp_path, monkeypatch, {}, log_for=lambda _: _answered())


def test_a_missing_extra_instructions_file_stops_the_run_before_any_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent told to follow a file it cannot read reviews without the caller's
    conventions and still posts a verdict. The agent reads only the checkout, so
    a real file outside it is refused too."""
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    for refused in ["absent.md", str(outside), "../outside.md"]:
        monkeypatch.setenv("EXTRA_INSTRUCTIONS_FILE", refused)
        with pytest.raises(SystemExit, match="extra-instructions names no file"):
            _walk(tmp_path, monkeypatch, {8: "oauth"}, log_for=lambda _: _answered())


def test_the_prompt_and_flags_scope_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller's rules reach the agent only through the prompt, and the read is
    safe only while no shell or network tool is on the command line."""
    (tmp_path / "checkout").mkdir()
    (tmp_path / "checkout" / "house-rules.md").write_text("x", encoding="utf-8")
    monkeypatch.setenv("EXTRA_INSTRUCTIONS_FILE", "house-rules.md")
    _walk(tmp_path, monkeypatch, {8: "oauth"}, log_for=lambda _: _answered())
    output = (tmp_path / "github-output").read_text(encoding="utf-8")
    assert "Then follow the instructions in house-rules.md as well." in output
    assert "--json-schema '" in output
    assert '--disallowedTools "Bash,' in output
    assert "WebFetch" in output.split("--disallowedTools", 1)[1]


def _claude_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The `claude_args` output a planned read hands every attempt, as argv."""
    _walk(tmp_path, monkeypatch, {8: "oauth"}, log_for=lambda _: _answered())
    output = (tmp_path / "github-output").read_text(encoding="utf-8")
    _, delimited = output.split("claude_args<<", 1)
    delimiter, rest = delimited.split("\n", 1)
    return shlex.split(rest.split(f"\n{delimiter}\n", 1)[0])


def test_every_file_tool_is_scoped_to_the_checkout_input_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare `Read` matches /proc/self/environ, where the attempt's credential
    lives, so every file tool the read may use carries one of three paths."""
    argv = _claude_args(tmp_path, monkeypatch)
    granted = argv[argv.index("--allowedTools") + 1].split(",")
    file_tools = {"Read", "Grep", "Glob"}
    file_grants = [g for g in granted if g.split("(", 1)[0] in file_tools]
    prompt_dir = (REVIEWER / "prompts").resolve()
    scopes = ["./**", f"/{tmp_path / 'pr-input'}/**", f"/{prompt_dir}/**"]
    assert sorted(file_grants) == sorted(
        f"{tool}({scope})" for tool in file_tools for scope in scopes
    )
    assert [g for g in granted if g not in file_grants] == [
        "Task",
        "Agent",
        "TodoWrite",
    ]


def test_a_shard_leg_is_told_which_files_its_slice_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leg that reads its slice as the whole PR reports every file another leg
    covers as missing, so its prompt names its own files."""
    shards = tmp_path / "pr-input" / "shards"
    shards.mkdir(parents=True)
    (shards / "manifest.json").write_text(
        json.dumps(
            {
                "shards": [
                    {"name": "shard-00.diff", "files": ["a.py"]},
                    {"name": "shard-01.diff", "files": ["b.py", "c.py"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("READ_LEG", "shard-01.diff")
    _walk(tmp_path, monkeypatch, {8: "oauth"}, log_for=lambda _: _answered())
    output = (tmp_path / "github-output").read_text(encoding="utf-8")
    assert (
        "The diff file holds ONLY shard shard-01.diff, which is these whole files: "
        "b.py, c.py." in output
    )
    assert "2 readers each read one slice" in output
