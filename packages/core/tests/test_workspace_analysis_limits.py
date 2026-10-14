"""Tests for what a workspace may choose about the limits of an analysis."""

import pytest

from openmarketer_core.repository_analysis.analyzer_agent.rules import Limits
from openmarketer_core.workspace_settings.analysis_limits import (
    MAX_COST_USD,
    MAX_STEPS,
    MIN_STEPS,
    AnalysisLimitsChoice,
    AnalysisLimitsRefused,
    acceptable_limits,
    limits_from_stored,
)


def refused_field(choice: AnalysisLimitsChoice) -> str:
    with pytest.raises(AnalysisLimitsRefused) as refusal:
        acceptable_limits(choice)
    return refusal.value.field


def test_limits_within_bounds_are_accepted():
    choice = AnalysisLimitsChoice(max_steps=60, max_cost_usd=2)
    assert acceptable_limits(choice) == AnalysisLimitsChoice(max_steps=60, max_cost_usd=2.0)


@pytest.mark.parametrize("steps", [MIN_STEPS - 1, MAX_STEPS + 1, 12.5, True])
def test_step_limit_out_of_bounds_is_refused(steps):
    assert refused_field(AnalysisLimitsChoice(max_steps=steps)) == "max_steps"


@pytest.mark.parametrize("cost", [0, MAX_COST_USD + 0.01, float("inf"), "1"])
def test_cost_limit_out_of_bounds_is_refused(cost):
    assert refused_field(AnalysisLimitsChoice(max_cost_usd=cost)) == "max_cost_usd"


def test_a_limit_not_chosen_keeps_the_default():
    limits = AnalysisLimitsChoice(max_steps=80).applied_to(Limits())
    assert limits == Limits(max_steps=80, max_cost_usd=Limits().max_cost_usd)


def test_stored_choice_is_read_back_under_the_same_rules():
    assert limits_from_stored({"max_cost_usd": 1.5}) == AnalysisLimitsChoice(max_cost_usd=1.5)
    with pytest.raises(AnalysisLimitsRefused):
        limits_from_stored({"max_submissions": 9})
    with pytest.raises(AnalysisLimitsRefused):
        limits_from_stored({"max_steps": 1})
