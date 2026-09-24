"""The credential ladder's rungs, as one ordered table every reader derives from.

PROBLEM CLASS — an ordered SET that GitHub Actions cannot express. A workflow
cannot loop `uses:` steps and cannot index `secrets.*` by a computed name, so the
reusable workflow hands every rung's secret to ONE step and `run-review-ladder.py`
walks this table at run time.

Standard library only, and the data file is resolved from `__file__`: the review
jobs run this out of the vendored `.github/reviewer` tree on the system python3,
with no virtualenv and no repository `git` context.
"""

import json
from dataclasses import dataclass
from pathlib import Path

_NAMES = json.loads(
    (Path(__file__).resolve().parent / "lib" / "shared-names.json").read_text(
        encoding="utf-8"
    )
)

# The wait before each attempt after the first, in attempt order: entry 0 precedes
# attempt 2. The first attempt waits for nothing because nothing has failed yet. A
# dead credential is rejected in about half a second, so back-to-back attempts would
# spend the whole ladder inside one provider-side blip; these waits make the ladder
# straddle it and still finish inside six minutes.
BACKOFF_SECONDS = (10, 20, 30, 45, 60, 90, 90)

# The free same-credential retry after the first attempt is not a rung, so it
# carries its own wait. Ten seconds is the first credential step: long enough to
# outlast a transient fault, short enough that a free retry cannot dominate wall clock.
FREE_RETRY_BACKOFF_SECONDS = 10


@dataclass(frozen=True)
class RungSpec:
    """One credential slot: its 1-based number and the secret it reads.

    Distinct from `_ladder.Rung`, which is one rung's RUNTIME state;
    `run-review-ladder.py` holds both. Which credential is paid is not a property
    of the slot: the runner reads it off the credential's own shape.
    """

    index: int
    env_var: str


def rungs() -> tuple[RungSpec, ...]:
    """The ladder's slots, in slot order, from `lib/shared-names.json`.

    INVARIANT — a table with more rungs than `BACKOFF_SECONDS` has waits is refused
    rather than given a default. A silent default would let a ladder grown past the
    schedule spend its new credentials back-to-back inside one blip, which is the
    failure the waits exist to prevent.
    """
    order = _NAMES["oauth_ladder_vars"]
    if len(order) > len(BACKOFF_SECONDS) + 1:
        raise ValueError(
            f"{len(order)} rungs but only {len(BACKOFF_SECONDS)} waits in "
            "BACKOFF_SECONDS. Extend the schedule in lib_credential_ladder.py "
            "before adding the rung."
        )
    return tuple(
        RungSpec(index=index, env_var=env_var)
        for index, env_var in enumerate(order, start=1)
    )
