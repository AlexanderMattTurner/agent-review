#!/usr/bin/env python3
"""Bounded live comparison of the real reviewer prompt against tests/eval/cases/.

Spends real model cost. NEVER invoked by CI or by any test — run it by hand:

    python3 tests/eval/run-live.py --max-cases 3 --model claude-opus-5

For each selected case this builds the same PR_INPUT_DIR layout the production
reviewer builds (diff.txt, meta.txt, sanitizer-report.txt, context.txt via the
real build-review-context.py, coverage.json), runs the `claude` CLI ONCE per
case with the production prompt and tool grant (imported from
run-review-ladder.py, never re-derived), and prints one JSON report: per case,
the model's turn count and cost off its own execution log, and which
must_flag/must_not_flag predictions it got right.

`--max-cases` (default 3) is a hard cap: cases beyond it are never selected,
never mind how many tests/eval/cases/ holds. The case list and the exact call
count this run will spend are printed before the first `claude` invocation.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# `git rev-parse` rather than a parent walk: moving this file must not silently
# point the import at the wrong tree.
sys.path.insert(
    0,
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip(),
)

# pylint: disable=wrong-import-position  # must follow the sys.path insert above
from tests._helpers import (  # noqa: E402
    REPO_ROOT,
    commit_files,
    init_test_repo,
    load_script,
)

CASES_DIR_DEFAULT = REPO_ROOT / "tests" / "eval" / "cases"
# The same file post-pr-review.mjs reads, so "would this hold the merge?" is
# answered here by the reviewer's own definition rather than a second list.
GATING_SEVERITIES = set(
    json.loads(
        (REPO_ROOT / "config" / "review-severities.json").read_text(encoding="utf-8")
    )["gating"]
)
BUILD_CONTEXT = REPO_ROOT / ".github" / "reviewer" / "build-review-context.py"
PROMPT_FILE = REPO_ROOT / ".github" / "reviewer" / "prompts" / "claude-pr-review.md"
REVIEWER_DIR = REPO_ROOT / ".github" / "reviewer"

_ladder = load_script(".github/reviewer/run-review-ladder.py")
_diff_sections = load_script(".github/reviewer/_diff_sections.py")

prompt_for = _ladder.prompt_for
TOOL_GRANT = _ladder.TOOL_GRANT


def _case_dirs(cases_dir: Path) -> list[Path]:
    return sorted(p.parent for p in cases_dir.glob("*/case.json"))


def _load_case(case_dir: Path) -> dict:
    return json.loads((case_dir / "case.json").read_text(encoding="utf-8"))


def _build_repo(case_dir: Path, workdir: Path) -> Path:
    """A git repo holding the case's tree/ files, standing in for the base checkout."""
    repo = workdir / "repo"
    init_test_repo(repo)
    tree = case_dir / "tree"
    files = {
        str(p.relative_to(tree)): p.read_text(encoding="utf-8")
        for p in tree.rglob("*")
        if p.is_file()
    }
    commit_files(repo, files, "fixture base tree")
    return repo


def _build_pr_input(case_dir: Path, case: dict, repo: Path, pr_input_dir: Path) -> None:
    """The diff.txt/meta.txt/sanitizer-report.txt/context.txt/coverage.json layout
    prepare-pr-review-input.sh produces for a real PR, built from the case's own
    (already-trusted) fixture data — nothing here needs the untrusted-input sanitizer."""
    pr_input_dir.mkdir(parents=True, exist_ok=True)
    diff_text = (case_dir / "diff.txt").read_text(encoding="utf-8")
    (pr_input_dir / "diff.txt").write_text(diff_text, encoding="utf-8")
    (pr_input_dir / "sanitizer-report.txt").write_text("", encoding="utf-8")

    _, files = _diff_sections.split_into_files(diff_text)
    meta = {
        "title": case["summary"],
        "body": f"Eval case {case_dir.name} — source {case['source']}",
        "author": {"login": "eval-harness"},
        "files": [
            {
                "path": _diff_sections.file_path_of(section),
                "additions": 0,
                "deletions": 0,
                "status": "modified",
            }
            for section in files
        ],
    }
    (pr_input_dir / "meta.txt").write_text(json.dumps(meta), encoding="utf-8")

    head = case["source"].rsplit("@", 1)[-1]
    coverage = {
        "head": head,
        "base": "",
        "reviewer": "",
        "read": "first",
        "scope": "whole",
        "since": "",
    }
    (pr_input_dir / "coverage.json").write_text(json.dumps(coverage), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(BUILD_CONTEXT),
            "--diff",
            str(pr_input_dir / "diff.txt"),
            "--out",
            str(pr_input_dir / "context.txt"),
            "--repo-dir",
            str(repo),
        ],
        check=True,
    )


def _run_one(case_dir: Path, case: dict, model: str, workdir: Path) -> dict:
    """Run the real reviewer prompt against one case; return its report entry."""
    repo = _build_repo(case_dir, workdir)
    pr_input_dir = workdir / "pr-input"
    _build_pr_input(case_dir, case, repo, pr_input_dir)

    prompt = prompt_for(
        str(pr_input_dir), str(PROMPT_FILE), case_dir.name, case["source"]
    )
    command = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--effort",
        "medium",
        "--setting-sources",
        "user",
        "--allowedTools",
        TOOL_GRANT.format(d=str(pr_input_dir), r=str(REVIEWER_DIR)),
        "--add-dir",
        str(pr_input_dir),
        "--add-dir",
        str(REVIEWER_DIR),
        "--output-format",
        "json",
    ]
    log_path = workdir / "execution.json"
    with log_path.open("wb") as log_file:
        try:
            subprocess.run(command, stdout=log_file, cwd=repo, check=False)
        except FileNotFoundError:
            sys.exit("::error:: no `claude` on PATH")

    calls, cost_usd, is_error = _read_log(log_path)
    findings = _read_findings(pr_input_dir / "review.json")
    traps = set(case["must_not_flag"])

    return {
        "case": case_dir.name,
        "kind": case["kind"],
        "calls": calls,
        "cost_usd": cost_usd,
        "is_error": is_error,
        "execution_log": str(log_path),
        "must_flag": [
            {
                "path": entry["path"],
                "why": entry["why"],
                "gating_findings": [
                    _quote(f)
                    for f in findings
                    if f.get("path") == entry["path"] and _is_gating(f)
                ],
            }
            for entry in case["must_flag"]
        ],
        "false_positives": [
            _quote(f) | {"path": f.get("path")}
            for f in findings
            if f.get("path") in traps
        ],
    }


def _is_gating(finding: dict) -> bool:
    """Whether this finding would hold the merge. `kind: defect` promises a
    BLOCKING or WARNING finding, so a nit on the right file is not the defect
    caught — scoring it as one overstates the reviewer's accuracy."""
    return finding.get("severity") in GATING_SEVERITIES


def _quote(finding: dict) -> dict:
    """What the model actually said, so a human grades it against the case's
    `why` instead of trusting the path it landed on."""
    body = str(finding.get("body") or "")
    return {
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "body": body[:400] + ("…" if len(body) > 400 else ""),
    }


def _read_log(log_path: Path) -> tuple[int, float, bool]:
    """(num_turns, total_cost_usd, is_error) off the execution log's result event,
    the same statement run-review-ladder.py's outcome_of trusts — a log missing,
    empty or result-less counts as an errored, zero-cost, zero-turn run."""
    try:
        events = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0.0, True
    if isinstance(events, list):
        results = [
            e for e in events if isinstance(e, dict) and e.get("type") == "result"
        ]
        result = results[-1] if results else None
    else:
        result = events if isinstance(events, dict) else None
    if result is None:
        return 0, 0.0, True
    return (
        int(result.get("num_turns", 0)),
        float(result.get("total_cost_usd", 0.0)),
        result.get("is_error") is not False,
    )


def _read_findings(review_json: Path) -> list[dict]:
    if not review_json.is_file():
        return []
    try:
        return json.loads(review_json.read_text(encoding="utf-8")).get("findings", [])
    except json.JSONDecodeError:
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--model", required=True)
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR_DEFAULT)
    args = parser.parse_args()
    if args.max_cases < 1:
        parser.error("--max-cases must be at least 1")

    all_cases = _case_dirs(args.cases_dir)
    selected = all_cases[: args.max_cases]
    print(
        f"selected {len(selected)} of {len(all_cases)} case(s), capped at "
        f"--max-cases={args.max_cases}: {[c.name for c in selected]}",
        file=sys.stderr,
    )
    print(
        f"this run will make exactly {len(selected)} `claude` call(s)", file=sys.stderr
    )

    # Not cleaned up: the execution log and pr-input dir this prints per case are the
    # checker for the report's cost/calls/finding claims, so a human must be able to
    # open them after the run ends.
    run_root = Path(tempfile.mkdtemp(prefix="eval-live-"))
    print(f"per-case inputs and execution logs kept under {run_root}", file=sys.stderr)

    results = []
    for case_dir in selected:
        case = _load_case(case_dir)
        workdir = run_root / case_dir.name
        workdir.mkdir()
        print(f"running {case_dir.name} with {args.model}...", file=sys.stderr)
        results.append(_run_one(case_dir, case, args.model, workdir))

    print(
        json.dumps(
            {"model": args.model, "max_cases": args.max_cases, "results": results},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
