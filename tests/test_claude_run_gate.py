"""The execution-log gate as a choke point, on both paths that run a model.

A green model step is not proof Claude ran (auth failure, crash before the
result event, corrupt log all exit 0), so every invocation must be gated.

Two paths run a model in this repository, and each has ONE gate:
  * `.github/actions/claude-run` — the composite action. The gate lives inside
    it rather than being re-typed at each call site, where it was previously
    missed by 5 of 7 callers.
  * `.github/workflows/review.yaml` — the reusable PR reviewer. It calls
    claude-code-action directly, once per credential attempt, so it cannot
    inherit the composite's gate; it runs `checks/claude-execution.py` instead.

These tests enumerate every call site on both paths, so a future one that opts
out (or a refactor that drops the gate step) reds without being named here.
"""

import yaml

from tests._helpers import REPO_ROOT

ACTION_DIR = REPO_ROOT / ".github" / "actions" / "claude-run"
ACTION = ACTION_DIR / "action.yaml"
GATE_SCRIPT_REL = "check-claude-execution.sh"
# The ${GITHUB_ACTION_PATH}-anchored form the gate step runs. Matching the bare
# filename instead would make test_no_call_site_rehand_rolls_the_gate vacuous:
# every mention of the script contains it, so no step could ever be flagged.
GATE_RUN_REF = "${GITHUB_ACTION_PATH}/" + GATE_SCRIPT_REL

# The reusable reviewer's own model path: the action that calls the model, and
# the gate that reads the log it leaves behind.
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review.yaml"
CLAUDE_CODE_ACTION = "anthropics/claude-code-action@"
REVIEWER_GATE = "checks/claude-execution.py"
REVIEWER_GATE_SCRIPT = (
    REPO_ROOT / ".github" / "reviewer" / "checks" / "claude-execution.py"
)


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _gate_step():
    steps = _load(ACTION)["runs"]["steps"]
    gates = [s for s in steps if GATE_RUN_REF in str(s.get("run", ""))]
    assert len(gates) == 1, f"expected exactly one gate step, found {len(gates)}"
    return gates[0]


def _all_steps():
    """Every step in every workflow and composite action, as (label, step)."""
    roots = [
        (REPO_ROOT / ".github" / "workflows", lambda d: (d.get("jobs") or {}).values()),
        (REPO_ROOT / ".github" / "actions", lambda d: [d.get("runs") or {}]),
    ]
    for directory, containers in roots:
        for path in sorted(directory.rglob("*.y*ml")):
            doc = _load(path)
            if not isinstance(doc, dict):
                continue
            for container in containers(doc):
                if not isinstance(container, dict):
                    continue
                for step in container.get("steps") or []:
                    if isinstance(step, dict):
                        yield f"{path.relative_to(REPO_ROOT)}:{step.get('name')}", step


def _claude_run_call_sites():
    return [
        (label, step)
        for label, step in _all_steps()
        if step.get("uses") == "./.github/actions/claude-run"
    ]


def test_gate_script_path_resolves_from_the_action_directory() -> None:
    """The gate's ${GITHUB_ACTION_PATH} reference must land on the real script.
    RED if the script moves out of the action directory — a broken path would
    fail every Claude run, or (worse) be 'fixed' by deleting the gate."""
    assert (ACTION_DIR / GATE_SCRIPT_REL).resolve().is_file()


def test_gate_is_on_by_default() -> None:
    """Fail closed: a caller that says nothing still gets gated."""
    assert _load(ACTION)["inputs"]["gate_execution"]["default"] == "true"


def test_gate_step_is_guarded_only_by_the_opt_out() -> None:
    """The gate must run on every invocation except an explicit opt-out — not be
    narrowed by some other condition that could silently disable it."""
    assert _gate_step()["if"] == "inputs.gate_execution == 'true'"


def test_every_claude_run_call_site_inherits_the_gate() -> None:
    """The choke-point property, asserted member by member: no call site opts
    out. A new caller is gated by construction; one that sets
    gate_execution: false must justify itself here."""
    call_sites = _claude_run_call_sites()
    # A floor, not a count: a parser that reads no call site would run the
    # opt-out check below over nothing and pass. The first-pass PR reviewer runs
    # its own attempts in review.yaml, which the tests below gate instead.
    assert len(call_sites) >= 4, f"expected the known callers, found {len(call_sites)}"
    opted_out = [
        label
        for label, step in call_sites
        if str((step.get("with") or {}).get("gate_execution", "true")).lower()
        == "false"
    ]
    assert opted_out == [], f"ungated claude-run call sites: {opted_out}"


# The one caller that invokes the gate SCRIPT directly, and why it cannot go
# through the claude-run composite: its workspace is the untrusted PR head left
# mid-merge, and the runner reads a local action's manifest out of the workspace
# at step time — so a PR whose conflict lands in an action.yaml would hand the
# runner a manifest full of conflict markers and kill the resolver before it
# starts. It runs the same one script from the base-ref staging dir; the gate is
# still not re-typed, only reached by a different path.
# Empty here: the one direct caller left with the resolver, which is now its own
# repository. An entry is a step that runs check-claude-execution.sh itself
# rather than through the claude-run composite.
GATE_DIRECT_CALLERS: set[str] = set()


def test_no_call_site_rehand_rolls_the_gate() -> None:
    """The obligation lives in one place. A workflow re-typing the gate means the
    choke point leaked back out into per-caller boilerplate."""
    rehandrolled = [
        label
        for label, step in _all_steps()
        if GATE_SCRIPT_REL in str(step.get("run", ""))
        and GATE_RUN_REF not in str(step.get("run", ""))
        and label not in GATE_DIRECT_CALLERS
    ]
    assert rehandrolled == [], f"gate re-implemented at: {rehandrolled}"


def test_every_direct_gate_caller_is_still_a_real_step() -> None:
    """An exemption that outlives its step is an exemption nobody notices has
    stopped meaning anything — and the next caller to re-type the gate would
    inherit it silently."""
    labels = {label for label, _ in _all_steps()}
    assert GATE_DIRECT_CALLERS <= labels, (
        f"stale gate exemption(s): {GATE_DIRECT_CALLERS - labels}"
    )


def test_execution_log_path_has_a_single_source() -> None:
    """The rung-coalesce is computed once and read by both the execution_file
    output and the gate — not copied per consumer, where the copies could drift
    and silently gate a different log than the caller reports on."""
    doc = _load(ACTION)
    assert doc["outputs"]["execution_file"]["value"] == (
        "${{ steps.resolve_log.outputs.execution_file }}"
    )
    assert _gate_step()["env"]["EXECUTION_FILE"] == (
        "${{ steps.resolve_log.outputs.execution_file }}"
    )


# ── The reusable reviewer's model path ───────────────────────────────────────
#
# review.yaml runs claude-code-action directly, once per credential attempt, so
# the claude-run composite's gate cannot reach it. The same obligation is
# asserted here at that shape: the attempts are continue-on-error, and ONE gate
# after the last of them reads the newest attempt's log.


def _review_jobs():
    return _load(REVIEW_WORKFLOW)["jobs"]


def _is_model_step(step) -> bool:
    return str(step.get("uses", "")).startswith(CLAUDE_CODE_ACTION)


def _model_jobs():
    """Every job in review.yaml that calls the model, as (name, job)."""
    return [
        (name, job)
        for name, job in _review_jobs().items()
        if any(_is_model_step(s) for s in job.get("steps") or [])
    ]


def test_the_reviewer_gate_script_exists() -> None:
    """The reviewer runs the gate by path from the cloned reviewer directory. A
    moved or renamed script would fail every review, or be 'fixed' by deleting
    the gate step."""
    assert REVIEWER_GATE_SCRIPT.is_file()


def test_one_job_calls_the_model_and_it_holds_no_write_scope() -> None:
    """The trust model's third rule: the job that reads the untrusted diff with a
    model posts nothing. A write scope here is what a prompt injection would
    need to act on the repository directly."""
    jobs = _model_jobs()
    assert [name for name, _ in jobs] == ["read"]
    assert jobs[0][1]["permissions"] == {"contents": "read"}


def test_every_attempt_is_judged_and_the_last_one_is_gated() -> None:
    """Each attempt is followed by the judge that moves its log aside and arms
    the next rung, on the same condition, so no attempt's log is overwritten
    unread. After the last attempt, ONE unconditional gate reads the newest log:
    an attempt that errored on every rung must red the job, never leave a green
    one with no findings."""
    steps = _review_jobs()["read"]["steps"]
    attempts = [i for i, s in enumerate(steps) if _is_model_step(s)]
    assert len(attempts) == len(
        _load(REVIEW_WORKFLOW)[True]["workflow_call"]["secrets"]
    )
    for i in attempts:
        judge = steps[i + 1]
        assert 'review-ladder.py" next' in judge["run"]
        assert judge["if"] == steps[i]["if"]
        assert steps[i]["continue-on-error"] is True
    gates = [i for i, s in enumerate(steps) if REVIEWER_GATE in str(s.get("run", ""))]
    assert len(gates) == 1 and gates[0] > attempts[-1] + 1
    gate = steps[gates[0]]
    assert "if" not in gate
    assert gate["env"]["EXECUTION_FILE"] == "${{ env.REVIEW_EXECUTION_FILE }}"


def _kind_expression(rung: str) -> str:
    return (
        f"${{{{ startsWith(secrets.{rung}, 'sk-ant-oat') && 'oauth' || "
        f"secrets.{rung} != '' && 'metered' || '' }}}}"
    )


def test_the_review_credentials_reach_only_the_steps_that_run_the_model() -> None:
    """A rung leaking into a step that runs repository-supplied code would hand
    that code a paid token. The model steps hold the values; the read job's env
    holds only each rung's KIND, the one expression that names a secret without
    exposing it."""
    holders = []
    for name, job in _review_jobs().items():
        for step in job.get("steps") or []:
            exposed = yaml.safe_dump({"env": step.get("env"), "with": step.get("with")})
            if "secrets.rung_" in exposed and not _is_model_step(step):
                holders.append(f"{name}:{step.get('name')}")
        job_env = job.get("env") or {}
        for key, value in job_env.items():
            if "secrets.rung_" in str(value):
                rung = f"rung_{key.removeprefix('RUNG_').removesuffix('_KIND')}"
                assert value == _kind_expression(rung), f"{name} env {key}"
    assert holders == [], f"credential holders outside the model steps: {holders}"


def test_every_rung_reaches_each_attempt() -> None:
    """Each declared secret must be selectable by every attempt, as an OAuth
    token and as a metered key. A rung an attempt cannot select is a fallback
    that silently never fires, and the review dies on the rung before it."""
    doc = _load(REVIEW_WORKFLOW)
    declared = set(doc[True]["workflow_call"]["secrets"])
    for _, job in _model_jobs():
        for step in filter(_is_model_step, job["steps"]):
            for key in ("claude_code_oauth_token", "anthropic_api_key"):
                value = str(step["with"][key])
                forwarded = {r for r in declared if f"secrets.{r} " in f"{value} "}
                assert forwarded == declared, f"{key} drops {declared - forwarded}"
