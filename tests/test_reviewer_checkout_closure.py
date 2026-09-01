"""Every file a reviewer script reads must reach the runner that runs it.

A sparse checkout naming individual FILES gets exactly those files, so a job that
runs a script without one of its dependencies dies at its `source` line under
`set -e` — or, for the severity SSOT, at the `jq` that reads which findings gate.
A gate that dies posts no verdict, and its required context then hangs at
"Expected — Waiting for status to be reported".
"""

import yaml

from tests._helpers import REPO_ROOT

LOGIN_LIB_REL = ".github/scripts/lib/reviewer-login.bash"
# Scripts that must read the reviewer identity through the shared library rather
# than re-deriving it. The predicate was copied into four scripts once; the point
# of the library is that it stops being four.
REVIEWER_SCRIPTS = ("approve-if-reviewer-hold-clear.sh",)

GATE_DEPS = (
    ".github/reviewer/review-findings-gate.sh",
    ".github/reviewer/lib/review-threads.bash",
    ".github/reviewer/lib/pr-reviews.bash",
    ".github/reviewer/lib/review-skip-set.bash",
    ".github/reviewer/lib-ci-retry.sh",
    "config/review-severities.json",
)
HOLD_CLEAR_DEPS = (
    ".github/scripts/approve-if-reviewer-hold-clear.sh",
    LOGIN_LIB_REL,
    ".github/scripts/lib/github-token-ladder.bash",
)
# The sweep runs BOTH per PR, so its runner needs the union.
REVIEWER_SCRIPT_DEPS = {
    "review-findings-gate.sh": GATE_DEPS,
    "approve-if-reviewer-hold-clear.sh": HOLD_CLEAR_DEPS,
    "sweep-reviewer-holds.sh": (
        ".github/scripts/sweep-reviewer-holds.sh",
        *GATE_DEPS,
        *HOLD_CLEAR_DEPS,
    ),
}


def _covered(entries: list[str], path: str) -> bool:
    """Whether a sparse-checkout list brings PATH in — named outright, or inside a
    directory it names."""
    return any(e == path or path.startswith(e.rstrip("/") + "/") for e in entries)


def _reviewer_jobs():
    """(workflow, job id, scripts it runs, sparse entries) per job that runs a
    reviewer script. A job whose checkout takes the whole repo yields nothing:
    every dependency is already there."""
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            steps = (job or {}).get("steps") or []
            run_text = "\n".join(str(step.get("run") or "") for step in steps)
            # A job that CLONES the reviewer repository carries its own closure:
            # the script and every library it sources arrive with the clone, and
            # the sparse checkout beside it fetches only the caller's severity
            # SSOT. Keyed on the clone itself, so renaming the variable that
            # holds its path cannot silently exempt a job that does not clone.
            if "git clone" in run_text and "REVIEWER_REPO" in run_text:
                continue
            scripts = [s for s in REVIEWER_SCRIPT_DEPS if s in run_text]
            if not scripts:
                continue
            entries: list[str] = []
            full_checkout = False
            for step in steps:
                if not str(step.get("uses") or "").startswith("actions/checkout@"):
                    continue
                sparse = (step.get("with") or {}).get("sparse-checkout")
                if not sparse:
                    full_checkout = True
                    continue
                entries += [e.strip() for e in str(sparse).split("\n") if e.strip()]
            if full_checkout:
                continue
            yield path.name, job_id, scripts, entries


def test_every_job_running_a_reviewer_script_checks_out_what_it_reads():
    checked = []
    for workflow, job_id, scripts, entries in _reviewer_jobs():
        for script in scripts:
            for dep in REVIEWER_SCRIPT_DEPS[script]:
                checked.append((workflow, job_id, dep))
                assert _covered(entries, dep), (
                    f"{workflow} / job {job_id} runs {script} but its sparse "
                    f"checkout ({entries}) does not bring in {dep}"
                )
    assert checked, (
        "no job runs a reviewer script any more — this guard would now pass "
        "without checking anything; re-point it or delete it"
    )


def test_every_reviewer_script_uses_the_shared_login_library():
    """A script that re-derives the bare login locally has forked the predicate
    the library exists to hold."""
    assert REVIEWER_SCRIPTS, "no script left to check — re-point this guard"
    for name in REVIEWER_SCRIPTS:
        text = (REPO_ROOT / ".github" / "scripts" / name).read_text(encoding="utf-8")
        assert "lib/reviewer-login.bash" in text and "reviewer_login_init" in text, (
            f"{name} does not source the shared reviewer-login library"
        )
        assert "REVIEWER_LOGIN%" not in text, (
            f"{name} re-derives REVIEWER_LOGIN_BARE itself instead of calling "
            "reviewer_login_init"
        )
        assert 'sub("\\\\[bot\\\\]$"' not in text, (
            f"{name} hand-writes the [bot]-stripping jq instead of using the "
            "library's select clause"
        )
