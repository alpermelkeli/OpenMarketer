"""Tests for the policy gate's verdict: which actions run, wait for a person, or are refused."""

import pytest

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionPolicy
from openmarketer_core.agent_runtime.gate import (
    GateVerdict,
    SpendNotDecided,
    may_wait_for_a_person,
    spends,
    verdict_for,
)
from openmarketer_core.budget.caps import SpendDecision


def policy(**changes) -> ActionPolicy:
    return ActionPolicy(
        attempt_timeout_seconds=30, max_attempts=1, retry_after_seconds=1, **changes
    )


def test_action_that_works_for_the_run_itself_runs():
    assert verdict_for(policy()) is GateVerdict.RUN


def test_action_that_publishes_waits_for_a_person():
    assert verdict_for(policy(effect=ActionEffect.PUBLISHES)) is GateVerdict.ASK_A_PERSON


def test_action_that_publishes_waits_for_a_person_whatever_a_spend_decision_says():
    published = policy(effect=ActionEffect.PUBLISHES)
    assert verdict_for(published, SpendDecision.RUN_AUTOMATICALLY) is GateVerdict.ASK_A_PERSON


@pytest.mark.parametrize(
    ("decision", "verdict"),
    [
        (SpendDecision.RUN_AUTOMATICALLY, GateVerdict.RUN),
        (SpendDecision.ASK_A_PERSON, GateVerdict.ASK_A_PERSON),
        (SpendDecision.REFUSE, GateVerdict.REFUSE),
    ],
)
def test_verdict_on_an_action_that_spends_is_the_ledgers_decision(decision, verdict):
    assert verdict_for(policy(effect=ActionEffect.SPENDS), decision) is verdict


def test_ledgers_decision_is_read_from_its_value_as_it_crosses_the_history():
    spending = policy(effect=ActionEffect.SPENDS)
    assert verdict_for(spending, "run_automatically") is GateVerdict.RUN


def test_action_that_spends_has_no_verdict_without_the_ledgers_decision():
    with pytest.raises(SpendNotDecided):
        verdict_for(policy(effect=ActionEffect.SPENDS))


def test_value_that_is_not_a_decision_of_the_ledger_is_refused():
    with pytest.raises(ValueError, match="approve"):
        verdict_for(policy(effect=ActionEffect.SPENDS), "approve")


@pytest.mark.parametrize(
    ("effect", "held"),
    [(ActionEffect.INTERNAL, False), (ActionEffect.PUBLISHES, True), (ActionEffect.SPENDS, True)],
)
def test_actions_that_publish_or_spend_may_wait_for_a_person(effect, held):
    assert may_wait_for_a_person(policy(effect=effect)) is held


@pytest.mark.parametrize(
    ("effect", "weighed"),
    [(ActionEffect.INTERNAL, False), (ActionEffect.PUBLISHES, False), (ActionEffect.SPENDS, True)],
)
def test_only_an_action_that_spends_is_weighed_by_the_budget(effect, weighed):
    assert spends(policy(effect=effect)) is weighed


def test_effect_that_is_not_one_of_the_declared_ones_is_refused():
    with pytest.raises(ValueError, match="effect"):
        policy(effect="internal")


@pytest.mark.parametrize("wait", [0, -1, float("nan"), 30 * 24 * 60 * 60 + 1])
def test_approval_wait_that_is_not_a_positive_number_within_a_month_is_refused(wait):
    with pytest.raises(ValueError, match="approval_wait_seconds"):
        policy(approval_wait_seconds=wait)
