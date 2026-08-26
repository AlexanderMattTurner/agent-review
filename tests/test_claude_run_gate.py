"""The execution-log gate as a choke point, on both paths that run a model.

A green model step is not proof Claude ran (auth failure, crash before the
result event, corrupt log all exit 0), so every invocation must be gated.

Two paths run a model in this repository, and each has ONE gate:
  * `.github/actions/claude-run` — the composite action. The gate lives inside
    it rather than being re-typed at each call site, where it was previously
    missed by 5 of 7 callers.
  * `.github/workflows/review.yaml` — the reusable PR reviewer. It calls the
    model through `run-review-ladder.py` in a `run:` step, so it cannot inherit
    the composite's gate; it runs `checks/claude-execution.py` instead.

These tests enumerate every call site on both paths, so a future one that opts
out (or a refactor that drops the gate step) reds without being named here.
"""

import yaml

from tests._helpers import REPO_ROOT

ACTION_DIR = REPO_ROOT / ".github" / "actions" / "claude-run"
ACTION = ACTION_DIR / "action.yaml"
GATE_SCRIPT_REL = "../../scripts/check-claude-execution.sh"

# The reusable reviewer's own model path: the script that calls the model, and
# the gate that reads the log it leaves behind.
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review.yaml"
LADDER = "run-review-ladder.py"
REVIEWER_GATE = "checks/claude-execution.py"
REVIEWER_GATE_SCRIPT = REPO_ROOT / ".github" / "reviewer" / "checks" / "claude-execution.py"


def _load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _gate_step():
    steps = _load(ACTION)["runs"]["steps"]
    gates = [s for s in steps if GATE_SCRIPT_REL in str(s.get("run", ""))]
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
    """The gate's ${GITHUB_ACTION_PATH}-relative path must land on the real
    script. RED if the script moves or the ../../ depth is wrong — a broken path
    would fail every Claude run, or (worse) be 'fixed' by deleting the gate."""
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
    # opt-out check below over nothing and pass. The first-pass PR reviewer left
    # this path for review.yaml's ladder, which the tests below gate instead.
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
        if "check-claude-execution.sh" in str(step.get("run", ""))
        and GATE_SCRIPT_REL not in str(step.get("run", ""))
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
# review.yaml calls the model from a `run:` step, so the claude-run composite's
# gate cannot reach it. The same obligation is asserted here at that shape.


def _review_jobs():
    return _load(REVIEW_WORKFLOW)["jobs"]


def _steps_running(job, needle):
    return [s for s in job.get("steps") or [] if needle in str(s.get("run", ""))]


def _model_jobs():
    """Every job in review.yaml that calls the model, as (name, job)."""
    return [(name, job) for name, job in _review_jobs().items() if _steps_running(job, LADDER)]


def test_the_reviewer_gate_script_exists() -> None:
    """The reviewer runs the gate by path from the cloned reviewer directory. A
    moved or renamed script would fail every review, or be 'fixed' by deleting
    the gate step."""
    assert REVIEWER_GATE_SCRIPT.is_file()


def test_the_reviewer_calls_the_model_from_more_than_one_job() -> None:
    """A floor, not a count: the whole-diff read and the shard leg both call the
    model, so a parser that found neither would run every check below over
    nothing and pass."""
    assert len(_model_jobs()) >= 2, f"found model jobs: {[n for n, _ in _model_jobs()]}"


def test_every_reviewer_model_call_is_gated() -> None:
    """The choke-point property at the reviewer's call shape: every job that
    runs the credential ladder also runs the execution-log gate, and the gate
    reads that ladder step's own log."""
    for name, job in _model_jobs():
        model_steps = _steps_running(job, LADDER)
        assert len(model_steps) == 1, f"{name} runs the ladder {len(model_steps)} times"
        gates = _steps_running(job, REVIEWER_GATE)
        assert len(gates) == 1, f"{name} has {len(gates)} gate steps, expected 1"
        model, gate = model_steps[0], gates[0]
        assert gate["env"]["EXECUTION_FILE"] == (
            "${{ steps.%s.outputs.execution_file }}" % model["id"]
        ), f"{name}'s gate reads a log its own model step did not write"


def test_no_reviewer_gate_is_narrowed_past_its_model_step() -> None:
    """A gate whose `if:` is stricter than the step it gates is a gate that can
    be skipped while the model still ran — the silent green this exists to
    prevent."""
    for name, job in _model_jobs():
        model = _steps_running(job, LADDER)[0]
        gate = _steps_running(job, REVIEWER_GATE)[0]
        assert gate.get("if") == model.get("if"), (
            f"{name}: gate `if:` {gate.get('if')!r} does not match the model "
            f"step's {model.get('if')!r}"
        )


def test_the_review_credentials_reach_only_the_steps_that_run_the_model() -> None:
    """The property the old claude-run call-site count was really about: the
    model credentials are handed to the steps that call the model and to no
    other step. A rung leaking into a step that runs repository-supplied code
    would hand that code a paid token."""
    holders, callers = [], []
    for name, job in _review_jobs().items():
        for step in job.get("steps") or []:
            label = f"{name}:{step.get('name')}"
            if "secrets.rung_" in yaml.safe_dump(step.get("env") or {}):
                holders.append(label)
            if LADDER in str(step.get("run", "")):
                callers.append(label)
    assert sorted(holders) == sorted(callers), (
        f"credential holders {sorted(holders)} != model callers {sorted(callers)}"
    )


def test_every_rung_reaches_the_model_step() -> None:
    """Each declared secret must actually be handed to the ladder. A rung
    declared but never forwarded is a fallback that silently never fires, and
    the review dies on the rung before it."""
    doc = _load(REVIEW_WORKFLOW)
    # YAML 1.1 reads a bare `on:` key as the boolean True, so the trigger
    # block is under `True` and not under the string.
    declared = set(doc[True]["workflow_call"]["secrets"])
    for name, job in _model_jobs():
        env = _steps_running(job, LADDER)[0]["env"]
        forwarded = {
            rung
            for rung in declared
            if any(f"secrets.{rung} " in f"{value} " for value in map(str, env.values()))
        }
        assert forwarded == declared, f"{name} drops rung(s): {declared - forwarded}"
