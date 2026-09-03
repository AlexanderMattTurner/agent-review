"""`.github/reviewer/decide-escalation.py` decides whether a cheap read's claim
is re-read by the full-price model.

The load-bearing property is WHAT it escalates on. Escalating everything buys the
diff twice; escalating nothing leaves a merge held by a claim nobody confirmed.
Every case here pins one side of that line.
"""

# covers: .github/reviewer/decide-escalation.py

import json
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, load_script

mod = load_script(".github/reviewer/decide-escalation.py")


def _decide(
    tmp_path: Path,
    review: dict | str | None,
    shard_model: str = "low-1",
    model: str = "high-1",
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> bool:
    """Run the real main() and read the `escalate=` it wrote."""
    # Truncated per call: the script APPENDS, the way a step writes GITHUB_OUTPUT,
    # so a case that decides twice would otherwise read the first answer back.
    out = tmp_path / "out.txt"
    out.write_text("", encoding="utf-8")
    review_json = tmp_path / "review.json"
    if isinstance(review, str):
        review_json.write_text(review, encoding="utf-8")
    elif review is not None:
        review_json.write_text(json.dumps(review), encoding="utf-8")
    assert monkeypatch is not None
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REVIEW_JSON", str(review_json))
    monkeypatch.setenv("SHARD_MODEL", shard_model)
    monkeypatch.setenv("MODEL", model)
    mod.main()
    lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln]
    assert len(lines) == 1, lines
    return lines[0] == "escalate=true"


def test_a_blocking_finding_is_confirmed_by_the_full_price_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `blocking` finding holds the merge, which is the one claim worth paying
    the full price twice to be right about."""
    review = {
        "verdict": "needs_changes",
        "findings": [{"severity": "nit"}, {"severity": "blocking"}],
    }
    assert _decide(tmp_path, review, monkeypatch=monkeypatch)


def test_a_blocking_verdict_escalates_without_a_blocking_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = {"verdict": "blocking", "findings": []}
    assert _decide(tmp_path, review, monkeypatch=monkeypatch)


@pytest.mark.parametrize("severity", ["warning", "nit"])
def test_the_severities_that_do_not_hold_the_merge_stay_cheap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, severity: str
) -> None:
    """Warnings and nits are the bulk of what a review says. Escalating them buys
    the whole diff at full price twice, which is the tier undone."""
    review = {"verdict": "needs_changes", "findings": [{"severity": severity}]}
    assert not _decide(tmp_path, review, monkeypatch=monkeypatch)


def test_a_shard_that_already_read_at_the_full_price_model_never_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nothing to escalate TO, and a second read of the same shard by the
    same model is the whole diff bought twice for one verdict."""
    review = {"verdict": "blocking", "findings": [{"severity": "blocking"}]}
    assert not _decide(tmp_path, review, shard_model="high-1", monkeypatch=monkeypatch)
    # The caller that named no tier at all reaches the same answer.
    assert not _decide(tmp_path, review, shard_model="", monkeypatch=monkeypatch)


def test_an_unreadable_review_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheap read produced no verdict this can act on, and the synthesis
    refuses to post a review missing a shard. Fail toward the read that works."""
    assert _decide(tmp_path, None, monkeypatch=monkeypatch)
    assert _decide(tmp_path, "{ not json", monkeypatch=monkeypatch)


def _shard_steps() -> list[dict]:
    import yaml  # local: only this test reads the workflow

    doc = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "review.yaml").read_text(
            encoding="utf-8"
        )
    )
    return doc["jobs"]["review_shard"]["steps"]


def test_the_two_reads_cannot_share_an_execution_log() -> None:
    """The ladder names each attempt's log by RUNG alone, so a second read in the
    same job truncates the first's and both steps report ONE path. The recorder
    then prices the shard from that log twice and credits the wrong model — and
    every unit test passes, because they hand it two paths production never makes.
    The re-read must run under its own RUNNER_TEMP."""
    escalated = next(s for s in _shard_steps() if s.get("id") == "escalated_review")
    script = REPO_ROOT / ".github" / "reviewer" / "escalated-read.sh"
    assert script.name in str(escalated.get("run", "")), escalated.get("run")
    assert "export RUNNER_TEMP=" in script.read_text(encoding="utf-8")
    recorder = next(
        s for s in _shard_steps() if "record-shard-cost.mjs" in str(s.get("run", ""))
    )
    env = recorder["env"]
    assert env["EXECUTION_FILE"] != env["EXECUTION_FILE_ESCALATED"]


def test_the_escalated_transcript_is_published_like_the_first() -> None:
    """A paid read of the untrusted diff whose transcript nothing stages is a
    transcript nobody can audit."""
    stage = next(s for s in _shard_steps() if s.get("id") == "stage_logs")
    assert "escalated_review" in str(stage["env"]["LOGS_PATH_ESCALATED"])
    assert "LOGS_PATH_ESCALATED" in str(stage["run"])


def test_the_script_is_the_one_the_workflow_runs() -> None:
    """The step names this path; a rename that misses the workflow leaves the
    escalation silently unwired, and every shard reads cheap with a green job."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "review.yaml").read_text(
        encoding="utf-8"
    )
    assert "decide-escalation.py" in workflow
