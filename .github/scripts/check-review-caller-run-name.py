#!/usr/bin/env python3
"""Every caller of review.yaml must end its `run-name:` with the pull request number.

PROBLEM CLASS — a bound that silently has no effect because the value it reads
is never there. dispatch-delta-review.sh stops asking for the accumulated read
of a pull request after two failed runs, and the only thing attributing a run to
a pull request is the caller's own `run-name:`. A caller with no such name, or
one that puts anything after the number, matches nothing: the bound is never
reached, and a reader that dies on every run is re-dispatched for the life of
the pull request. Nothing goes red, so the waste is invisible.

The check reads the caller's YAML and accepts two shapes, both of which end a
dispatch run's name with `PR <number>`:

    run-name: Claude reviewers — PR ${{ inputs.pr }}
    run-name: Claude reviewers${{ inputs.pr && format(' — PR {0}', inputs.pr) || '' }}

The second is the one a caller wants. A `pull_request_target` run has no
`inputs.pr`, so its name stays plain, and only the dispatch runs the dispatcher
filters for carry the suffix.

A caller that passes `max-delta-reviews-per-pr: 0` asks for no accumulated read,
so it needs no attribution and is exempt. Absence of that input is NOT exempt:
review.yaml defaults it to 1.
"""

import re
import sys
from pathlib import Path

import yaml

# The reusable reviewer, however a caller spells the path to it.
REVIEWER_WORKFLOW = re.compile(r"(?:^|/)\.github/workflows/review\.yaml(?:@|$)")

# `... PR ${{ <expr> }}` — the number interpolated at the very end.
DIRECT = re.compile(r"PR \$\{\{(?:[^}]|\}(?!\}))*\}\}\s*$")
# The final `${{ … }}` of the name, whatever precedes it.
TRAILING_EXPR = re.compile(r"\$\{\{(?P<expr>(?:[^}]|\}(?!\}))*)\}\}\s*$")
# `format(' … PR {0}', …)` — the number is the format template's LAST field, so
# the template's closing quote must follow it. Unanchored, this accepts
# `format(' — PR {0} (accumulated)', …)`, whose run name ends in prose and
# matches nothing the dispatcher looks for.
FORMATTED = re.compile(r"PR \{\d+\}'")

HOW = (
    "end it with the pull request number, e.g. "
    "run-name: Claude reviewers${{ inputs.pr && format(' — PR {0}', inputs.pr) || '' }}"
)


def calls_the_reviewer(workflow: dict) -> bool:
    """True when one of the workflow's jobs calls review.yaml with accumulated
    reads left on."""
    for job in (workflow.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if not isinstance(uses, str) or not REVIEWER_WORKFLOW.search(uses):
            continue
        given = (job.get("with") or {}).get("max-delta-reviews-per-pr")
        if str(given).strip() == "0":
            continue
        return True
    return False


def names_the_pull_request(run_name: str) -> bool:
    """True when a dispatch run of this caller is named `… PR <number>`."""
    if DIRECT.search(run_name):
        return True
    match = TRAILING_EXPR.search(run_name)
    return bool(match and FORMATTED.search(match.group("expr")))


def complain(path: Path, workflow: dict) -> str | None:
    """The one line this file earns, or None when it is fine."""
    if not calls_the_reviewer(workflow):
        return None
    run_name = workflow.get("run-name")
    if not isinstance(run_name, str):
        return (
            f"{path}: calls review.yaml with accumulated reads on but declares no "
            f"`run-name:`, so dispatch-delta-review.sh can attribute none of its runs "
            f"to a pull request and never stops re-dispatching a failing read — {HOW}"
        )
    if not names_the_pull_request(run_name):
        return (
            f"{path}: `run-name: {run_name}` does not end with the pull request "
            f"number, so dispatch-delta-review.sh matches none of its runs and never "
            f"stops re-dispatching a failing read — {HOW}"
        )
    return None


def main() -> None:
    problems = []
    for name in sys.argv[1:]:
        path = Path(name)
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict):
            continue
        problem = complain(path, workflow)
        if problem:
            problems.append(problem)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
