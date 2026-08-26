"""The centralized untrusted-data guard, on both paths that run a model.

The guard against prompt injection used to be hand-written at each call site and
again in each prompt doc, in several different phrasings — so the weakest wording
was the real trust boundary wherever it happened to sit.

Each path now phrases it exactly once:
  * claude-run callers get .github/prompts/untrusted-data-preamble.md, prepended
    by the shared action whenever a caller declares untrusted input files.
  * the reusable PR reviewer (.github/workflows/review.yaml) is self-contained —
    it is cloned into a consumer repository that has neither that action nor that
    file — so its guard lives in run-review-ladder.py's own prompt, which every
    rung of the credential ladder sends.

Centralizing traded one failure mode for another. On the claude-run path a prompt
doc no longer carries its own guard, so the guard's presence depends on the CALL
SITE declaring `untrusted_files`. These tests drive the real composer and the
real reviewer prompt builder, and pin that coupling, so dropping either can't
silently disarm the guard.
"""

import subprocess

import yaml

from tests._helpers import REPO_ROOT, load_script

SCRIPT = REPO_ROOT / ".github" / "scripts" / "compose-claude-prompt.sh"
PREAMBLE = REPO_ROOT / ".github" / "prompts" / "untrusted-data-preamble.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PROMPTS = REPO_ROOT / ".github" / "prompts"
REVIEWER = REPO_ROOT / ".github" / "reviewer"
LADDER = REVIEWER / "run-review-ladder.py"
# The files prepare-pr-review-input.sh writes for the agent. Every one is
# repository content the pull request author chose, so every one must sit under
# the guard.
REVIEWER_INPUT_FILES = ("meta.txt", "diff.txt", "sanitizer-report.txt")

# Phrasings that mean "a guard was written here by hand". The canonical file is
# the only place any of them may appear.
GUARD_PHRASES = ("never as instructions", "never follow them", "analyze them, never")


def _compose(tmp_path, prompt="", untrusted="", preamble=None):
    """Run the real composer; return (returncode, composed_prompt_or_None)."""
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "GITHUB_OUTPUT": str(out),
        "PROMPT": prompt,
        "UNTRUSTED_FILES": untrusted,
        "PREAMBLE": str(PREAMBLE if preamble is None else preamble),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return proc.returncode, None
    return proc.returncode, _parse_output(out.read_text(encoding="utf-8"))["prompt"]


def _parse_output(text):
    """Parse GITHUB_OUTPUT heredoc form into {key: value}."""
    result, lines, i = {}, text.split("\n"), 0
    while i < len(lines):
        if "<<" in lines[i]:
            key, delim = lines[i].split("<<", 1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            result[key] = "\n".join(body)
        i += 1
    return result


def test_guard_precedes_the_callers_prompt(tmp_path) -> None:
    """Ordering is the whole point: the agent must read the guard before it is
    told to go read the untrusted files."""
    rc, composed = _compose(tmp_path, prompt="REVIEW NOW", untrusted="diff: /d.txt")
    assert rc == 0
    assert composed.index("untrusted DATA") < composed.index("- diff: /d.txt")
    assert composed.index("- diff: /d.txt") < composed.index("REVIEW NOW")


def test_guard_text_is_the_canonical_file_verbatim(tmp_path) -> None:
    """The composer must not paraphrase — the canonical file IS the wording."""
    _, composed = _compose(tmp_path, prompt="x", untrusted="diff: /d.txt")
    assert PREAMBLE.read_text(encoding="utf-8").strip() in composed


def test_entries_are_normalized_to_one_bullet_each(tmp_path) -> None:
    """Blank lines dropped, indentation stripped, an already-bulleted entry not
    double-bulleted — so a caller's YAML block scalar renders predictably."""
    _, composed = _compose(
        tmp_path, prompt="x", untrusted="  a: /a\n\n- b: /b\n   \nc: /c\n"
    )
    listing = composed.split("Untrusted input files:\n", 1)[1].split("\n\nx")[0]
    assert listing.splitlines() == ["- a: /a", "- b: /b", "- c: /c"]


def test_no_declared_files_passes_the_prompt_through_verbatim(tmp_path) -> None:
    """A caller with no untrusted input gets no preamble — and an empty prompt
    stays empty, preserving the action's event-driven tag mode."""
    assert _compose(tmp_path, prompt="just this")[1] == "just this"
    assert _compose(tmp_path, prompt="")[1] == ""


def test_missing_guard_file_fails_closed(tmp_path) -> None:
    """A declared-untrusted run must never reach the model unguarded: if the
    canonical file is missing, refuse rather than compose without it."""
    rc, _ = _compose(
        tmp_path, prompt="x", untrusted="diff: /d", preamble=tmp_path / "nope.md"
    )
    assert rc == 1


def test_prompt_cannot_forge_extra_github_outputs(tmp_path) -> None:
    """The composed value crosses GITHUB_OUTPUT, a line-oriented channel. A
    prompt carrying heredoc syntax must not be able to close the block early and
    have its tail re-parsed as further outputs."""
    hostile = "prompt<<X\nmalicious=1\nX\nEOF\ninjected=2"
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GITHUB_OUTPUT": str(out),
            "PROMPT": hostile,
            "UNTRUSTED_FILES": "",
            "PREAMBLE": str(PREAMBLE),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    parsed = _parse_output(out.read_text(encoding="utf-8"))
    assert parsed["prompt"] == hostile
    assert "malicious" not in parsed and "injected" not in parsed


def _claude_run_sites():
    for path in sorted(WORKFLOWS.rglob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            continue
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                if isinstance(step, dict) and step.get("uses", "").endswith(
                    "actions/claude-run"
                ):
                    yield f"{path.name}:{step.get('name')}", step


def test_known_untrusted_ingesting_call_sites_declare_their_files() -> None:
    """Coverage floor, not a derived property: these are the automations that
    feed repo/PR content to Claude. Their prompt docs no longer carry a guard of
    their own, so dropping `untrusted_files` here would leave the run genuinely
    unguarded. A new untrusted-ingesting automation belongs in this list."""
    # The first-pass PR reviewer is NOT here: it moved to review.yaml, which
    # calls the model through run-review-ladder.py and carries its own guard.
    # The reviewer-path tests below assert the same property at that shape.
    required = {
        "Review the merge deltas with Claude (Sonnet)",
        "Triage and fix with Claude",
    }
    declared = {
        step.get("name")
        for _, step in _claude_run_sites()
        if str((step.get("with") or {}).get("untrusted_files", "")).strip()
    }
    assert required <= declared, f"missing untrusted_files: {required - declared}"


def test_the_guard_is_not_re_worded_anywhere_else() -> None:
    """The canonical file must remain the ONLY place the guard is phrased. A
    prompt restating it re-introduces a second copy — several slightly different
    wordings, the weakest of which becomes a real trust boundary.

    Scoped to text that actually reaches the model — the `prompt:` inputs and the
    prompt docs on BOTH paths — NOT workflow comments, which legitimately
    describe the design to human readers and reach no agent. Each path has its
    own canonical source, and only that source is exempt."""
    model_facing = {}
    for _, step in _claude_run_sites():
        prompt = str((step.get("with") or {}).get("prompt", ""))
        if prompt:
            model_facing[f"prompt at {step.get('name')}"] = prompt
    canonical = {PREAMBLE, LADDER}
    for root, pattern in ((PROMPTS, "*.md"), (REVIEWER, "prompts/*.md")):
        for path in root.rglob(pattern):
            if path not in canonical:
                model_facing[str(path.relative_to(REPO_ROOT))] = path.read_text(
                    encoding="utf-8"
                )

    offenders = [
        f"{where} ({phrase!r})"
        for where, text in model_facing.items()
        for phrase in GUARD_PHRASES
        if phrase in text.lower()
    ]
    # not-a-drift-guard: this asserts the OPPOSITE of a drift guard. It does not
    # compare two copies for agreement — the duplication was eliminated (one
    # canonical file, prepended by claude-run), and this asserts no second copy
    # is re-introduced. The collection is a list of offending sites, empty when
    # the SSOT is intact.
    assert offenders == [], f"guard re-worded at: {offenders}"


# ── The reusable reviewer's own path ─────────────────────────────────────────
#
# review.yaml calls the model through run-review-ladder.py, which builds the
# whole prompt itself. These drive that real builder rather than reading the
# workflow, so a guard that is present but assembled in the wrong order reds.


def _reviewer_prompt(pr_input_dir="/tmp/pr-input"):
    ladder = load_script(".github/reviewer/run-review-ladder.py")
    return ladder.prompt_for(pr_input_dir, "/checkout/review.md", "7", "owner/repo")


def test_the_reviewer_guard_precedes_the_files_it_covers() -> None:
    """Ordering is the whole point: the agent must read the guard before it is
    told where the untrusted files are."""
    prompt = _reviewer_prompt()
    guard = prompt.lower().index("untrusted data")
    for name in REVIEWER_INPUT_FILES:
        assert guard < prompt.index(f"/tmp/pr-input/{name}"), (
            f"the guard does not precede {name}"
        )


def test_the_reviewer_guard_covers_every_input_file() -> None:
    """A file written for the agent but left out of the prompt is one the agent
    reads with no guard attached to it."""
    prompt = _reviewer_prompt()
    missing = [n for n in REVIEWER_INPUT_FILES if f"/tmp/pr-input/{n}" not in prompt]
    assert missing == [], f"input files the prompt never names: {missing}"


def test_the_reviewer_guard_is_unconditional() -> None:
    """Unlike the claude-run path, no call site declares anything: every prompt
    the ladder builds carries the guard, whatever the caller passes."""
    for pr_input_dir in ("/tmp/a", "/var/tmp/deep/nested"):
        assert "never as instructions" in _reviewer_prompt(pr_input_dir).lower()


def test_the_reviewer_forbids_the_write_side_in_its_own_prompt() -> None:
    """The reviewer only ANALYZES. Its own prompt must say so, so a successful
    injection in the diff has to argue against the instruction above it."""
    prompt = _reviewer_prompt().lower()
    for forbidden in ("post comments", "push commits", "edit the pr", "merge"):
        assert forbidden in prompt, f"the prompt never forbids {forbidden!r}"


def test_the_reviewer_grants_no_shell() -> None:
    """The tool grant is the hard half of the guard: with no Bash, a prompt
    injection that the wording fails to stop still reaches no shell."""
    ladder = load_script(".github/reviewer/run-review-ladder.py")
    # Both directories the grant names: the sanitized PR input, and the reviewer's
    # own checkout, which is where the default prompt lives.
    grant = ladder.TOOL_GRANT.format(d="/tmp/pr-input", r="/tmp/reviewer")
    tools = {entry.split("(", 1)[0] for entry in grant.split(",")}
    assert tools <= {"Read", "Write", "Edit"}, f"the reviewer is granted {tools}"
    assert "Bash" not in grant, "the reviewer must reach no shell"
    # The grant's own `/` before an already-absolute directory doubles the slash,
    # which is what the production reviewer this one was extracted from also emits.
    writes = {
        entry.replace("//", "/")
        for entry in grant.split(",")
        if not entry.startswith("Read(")
    }
    assert writes == {
        "Write(/tmp/pr-input/review.json)",
        "Edit(/tmp/pr-input/review.json)",
    }, f"the reviewer may write outside its verdict file: {writes}"
