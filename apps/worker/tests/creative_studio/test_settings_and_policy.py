"""Tests for the studio's settings read from the environment, and the longest a run lasts."""

from datetime import timedelta

import pytest

from openmarketer_core.agent_runtime.actions import DEFAULT_APPROVAL_WAIT_SECONDS
from openmarketer_core.studio.studio_agent import rules
from openmarketer_worker.creative_studio.policy import (
    StudioPolicy,
    graph_steps,
    longest_studio_seconds,
    operations_of_a_run,
)
from openmarketer_worker.creative_studio.settings import StudioSettings
from openmarketer_worker.settings import SettingsError


def test_environment_sets_the_runs_limits_and_the_sandbox():
    settings = StudioSettings.from_env(
        {
            "STUDIO_TIMEOUT_MINUTES": "5",
            "STUDIO_MAX_ITERATIONS": "2",
            "STUDIO_MAX_COST_PER_ASSET_USD": "0.50",
            "STUDIO_SANDBOX_IMAGE": "example/sandbox:1",
            "STUDIO_SANDBOX_CPUS": "0.5",
            "STUDIO_SANDBOX_MAX_OUTPUTS": "3",
            "STUDIO_SANDBOX_RUNTIME": "runsc",
        }
    )

    assert settings.policy.attempt_timeout_seconds == 300
    assert (settings.limits.max_rounds, settings.limits.max_cost_usd) == (2, 0.5)
    assert (settings.sandbox.image, settings.sandbox.cpus) == ("example/sandbox:1", 0.5)
    assert (settings.sandbox.outputs.max_files, settings.sandbox.runtime) == (3, "runsc")


def test_nothing_set_is_the_defaults():
    settings = StudioSettings.from_env({})

    assert settings == StudioSettings()
    assert settings.sandbox.runtime is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("STUDIO_MAX_ITERATIONS", "2.5"),
        ("STUDIO_MAX_ITERATIONS", "soon"),
        ("STUDIO_MAX_COST_PER_ASSET_USD", "0"),
        ("STUDIO_SANDBOX_CPUS", "-1"),
    ],
)
def test_value_that_does_not_fit_is_refused_with_its_name(name, value):
    with pytest.raises(SettingsError, match=name):
        StudioSettings.from_env({name: value})


def test_sandbox_whose_program_cannot_finish_in_its_actions_time_is_refused():
    with pytest.raises(SettingsError, match="STUDIO_SANDBOX_TIMEOUT_S"):
        StudioSettings.from_env({"STUDIO_SANDBOX_TIMEOUT_S": "600"})


def test_longest_run_covers_every_paid_image_waiting_for_a_person():
    limits = rules.Limits()

    longest = longest_studio_seconds(StudioPolicy(), limits)

    assert longest > operations_of_a_run(limits) * DEFAULT_APPROVAL_WAIT_SECONDS
    assert timedelta(seconds=longest) > timedelta(days=3)


def test_more_rounds_make_a_longer_run():
    fewer, more = rules.Limits(max_rounds=1), rules.Limits(max_rounds=4)

    assert graph_steps(more) > graph_steps(fewer)
    assert longest_studio_seconds(StudioPolicy(), more) > longest_studio_seconds(
        StudioPolicy(), fewer
    )
