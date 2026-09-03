#!/usr/bin/env python3
"""Decide whether a shard a CHEAP model read is re-read by the full-price one.

PROBLEM CLASS — a cheap model's claim that holds a merge. Tiering pays less per
shard, and the shards it pays less for still produce the verdicts and the
`blocking` findings that stop the merge. This asks one question about a read that
already happened: did it make a claim expensive enough to be worth confirming?

Escalates when the read ran at a model other than the caller's own AND its output
holds the merge — verdict `blocking`, or a finding of severity `blocking`. A
`warning`, a `nit` and a `needs_changes` verdict do not: they are the bulk of what
a review says, so escalating them buys the whole diff at full price twice.

It does NOT recover what the cheap read never noticed. Escalation confirms a
claim; it cannot find a finding nobody made. A path whose misses are expensive
belongs on the high model from the start (`low-tier-paths`).

Cost: a shard that escalates costs its cheap read PLUS a full-price one, so a
pull request whose every shard is blocking costs 1.4x what it costs untiered.
That is the worst case and it is bounded; the ordinary case is a handful.

Env: REVIEW_JSON (the read's output, may be absent), SHARD_MODEL (the model that
     read it, empty when the caller named no tier), MODEL (the caller's own),
     GITHUB_OUTPUT.

Output: `escalate=true|false`, plus a line saying which and why.
"""

import json
import os
from pathlib import Path

BLOCKING = "blocking"


def escalates(review: dict) -> bool:
    """True when this read's own output holds the merge.

    A malformed `findings` entry is read as no severity rather than raising: the
    verdict below it still decides, and a review this script cannot parse is one
    the merge gate cannot act on either.
    """
    if review.get("verdict") == BLOCKING:
        return True
    findings = review.get("findings")
    if not isinstance(findings, list):
        return False
    return any(isinstance(f, dict) and f.get("severity") == BLOCKING for f in findings)


def main() -> None:
    shard_model = os.environ.get("SHARD_MODEL", "")
    model = os.environ["MODEL"]
    review_json = Path(os.environ["REVIEW_JSON"])

    def emit(escalate: bool, reason: str) -> None:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"escalate={'true' if escalate else 'false'}\n")
        print(f"escalation: {escalate} ({reason})")

    # Nothing to escalate TO: this shard already read at the caller's model, either
    # because the caller named no tier or because the tier chose the high one.
    if not shard_model or shard_model == model:
        emit(False, f"the shard already read with {model}")
        return
    try:
        review = json.loads(review_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        # A read that produced no parsable verdict escalates: the alternative is
        # publishing nothing for this shard, and the synthesis refuses that anyway.
        emit(True, f"no readable review from {shard_model} ({err})")
        return
    if escalates(review):
        emit(True, f"{shard_model} made a blocking claim; {model} confirms it")
    else:
        emit(False, f"{shard_model} found nothing that holds the merge")


if __name__ == "__main__":
    main()
