"""Real HTTPS GitHub API servers, so tests drive the REAL `gh` binary.

Nothing here fakes `gh`. The tests run the real `gh` and the real reviewer
scripts unmodified; what is simulated is GitHub itself — a localhost HTTPS
server holding mutable state and answering the endpoints the mechanism under
test uses. That boundary is what makes the tests worth running: a wrong request
path, a malformed body, a flag the CLI does not accept, or a response shape gh
rejects all surface as a loud failure, none of which an argv-level `gh` stub can
see.

Two mechanics make it work, both observed rather than assumed:

  * gh treats any GH_HOST other than github.com as GitHub Enterprise and talks
    to `https://HOST/api/v3/…`, so the server must serve TLS — plain HTTP is
    refused with "first record does not look like a TLS handshake".
  * gh is a Go program, and Go's x509 loader honours SSL_CERT_FILE, so pointing
    it at a throwaway self-signed cert is enough to be trusted. No verification
    is disabled anywhere.

PROBLEM CLASS — a real-gh fixture that cannot trust its own self-signed CA on
macOS. That second mechanic is LINUX-ONLY: on macOS Go builds its root pool from
the system Security framework and ignores SSL_CERT_FILE, so gh rejects this
server's certificate with `tls: failed to verify certificate: x509: certificate
signed by unknown authority`. `_LocalGitHub` therefore SKIPS itself on darwin,
in its constructor, and skips again on a host with no `gh` at all.

`_LocalGitHub` carries the transport, plus the two things every table would
otherwise restate: `GET /api/v3/meta`, the probe each gh command opens with, and
the 404 that names any path no table modelled — so a script reaching for an
unmodelled endpoint fails loudly instead of reading a plausible empty answer.
Each subclass supplies only its own endpoints, via `resolve`.

`FakePRReviews` — the shared PR-review read in lib/pr-reviews.bash.

`FakeReviewPoster` — the reviewer's posting surface, including the 422 the
degraded path exists for.

`FakePrStatus` — one open PR, its check runs and its review threads.
"""

import functools
import json
import re
import shutil
import ssl
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import pytest
from graphql import parse
from graphql.language import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    VariableNode,
)

from tests._helpers import current_path, run_capture

# gh reads the Enterprise version out of /api/v3/meta and PARSES it, so an empty
# object fails every search on this server with a bare "malformed version:".
GHE_VERSION = "3.14.0"

DARWIN_GH_TLS_SKIP = (
    "gh cannot trust this fixture's self-signed CA on macOS: Go builds its root "
    "pool from the system Security framework there and ignores SSL_CERT_FILE"
)
NO_GH_BINARY_SKIP = "the real `gh` binary every fixture here drives is not on PATH"


def _fragments(document: DocumentNode) -> dict[str, FragmentDefinitionNode]:
    return {
        d.name.value: d
        for d in document.definitions
        if isinstance(d, FragmentDefinitionNode)
    }


def _selections(node, fragments: dict) -> list[FieldNode]:
    """The fields `node` selects, with fragment spreads resolved in place — which
    is where a fragment's connections really nest, and what a text scan of gh's
    query (whose whole projection is one fragment) cannot see."""
    fields: list[FieldNode] = []
    for selection in node.selection_set.selections if node.selection_set else ():
        if isinstance(selection, FieldNode):
            fields.append(selection)
        elif isinstance(selection, FragmentSpreadNode):
            fields.extend(_selections(fragments[selection.name.value], fragments))
        elif isinstance(selection, InlineFragmentNode):
            fields.extend(_selections(selection, fragments))
    return fields


def _operations(document: DocumentNode) -> list[OperationDefinitionNode]:
    return [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]


_NO_CURSOR = object()


def _field_named(node, fragments: dict, name: str) -> FieldNode | None:
    """The first field called `name` anywhere under `node`."""
    for field in _selections(node, fragments):
        if field.name.value == name:
            return field
        found = _field_named(field, fragments, name)
        if found is not None:
            return found
    return None


def _after_cursor(
    document: DocumentNode, fragments: dict, variables: dict, connection: str
) -> object:
    """The `after:` cursor a query binds on `connection`, resolved through the
    variables. `_NO_CURSOR` when the query binds none, which is a client that
    cannot walk past the first page."""
    for operation in _operations(document):
        field = _field_named(operation, fragments, connection)
        if field is None:
            continue
        for argument in field.arguments:
            if argument.name.value != "after":
                continue
            value = argument.value
            if isinstance(value, VariableNode):
                return variables.get(value.name.value)
            return getattr(value, "value", None)
    return _NO_CURSOR


def _pr_list_reply(nodes: list[dict]) -> tuple[int, object]:
    """GitHub's envelope around a one-page `pr list` result. gh parses this, so
    its shape is a contract every server answering a listing must meet."""
    return 200, {
        "data": {
            "repository": {
                "pullRequests": {
                    "totalCount": len(nodes),
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }


@functools.lru_cache(maxsize=8)
def _minted_cert(sans: str, cn: str) -> tuple[bytes, bytes]:
    """The RSA-2048 cert+key bytes for one (SANS, CN) pair, minted once per
    worker process rather than once per test — the keygen alone costs seconds."""
    with tempfile.TemporaryDirectory() as scratch:
        cert, key = Path(scratch) / "cert.pem", Path(scratch) / "key.pem"
        proc = run_capture(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                f"/CN={cn}",
                "-addext",
                f"subjectAltName={sans}",
            ],
            timeout=60,
        )
        assert cert.is_file() and key.is_file(), (
            f"could not mint a localhost cert (openssl rc={proc.returncode}); gh "
            f"only speaks TLS, so this suite cannot run without one:\n{proc.stderr}"
        )
        return cert.read_bytes(), key.read_bytes()


def self_signed_cert(
    dir_: Path, *, sans: str = "DNS:localhost,IP:127.0.0.1", cn: str = "localhost"
) -> Path:
    """A throwaway cert+key pair under DIR_, returning the cert path. The key is
    always written beside the cert as `key.pem`."""
    cert_bytes, key_bytes = _minted_cert(sans, cn)
    cert, key = dir_ / "cert.pem", dir_ / "key.pem"
    cert.write_bytes(cert_bytes)
    key.write_bytes(key_bytes)
    return cert


class _LocalGitHub:
    """A GitHub over TLS on localhost, plus the env that points real `gh` at it.

    Used as a context manager so the socket and serving thread die with the test.
    Subclasses answer requests by overriding `resolve`.
    """

    # Both live on `_current`, a `threading.local`, because ThreadingHTTPServer
    # serves each connection in its own thread and gh opens several at once. Held
    # on the server instead, one request's `parse_qs` and one `resolve`'s `Link`
    # header land in the reply to another request.
    @property
    def query(self) -> dict[str, list[str]]:
        """This thread's request's query string, parsed."""
        return getattr(self._current, "query", {})

    @query.setter
    def query(self, value: dict[str, list[str]]) -> None:
        self._current.query = value

    @property
    def response_headers(self) -> dict[str, str]:
        """Extra headers for this thread's reply — how a server that PAGES
        advertises its next page in `Link`."""
        return getattr(self._current, "response_headers", {})

    @response_headers.setter
    def response_headers(self, value: dict[str, str]) -> None:
        self._current.response_headers = value

    def __init__(self, tmp_path: Path):
        # The one place every real-gh fixture passes through, so this refusal is
        # what keeps a darwin leg from reporting a TLS trust gap as a failure of
        # the script under test.
        if sys.platform == "darwin":
            pytest.skip(DARWIN_GH_TLS_SKIP)
        # A host with no gh runs none of these tests, and a script under test
        # reads an absent gh as "no account" and takes its fallback — so an
        # unskipped test would assert a fallback it never exercised.
        if shutil.which("gh") is None:
            pytest.skip(NO_GH_BINARY_SKIP)
        self.requests: list[tuple[str, str]] = []
        self._current = threading.local()

        (tmp_path / "home").mkdir(exist_ok=True)
        cert = self_signed_cert(tmp_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, tmp_path / "key.pem")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

        self.host = f"localhost:{self._server.server_address[1]}"
        self.env: dict[str, str] = {
            # An explicit PATH and a throwaway HOME: without them a child bash
            # falls back to a default PATH and gh reads the developer's real
            # ~/.config/gh, so the suite's behavior would depend on the host's
            # login state instead of only on this server.
            "PATH": current_path(),
            "HOME": str(tmp_path / "home"),
            "GH_HOST": self.host,
            "GH_TOKEN": "fixture-token",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "SSL_CERT_FILE": str(cert),
        }

    def paged(self, path: str, items: list) -> list:
        """One page of `items`, plus the `Link` header that names the next one.

        GitHub pages every list endpoint and advertises the next page in `Link`;
        `gh api --paginate` follows that header. A server that answered every item
        in one reply would let a caller that forgot `--paginate` pass this suite
        while it truncates against the real GitHub.
        """
        per_page = min(int(self.query.get("per_page", ["30"])[0]), 100)
        page = int(self.query.get("page", ["1"])[0])
        start = (page - 1) * per_page
        if start + per_page < len(items):
            # The next page carries THIS request's whole query, not just the
            # paging pair: a link that dropped the caller's filter would answer
            # page 2 from a different row set.
            params = {name: values[0] for name, values in self.query.items()}
            params |= {"per_page": str(per_page), "page": str(page + 1)}
            nxt = f"https://{self.host}{path}?{urlencode(params)}"
            self.response_headers["Link"] = f'<{nxt}>; rel="next"'
        return items[start : start + per_page]

    def dispatch(self, method: str, path: str, body: dict) -> tuple[int, object]:
        """Answer one request: the GHE probe every gh command opens with, then
        this server's own table, then the 404 that names what nobody modelled."""
        if path == "/api/v3/meta":
            return 200, {"installed_version": GHE_VERSION}
        answer = self.resolve(method, path, body)
        if answer is None:
            return 404, {"message": f"fake GitHub: unmodelled {method} {path}"}
        return answer

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        """The status and JSON body for one request this server models, or None
        to let `dispatch` answer the unmodelled 404."""
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "the fake GitHub thread did not stop"

    def paths(self, method: str) -> list[str]:
        return [path for verb, path in self.requests if verb == method]


class _IssueCommentStore:
    """The four issue-comment endpoints, for every fake a sticky poster drives.

      GET   /api/v3/repos/{o}/{r}/issues/{n}/comments   (find the marked comment)
      GET   /api/v3/repos/{o}/{r}/issues/comments/{id}  (read its current body)
      PATCH /api/v3/repos/{o}/{r}/issues/comments/{id}  (rewrite it in place)
      POST  /api/v3/repos/{o}/{r}/issues/{n}/comments   (first run only)
    """

    def _init_comments(self) -> None:
        self.comments: list[dict] = []
        # Every PATCH this server served, in order: GitHub sends an
        # `issue_comment: edited` webhook for each one, so a test that wants to
        # prove a run woke nobody counts the calls rather than reading bodies.
        self.patched: list[int] = []
        self.fail_listings = False
        self._next_id = 1001

    def add_comment(self, body: str) -> int:
        """Seed an existing comment, standing in for a previous run's post."""
        self._next_id += 1
        self.comments.append({"id": self._next_id, "body": body})
        return self._next_id

    def bodies(self) -> list[str]:
        return [comment["body"] for comment in self.comments]

    def resolve_comment(
        self, method: str, path: str, body: dict
    ) -> tuple[int, object] | None:
        """The answer for a comment path, or None when PATH is not one."""
        listing = f"/api/v3/repos/{self.repo}/issues/{self.pr}/comments"
        if path == listing:
            if method == "GET":
                # A failed listing answers 500, never an empty page: the lookup
                # must keep "could not read" apart from "no match".
                if self.fail_listings:
                    return 500, {"message": "fake GitHub: the listing is unavailable"}
                return 200, self.paged(path, self.comments)
            if method == "POST":
                return 201, {"id": self.add_comment(body["body"])}
            return None

        match = re.fullmatch(
            rf"/api/v3/repos/{re.escape(self.repo)}/issues/comments/(?P<id>\d+)", path
        )
        if match is None:
            return None
        wanted = int(match.group("id"))
        for comment in self.comments:
            if comment["id"] != wanted:
                continue
            if method == "GET":
                return 200, comment
            if method == "PATCH":
                comment["body"] = body["body"]
                self.patched.append(wanted)
                return 200, comment
        return None


class FakePRReviews(_LocalGitHub):
    """A GitHub that answers the shared PR-review READ:

      POST /api/graphql                              (lib/pr-reviews.bash's read)
      GET  /api/v3/repos/{o}/{r}/pulls/{n}/reviews   (the REST reviews endpoint)

    Real `gh api graphql --paginate` walks this server, so the pagination the
    shared read depends on is EXERCISED rather than asserted as a flag. Two facts
    make that real. The server serves `PAGE` reviews per page, so a caller that
    reads page one alone sees a different PR than the one it was given. And it
    REFUSES a query that binds no `after:` cursor at all, because such a query
    can never read past page one — so a `REVIEWS_QUERY` that lost its
    `$endCursor` reds every test here rather than only the paging one.

    The cursor is read from the query's own `after:` argument, not from the
    variables gh sends: taking the variable directly would page a client that
    cannot page, because gh omits `endCursor` on the first request either way.

    Page one of `reviews` is the OLDEST page, which is the whole reason the read
    folds across pages: `add_review` appends, so the LAST review added is the one
    a correct client answers from.

    The REST endpoint answers from the same reviews, so a caller that went back
    to reading REST is caught by the VERDICT it computes rather than by an
    unhandled call — a 404 would name the regression for it.
    """

    #: Reviews per page. One, so any PR carrying two reviews pages for real.
    PAGE = 1

    def __init__(self, tmp_path: Path, *, repo: str = "owner/repo", pr: int = 5):
        self.repo = repo
        self.pr = pr
        #: Oldest first, the order GitHub returns them in.
        self.reviews: list[dict] = []
        #: When set, both reviews reads answer 502 — the can't-verify path.
        self.fail_reads = False
        super().__init__(tmp_path)

    def add_review(
        self,
        *,
        login: str = "github-actions",
        state: str = "COMMENTED",
        body: str = "## Review\n\nfindings…",
        submitted_at: str = "2026-07-01T00:00:00Z",
    ) -> None:
        """One review of the PR. The server projects it into both API shapes.

        `login` is the BARE form. GraphQL returns an app bot's login without the
        REST `[bot]` suffix, so the GraphQL nodes carry it as given and the REST
        projection adds the suffix back, the way each surface really answers.
        """
        self.reviews.append(
            {
                "login": login,
                "state": state,
                "body": body,
                "submittedAt": submitted_at,
            }
        )

    def _reviews_page(self, body: dict) -> tuple[int, object]:
        document = parse(body.get("query", ""))
        cursor = _after_cursor(
            document, _fragments(document), body.get("variables", {}), "reviews"
        )
        if cursor is _NO_CURSOR:
            return 200, {
                "errors": [
                    {
                        "message": "fake GitHub: this query binds no `after:` on "
                        "reviews, so it can never read past the first page — and "
                        "page one of reviews is the OLDEST page"
                    }
                ]
            }
        start = int(cursor or 0)
        end = min(start + self.PAGE, len(self.reviews))
        return 200, {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviews": {
                            "pageInfo": {
                                "hasNextPage": end < len(self.reviews),
                                "endCursor": str(end)
                                if end < len(self.reviews)
                                else None,
                            },
                            "nodes": [
                                {
                                    "author": {"login": r["login"]},
                                    "state": r["state"],
                                    "body": r["body"],
                                    "submittedAt": r["submittedAt"],
                                    # Over Int32, which is why the query reads
                                    # fullDatabaseId rather than databaseId.
                                    "fullDatabaseId": 4802416227,
                                    "commit": {"oid": "abc123"},
                                }
                                for r in self.reviews[start:end]
                            ],
                        }
                    }
                }
            },
        }

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        rest = f"/api/v3/repos/{self.repo}/pulls/{self.pr}/reviews"
        if self.fail_reads and path in ("/api/graphql", rest):
            return 502, {"message": "fake GitHub: outage on the reviews read"}
        if method == "POST" and path == "/api/graphql":
            return self._reviews_page(body)
        if method == "GET" and path == rest:
            return 200, self.paged(
                path,
                [
                    {
                        "id": i,
                        "user": {"login": f"{r['login']}[bot]"},
                        "state": r["state"],
                        "body": r["body"],
                        "submitted_at": r["submittedAt"],
                    }
                    for i, r in enumerate(self.reviews, 1)
                ],
            )
        return None


class FakeReviewPoster(_LocalGitHub):
    """A GitHub that answers the PR reviewer's posting surface:

      POST /api/v3/repos/{o}/{r}/pulls/{n}/reviews   (the review, whole or summary)
      GET  /api/v3/repos/{o}/{r}/pulls/{n}/reviews   (the degraded path's re-post read)
      POST /api/v3/repos/{o}/{r}/pulls/{n}/comments  (one finding's own thread)
      GET  /api/v3/repos/{o}/{r}/pulls/{n}/files     (the hold's anchor file)
      POST /api/graphql                              (the hold's idempotence read)

    The GET of reviews replays what this instance accepted, so a second run sees
    the state the first left — which is what makes the re-post check testable end
    to end rather than against a canned list.

    The reviews API is ALL-OR-NOTHING, and its refusal is what
    post-pr-review.sh's degraded path exists for — so `refuse_structured`
    answers 422 to a review carrying `comments`, and `refuse_comment_paths`
    answers 422 to one finding at a time. What the caller needs a REAL `gh` for
    is that a 422 reaches it as a non-zero exit at all: a stubbed gh would be
    the test's own belief on both sides of that.
    """

    def __init__(self, tmp_path: Path, *, repo: str = "owner/repo", pr: int = 5):
        self.repo = repo
        self.pr = pr
        self.refuse_structured = False
        self.refuse_comment_paths: tuple[str, ...] = ()
        self.files = [{"filename": "src/first.py"}]
        # Every accepted POST, in order — the order is the assertion that a
        # lost finding's hold lands before the review that greens the gate.
        self.posted: list[tuple[str, dict]] = []
        super().__init__(tmp_path)

    def _refused(self) -> tuple[int, object]:
        return 422, {"message": "Unprocessable Entity"}

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        prefix = f"/api/v3/repos/{self.repo}/pulls/{self.pr}"
        if method == "POST" and path == f"{prefix}/reviews":
            if self.refuse_structured and body.get("comments"):
                return self._refused()
            self.posted.append(("review", body))
            return 200, {"id": len(self.posted), "state": "COMMENTED"}
        if method == "POST" and path == f"{prefix}/comments":
            if body.get("path") in self.refuse_comment_paths:
                return self._refused()
            self.posted.append(("comment", body))
            return 201, {"id": len(self.posted)}
        if method == "GET" and path == f"{prefix}/reviews":
            # The reviewer posts with the workflow token, so GitHub attributes
            # its reviews to the app bot — and returns that login WITH the REST
            # `[bot]` suffix, which the reader strips before comparing.
            return 200, self.paged(
                path,
                [
                    {
                        "id": i,
                        "user": {"login": "github-actions[bot]"},
                        "body": b.get("body", ""),
                    }
                    for i, (kind, b) in enumerate(self.posted, 1)
                    if kind == "review"
                ],
            )
        if method == "GET" and path == f"{prefix}/files":
            return 200, self.paged(path, self.files)
        if method == "POST" and path == "/api/graphql":
            return 200, {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    }
                }
            }
        return None

    def of_kind(self, kind: str) -> list[dict]:
        """Every accepted POST of one kind, in the order the script made them."""
        return [body for k, body in self.posted if k == kind]


class FakePrStatus(_IssueCommentStore, _LocalGitHub):
    """A GitHub holding one open PR, its check runs and its review threads.

      GET  /api/v3/repos/{o}/{r}/pulls                      (the branch's open PR)
      GET  /api/v3/repos/{o}/{r}/pulls/{n}                  (state, head, mergeability)
      GET  /api/v3/repos/{o}/{r}/commits/{sha}/check-runs   (paged, 100 per page)
      GET  /api/v3/repos/{o}/{r}/commits/{sha}/status       (the commit statuses)
      POST /api/graphql                                     (the review threads, and
                                                             the `gh pr view` read)

    The check-runs endpoint pages exactly as GitHub does — 100 per page, with a
    `total_count` — because reading only page one is the failure the caller under
    test must not have: a busy PR carries hundreds of runs, so a truncated read
    can report a red PR as green.
    """

    PAGE = 100

    def __init__(self, tmp_path: Path, *, repo: str = "owner/repo", pr: int = 7):
        self.repo = repo
        self.pr = pr
        self.head_sha = "f" * 40
        self.branch = "claude/a-branch"
        self.base_ref = "main"
        self.draft = False
        # True makes this PR a FORK's. gh filters a listing on the branch name
        # alone, so a caller commenting on this repository's PR must reject it.
        self.cross_repo = False
        #: The description `gh pr view --json body` projects.
        self.body = "a description"
        self.labels: tuple[str, ...] = ()
        self.mergeable_state = "clean"
        # One entry per GraphQL operation served. The REST `requests` list cannot
        # tell a `gh pr view` from a review-thread walk, since both are one POST
        # to /api/graphql, and a test asserting no PR was read needs to.
        self.operations: list[str] = []
        self.check_runs: list[dict] = []
        # A required context may report as a commit STATUS rather than a check
        # run, so a caller reading only the check runs must not read as green.
        self.statuses: list[dict] = []
        self.threads: list[dict] = []
        # When set, POST /api/graphql answers with it instead of the threads.
        self.graphql_refusal: str | None = None
        self.graphql_refusal_status = 403
        # When set, every REST read answers with it too — a host that serves this
        # session no REST at all, which is a different failure.
        self.rest_refusal: str | None = None
        self.rest_refusal_status = 403
        self._init_comments()
        super().__init__(tmp_path)

    def add_check(
        self,
        name: str,
        *,
        status: str = "completed",
        conclusion: str | None = "success",
        started_at: str = "2026-01-01T00:00:00Z",
        details_url: str | None = None,
    ) -> None:
        # details_url is chosen by whichever app filed the check, not by GitHub,
        # so a renderer must be drivable with one that leaves this host.
        self.check_runs.append(
            {
                "id": len(self.check_runs) + 1,
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "started_at": started_at,
                "details_url": details_url,
            }
        )

    def add_status(
        self,
        context: str,
        *,
        state: str = "success",
        created_at: str = "2026-01-01T00:00:00Z",
    ) -> None:
        self.statuses.append(
            {
                "context": context,
                "state": state,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )

    def add_thread(self, *, path: str, line: int, author: str, body: str) -> None:
        self.threads.append(
            {
                "id": f"PRRT_{len(self.threads) + 1}",
                "isResolved": False,
                "isOutdated": False,
                "path": path,
                "line": line,
                "comments": {"nodes": [{"author": {"login": author}, "body": body}]},
            }
        )

    def _page(self) -> tuple[int, object]:
        page = int((self.query.get("page") or ["1"])[0])
        start = (page - 1) * self.PAGE
        return 200, {
            "total_count": len(self.check_runs),
            "check_runs": self.check_runs[start : start + self.PAGE],
        }

    def resolve(self, method: str, path: str, body: dict) -> tuple[int, object] | None:
        if self.rest_refusal and path.startswith("/api/v3/"):
            return self.rest_refusal_status, {"message": self.rest_refusal}
        answer = self.resolve_comment(method, path, body)
        if answer is not None:
            return answer
        if method == "GET" and path == f"/api/v3/repos/{self.repo}/pulls":
            # `head=` filters to one branch's PR; without it GitHub lists them
            # all. Answering [] to the unfiltered form would make an empty watch
            # set look correct.
            wanted = f"{self.repo.split('/')[0]}:{self.branch}"
            head = (self.query.get("head") or [""])[0]
            listed = [{"number": self.pr, "created_at": "2026-01-01T00:00:00Z"}]
            return 200, (listed if head in ("", wanted) else [])
        if method == "GET" and path == f"/api/v3/repos/{self.repo}/pulls/{self.pr}":
            return 200, {
                "number": self.pr,
                "title": "a pull request",
                "state": "open",
                "draft": self.draft,
                "labels": [{"name": name} for name in self.labels],
                "base": {"ref": self.base_ref},
                "head": {"sha": self.head_sha},
                "mergeable_state": self.mergeable_state,
            }
        commit = f"/api/v3/repos/{self.repo}/commits/{self.head_sha}"
        if method == "GET" and path == f"{commit}/check-runs":
            return self._page()
        if method == "GET" and path == f"{commit}/status":
            return 200, {"total_count": len(self.statuses), "statuses": self.statuses}
        if method == "POST" and path == "/api/graphql":
            if self.graphql_refusal:
                return self.graphql_refusal_status, {"message": self.graphql_refusal}
            return self._graphql(body)
        return None

    def _pr_node(self) -> dict:
        """This PR as GraphQL returns it, carrying every field the listing and
        the single-PR read project: gh exports only what its caller asked for."""
        return {
            "number": self.pr,
            "title": "a pull request",
            "state": "OPEN",
            "isDraft": self.draft,
            "headRefOid": self.head_sha,
            "headRefName": self.branch,
            "baseRefName": self.base_ref,
            "isCrossRepository": self.cross_repo,
            "body": self.body,
            "id": f"PR_{self.pr}",
            "createdAt": "2026-01-01T00:00:00Z",
            "labels": {
                "nodes": [{"name": name} for name in self.labels],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "totalCount": len(self.labels),
            },
        }

    def _graphql(self, body: dict) -> tuple[int, object]:
        """The review-thread walk, the open-PR listing, or the single-PR read
        `gh pr view` builds.

        Routed on the operation NAME, parsed as GraphQL rather than matched as
        text: gh names its own query, and answering the thread shape to a
        `gh pr view` hands the caller a null field where a loud error belongs.
        """
        document = parse(body.get("query", ""))
        operation = _operations(document)[0].name
        if operation and operation.value == "PullRequestList":
            self.operations.append("PullRequestList")
            wanted = body.get("variables", {}).get("headBranch")
            listed = [] if wanted not in (None, self.branch) else [self._pr_node()]
            return _pr_list_reply(listed)
        if operation and operation.value == "PullRequestByNumber":
            self.operations.append("PullRequestByNumber")
            return 200, {"data": {"repository": {"pullRequest": self._pr_node()}}}
        # The thread walk is the one ANONYMOUS query here, so a name that reached
        # this arm is an operation nothing models.
        assert operation is None, (
            f"fake GitHub: unmodelled GraphQL operation: {operation.value}"
        )
        self.operations.append("reviewThreads")
        return 200, {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": self.threads,
                        }
                    }
                }
            }
        }


def _handler_for(state: _LocalGitHub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a) -> None:  # no stderr access log in test output
            pass

        def _send(self, status: int, payload: object) -> None:
            json_body = not isinstance(payload, bytes)
            body = json.dumps(payload).encode() if json_body else payload
            self.send_response(status)
            content_type = (
                "application/json" if json_body else "application/octet-stream"
            )
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in state.response_headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length)) if length else {}

        def _route(self) -> None:
            state.requests.append((self.command, self.path))
            path, _, query = self.path.partition("?")
            # `resolve` is routed on the path alone, so a server that PAGES reads
            # its cursor from here rather than re-parsing the path everywhere.
            state.query = parse_qs(query)
            state.response_headers = {}
            self._send(*state.dispatch(self.command, path, self._body()))

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _route

    return Handler
