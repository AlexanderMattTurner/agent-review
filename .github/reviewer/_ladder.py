"""The credential ladder's retry policy, as one function over recorded rung outcomes.

PROBLEM CLASS — a retry policy written as N copies of a workflow step states its rules
N times, so a rule that changes has N sites to change and a rung added later inherits
whichever copy was pasted. This module is those rules once, and `review-ladder.py`
applies them after every attempt.

The four rules, each load-bearing:
  * A rung that did NOT error is the answer. A run with is_error false and a real
    cost found what it found; retrying it spends again on the same wall.
  * The second attempt alone may retry the SAME credential, and only on a proven
    zero_cost error — no inference was billed, so the retry is free whether the cause
    was a transient blip or a dead token.
  * Every later rung needs a DISTINCT configured credential. The free retry already
    happened second; a further same-token attempt only spends.
  * A wall-clock-only failure never advances, at any rung. A fresh credential faces
    the identical wall, so a further attempt spends again with no new information.

Pure functions over already-read values, so the caller owns every read of the
environment. Standard library only: the review jobs run the vendored `.github/reviewer` tree
on the system python3.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RungOutcome:
    """What a rung that RAN reported. `errored` is a crash, a missing log, or a run
    that answered nothing; `zero_cost` is a PROVEN zero-billed run;
    `wall_clock_only` is a PROVEN wall-clock-only failure."""

    errored: bool
    zero_cost: bool
    wall_clock_only: bool = False


def advances(index: int, outcome: RungOutcome, next_configured: bool) -> bool:
    """True when the ladder runs the rung after `index` (0-based).

    The asymmetry at index 0 is rule 2: only the first retry may reuse the same
    credential, and only on a proven zero-cost failure. Rule 4: a wall-clock-only
    failure never advances, at any index — a fresh credential faces the identical
    wall, so the next rung would buy another bill and no new information.
    """
    if not outcome.errored:
        return False
    if outcome.wall_clock_only:
        return False
    if index == 0:
        return next_configured or outcome.zero_cost
    return next_configured
