# PR reviewer eval set

`cases/<name>/` holds one real, hand-verified PR diff each, used to check the
reviewer's behavior against a known answer. Every case comes from a real
glovebox commit; none is invented.

## Layout

```text
cases/<name>/
  case.json   # the expected answer, schema below
  diff.txt    # a real unified diff (git show <sha> [-- <paths>])
  tree/       # the handful of base-tree files this case's answer depends on
```

`tree/` is not a full checkout. It holds only the files `build-review-context.py`
must search to produce the `context_expect` hits, or that a human needs to see
to verify a `must_not_flag` call (transitive sourcing, a sibling caller). A case
with no such dependency ships an empty `tree/`.

## `case.json` schema

| Field            | Type                          | Meaning                                                                                                                    |
| ---------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `kind`           | `"defect"` \| `"non_bug"`     | Whether a correct review must file a blocking/warning finding.                                                             |
| `source`         | string                        | `<owner>/<repo>@<sha>` the diff came from.                                                                                 |
| `summary`        | string                        | One sentence describing the change.                                                                                        |
| `must_flag`      | array of `{path, why}`        | Paths a correct review must raise a finding on. Non-empty for every `defect` case.                                         |
| `must_not_flag`  | array of strings              | Paths a correct review must NOT raise a finding on (the false-positive traps).                                             |
| `context_expect` | array of `{identifier, path}` | Identifier/path pairs that must appear in `build-review-context.py`'s `context.txt`, under that identifier's `##` section. |

`must_flag`, `must_not_flag` and `context_expect` may be empty arrays. Every
path named in `must_flag`/`must_not_flag` must appear either as a changed path
in `diff.txt` or as a file under `tree/`.

## Running the eval

`tests/test_eval_cases.py` validates the schema and the `build-review-context.py`
context extraction — no live model calls, safe for CI.

`tests/eval/run-live.py` runs the real `claude` CLI against a bounded number of
cases. It costs real money and is never invoked by CI or by any test; run it by
hand. Its report quotes, per `must_flag` entry, every BLOCKING or WARNING
finding the model raised on that path, beside the entry's own `why`: a nit on
the right file is not the defect caught, and only a reader comparing the two
texts can say whether the model found this defect or another one. A
`must_not_flag` hit is quoted with its severity for the same reason.
