"""What a workspace can choose about the limits of an analysis, and what a valid choice is.

Two of the analyzer's limits (``analyzer_agent/rules.py``, ``Limits``) can be
set from the dashboard: how many turns the agent may take and how much its model
calls may cost in one run. The number of submissions before the draft is
repaired in code stays the analyzer's: it is a rule of how the agent works, not
a budget.

Why the bounds:

- At least 5 steps: the last two are kept for submitting the profile
  (``WRAP_UP_STEPS``), and with fewer than three to explore the agent submits
  what it has not read.
- At most 200 steps: the worker sets the graph's own step limit, the one that
  stops a run as a defect, from this bound, once, when it starts
  (``LARGEST_LIMITS``).
- A cost above zero: a run whose spending reached the limit is ended, so a
  limit of zero ends every run before it starts, even on a free model.
- At most 20 US dollars: the measured runs cost cents, and the bound is there to
  catch a mistyped decimal point, not to stop a deliberate budget.

This module is the rule only; storing a choice is ``analysis_settings.py``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from openmarketer_core.repository_analysis.analyzer_agent.rules import Limits

MIN_STEPS = 5
MAX_STEPS = 200
MIN_COST_USD = 0.01
MAX_COST_USD = 20.0

# The limits no workspace can go beyond: what the worker sizes the graph for.
LARGEST_LIMITS = Limits(max_steps=MAX_STEPS, max_cost_usd=MAX_COST_USD)


class AnalysisLimitsRefused(Exception):
    """A choice breaks a rule; ``field`` is the limit that does."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class AnalysisLimitsChoice:
    """What a workspace chose; ``None`` leaves that limit at the analyzer's default."""

    max_steps: int | None = None
    max_cost_usd: float | None = None

    @property
    def is_empty(self) -> bool:
        return self.max_steps is None and self.max_cost_usd is None

    def applied_to(self, limits: Limits) -> Limits:
        """``limits`` with what was chosen in place of its own values."""
        chosen: dict[str, Any] = {}
        if self.max_steps is not None:
            chosen["max_steps"] = self.max_steps
        if self.max_cost_usd is not None:
            chosen["max_cost_usd"] = self.max_cost_usd
        return replace(limits, **chosen)

    def as_stored(self) -> dict[str, Any]:
        values = {"max_steps": self.max_steps, "max_cost_usd": self.max_cost_usd}
        return {key: value for key, value in values.items() if value is not None}


def acceptable_limits(choice: AnalysisLimitsChoice) -> AnalysisLimitsChoice:
    """``choice`` if each limit in it is within its bounds."""
    steps = choice.max_steps
    if steps is not None and (
        not isinstance(steps, int) or isinstance(steps, bool) or not MIN_STEPS <= steps <= MAX_STEPS
    ):
        raise AnalysisLimitsRefused(
            "max_steps", f"the step limit is a whole number from {MIN_STEPS} to {MAX_STEPS}"
        )
    cost = choice.max_cost_usd
    if cost is not None and (
        not isinstance(cost, int | float)
        or isinstance(cost, bool)
        or not math.isfinite(cost)
        or not MIN_COST_USD <= cost <= MAX_COST_USD
    ):
        raise AnalysisLimitsRefused(
            "max_cost_usd",
            f"the cost limit is from {MIN_COST_USD:g} to {MAX_COST_USD:g} US dollars",
        )
    return AnalysisLimitsChoice(max_steps=steps, max_cost_usd=None if cost is None else float(cost))


def limits_from_stored(value: Mapping[str, Any]) -> AnalysisLimitsChoice:
    """A stored choice read back under the same rules it was stored with."""
    unknown = set(value) - {"max_steps", "max_cost_usd"}
    if unknown:
        raise AnalysisLimitsRefused(sorted(unknown)[0], "not a limit of an analysis")
    return acceptable_limits(
        AnalysisLimitsChoice(
            max_steps=value.get("max_steps"), max_cost_usd=value.get("max_cost_usd")
        )
    )
