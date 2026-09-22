"""Behavioral tests for the PR-reviewer eval case set under tests/eval/cases/.

Contract (schema documented in tests/eval/README.md):
  * Every case.json carries the required keys, a `kind` of `defect` or
    `non_bug`, and a non-empty `must_flag` for every `defect` case.
  * Every path named in must_flag/must_not_flag/context_expect resolves to a
    file the case actually ships, in diff.txt or under tree/.
  * diff.txt parses as a real unified diff through the reviewer's own
    .github/reviewer/_diff_sections.py splitter.
  * Running the real .github/reviewer/build-review-context.py against the
    case's diff.txt, over a git repo built from tree/, surfaces every
    context_expect identifier/path pair under that identifier's section —
    the sibling-caller evidence the reviewer prompt depends on.

No live model call runs here: this only exercises the deterministic context
pipeline the reviewer runs before any prompt is built.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, commit_files, init_test_repo, load_script

CASES_DIR = REPO_ROOT / "tests" / "eval" / "cases"
BUILD_CONTEXT = REPO_ROOT / ".github" / "reviewer" / "build-review-context.py"

_diff_sections = load_script(".github/reviewer/_diff_sections.py")

REQUIRED_KEYS = {
    "kind",
    "source",
    "summary",
    "must_flag",
    "must_not_flag",
    "context_expect",
}
VALID_KINDS = {"defect", "non_bug"}


def _case_dirs() -> list[Path]:
    return sorted(p.parent for p in CASES_DIR.glob("*/case.json"))


def _load(case_dir: Path) -> dict:
    return json.loads((case_dir / "case.json").read_text(encoding="utf-8"))


def _diff_paths(case_dir: Path) -> set[str]:
    diff_text = (case_dir / "diff.txt").read_text(encoding="utf-8")
    _, files = _diff_sections.split_into_files(diff_text)
    return {_diff_sections.file_path_of(section) for section in files}


def _tree_paths(case_dir: Path) -> set[str]:
    tree = case_dir / "tree"
    if not tree.is_dir():
        return set()
    return {str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file()}


CASE_IDS = [p.name for p in _case_dirs()]


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=CASE_IDS)
def test_case_schema(case_dir: Path) -> None:
    # covers: case.json carries every required key, a valid `kind`, and a
    # non-empty must_flag for a defect case.
    case = _load(case_dir)
    missing = REQUIRED_KEYS - case.keys()
    assert not missing, f"{case_dir.name}: missing keys {missing}"
    assert case["kind"] in VALID_KINDS, f"{case_dir.name}: bad kind {case['kind']!r}"
    if case["kind"] == "defect":
        assert case["must_flag"], (
            f"{case_dir.name}: a defect case needs a non-empty must_flag"
        )
    for entry in case["must_flag"]:
        assert entry.get("path") and entry.get("why"), (
            f"{case_dir.name}: must_flag entry missing path/why: {entry}"
        )
    for path in case["must_not_flag"]:
        assert isinstance(path, str) and path, (
            f"{case_dir.name}: bad must_not_flag entry {path!r}"
        )
    for entry in case["context_expect"]:
        assert entry.get("identifier") and entry.get("path"), (
            f"{case_dir.name}: context_expect entry missing identifier/path: {entry}"
        )


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=CASE_IDS)
def test_case_paths_resolve(case_dir: Path) -> None:
    # covers: every must_flag/must_not_flag/context_expect path names a file
    # the case actually ships, in diff.txt or tree/ — never a stale reference.
    case = _load(case_dir)
    known = _diff_paths(case_dir) | _tree_paths(case_dir)
    named = (
        {entry["path"] for entry in case["must_flag"]}
        | set(case["must_not_flag"])
        | {entry["path"] for entry in case["context_expect"]}
    )
    unresolved = named - known
    assert not unresolved, (
        f"{case_dir.name}: path(s) not in diff.txt or tree/: {unresolved}"
    )


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=CASE_IDS)
def test_diff_parses(case_dir: Path) -> None:
    # covers: diff.txt is a real unified diff the reviewer's own splitter can
    # walk — at least one file section, each with a resolvable path.
    diff_text = (case_dir / "diff.txt").read_text(encoding="utf-8")
    _, files = _diff_sections.split_into_files(diff_text)
    assert files, f"{case_dir.name}: diff.txt has no file sections"
    for section in files:
        path = _diff_sections.file_path_of(section)
        assert path and not path.startswith(("a/", "b/")), (
            f"{case_dir.name}: bad parsed path {path!r}"
        )


def _build_context(case_dir: Path, tmp_path: Path) -> str:
    """Run the real build-review-context.py against `case_dir`'s diff, over a
    repo built from its tree/ files, and return the produced context.txt."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    tree = case_dir / "tree"
    files = {
        str(p.relative_to(tree)): p.read_text(encoding="utf-8")
        for p in tree.rglob("*")
        if p.is_file()
    }
    commit_files(repo, files, "fixture base tree")
    out = tmp_path / "context.txt"
    subprocess.run(
        [
            sys.executable,
            str(BUILD_CONTEXT),
            "--diff",
            str(case_dir / "diff.txt"),
            "--out",
            str(out),
            "--repo-dir",
            str(repo),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.read_text(encoding="utf-8")


def _section(context_text: str, identifier: str) -> str:
    """The body of context.txt's `## <identifier>` section, or "" when the
    identifier has no section."""
    marker = f"## {identifier}\n"
    start = context_text.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    end = context_text.find("\n## ", start)
    return context_text[start : end if end != -1 else None]


@pytest.mark.parametrize("case_dir", _case_dirs(), ids=CASE_IDS)
def test_context_expect(case_dir: Path, tmp_path: Path) -> None:
    # covers: build-review-context.py, run for real over the case's tree/,
    # surfaces every declared identifier/path pair under that identifier's
    # section — the sibling-caller evidence a fix-one-caller defect needs.
    case = _load(case_dir)
    if not case["context_expect"]:
        pytest.skip(f"{case_dir.name}: no context_expect declared")
    context_text = _build_context(case_dir, tmp_path)
    for entry in case["context_expect"]:
        section = _section(context_text, entry["identifier"])
        assert section, (
            f"{case_dir.name}: no ## {entry['identifier']} section in context.txt"
        )
        assert entry["path"] in section, (
            f"{case_dir.name}: {entry['path']} missing from the "
            f"{entry['identifier']} section:\n{section}"
        )
