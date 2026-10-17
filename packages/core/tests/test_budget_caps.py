"""Tests for the caps on spending for generated media and the rule that decides one spend."""

from decimal import Decimal

import pytest

from openmarketer_core.budget.caps import (
    CreativeCaps,
    InvalidAmount,
    SpendDecision,
    decide_spend,
    usd,
)

CAPS = CreativeCaps(monthly_usd=Decimal(60), auto_per_action_usd=Decimal("0.25"))


def decide(estimate: str, spent: str = "0", reserved: str = "0", caps: CreativeCaps = CAPS):
    return decide_spend(Decimal(estimate), Decimal(spent), Decimal(reserved), caps)


def test_a_small_spend_within_the_cap_runs_automatically():
    assert decide("0.25") is SpendDecision.RUN_AUTOMATICALLY


def test_a_spend_above_the_amount_of_one_action_asks_a_person():
    assert decide("0.26") is SpendDecision.ASK_A_PERSON


def test_a_spend_that_would_pass_the_monthly_cap_is_refused():
    assert decide("2", spent="59") is SpendDecision.REFUSE


def test_what_is_held_counts_against_the_cap():
    assert decide("0.10", spent="50", reserved="9.95") is SpendDecision.REFUSE


def test_a_spend_that_reaches_the_cap_exactly_is_allowed():
    assert decide("0.25", spent="59.75") is SpendDecision.RUN_AUTOMATICALLY


def test_a_small_spend_past_the_cap_is_refused_rather_than_run():
    assert decide("0.01", spent="60") is SpendDecision.REFUSE


def test_a_cap_of_zero_lets_only_free_actions_run():
    nothing = CreativeCaps(monthly_usd=Decimal(0), auto_per_action_usd=Decimal(0))
    assert decide("0", caps=nothing) is SpendDecision.RUN_AUTOMATICALLY
    assert decide("0.01", caps=nothing) is SpendDecision.REFUSE


def test_an_amount_is_rounded_up_to_the_micro_dollar():
    assert usd(Decimal("0.0000001")) == Decimal("0.000001")


def test_a_float_is_read_through_its_shortest_text():
    assert usd(0.1) == Decimal("0.1")


@pytest.mark.parametrize("amount", [Decimal(-1), Decimal("NaN"), Decimal("Infinity"), True, "ten"])
def test_an_amount_that_is_not_one_is_refused(amount):
    with pytest.raises(InvalidAmount):
        usd(amount)


def test_a_negative_cap_is_refused():
    with pytest.raises(InvalidAmount, match="the monthly cap"):
        CreativeCaps(monthly_usd=Decimal(-1), auto_per_action_usd=Decimal(0))


def test_a_negative_estimate_is_refused():
    with pytest.raises(InvalidAmount, match="the estimate"):
        decide("-0.01")
