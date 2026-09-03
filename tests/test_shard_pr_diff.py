"""`.github/reviewer/shard-pr-diff.py` splits an oversized PR diff for review.

The load-bearing properties are reunion and atomicity: concatenating the shards
must reproduce the input byte-for-byte (nothing silently dropped from a review
that then reports full coverage), and no file's section may be split across two
shards (half a hunk reads like a complete change to a reviewer, which is worse
than not reviewing it).
"""

import contextlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from tests._helpers import REPO_ROOT, load_script

SHARDER = REPO_ROOT / ".github" / "reviewer" / "shard-pr-diff.py"
mod = load_script(".github/reviewer/shard-pr-diff.py")


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


def _argv(
    diff: Path, out: Path, max_lines: int, max_shards: int, **tier: str
) -> list[str]:
    argv = [
        str(SHARDER),
        "--diff",
        str(diff),
        "--out-dir",
        str(out),
        "--max-lines",
        str(max_lines),
        "--max-shards",
        str(max_shards),
    ]
    # Off unless a case asks: the flags default to no tiering, and the cases that
    # predate them must keep exercising that default.
    for flag, value in tier.items():
        argv += [f"--{flag.replace('_', '-')}", value]
    return argv


def _run(
    diff: Path,
    out: Path,
    max_lines: int = 8000,
    max_shards: int = 0,
    tier: dict[str, str] | None = None,
    **env_extra: str,
) -> Result:
    """Drive the sharder's own ``main`` in this interpreter.

    The environment is replaced wholesale for the call (PATH plus whatever the
    test asks for) so an ambient ``GITHUB_OUTPUT`` — which CI always sets —
    cannot make a test append to the real step-output file.
    """
    out_buf, err_buf = io.StringIO(), io.StringIO()
    env = {"PATH": os.environ.get("PATH", ""), **env_extra}
    code = 0
    with (
        mock.patch.object(
            sys, "argv", _argv(diff, out, max_lines, max_shards, **(tier or {}))
        ),
        mock.patch.dict(os.environ, env, clear=True),
        # contextlib's own redirects: scoped to this `with`, restored on exit,
        # and the suite runs these cases in one thread.
        contextlib.redirect_stdout(out_buf),  # allow-stdio-swap: restored on exit
        contextlib.redirect_stderr(err_buf),  # allow-stdio-swap: restored on exit
    ):
        try:
            mod.main()
        except SystemExit as exit_:
            # Mirror what the interpreter does with the exits main() raises: an
            # int is the status, and a message is printed to stderr with status 1.
            if isinstance(exit_.code, int):
                code = exit_.code
            else:
                print(exit_.code, file=err_buf)
                code = 1
    return Result(code, out_buf.getvalue(), err_buf.getvalue())


def _run_cli(
    diff: Path, out: Path, max_lines: int = 8000, max_shards: int = 0
) -> subprocess.CompletedProcess[str]:
    """Run the script as its caller does — a real process, read by exit status.

    `prepare-pr-review-input.sh` invokes `python3 .github/reviewer/shard-pr-diff.py`
    and branches on the status, so the process boundary is part of the contract
    and cannot be asserted through an in-process call.
    """
    return subprocess.run(
        [sys.executable, *_argv(diff, out, max_lines, max_shards)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )


def _file_section(path: str, body_lines: int) -> str:
    """One `diff --git` section with `body_lines` added lines."""
    body = "".join(f"+line {n}\n" for n in range(body_lines))
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{body_lines} @@\n{body}"
    )


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _shard_bodies(out: Path, manifest: dict) -> list[str]:
    return [(out / s["name"]).read_text(encoding="utf-8") for s in manifest["shards"]]


def test_shards_reunite_byte_for_byte(tmp_path: Path) -> None:
    """Concatenating the shards in order reproduces the input exactly.

    This is what lets a review claim coverage honestly: any line the sharder
    dropped is a line the reviewer never saw while the count says otherwise.
    """
    diff = tmp_path / "d.diff"
    diff.write_text(
        "".join(_file_section(f"f{i}.py", 40) for i in range(20)), encoding="utf-8"
    )
    out = tmp_path / "out"

    assert _run(diff, out, max_lines=100).returncode == 0
    assert "".join(_shard_bodies(out, _manifest(out))) == diff.read_text(
        encoding="utf-8"
    )


def test_no_file_is_split_across_shards(tmp_path: Path) -> None:
    """Every shard starts at a file header, and each file appears in exactly one."""
    diff = tmp_path / "d.diff"
    diff.write_text(
        "".join(_file_section(f"f{i}.py", 30) for i in range(15)), encoding="utf-8"
    )
    out = tmp_path / "out"

    assert _run(diff, out, max_lines=100).returncode == 0
    manifest = _manifest(out)
    for body in _shard_bodies(out, manifest):
        assert body.startswith("diff --git ")
    listed = [f for shard in manifest["shards"] for f in shard["files"]]
    assert sorted(listed) == sorted(f"f{i}.py" for i in range(15))
    assert len(listed) == len(set(listed))


def test_every_shard_respects_the_budget(tmp_path: Path) -> None:
    diff = tmp_path / "d.diff"
    diff.write_text(
        "".join(_file_section(f"f{i}.py", 20) for i in range(30)), encoding="utf-8"
    )
    out = tmp_path / "out"

    assert _run(diff, out, max_lines=200).returncode == 0
    manifest = _manifest(out)
    assert manifest["shards"], "expected at least one shard"
    for shard in manifest["shards"]:
        assert shard["lines"] <= 200
        assert shard["oversize"] is False


def test_a_single_oversized_file_is_kept_whole_and_flagged(tmp_path: Path) -> None:
    """A file larger than the budget gets its own shard, flagged — never split.

    Non-vacuity for the atomicity rule: without this branch the packer would have
    to either split the file or drop it, and both would pass the reunion test
    alone (a split still reunites). Only this case distinguishes them.
    """
    diff = tmp_path / "d.diff"
    diff.write_text(
        _file_section("small.py", 10) + _file_section("huge.py", 500),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    assert _run(diff, out, max_lines=100).returncode == 0
    manifest = _manifest(out)
    huge = [s for s in manifest["shards"] if "huge.py" in s["files"]]
    assert len(huge) == 1, "the oversized file must live in exactly one shard"
    assert huge[0]["files"] == ["huge.py"], "it must not be packed with anything else"
    assert huge[0]["oversize"] is True
    assert huge[0]["lines"] > 100


def test_a_path_containing_spaces_is_reported_whole(tmp_path: Path) -> None:
    """The b-side path is parsed from the right, so a space in it survives."""
    diff = tmp_path / "d.diff"
    diff.write_text(_file_section("dir/a file.py", 5), encoding="utf-8")
    out = tmp_path / "out"

    assert _run(diff, out).returncode == 0
    assert _manifest(out)["shards"][0]["files"] == ["dir/a file.py"]


def test_an_added_line_that_looks_like_a_header_does_not_split(tmp_path: Path) -> None:
    """`+++ b/x` as diff CONTENT must not be mistaken for a file boundary.

    A PR that edits a diff fixture (this repo has several) adds lines beginning
    `+++ b/`. Splitting on those would fabricate shards whose reunion still
    matches, so only the file COUNT catches it.
    """
    diff = tmp_path / "d.diff"
    diff.write_text(
        "diff --git a/fixture.diff b/fixture.diff\n"
        "--- a/fixture.diff\n+++ b/fixture.diff\n"
        "@@ -0,0 +1,3 @@\n"
        "+--- a/inner.py\n"
        "++++ b/inner.py\n"
        "+@@ -1 +1 @@\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    assert _run(diff, out).returncode == 0
    manifest = _manifest(out)
    assert manifest["total_files"] == 1
    assert manifest["shards"][0]["files"] == ["fixture.diff"]


def test_a_non_diff_input_fails_loud(tmp_path: Path) -> None:
    """No file headers means the input is not a diff — refuse, never emit zero
    shards that a caller would read as 'reviewed everything'."""
    diff = tmp_path / "d.diff"
    diff.write_text("this is not a diff\n", encoding="utf-8")

    result = _run(diff, tmp_path / "out")
    assert result.returncode != 0
    assert "not a unified diff" in result.stderr

    # Through a real process too: the caller shell reads an exit status, so the
    # refusal has to survive the interpreter boundary, not just the call.
    cli = _run_cli(diff, tmp_path / "out-cli")
    assert cli.returncode != 0
    assert "not a unified diff" in cli.stderr


def test_over_max_shards_refuses_with_its_own_exit_code(tmp_path: Path) -> None:
    """Too big to shard exits 3 (not 1) and writes NOTHING.

    The caller routes 3 to the human-review notice and any other non-zero to a red
    job, so the two must not collide; and a half-written shard set would read to a
    later step as a complete one.
    """
    diff = tmp_path / "d.diff"
    diff.write_text(
        "".join(_file_section(f"f{i}.py", 20) for i in range(10)), encoding="utf-8"
    )
    out = tmp_path / "out"

    result = _run(diff, out, max_lines=100, max_shards=2)
    assert result.returncode == 3, result.stderr
    assert "over --max-shards=2" in result.stderr
    assert not out.exists(), "an over-budget run must leave no shards behind"

    # The distinct code is only useful if the process reports it, and that is
    # what `prepare-pr-review-input.sh` branches on.
    assert (
        _run_cli(diff, tmp_path / "out-cli", max_lines=100, max_shards=2).returncode
        == 3
    )

    # Non-vacuity: the same input under a budget that fits still succeeds, so the
    # refusal is the bound firing rather than the input being unshardable.
    assert _run(diff, out, max_lines=100, max_shards=24).returncode == 0


def test_content_before_the_first_file_header_rides_with_shard_0(
    tmp_path: Path,
) -> None:
    """A `gh pr diff` preamble belongs to the first shard, not to nothing.

    Dropping it would still let every shard start at a file header and every
    file appear once — the reunion assertion is the only one that notices.
    """
    preamble = "Some `gh pr diff` context\nabout the change\n"
    diff = tmp_path / "d.diff"
    diff.write_text(
        preamble + "".join(_file_section(f"f{i}.py", 40) for i in range(6)),
        encoding="utf-8",
    )
    out = tmp_path / "out"

    assert _run(diff, out, max_lines=100).returncode == 0
    bodies = _shard_bodies(out, _manifest(out))
    assert bodies[0].startswith(preamble)
    assert "".join(bodies) == diff.read_text(encoding="utf-8")


def test_packing_nothing_yields_no_shards() -> None:
    """No file sections must yield zero shards, never one empty shard.

    An empty shard would be dispatched as a review leg that reads nothing and
    reports success, which is the coverage line lying by one shard.
    """
    assert mod.pack([], 100) == []


def test_a_header_without_a_b_side_still_names_the_file() -> None:
    """A header the `a/x b/y` parse cannot split names something, never crashes.

    The manifest's file list feeds the review's coverage line, so an unparseable
    header must degrade to the raw path rather than take the run down with it.
    """
    assert mod.file_path_of(["diff --git odd-header\n"]) == "odd-header"


def test_github_output_carries_the_matrix(tmp_path: Path) -> None:
    diff = tmp_path / "d.diff"
    diff.write_text(
        "".join(_file_section(f"f{i}.py", 20) for i in range(10)), encoding="utf-8"
    )
    out = tmp_path / "out"
    gh_out = tmp_path / "gh-output"
    gh_out.touch()

    assert _run(diff, out, max_lines=100, GITHUB_OUTPUT=str(gh_out)).returncode == 0
    written = dict(
        line.split("=", 1)
        for line in gh_out.read_text(encoding="utf-8").splitlines()
        if line
    )
    shards = json.loads(written["shards"])
    assert int(written["shard_count"]) == len(shards)
    assert shards == [s["name"] for s in _manifest(out)["shards"]]


def _real_git_diff(repo: Path, files: int, lines_per_file: int) -> bytes:
    """A genuine `git diff` carrying the shapes a synthetic fixture never has.

    Built by driving real git rather than pinning a commit from this repo's
    history: a shallow clone (which is what CI checks out) cannot reach a historical
    sha, so that fixture skipped in exactly the environment the test exists to
    guard. Same generator as production — `git diff` — and it always runs.
    """
    run = lambda *a: subprocess.run(  # noqa: E731 - a local alias, not an API
        ["git", "-C", str(repo), *a], check=True, capture_output=True
    )
    repo.mkdir(parents=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")

    for index in range(files):
        (repo / f"f{index}.py").write_text(
            "".join(f"line {n}\n" for n in range(lines_per_file)), encoding="utf-8"
        )
    (repo / "renamed.py").write_text("keep me\n", encoding="utf-8")
    (repo / "deleted.py").write_text("gone\n", encoding="utf-8")
    (repo / "mode.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (repo / "blob.bin").write_bytes(bytes(range(256)) * 8)
    run("add", "-A")
    run("commit", "-qm", "base")

    # The shapes: a rename, a deletion, a mode change, a binary edit, and ordinary
    # content churn — each a boundary a naive per-file splitter gets wrong.
    for index in range(files):
        # Rewrite each file wholesale, not a single line: a one-line edit yields a
        # hunk of ~7 lines, far under the shard budget, so the packing this test
        # exists to exercise would never run.
        (repo / f"f{index}.py").write_text(
            "".join(f"changed {n}\n" for n in range(lines_per_file)), encoding="utf-8"
        )
    run("mv", "renamed.py", "renamed-to.py")
    run("rm", "-q", "deleted.py")
    (repo / "mode.sh").chmod(0o755)
    (repo / "blob.bin").write_bytes(bytes(reversed(range(256))) * 8)
    run("add", "-A")
    run("commit", "-qm", "churn")

    return subprocess.run(
        ["git", "-C", str(repo), "diff", "HEAD~1", "HEAD"],
        check=True,
        capture_output=True,
    ).stdout


@pytest.mark.parametrize("max_lines", [8000])
def test_against_a_real_oversized_git_diff(tmp_path: Path, max_lines: int) -> None:
    """A real, oversized `git diff` splits and reunites with nothing lost.

    Synthetic sections are uniform; a real diff carries renames, mode changes,
    binary stubs, and deletions, which is where a boundary parser actually breaks.
    """
    diff = tmp_path / "real.diff"
    diff.write_bytes(_real_git_diff(tmp_path / "repo", files=15, lines_per_file=400))
    assert diff.read_text(encoding="utf-8", errors="replace").count("\n") > max_lines, (
        "the fixture must exceed the shard budget, or this exercises no packing"
    )
    out = tmp_path / "out"

    assert _run(diff, out, max_lines=max_lines).returncode == 0
    manifest = _manifest(out)
    assert len(manifest["shards"]) > 1, "an oversized diff must produce several shards"
    assert "".join(_shard_bodies(out, manifest)) == diff.read_text(
        encoding="utf-8", errors="replace"
    )
    for shard in manifest["shards"]:
        assert shard["oversize"] or shard["lines"] <= max_lines
    listed = [f for shard in manifest["shards"] for f in shard["files"]]
    assert len(listed) == manifest["total_files"] == len(set(listed))
    # The non-uniform shapes survived the split as whole, distinct files.
    assert {"renamed-to.py", "deleted.py", "mode.sh", "blob.bin"} <= set(listed)


# The model each shard is read with. The tier exists to spend the top model where
# a mistake is expensive, so every case here asserts which side of that line a
# shard lands on — never merely that some model was written.


def test_no_low_model_leaves_every_shard_on_the_callers_model(tmp_path: Path) -> None:
    """The default, and the whole behaviour of a caller that never opts in."""
    diff = tmp_path / "d.diff"
    diff.write_text(_file_section("docs/a.md", 5) + _file_section("bin/run.py", 5))
    out = tmp_path / "out"
    assert _run(diff, out, max_lines=8, tier={"model": "high-1"}).returncode == 0
    assert {s["model"] for s in _manifest(out)["shards"]} == {"high-1"}


def test_a_shard_is_low_only_when_every_file_in_it_qualifies(tmp_path: Path) -> None:
    """A shard packs whole files, so one source file rides along with a doc change.
    Downgrading that shard would read the source file at the cheaper price without
    anything saying so."""
    diff = tmp_path / "d.diff"
    diff.write_text(
        _file_section("docs/a.md", 5)
        + _file_section("docs/b.md", 5)
        + _file_section("sandbox-policy/egress.py", 5)
    )
    out = tmp_path / "out"
    # max_lines forces one file per shard, then a mixed shard is built by hand below.
    tier = {"model": "high-1", "model_low": "low-1", "low_tier_paths": r"^docs/"}
    assert _run(diff, out, max_lines=8, tier=tier).returncode == 0
    by_file = {s["files"][0]: s["model"] for s in _manifest(out)["shards"]}
    assert by_file == {
        "docs/a.md": "low-1",
        "docs/b.md": "low-1",
        "sandbox-policy/egress.py": "high-1",
    }
    # The mixed shard, through the same function the manifest is written from.
    assert (
        mod.model_for(
            ["docs/a.md", "sandbox-policy/egress.py"],
            ("high-1", "low-1"),
            r"^docs/",
            False,
        )
        == "high-1"
    )


def test_a_bulk_diff_reads_every_shard_with_the_low_model(tmp_path: Path) -> None:
    """The mechanical sweep: 136,480 lines of it cost $103.84 to read at the top
    model's price. Past the caller's bulk threshold the read still happens, at the
    cheaper price, whatever the paths say."""
    diff = tmp_path / "d.diff"
    diff.write_text(_file_section("sandbox-policy/egress.py", 20))
    out = tmp_path / "out"
    tier = {
        "model": "high-1",
        "model_low": "low-1",
        "low_tier_paths": r"^docs/",
        "bulk_lines": "5",
    }
    assert _run(diff, out, max_lines=8000, tier=tier).returncode == 0
    assert {s["model"] for s in _manifest(out)["shards"]} == {"low-1"}
    # And below the threshold the same diff keeps the full-price read.
    out2 = tmp_path / "out2"
    assert (
        _run(diff, out2, max_lines=8000, tier={**tier, "bulk_lines": "500"}).returncode
        == 0
    )
    assert {s["model"] for s in _manifest(out2)["shards"]} == {"high-1"}
