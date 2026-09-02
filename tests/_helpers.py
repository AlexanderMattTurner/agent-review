"""Shared helpers used by multiple test modules.

Lives in a regular module (not `conftest.py`) so it can be imported directly
without manipulating `sys.path` or relying on the conftest plugin loader.
"""

import os
import re
import shutil
import subprocess
import types
from importlib import util as importlib_util
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _repo_root() -> Path:
    """Repo root from git itself, anchored at this file's directory (not the
    caller's cwd), so moving test files can never silently repoint it the way
    depth-based parent-walking does."""
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(toplevel)


REPO_ROOT = _repo_root()

GIT_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git_env() -> dict[str, str]:
    """Environment for running git in test sandboxes."""
    return {**os.environ, **GIT_IDENTITY_ENV}


def init_test_repo(path: Path) -> None:
    """Init a throwaway repo with signing/hooks disabled so fixtures can commit
    in any environment (including CI runners with enforced commit signing)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    for k, v in [
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("user.name", "t"),
        ("user.email", "t@t"),
        ("core.hooksPath", "/dev/null"),
    ]:
        subprocess.run(["git", "config", "--local", k, v], cwd=path, check=True)


def commit_all(repo: Path, message: str = "fixture") -> str:
    """Stage everything and create a commit; returns the resulting SHA."""
    env = git_env()
    subprocess.run(["git", "add", "-A"], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo,
        env=env,
        check=True,
    )
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return sha.stdout.strip()


def commit_files(repo: Path, files: dict[str, str], message: str) -> str:
    """Write `files` (repo-relative path -> content) into `repo`, stage everything
    and commit; returns the new SHA. Parent dirs are created, so a case can add a
    file in a directory the fixture repo does not have yet."""
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return commit_all(repo, message)


def git_out(repo: Path, *args: str) -> str:
    """Run git in `repo` with the test identity env and return its stripped
    stdout, raising on a non-zero exit."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def run_capture(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    """`subprocess.run` with the capture_output/text/check defaults every test
    uses. `kwargs` (env, cwd, input, ...) are forwarded verbatim."""
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


_SCRIPT_DIRS = [
    REPO_ROOT / ".github" / "scripts",
    REPO_ROOT / ".claude" / "hooks",
    REPO_ROOT / ".hooks",
]


def copy_script_to(script_name: str, dest_dir: Path) -> Path:
    """Copy a repo script into `dest_dir`, preserving the executable bit."""
    for src_dir in _SCRIPT_DIRS:
        src = src_dir / script_name
        if src.exists():
            dest = dest_dir / script_name
            shutil.copy2(src, dest)
            dest.chmod(0o755)
            return dest
    raise FileNotFoundError(f"Could not find {script_name} in any known location")


def load_script_module(name: str, path: Path) -> types.ModuleType:
    """Import the script at PATH under module NAME so its functions can be driven
    in-process, whatever its filename — a hyphenated `.github/reviewer/*.py` is
    not a legal import name. Naming the loader explicitly (rather than deriving
    one from the path) is what makes the spec unconditional, so the caller gets a
    module or an exception, never a silently half-built one."""
    loader = SourceFileLoader(name, str(path))
    spec = importlib_util.spec_from_loader(loader.name, loader)
    # A SourceFileLoader always yields a spec; None means the import machinery
    # refused this path, and executing nothing would hand back an empty module
    # whose missing attributes surface much later as a confusing AttributeError.
    if spec is None:
        raise ImportError(f"no module spec for {path}")
    module = importlib_util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def load_script(rel: str) -> types.ModuleType:
    """Import the script at REPO_ROOT/<rel> in-process (see load_script_module),
    under a module name derived from the filename: suffix dropped, every
    non-identifier character mapped to `_` (`pr/files-to-diff.py` -> files_to_diff)."""
    path = REPO_ROOT / rel
    return load_script_module(re.sub(r"\W", "_", path.stem), path)


def workflow_jobs(workflow_path: Path) -> dict:
    """The `jobs:` mapping of a workflow file, via the real YAML parser."""
    import yaml

    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))["jobs"]


def current_path() -> str:
    """The live PATH, so a hermetic test env can still resolve git/bash."""
    return os.environ.get("PATH", "/usr/bin:/bin")


def reviewer_marker(name: str) -> str:
    """A marker string out of .github/reviewer/lib/pr-reviews.bash, which is the one
    home for it. Read through bash rather than copied here, so a rename reds the
    tests that assert on the marker instead of silently splitting them from it."""
    lib = REPO_ROOT / ".github" / "reviewer" / "lib" / "pr-reviews.bash"
    proc = subprocess.run(
        ["bash", "-c", 'source "$1"; printf %s "${!2}"', "_", str(lib), name],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout, f"pr-reviews.bash defines no {name}"
    return proc.stdout
