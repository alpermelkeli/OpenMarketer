"""What a workspace can choose about its budget for generated media, and what a valid choice is.

The two caps of ``budget/caps.py`` can be set from the dashboard: what the
workspace may spend on generated media in a calendar month, and the most one
action may spend without a person's approval. Without a choice, the defaults
are the ones ``.env.example`` documents: 60 US dollars a month, and a quarter
of a dollar for one action.

Why the bounds:

- Zero is allowed for both: a monthly cap of zero spends nothing, and zero for
  one action asks a person for every spend that costs anything.
- At most 10,000 US dollars a month and 100 for one action: the bounds are there
  to catch a mistyped decimal point, not to stop a deliberate budget.

An amount is kept to the micro-dollar the ledger stores, and stored as text so
that it reads back exactly. This module is the rule only; storing a choice and
the environment's part are ``creative_budget_settings.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from openmarketer_core.budget.caps import CreativeCaps, InvalidAmount, usd

MAX_MONTHLY_USD = Decimal(10_000)
MAX_AUTO_PER_ACTION_USD = Decimal(100)

DEFAULT_CAPS = CreativeCaps(monthly_usd=Decimal(60), auto_per_action_usd=Decimal("0.25"))

_BOUNDS = {"monthly_usd": MAX_MONTHLY_USD, "auto_per_action_usd": MAX_AUTO_PER_ACTION_USD}
_WHAT = {
    "monthly_usd": "the monthly cap",
    "auto_per_action_usd": "the amount one action may spend without approval",
}


class CreativeBudgetRefused(Exception):
    """A choice breaks a rule; ``field`` is the cap that does."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True)
class CreativeBudgetChoice:
    """What a workspace chose; ``None`` leaves that cap to the environment or the default."""

    monthly_usd: Decimal | None = None
    auto_per_action_usd: Decimal | None = None

    @property
    def is_empty(self) -> bool:
        return self.monthly_usd is None and self.auto_per_action_usd is None

    def applied_to(self, caps: CreativeCaps) -> CreativeCaps:
        """``caps`` with what was chosen in place of its own values."""
        chosen: dict[str, Decimal] = {}
        if self.monthly_usd is not None:
            chosen["monthly_usd"] = self.monthly_usd
        if self.auto_per_action_usd is not None:
            chosen["auto_per_action_usd"] = self.auto_per_action_usd
        return replace(caps, **chosen)

    def as_stored(self) -> dict[str, str]:
        values = {"monthly_usd": self.monthly_usd, "auto_per_action_usd": self.auto_per_action_usd}
        return {key: str(value) for key, value in values.items() if value is not None}


def acceptable_amount(field: str, value: object) -> Decimal:
    """``value`` as the cap ``field``, if it is an amount within that cap's bounds.

    ``field`` is ``monthly_usd`` or ``auto_per_action_usd``. A number or its text is read.
    """
    bound = _BOUNDS[field]
    message = f"{_WHAT[field]} is an amount from 0 to {bound:,} US dollars"
    if not isinstance(value, Decimal | int | float | str):
        raise CreativeBudgetRefused(field, message)
    try:
        amount = usd(value, _WHAT[field])
    except InvalidAmount:
        raise CreativeBudgetRefused(field, message) from None
    if amount > bound:
        raise CreativeBudgetRefused(field, message)
    return amount


def acceptable_budget(choice: CreativeBudgetChoice) -> CreativeBudgetChoice:
    """``choice`` if each cap in it is within its bounds, kept to the micro-dollar."""
    return CreativeBudgetChoice(
        monthly_usd=_chosen("monthly_usd", choice.monthly_usd),
        auto_per_action_usd=_chosen("auto_per_action_usd", choice.auto_per_action_usd),
    )


def budget_from_stored(value: Mapping[str, Any]) -> CreativeBudgetChoice:
    """A stored choice read back under the same rules it was stored with."""
    unknown = set(value) - set(_BOUNDS)
    if unknown:
        raise CreativeBudgetRefused(sorted(unknown)[0], "not a cap of the budget for media")
    return acceptable_budget(
        CreativeBudgetChoice(
            monthly_usd=value.get("monthly_usd"),
            auto_per_action_usd=value.get("auto_per_action_usd"),
        )
    )


def _chosen(field: str, value: object) -> Decimal | None:
    return None if value is None else acceptable_amount(field, value)
