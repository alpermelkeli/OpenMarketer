"""Tests for the task's settings, its longest run, its wiring, and the ceilings of the runs it
starts. All plain functions: the settings from an environment mapping, the policy's arithmetic,
what the registration holds, and the refusals of each child's worker at start-up."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from openmarketer_core.agent_runtime.actions import DEFAULT_APPROVAL_WAIT_SECONDS
from openmarketer_core.llm_config import ConfigError, ModelRouter
from openmarketer_core.orchestrator.task.plan import MAX_TASK_DEADLINE_DAYS
from openmarketer_core.orchestrator.task.task_agent.rules import TaskLimits
from openmarketer_core.orchestrator.task.workflow_contract import TASK_TASK_QUEUE
from openmarketer_core.studio.workflow_contract import STUDIO_LONGEST_RUN_SECONDS
from openmarketer_worker.agent_runtime.vocabulary import (
    PREPARE_RUN,
    PROPOSE_ACTION,
    SETTLE_PROPOSAL,
)
from openmarketer_worker.orchestrator_task import steps
from openmarketer_worker.orchestrator_task.policy import (
    TaskPolicy,
    longest_child_seconds,
    longest_task_seconds,
)
from openmarketer_worker.orchestrator_task.settings import TaskSettings
from openmarketer_worker.orchestrator_task.wiring import require_the_role
from openmarketer_worker.settings import SettingsError, require_a_run_within

MODELS = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
DAY = 24 * 60 * 60


# ------------------------------------------------------------------------------- settings
def test_settings_default_to_six_steps_two_changes_a_week_and_a_dollar():
    assert TaskSettings.from_env({}).limits == TaskLimits(
        max_steps=6, max_plan_changes=2, deadline_days=7, max_cost_usd=1.0
    )


def test_settings_take_what_the_operator_set():
    settings = TaskSettings.from_env(
        {
            "TASK_MAX_STEPS": "3",
            "TASK_MAX_PLAN_CHANGES": "0",
            "TASK_DEADLINE_DAYS": "2",
            "TASK_MAX_COST_USD": "0.5",
        }
    )

    assert settings.limits == TaskLimits(
        max_steps=3, max_plan_changes=0, deadline_days=2, max_cost_usd=0.5
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TASK_MAX_STEPS", "0"),
        ("TASK_MAX_STEPS", "13"),
        ("TASK_MAX_STEPS", "two"),
        ("TASK_MAX_PLAN_CHANGES", "-1"),
        ("TASK_MAX_PLAN_CHANGES", "1.5"),
        ("TASK_DEADLINE_DAYS", "31"),
        ("TASK_MAX_COST_USD", "0"),
        ("TASK_MAX_COST_USD", "21"),
        ("TASK_MAX_COST_USD", "nan"),
    ],
)
def test_a_setting_that_does_not_fit_stops_the_worker_naming_it(name, value):
    with pytest.raises(SettingsError, match=name):
        TaskSettings.from_env({name: value})


# ------------------------------------------------------------------------- longest task
def test_longest_task_outlasts_its_longest_deadline_a_confirmation_and_the_longest_child():
    longest = longest_task_seconds(TaskPolicy())

    assert longest_child_seconds() == STUDIO_LONGEST_RUN_SECONDS
    floor = (
        MAX_TASK_DEADLINE_DAYS * DAY + DEFAULT_APPROVAL_WAIT_SECONDS + STUDIO_LONGEST_RUN_SECONDS
    )
    assert floor < longest < floor + 3 * DAY


def test_longer_steps_make_a_longer_task():
    slow = replace(TaskPolicy(), attempt_timeout_seconds=60 * 60)

    assert longest_task_seconds(slow) > longest_task_seconds(TaskPolicy())


# -------------------------------------------------------------------------------- wiring
def test_registration_serves_the_task_on_its_queue_with_the_gate_and_the_runs_it_prepares(
    task_registration,
):
    registration = task_registration()
    names = {activity.__name__ for activity in registration.activities}

    assert registration.task_queue == TASK_TASK_QUEUE
    assert [workflow.__name__ for workflow in registration.workflows] == ["OrchestratorTask"]
    assert {steps.START_TASK, steps.RECORD_END, steps.RECORD_FAILURE} <= names
    assert {PROPOSE_ACTION, SETTLE_PROPOSAL, PREPARE_RUN} <= names
    # Core's rule for threads nothing wrote for a task's length, and the tasks' own.
    assert [cleanup.runs for cleanup in registration.checkpoint_cleanups][0] == "orchestrator tasks"
    assert len(registration.checkpoint_cleanups) == 2


def test_a_configuration_without_the_orchestrators_role_stops_the_worker(tmp_path):
    config = yaml.safe_load(MODELS.read_text())
    config["roles"].pop("orchestrator")
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(config))

    with pytest.raises(ConfigError, match="orchestrator"):
        require_the_role(ModelRouter.from_file(str(path), env={}))


# ------------------------------------------------------- the runs a task waits for
def test_a_run_that_can_last_longer_than_its_contracts_ceiling_is_refused_naming_its_kind():
    with pytest.raises(SettingsError, match="studio run can last 61.0 days"):
        require_a_run_within(61 * DAY, 60 * DAY, runs="studio")

    require_a_run_within(60 * DAY, 60 * DAY, runs="studio")
    assert timedelta(seconds=longest_task_seconds(TaskPolicy())) > timedelta(days=60)
