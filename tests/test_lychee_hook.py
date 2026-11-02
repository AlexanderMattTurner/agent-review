"""lychee.sh — the pre-commit link check's installer: it downloads the pinned
release once, refuses a tarball whose digest is not the pinned one, and execs
the cached binary with the hook's arguments.

`curl` is stubbed because the real release is a network download. It copies a
tarball the test built and logs each call. Everything after it — the digest
check, `tar`, the cache and the `exec` — runs for real.
"""

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "lychee.sh"
PLATFORMS = ("linux_amd64", "linux_arm64", "darwin_amd64", "darwin_arm64")
TARGET_DIRS = {
    "Linux-x86_64": "lychee-x86_64-unknown-linux-gnu",
    "Linux-aarch64": "lychee-aarch64-unknown-linux-gnu",
    "Darwin-x86_64": "lychee-x86_64-apple-darwin",
    "Darwin-arm64": "lychee-aarch64-apple-darwin",
}


def release_tarball() -> bytes:
    """A release-shaped tarball whose `lychee` prints the argv it was given."""
    uname = f"{os.uname().sysname}-{os.uname().machine}"
    body = b'#!/usr/bin/env bash\necho "fake-lychee $*"\n'
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{TARGET_DIRS[uname]}/lychee")
        info.size = len(body)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A tree holding the real script, a stub `curl` that serves `release.tgz`
    and logs each call, and an empty cache."""
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    (tmp_path / ".github" / "scripts" / "lychee.sh").write_bytes(SCRIPT.read_bytes())
    (tmp_path / "release.tgz").write_bytes(release_tarball())
    (tmp_path / "bin").mkdir()
    curl = tmp_path / "bin" / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >>"{tmp_path}/curl.log"\n'
        'while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && out="$2"; shift; done\n'
        f'cp "{tmp_path}/release.tgz" "$out"\n',
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return tmp_path


def write_pins(sandbox: Path, digest: str, version: str = "9.9.9") -> None:
    lines = [f"LYCHEE_VERSION={version}"]
    lines += [f"LYCHEE_SHA256_{p}={digest}" for p in PLATFORMS]
    (sandbox / ".github" / "tool-versions.sh").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def run(sandbox: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(sandbox / ".github" / "scripts" / "lychee.sh"), *args],
        env={
            **os.environ,
            "PATH": f"{sandbox / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "XDG_CACHE_HOME": str(sandbox / "cache"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def curl_calls(sandbox: Path) -> list[str]:
    log = sandbox / "curl.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_downloads_once_then_runs_the_cached_binary(sandbox: Path) -> None:
    write_pins(
        sandbox, hashlib.sha256((sandbox / "release.tgz").read_bytes()).hexdigest()
    )

    first = run(sandbox, "--offline", "a.md")
    second = run(sandbox, "--offline", "b.md")

    assert first.returncode == 0
    assert first.stdout == "fake-lychee --offline a.md\n"
    assert second.returncode == 0
    assert second.stdout == "fake-lychee --offline b.md\n"
    calls = curl_calls(sandbox)
    assert len(calls) == 1
    assert (
        "https://github.com/lycheeverse/lychee/releases/download/lychee-v9.9.9/"
        in calls[0]
    )


def test_refuses_a_tarball_whose_digest_is_not_pinned(sandbox: Path) -> None:
    write_pins(sandbox, "0" * 64)

    result = run(sandbox, "a.md")

    assert result.returncode == 1
    assert "pins " + "0" * 64 in result.stderr
    assert "fake-lychee" not in result.stdout
    assert not list((sandbox / "cache").rglob("lychee"))


def test_refuses_before_downloading_when_the_pin_is_empty(sandbox: Path) -> None:
    write_pins(sandbox, "")

    result = run(sandbox, "a.md")

    assert result.returncode == 1
    assert "unset in .github/tool-versions.sh" in result.stderr
    assert curl_calls(sandbox) == []
