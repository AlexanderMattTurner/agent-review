"""Behavioral tests for .github/reviewer/build-review-context.py — the step that
puts the base tree's OTHER uses of a changed name in front of the reviewer.

The defect class it answers: a change fixes the caller that failed and leaves
the sibling caller of the same contract alone. The reviewer sees only the diff,
so the sibling is invisible and the review reads as complete.

Nothing is stubbed. Each case builds a real git repository, writes a real diff,
and runs the real script, because the search IS `git grep` over a real index —
a stubbed grep would test this file's idea of one.
"""

# covers: .github/reviewer/build-review-context.py

import subprocess
from pathlib import Path

from tests._helpers import REPO_ROOT, commit_files, init_test_repo, load_script

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "build-review-context.py"
MODULE = load_script(".github/reviewer/build-review-context.py")


def diff_for(path: str, *, added: list[str], removed: list[str] = ()) -> str:
    """One file section in the shape the sanitized diff has."""
    body = "".join(f"-{line}\n" for line in removed)
    body += "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n" + body
    )


def build(tmp_path: Path, tree: dict[str, str], diff: str) -> str:
    """Run the real script over a real repository; return context.txt."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    commit_files(repo, tree, "fixture")
    diff_file = tmp_path / "diff.txt"
    diff_file.write_text(diff, encoding="utf-8")
    out = tmp_path / "context.txt"
    proc = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--diff",
            str(diff_file),
            "--out",
            str(out),
            "--repo-dir",
            str(repo),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return out.read_text(encoding="utf-8")


def test_the_sibling_caller_the_diff_left_alone_is_listed(tmp_path: Path) -> None:
    """THE case this exists for. The diff teaches `vm-exec.sh` to report the
    probe's status; `export.sh` still swallows it, and nothing in the diff says
    so. A reviewer given only the diff calls the change complete."""
    context = build(
        tmp_path,
        {
            "bin/vm-exec.sh": 'probe_workspace_image "$key"\n',
            "bin/export.sh": 'probe_workspace_image "$key" >/dev/null 2>&1 || return 1\n',
        },
        diff_for(
            "bin/vm-exec.sh",
            removed=['probe_workspace_image "$key" || return 1'],
            added=['probe_workspace_image "$key" || { report "$?"; return 1; }'],
        ),
    )
    assert "## probe_workspace_image" in context, context
    assert "bin/export.sh:1:" in context, context


def test_the_files_the_diff_changed_are_not_listed_back(tmp_path: Path) -> None:
    """The reviewer already has those lines. Listing them again spends budget on
    what it can read in the diff, and buries the site it cannot."""
    context = build(
        tmp_path,
        {"bin/vm-exec.sh": 'probe_workspace_image "$key"\n'},
        diff_for("bin/vm-exec.sh", added=['probe_workspace_image "$key"']),
    )
    assert "bin/vm-exec.sh" not in context.split("\n#")[-1], context


def test_a_name_used_everywhere_gets_no_section_rather_than_a_truncated_one(
    tmp_path: Path,
) -> None:
    """Past the per-name cap a name is not about one thing. A truncated list
    reads as the whole set, which is worse than no list: the reviewer concludes
    it has seen every site."""
    cap = MODULE.MAX_HITS_PER_IDENTIFIER
    tree = {f"lib/f{n}.sh": 'common_helper "$1"\n' for n in range(cap + 3)}
    tree["bin/changed.sh"] = 'common_helper "$1"\n'
    context = build(
        tmp_path, tree, diff_for("bin/changed.sh", added=['common_helper "$1"'])
    )
    assert "## common_helper" not in context, context


def test_a_name_just_under_the_cap_is_listed_in_full(tmp_path: Path) -> None:
    """The pair for the case above, differing only in how many sites exist."""
    cap = MODULE.MAX_HITS_PER_IDENTIFIER
    tree = {f"lib/f{n}.sh": 'common_helper "$1"\n' for n in range(cap)}
    tree["bin/changed.sh"] = 'common_helper "$1"\n'
    context = build(
        tmp_path, tree, diff_for("bin/changed.sh", added=['common_helper "$1"'])
    )
    assert "## common_helper" in context, context
    assert context.count('common_helper "$1"') == cap, context


def test_a_short_ordinary_word_is_not_searched(tmp_path: Path) -> None:
    """`path` and `name` match every file in a tree and say nothing about the
    change, so the file would be noise and the real names would fall off the
    budget behind them."""
    assert MODULE.identifiers_of(diff_for("a.sh", added=["path=$1; name=$2"])) == []


def test_a_name_the_diff_changed_outranks_one_it_only_added(tmp_path: Path) -> None:
    """A name on both a removed and an added line is a contract the diff CHANGED,
    which is exactly where a sibling caller still holds the old contract."""
    names = MODULE.identifiers_of(
        diff_for(
            "a.sh",
            removed=["probe_status old_helper"],
            added=["probe_status new_helper"],
        )
    )
    assert names[0] == "probe_status", names


def test_a_timed_out_search_states_the_gap_rather_than_claiming_none(
    tmp_path: Path,
) -> None:
    """A can't-verify must not read as "this name is used nowhere else". The
    search is an optimization, so a tree too large to walk says so and the job
    stays green."""
    rendered = MODULE.render({}, "deadbeef", False)
    assert "INCOMPLETE" in rendered
    assert "timed out" in rendered


def test_the_whole_file_stays_inside_its_line_budget(tmp_path: Path) -> None:
    """A diff touching hundreds of names must not push the diff itself out of the
    model's context."""
    hits = {f"identifier_{n}": ["a hit"] * 10 for n in range(500)}
    rendered = MODULE.render(hits, "deadbeef", True)
    assert len(rendered.splitlines()) <= MODULE.MAX_TOTAL_LINES
    assert "Budget reached" in rendered


def test_the_header_says_the_lines_are_not_from_the_pull_request(
    tmp_path: Path,
) -> None:
    """The reviewer is told which of its inputs are untrusted. This file is base
    -tree content, and a reviewer treating it as PR text would discount the one
    input that is safe to trust."""
    context = build(
        tmp_path,
        {"bin/other.sh": 'probe_workspace_image "$1"\n'},
        diff_for("bin/changed.sh", added=['probe_workspace_image "$1"']),
    )
    assert "NOT from the pull request" in context
