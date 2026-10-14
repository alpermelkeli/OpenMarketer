"""Tests for the policy gate's verdict: which actions run and which wait for a person."""

import pytest

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionPolicy
from openmarketer_core.agent_runtime.gate import GateVerdict, verdict_for


def policy(**changes) -> ActionPolicy:
    return ActionPolicy(
        attempt_timeout_seconds=30, max_attempts=1, retry_after_seconds=1, **changes
    )


def test_action_that_works_for_the_run_itself_runs():
    assert verdict_for(policy()) is GateVerdict.RUN


@pytest.mark.parametrize("effect", [ActionEffect.PUBLISHES, ActionEffect.SPENDS])
def test_action_that_publishes_or_spends_waits_for_a_person(effect):
    assert verdict_for(policy(effect=effect)) is GateVerdict.ASK_A_PERSON


def test_effect_that_is_not_one_of_the_declared_ones_is_refused():
    with pytest.raises(ValueError, match="effect"):
        policy(effect="internal")


@pytest.mark.parametrize("wait", [0, -1, float("nan"), 30 * 24 * 60 * 60 + 1])
def test_approval_wait_that_is_not_a_positive_number_within_a_month_is_refused(wait):
    with pytest.raises(ValueError, match="approval_wait_seconds"):
        policy(approval_wait_seconds=wait)
