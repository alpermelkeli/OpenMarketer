"""Settings of the orchestrator's task, read from the environment at the edge.

What an operator can change about a task: the installation's default scope of one, which a
plan keeps to unless a person confirms more: how many runs it may start (``TASK_MAX_STEPS``),
how often its plan may change (``TASK_MAX_PLAN_CHANGES``), its deadline in days
(``TASK_DEADLINE_DAYS``) and what its own model calls may cost (``TASK_MAX_COST_USD``; each run
it starts keeps its own caps). The defaults, the ceilings no plan goes beyond and their reasons
are core's (``task_agent/rules.TaskLimits``, ``plan.py``); the limits of the graph's steps are
in ``policy.py``. This module only fills them. It takes the environment as an argument and is
called when the worker starts; the workflow and the activities read none of it.

A value that does not fit stops the worker at start-up with the variable's name, not every
task. Which model the role ``orchestrator`` resolves to is the model configuration's, read by
the wiring.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from openmarketer_core.orchestrator.task.plan import (
    MAX_TASK_COST_USD,
    MAX_TASK_DEADLINE_DAYS,
    MAX_TASK_PLAN_CHANGES,
    MAX_TASK_STEPS,
)
from openmarketer_core.orchestrator.task.task_agent.rules import TaskLimits
from openmarketer_worker.orchestrator_task.policy import TaskPolicy
from openmarketer_worker.settings import SettingsError


@dataclass(frozen=True)
class TaskSettings:
    """What the task's worker side is configured with."""

    policy: TaskPolicy = field(default_factory=TaskPolicy)
    limits: TaskLimits = field(default_factory=TaskLimits)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> TaskSettings:
        """Raises ``SettingsError``, naming the variable, for a value that does not fit."""
        defaults = TaskLimits()
        return cls(
            limits=TaskLimits(
                max_steps=_whole_number(
                    environ, "TASK_MAX_STEPS", defaults.max_steps, low=1, high=MAX_TASK_STEPS
                ),
                max_plan_changes=_whole_number(
                    environ,
                    "TASK_MAX_PLAN_CHANGES",
                    defaults.max_plan_changes,
                    low=0,
                    high=MAX_TASK_PLAN_CHANGES,
                ),
                deadline_days=_whole_number(
                    environ,
                    "TASK_DEADLINE_DAYS",
                    defaults.deadline_days,
                    low=1,
                    high=MAX_TASK_DEADLINE_DAYS,
                ),
                max_cost_usd=_amount(environ, "TASK_MAX_COST_USD", defaults.max_cost_usd),
            )
        )


def _whole_number(
    environ: Mapping[str, str], name: str, default: int, *, low: int, high: int
) -> int:
    text = environ.get(name, "").strip()
    if not text:
        return default
    try:
        number = int(text)
    except ValueError:
        number = low - 1
    if not low <= number <= high:
        raise SettingsError(f"{name} must be a whole number from {low} to {high}, not {text!r}")
    return number


def _amount(environ: Mapping[str, str], name: str, default: float) -> float:
    """A number of US dollars above 0 and at most the ceiling of a task; a fraction is fine."""
    text = environ.get(name, "").strip()
    if not text:
        return default
    try:
        amount = float(text)
    except ValueError:
        amount = 0.0
    if not math.isfinite(amount) or not 0 < amount <= float(MAX_TASK_COST_USD):
        raise SettingsError(
            f"{name} must be a number of US dollars above 0 and at most {MAX_TASK_COST_USD}, "
            f"not {text!r}"
        )
    return amount
