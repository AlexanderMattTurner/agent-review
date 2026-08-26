"""Behavioral tests for .github/reviewer/run-review-ladder.py — the loop that
spends the credential ladder on one review.

Contract:
  * The walk is the CONFIGURED rungs, in order. An unset rung is skipped, never
    the end of the ladder: a repository holding rungs 1 and 5 walks both.
  * Rung 2 alone may re-spend rung 1's credential, and only on a proven
    zero-cost error. That retry authenticates the way rung 1 does — rung 1 is
    metered, so its key goes to ANTHROPIC_API_KEY and not to the OAuth variable.
  * A run the wall clock killed is NOT proof the attempt was free, so it neither
    buys the free retry nor advances to a fresh credential.
  * Each attempt starts with no review.json, so a rung that errors after writing
    a partial verdict cannot leave it for a later rung to publish.

The tests drive the real module with `attempt` replaced, because the thing under
test is the WALK: which rungs run, with which credential, through which variable.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

LADDER = REPO_ROOT / ".github" / "reviewer" / "run-review-ladder.py"


def _module():
    """The ladder, loaded fresh.

    `sys.path` is restored: the module resolves its siblings at import, and an
    entry left behind makes every later test in this worker import `_ladder` and
    friends from the reviewer directory instead of their own package.
    """
    spec = importlib.util.spec_from_file_location("run_review_ladder", LADDER)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(LADDER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(LADDER.parent))
    return module


def _walk(tmp_path: Path, tokens: dict[int, str], *, log_for, timed_out=()):
    """Run the ladder with `attempt` recorded rather than run. `log_for(index)`
    is the execution log that rung leaves behind; `timed_out` names the rungs the
    wall clock killed. Returns (attempts, module), where each attempt is
    (rung index, credential, metered)."""
    env = {
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        "PR_INPUT_DIR": str(tmp_path / "pr-input"),
        "PROMPT_FILE": "prompt.md",
        "MODEL": "claude-opus-5",
    }
    (tmp_path / "pr-input").mkdir(exist_ok=True)
    for index, value in tokens.items():
        env[f"RUNG_{index}_TOKEN"] = value
    module = _module()
    attempts: list[tuple[int, str, bool]] = []

    def fake_attempt(index, token, metered, log, timeout):  # noqa: ARG001
        attempts.append((index, token, metered))
        Path(log).write_text(json.dumps(log_for(index)), encoding="utf-8")
        return index in timed_out

    module.attempt = fake_attempt
    # `module.time` IS the stdlib module, so assigning `module.time.sleep`
    # patches every consumer in this worker for the rest of the session — a
    # later test that waits on a real timeout then never waits at all.
    real_sleep = module.time.sleep
    module.time.sleep = lambda *_: None
    old = dict(os.environ)
    os.environ.update(env)
    try:
        module.main()
    finally:
        module.time.sleep = real_sleep
        os.environ.clear()
        os.environ.update(old)
    return attempts, module


def _errored(*, cost: float):
    return [{"type": "result", "is_error": True, "total_cost_usd": cost}]


def _clean():
    return [{"type": "result", "is_error": False, "total_cost_usd": 1.5}]


def test_an_unset_rung_is_skipped_not_the_end_of_the_ladder(tmp_path: Path) -> None:
    """The contract every consumer reads: an empty rung is skipped. A walk that
    stopped at the first gap would leave a repository holding rungs 1 and 5
    spending only rung 1, with its other credential never attempted."""
    attempts, _ = _walk(
        tmp_path,
        {1: "one", 5: "five"},
        log_for=lambda _: _errored(cost=0),
    )
    assert [index for index, _, _ in attempts] == [1, 2, 5]


def test_the_free_retry_re_spends_rung_ones_own_credential(tmp_path: Path) -> None:
    """Rung 2's slot with no secret of its own is the free same-credential retry.
    It must carry rung 1's METERED wiring too: rung 1's API key handed to the
    OAuth variable authenticates as nothing, which turns the advertised retry
    into a second failure."""
    attempts, _ = _walk(tmp_path, {1: "one"}, log_for=lambda _: _errored(cost=0))
    assert attempts == [(1, "one", True), (2, "one", True)]


def test_a_paid_failure_buys_no_free_retry(tmp_path: Path) -> None:
    """The retry is free only because nothing was billed. A rung that tried and
    failed on the work itself carries a cost, and re-spending it buys the same
    wall at full price."""
    attempts, _ = _walk(tmp_path, {1: "one"}, log_for=lambda _: _errored(cost=0.42))
    assert [index for index, _, _ in attempts] == [1]


def test_a_wall_clock_kill_does_not_advance(tmp_path: Path) -> None:
    """A killed attempt may already have billed a whole read, and a fresh
    credential faces the identical wall. Reading its truncated log as proof of a
    zero-cost failure would buy a second paid read and no new information."""
    attempts, _ = _walk(
        tmp_path,
        {1: "one", 2: "two"},
        log_for=lambda _: [],
        timed_out=(1,),
    )
    assert [index for index, _, _ in attempts] == [1]


def test_a_clean_run_stops_the_walk(tmp_path: Path) -> None:
    attempts, _ = _walk(tmp_path, {1: "one", 2: "two"}, log_for=lambda _: _clean())
    assert [index for index, _, _ in attempts] == [1]


def test_each_attempt_starts_with_no_review(tmp_path: Path) -> None:
    """A rung that errors after writing a partial verdict must not leave it for a
    later rung to publish as its own. Driven through the REAL attempt(), with the
    CLI replaced, because the removal is that function's job."""
    module = _module()
    review = tmp_path / "review.json"
    review.write_text('{"stale": true}', encoding="utf-8")
    seen: list[bool] = []

    def fake_run(*_args, **_kwargs):
        seen.append(review.exists())
        raise module.subprocess.TimeoutExpired(cmd="claude", timeout=1)

    # `module.subprocess` IS the stdlib module, so this assignment replaces
    # `subprocess.run` for every consumer in this worker. Restored below: left
    # standing, every later test that runs a real command raises this
    # TimeoutExpired instead.
    real_run = module.subprocess.run
    module.subprocess.run = fake_run
    old = dict(os.environ)
    os.environ.update({"PR_INPUT_DIR": str(tmp_path), "PROMPT_FILE": "p.md", "MODEL": "m"})
    try:
        module.attempt(1, "token", True, tmp_path / "log.json", 1)
    finally:
        module.subprocess.run = real_run
        os.environ.clear()
        os.environ.update(old)
    assert seen == [False], "the previous attempt's review.json outlived it"


@pytest.mark.parametrize("grant", ["Write(", "Edit("])
def test_the_agent_may_create_its_output_file(grant: str) -> None:
    """review.json does not exist when the model starts, so an Edit-only grant
    leaves the agent unable to produce the verdict the posting step reads."""
    assert grant in _module().TOOL_GRANT


def test_the_agent_can_read_its_own_instructions() -> None:
    """The default prompt lives in the reviewer's own checkout, outside the
    workspace `Read(./**)` covers. Without this grant the agent is told to follow
    a file it cannot open."""
    assert "Read(/{r}/**)" in _module().TOOL_GRANT
