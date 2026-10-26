"""Behavioral tests for .github/reviewer/merge-group-gate.sh — the merge-queue leg
of the review gate.

The queue ref names only the LAST pull request of a batch. The script must judge
every pull request the batch carries and red on any red one. It runs the real
merge-group-prs.sh and review-findings-gate.sh against a `gh` stub that applies
their own `--jq` filters to canned payloads, so the batch walk and the verdict
are exercised rather than re-implemented.
"""

# covers: .github/reviewer/merge-group-gate.sh

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "reviewer" / "merge-group-gate.sh"
SEVERITIES = REPO_ROOT / "config" / "review-severities.json"

# The batch: the queue ref names #7, and the compare also carries #5's commit.
# Commit aaa is also on #9, a stacked draft that is NOT in the merge queue.
REF = "refs/heads/gh-readonly-queue/main/pr-7-abcdef1"
SHAS = {"aaa": [5, 9], "bbb": [7]}
QUEUED = [5, 7, 12]

STUB = r"""#!/usr/bin/env bash
filter=""; pr=""; path=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --jq) filter="$2"; shift 2 ;;
    -F) [[ "$2" == pr=* ]] && pr="${2#pr=}"; shift 2 ;;
    -f) shift 2 ;;
    api|graphql|--paginate) [[ "$1" == graphql ]] && path=graphql; shift ;;
    *) path="$1"; shift ;;
  esac
done
case "$path" in
  */compare/*) jq -r "$filter" "$D/compare.json"; exit ;;
  */commits/*/pulls) sha="${path#*/commits/}"; jq -r "$filter" "$D/pulls-${sha%/pulls}.json"; exit ;;
  */pulls/*) jq -r "$filter" <<<'{"user": {"login": "pr-author"}}'; exit ;;
esac
if [[ "$path" == graphql ]]; then
  case "$filter" in
    *reviewThreads.nodes*) jq -r "$filter" "$D/threads-$pr.json"; exit ;;
    *mergeQueue*) jq -r "$filter" "$D/queue.json"; exit ;;
    *reviews.nodes*) jq -r "$filter" "$D/reviews.json"; exit ;;
  esac
fi
exit 0
"""


def _page(key: str, nodes: list) -> dict:
    return {"data": {"repository": {"pullRequest": {key: {"nodes": nodes}}}}}


def _run(tmp_path: Path, red_prs: set[int]) -> subprocess.CompletedProcess:
    (tmp_path / "compare.json").write_text(
        json.dumps({"commits": [{"sha": s} for s in SHAS]})
    )
    for sha, numbers in SHAS.items():
        (tmp_path / f"pulls-{sha}.json").write_text(
            json.dumps([{"number": n} for n in numbers])
        )
    review = {
        "state": "COMMENTED",
        "body": "Automated review.",
        "author": {"login": "github-actions"},
        "submittedAt": "2026-01-01T00:00:00Z",
    }
    (tmp_path / "reviews.json").write_text(json.dumps(_page("reviews", [review])))
    entries = [{"pullRequest": {"number": n}} for n in QUEUED]
    (tmp_path / "queue.json").write_text(
        json.dumps(
            {"data": {"repository": {"mergeQueue": {"entries": {"nodes": entries}}}}}
        )
    )
    for pr in (5, 7, 9):
        threads = []
        if pr in red_prs:
            body = "<!-- severity: blocking -->\nthis breaks"
            author = {"login": "github-actions"}
            threads = [
                {
                    "isResolved": False,
                    "resolvedBy": None,
                    "path": "a.py",
                    "line": 1,
                    "comments": {"nodes": [{"body": body, "author": author}]},
                }
            ]
        (tmp_path / f"threads-{pr}.json").write_text(
            json.dumps(_page("reviewThreads", threads))
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(STUB)
    (bin_dir / "gh").chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/local/bin",
            "D": str(tmp_path),
            "GH_TOKEN": "t",
            "GH_REPO": "o/r",
            "MG_REF": REF,
            "MG_HEAD_SHA": "abcdef1",
            "MG_BASE_REF": "refs/heads/main",
            "SEVERITY_CONFIG": str(SEVERITIES),
            "RETRY_BASE_DELAY": "0",
        },
    )


@pytest.mark.parametrize(
    ("red_prs", "rc"), [(set(), 0), ({5}, 1), ({7}, 1), ({5, 7}, 1), ({9}, 0)]
)
def test_every_pull_request_in_the_batch_is_judged(
    tmp_path: Path, red_prs: set[int], rc: int
) -> None:
    """#5 is not the one the ref names, so a leg that read the ref alone would
    merge it past its open finding. Both are judged, and the log names each red
    one. #9 shares a commit but is not in the queue, so its finding must not
    eject the batch."""
    proc = _run(tmp_path, red_prs)
    assert proc.returncode == rc, proc.stderr
    assert proc.stderr.count("review-findings gate on o/r#") == 2, proc.stderr
    assert "o/r#9" not in proc.stderr, proc.stderr
    if rc:
        named = proc.stderr.split("red for pull request(s): ")[1].split()
        assert sorted(int(n) for n in named) == sorted(red_prs)
