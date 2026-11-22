"""Tests for .github/scripts/check-token-scope.sh scope validation logic.

The required-env smoke test (test_required_env.py) only checks that the script
exits non-zero when TOKEN is unset. This module covers the actual scope-check
behaviour by injecting a fake `curl` that returns controlled header output.
"""

import os
import subprocess
from pathlib import Path

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "check-token-scope.sh"


def run_scope_check(
    tmp_path: Path, headers: list[str], curl_exit: int = 0
) -> subprocess.CompletedProcess:
    """Run check-token-scope.sh with a fake curl that emits the given headers.

    The fake honours the flags the script uses: `-D FILE` receives the headers
    (stdout when absent) and `-w '%{http_code}'` prints the code of the last HTTP status line. A failing
    curl writes the lines to stderr, like the real one.
    """
    status = [h.split()[1] for h in headers if h.startswith("HTTP/")][-1:] or ["000"]
    echo_lines = "\n".join(f"echo {h!r}" for h in headers)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        f"""#!/usr/bin/env bash
dump=/dev/stdout write_out=
while [[ $# -gt 0 ]]; do
  case "$1" in -D) dump=$2; shift;; -w) write_out=$2; shift;; esac
  shift
done
if [[ {curl_exit} -ne 0 ]]; then ( {echo_lines} ) >&2; exit {curl_exit}; fi
( {echo_lines} ) >"$dump"
[[ -z $write_out ]] || printf %s {status[0]}
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "TOKEN": "fake-token",
        "TOKEN_SECRET_NAME": "SYNC_PAT_UNDER_TEST",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )


def test_the_status_is_the_apis_not_the_proxys_connect_reply(tmp_path: Path) -> None:
    """Behind an HTTPS proxy the header dump opens with the CONNECT reply, so a
    real 403 read as 200 and the check passed."""
    result = run_scope_check(
        tmp_path,
        [
            "HTTP/1.1 200 Connection Established",
            "",
            "HTTP/2 403",
            "x-ratelimit-remaining: 5",
        ],
    )
    assert result.returncode == 1
    assert "HTTP status '403'" in result.stderr


def test_passes_for_fine_grained_pat(tmp_path: Path) -> None:
    """Fine-grained PATs expose no x-oauth-scopes header → skip check, exit 0."""
    result = run_scope_check(tmp_path, ["HTTP/2 200", "content-type: application/json"])
    assert result.returncode == 0
    assert "skipping" in result.stdout.lower()


def test_passes_for_classic_pat_with_workflow_scope(tmp_path: Path) -> None:
    """Classic PAT with 'workflow' in x-oauth-scopes → exit 0."""
    result = run_scope_check(
        tmp_path,
        ["HTTP/2 200", "x-oauth-scopes: repo, workflow, read:org"],
    )
    assert result.returncode == 0
    assert "workflow" in result.stdout.lower()


def test_fails_for_classic_pat_missing_workflow_scope(tmp_path: Path) -> None:
    """Classic PAT without 'workflow' scope → exit non-zero with clear error."""
    result = run_scope_check(
        tmp_path,
        ["HTTP/2 200", "x-oauth-scopes: repo, read:org"],
    )
    assert result.returncode != 0
    assert "workflow" in result.stdout + result.stderr


def test_fails_when_curl_errors(tmp_path: Path) -> None:
    """Network failure from curl → exit non-zero (must not silently pass)."""
    result = run_scope_check(
        tmp_path,
        ["curl: (6) Could not resolve host: api.github.com"],
        curl_exit=6,
    )
    assert result.returncode != 0


def test_the_error_names_the_secret_the_workflow_reads(tmp_path: Path) -> None:
    """The fix has to land in the secret the workflow reads, so the message names
    that one. A hard-coded name sent users to set a secret nothing consumed."""
    result = run_scope_check(
        tmp_path,
        ["HTTP/2 200", "x-oauth-scopes: repo, read:org"],
    )
    assert result.returncode != 0
    assert "SYNC_PAT_UNDER_TEST" in result.stdout + result.stderr
    assert "TEMPLATE_SYNC_TOKEN " not in result.stdout + result.stderr


def test_rate_limited_token_gets_a_rate_limit_message(tmp_path: Path) -> None:
    """A 403 with x-ratelimit-remaining: 0 must name the rate limit, not scopes."""
    result = run_scope_check(
        tmp_path,
        [
            "HTTP/2 403",
            "x-ratelimit-remaining: 0",
            "x-ratelimit-reset: 1700000000",
        ],
    )
    assert result.returncode != 0
    assert "rate limit" in result.stderr
    assert "SYNC_PAT_UNDER_TEST" in result.stderr


def test_other_http_errors_still_fail_loudly(tmp_path: Path) -> None:
    """A 401 (bad token) is not a rate limit and must not pass as fine-grained."""
    result = run_scope_check(tmp_path, ["HTTP/2 401", "x-ratelimit-remaining: 4999"])
    assert result.returncode != 0
    assert "rate limit" not in result.stderr
    assert "401" in result.stderr
