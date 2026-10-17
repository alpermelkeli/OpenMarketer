"""The caps on what a workspace spends on generated media, and the rule that decides one spend.

The design report's section "Budget controls" gives creative generation (images,
video, voice) a monthly cap of its own, and its risk levels let a small spend run
without a person (L0) while a larger one waits in the approval queue (L1).
``decide_spend`` is that rule, on amounts alone:

- ``REFUSE`` when the estimate, with what was spent and what is held this month,
  would go beyond the monthly cap: no person can approve past the cap;
- ``RUN_AUTOMATICALLY`` when it stays within the cap and the estimate is at most
  the amount one action may spend on its own;
- ``ASK_A_PERSON`` otherwise.

Amounts are US dollars as ``Decimal``, kept to the micro-dollar the ledger stores
(``usd``), so that a sum of many small spends is exact.

This module is the rule only: it holds no money, reads no database and does not
know where the caps were chosen. Reserving under it is ``ledger.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from enum import StrEnum

# The ledger's precision: what a column of ``budget_ledger_entry`` keeps.
MICRO_USD = Decimal("0.000001")


class InvalidAmount(ValueError):
    """An amount of money that is not a finite, non-negative number."""

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} is not a finite, non-negative amount of US dollars")
        self.what = what


class SpendDecision(StrEnum):
    RUN_AUTOMATICALLY = "run_automatically"
    ASK_A_PERSON = "ask_a_person"
    REFUSE = "refuse"


def usd(value: Decimal | int | float | str, what: str = "the amount") -> Decimal:
    """``value`` as US dollars, rounded up to the micro-dollar the ledger keeps.

    Rounded up, so that an estimate is never held as less than it is. A float is
    read through its shortest text, so ``0.1`` is a tenth of a dollar. Raises
    ``InvalidAmount`` for a negative, infinite or unreadable value, and for a bool.
    """
    if isinstance(value, bool):
        raise InvalidAmount(what)
    try:
        amount = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidAmount(what) from None
    if not amount.is_finite() or amount < 0:
        raise InvalidAmount(what)
    return amount.quantize(MICRO_USD, rounding=ROUND_CEILING)


@dataclass(frozen=True)
class CreativeCaps:
    """What a workspace may spend on generated media in a calendar month (UTC), and the most
    one action may spend without a person's approval.

    Both are read with ``usd``, so ``CreativeCaps(Decimal(60), Decimal("0.25"))`` and a cap
    of zero (nothing is spent) are valid, a negative one is ``InvalidAmount``.
    """

    monthly_usd: Decimal
    auto_per_action_usd: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "monthly_usd", usd(self.monthly_usd, "the monthly cap"))
        object.__setattr__(
            self,
            "auto_per_action_usd",
            usd(self.auto_per_action_usd, "the amount one action may spend on its own"),
        )


def decide_spend(
    estimate_usd: Decimal,
    spent_this_month_usd: Decimal,
    reserved_usd: Decimal,
    caps: CreativeCaps,
) -> SpendDecision:
    """Whether an action estimated at ``estimate_usd`` runs, waits for a person or is refused.

    ``spent_this_month_usd`` is what the month's actions spent, ``reserved_usd`` what is
    held for actions not settled yet; both count against the cap. Raises ``InvalidAmount``.
    """
    estimate = usd(estimate_usd, "the estimate")
    committed = usd(spent_this_month_usd, "the month's spending") + usd(
        reserved_usd, "the amount held"
    )
    if committed + estimate > caps.monthly_usd:
        return SpendDecision.REFUSE
    if estimate <= caps.auto_per_action_usd:
        return SpendDecision.RUN_AUTOMATICALLY
    return SpendDecision.ASK_A_PERSON
