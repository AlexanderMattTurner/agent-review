"""Split a unified diff into per-file sections.

Shared by the PR reviewer's input pipeline: the sharder packs these sections into
shards, and the artifact elider replaces the body of some of them. Both must
agree on where one file's section ends and the next begins, so the parse lives
here rather than in either caller.
"""

# `git diff` starts every file's section with this; it is the only reliable
# boundary. A `+++ b/...` line can be forged by a diff that ADDS a line reading
# "+++ b/x", whereas "diff --git" only ever appears at column 0 of a real header.
FILE_HEADER = "diff --git "


def split_into_files(diff_text: str) -> tuple[list[str], list[list[str]]]:
    """Split a unified diff into per-file line groups.

    Returns (preamble, files): `preamble` is any content before the first file
    header (normally empty), and `files` is one list of lines per file section.
    """
    lines = diff_text.splitlines(keepends=True)
    preamble: list[str] = []
    files: list[list[str]] = []
    for line in lines:
        if line.startswith(FILE_HEADER):
            files.append([line])
        elif files:
            files[-1].append(line)
        else:
            preamble.append(line)
    return preamble, files


def unquote_path(quoted: str) -> str:
    """Git's C-style quoting, decoded back to the name the file actually has.

    `\\303\\251` is one UTF-8 character written as two octal BYTES, not two code
    points. `unicode_escape` turns each escape into a code point below 256, so
    the round trip back through latin-1 recovers the original bytes to decode.
    """
    escaped = quoted.encode("utf-8", "backslashreplace").decode(
        "unicode_escape", "replace"
    )
    return escaped.encode("latin-1", "replace").decode("utf-8", "replace")


def file_path_of(section: list[str]) -> str:
    """The b-side path from a `diff --git a/x b/y` header, for the manifest.

    Split from the right: a path containing a space makes a left-anchored parse
    ambiguous, but the b-side is always the final token. Git QUOTES a path
    holding a non-ASCII byte, a quote, a backslash or a control character, and
    writes it as `"b/f\\303\\251.py"`. Callers compare this against the plain
    name GitHub's compare API reports, so the quoted form is decoded here: a
    caller matching the raw header tail drops that file's section silently.
    """
    header = section[0].rstrip("\n")
    tail = header[len(FILE_HEADER) :]
    quoted = tail.rfind(' "b/')
    if quoted != -1 and tail.endswith('"'):
        return unquote_path(tail[quoted + 4 : -1])
    b_side = tail.rsplit(" b/", 1)
    return b_side[-1] if len(b_side) == 2 else tail
