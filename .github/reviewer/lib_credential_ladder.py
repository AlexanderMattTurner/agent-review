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

# The wait before each rung's attempt, indexed by rung number. Rung 1 has none:
# nothing has failed yet. A dead credential is rejected in about half a second, so
# back-to-back rungs would spend the whole ladder inside one provider-side blip;
# these waits make the ladder straddle it and still finish inside six minutes.
_BACKOFF_SECONDS = {2: 10, 3: 20, 4: 30, 5: 45, 6: 60, 7: 90, 8: 90}

# The free same-credential retry between rung 1 and rung 2 is not a rung, so it
# carries its own wait. Ten seconds is the first credential step: long enough to
# outlast a transient fault, short enough that a free retry cannot dominate wall clock.
FREE_RETRY_BACKOFF_SECONDS = 10


@dataclass(frozen=True)
class RungSpec:
    """One credential slot, and every name the unrolled copies spell it with.

    Distinct from `_ladder.Rung`, which is one rung's RUNTIME state;
    `run-review-ladder.py` holds both. `metered` says the slot bills real credits,
    which decides whether an attempt authenticates through `ANTHROPIC_API_KEY` or
    `CLAUDE_CODE_OAUTH_TOKEN`.
    """

    index: int  # 1-based, the number every rendered id and message counts with
    env_var: str
    metered: bool
    backoff_seconds: int | None  # None for rung 1, which waits for nothing

    @property
    def wait_seconds(self) -> int:
        """The wait before this rung's attempt.

        INVARIANT — only rung 1 has none, because nothing has failed before it. Every
        renderer that asks for a wait is rendering a backoff step, and a rung 1 backoff
        step would delay every run of every caller by the first credential step.
        """
        if self.backoff_seconds is None:
            raise ValueError(
                f"rung {self.index} takes no backoff step: nothing failed before it"
            )
        return self.backoff_seconds

    @property
    def label(self) -> str:
        """The rung label the run log names the winning credential with."""
        return "api" if self.metered else str(self.index)


def rungs() -> tuple[RungSpec, ...]:
    """The ladder, in attempt order, from `lib/shared-names.json`.

    INVARIANT — a rung with no wait in `_BACKOFF_SECONDS` is refused rather than
    given a default. A silent default would let a ladder grown past the schedule
    spend its new credentials back-to-back inside one blip, which is the failure
    the waits exist to prevent.
    """
    order = _NAMES["oauth_ladder_vars"]
    metered = set(_NAMES["oauth_ladder_metered_vars"])
    unknown = sorted(metered - set(order))
    if unknown:
        raise ValueError(
            f"oauth_ladder_metered_vars names variables that are not rungs: {unknown}. "
            "A metered slot the ladder never walks bills nothing and hides a typo."
        )
    out = []
    for index, env_var in enumerate(order, start=1):
        if index > 1 and index not in _BACKOFF_SECONDS:
            raise ValueError(
                f"rung {index} ({env_var}) has no wait in _BACKOFF_SECONDS. "
                "Extend the schedule in lib_credential_ladder.py before adding the rung."
            )
        out.append(
            RungSpec(
                index=index,
                env_var=env_var,
                metered=env_var in metered,
                backoff_seconds=_BACKOFF_SECONDS.get(index),
            )
        )
    metered_indices = sorted(
        i for i, name in enumerate(order, start=1) if name in metered
    )
    if metered_indices and metered_indices != [1]:
        raise ValueError(
            f"metered rung(s) {metered_indices} of {len(order)} are not the ladder's "
            "first rung. A metered rung must be rung 1 so it is attempted "
            "unconditionally — every consumer reads `ladder[0].metered` alone to decide "
            "which credential variable a rung authenticates through, and to warn that "
            "the run bills real credits."
        )
    return tuple(out)
