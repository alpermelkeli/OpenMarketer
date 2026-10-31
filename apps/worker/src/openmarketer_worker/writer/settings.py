"""Settings of the writer's workflow, read from the environment at the edge.

What an operator can change about a writing run: how long one attempt at a step may take
(``WRITER_TIMEOUT_MINUTES``), how many attempts a step gets (``WRITER_MAX_ATTEMPTS``), how many
rounds of drafting and review a run may make (``WRITER_MAX_ROUNDS``) and what its model calls
may cost (``WRITER_MAX_COST_USD``). The other limits and every default, with their reasons, are
in ``policy.py`` and core's ``rules.Limits``; this module only fills them. It takes the
environment as an argument and is called when the worker starts; the workflow and the
activities read none of it.

A value that does not fit stops the worker at start-up with the variable's name, not every run.
Which models the roles ``writer`` and ``critic`` resolve to is the model configuration's, read
by the wiring; that both resolve to one model is a warning there, never a refusal.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from openmarketer_core.writing.writer_agent.rules import Limits
from openmarketer_worker.settings import SettingsError, positive_number
from openmarketer_worker.writer.policy import WriterPolicy


@dataclass(frozen=True)
class WriterSettings:
    """What the writer's worker side is configured with."""

    policy: WriterPolicy = field(default_factory=WriterPolicy)
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> WriterSettings:
        """Raises ``SettingsError``, naming the variable, for a value that does not fit."""
        policy = WriterPolicy()
        limits = Limits()
        return cls(
            policy=replace(
                policy,
                attempt_timeout_seconds=60
                * positive_number(
                    environ, "WRITER_TIMEOUT_MINUTES", policy.attempt_timeout_seconds / 60
                ),
                max_attempts=_whole_number(environ, "WRITER_MAX_ATTEMPTS", policy.max_attempts),
            ),
            limits=replace(
                limits,
                max_rounds=_whole_number(environ, "WRITER_MAX_ROUNDS", limits.max_rounds),
                max_cost_usd=_amount(environ, "WRITER_MAX_COST_USD", limits.max_cost_usd),
            ),
        )


def _whole_number(environ: Mapping[str, str], name: str, default: int) -> int:
    text = environ.get(name)
    if not text:
        return default
    number = positive_number(environ, name, default)
    if number != int(number):
        raise SettingsError(f"{name} must be a whole number, not {text!r}")
    return int(number)


def _amount(environ: Mapping[str, str], name: str, default: float) -> float:
    """A number of US dollars above 0; a fraction is fine (half a dollar)."""
    text = environ.get(name)
    if not text:
        return default
    try:
        amount = float(text)
    except ValueError:
        amount = 0.0
    if not math.isfinite(amount) or amount <= 0:
        raise SettingsError(f"{name} must be a number of US dollars above 0, not {text!r}")
    return amount
