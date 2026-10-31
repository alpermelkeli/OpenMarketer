"""Tests for the writer's settings, its longest run, its wiring and how the workflow reads a result.

All plain functions: the settings from an environment mapping, the policy's arithmetic, what
the registration holds, the warning of one model for both roles, and ``outcome_of``, which is
workflow code that calls nothing of Temporal's.
"""

import logging
import uuid
from pathlib import Path

import pytest
import yaml
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import DEFAULT_APPROVAL_WAIT_SECONDS
from openmarketer_core.llm_config import ConfigError, ModelRouter
from openmarketer_core.writing.workflow_contract import WRITING_TASK_QUEUE, WritingRunInput
from openmarketer_core.writing.writer_agent.rules import Limits
from openmarketer_worker.agent_runtime.vocabulary import PROPOSE_ACTION, SETTLE_PROPOSAL
from openmarketer_worker.settings import SettingsError
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.policy import WriterPolicy, longest_writing_seconds
from openmarketer_worker.writer.settings import WriterSettings
from openmarketer_worker.writer.wiring import warn_of_one_model_for_both_roles
from openmarketer_worker.writer.workflow import outcome_of

MODELS = Path(__file__).resolve().parents[4] / "config" / "models.yaml"
RUN = WritingRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
ITEM = str(uuid.uuid4())


# ------------------------------------------------------------------------------- settings
def test_settings_default_to_three_rounds_half_a_dollar_and_the_researchers_timeouts():
    settings = WriterSettings.from_env({})

    assert settings.limits == Limits(max_rounds=3, max_cost_usd=0.50)
    assert settings.policy == WriterPolicy(
        attempt_timeout_seconds=30 * 60, heartbeat_timeout_seconds=60, max_attempts=5
    )


def test_settings_take_what_the_operator_set():
    settings = WriterSettings.from_env(
        {
            "WRITER_MAX_ROUNDS": "2",
            "WRITER_MAX_COST_USD": "0.25",
            "WRITER_TIMEOUT_MINUTES": "10",
            "WRITER_MAX_ATTEMPTS": "3",
        }
    )

    assert settings.limits == Limits(max_rounds=2, max_cost_usd=0.25)
    assert settings.policy.attempt_timeout_seconds == 600
    assert settings.policy.max_attempts == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("WRITER_MAX_ROUNDS", "1.5"),
        ("WRITER_MAX_ROUNDS", "0"),
        ("WRITER_MAX_COST_USD", "0"),
        ("WRITER_MAX_COST_USD", "free"),
        ("WRITER_MAX_COST_USD", "inf"),
        ("WRITER_TIMEOUT_MINUTES", "soon"),
        ("WRITER_MAX_ATTEMPTS", "2.5"),
    ],
)
def test_settings_refuse_a_value_that_does_not_fit_naming_the_variable(name, value):
    with pytest.raises(SettingsError, match=name):
        WriterSettings.from_env({name: value})


# ------------------------------------------------------------------------- the longest run
def test_longest_run_covers_the_wait_for_a_persons_decision_on_the_post():
    longest = longest_writing_seconds(WriterPolicy())

    assert longest > DEFAULT_APPROVAL_WAIT_SECONDS + 3 * 30 * 60


def test_longest_run_grows_with_the_time_an_attempt_may_take():
    short = longest_writing_seconds(WriterPolicy(attempt_timeout_seconds=60))
    long = longest_writing_seconds(WriterPolicy(attempt_timeout_seconds=600))

    # Three steps, five attempts each.
    assert long - short == pytest.approx(3 * 5 * 540)


# ------------------------------------------------------------------------------- the wiring
def test_registration_serves_the_writer_with_the_gate_and_its_hooks_activities(
    writer_registration,
):
    registration = writer_registration()

    names = {activity.__name__ for activity in registration.activities}
    assert registration.task_queue == WRITING_TASK_QUEUE
    assert {steps.START_RUN, steps.RECORD_OUTCOME, steps.RECORD_FAILURE} <= names
    assert {PROPOSE_ACTION, SETTLE_PROPOSAL} <= names
    assert [workflow.__name__ for workflow in registration.workflows] == ["WritePost"]


def test_registration_removes_left_over_checkpoints_of_writing_runs(writer_registration):
    registration = writer_registration()

    runs = [cleanup.runs for cleanup in registration.checkpoint_cleanups]
    assert runs[0] == "writing runs"
    assert len(runs) == 2  # the writing runs' rule, and core's rule for the kind's threads


def test_one_model_for_the_writer_and_the_critic_is_a_warning(caplog):
    writer = ModelRouter.from_file(MODELS, env={}).resolve("writer").model
    router = ModelRouter.from_file(MODELS, env={"LLM_MODEL__CRITIC": writer})

    with caplog.at_level(logging.WARNING, logger="openmarketer_worker.writer.wiring"):
        warn_of_one_model_for_both_roles(router)

    [warning] = caplog.records
    assert warning.levelno == logging.WARNING
    assert router.resolve("writer").model in warning.getMessage()


def test_a_critic_of_another_model_is_no_warning(caplog):
    router = ModelRouter.from_file(MODELS, env={"LLM_MODEL__CRITIC": "vendor/another-model"})

    with caplog.at_level(logging.WARNING, logger="openmarketer_worker.writer.wiring"):
        warn_of_one_model_for_both_roles(router)

    assert caplog.records == []


def test_a_configuration_without_the_critic_stops_the_worker(tmp_path):
    config = yaml.safe_load(MODELS.read_text())
    del config["roles"]["critic"]
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(config))

    with pytest.raises(ConfigError, match="critic"):
        warn_of_one_model_for_both_roles(ModelRouter.from_file(path, env={}))


# ------------------------------------------------------------------ the result, as read
@pytest.mark.parametrize("status", ["dry_run", "rejected", "expired"])
def test_a_post_a_person_decided_on_is_settled_as_it_ended(status):
    output = {"status": status, "content_item_id": ITEM, "cost_usd": "0.03", "error": ""}

    assert outcome_of(RUN, output) == steps.RunOutcome(
        run=RUN, content_item_id=uuid.UUID(ITEM), status=status, cost_usd="0.03"
    )


def test_an_approved_post_that_was_not_recorded_ends_the_run_with_why():
    output = {
        "status": "failed",
        "content_item_id": ITEM,
        "cost_usd": "0.03",
        "error": "the action did not finish in time",
    }

    with pytest.raises(ApplicationError) as failure:
        outcome_of(RUN, output)

    assert (failure.value.type, failure.value.non_retryable) == (steps.WRITING_FAILED, True)
    assert failure.value.message == (
        "the approved post could not be recorded: the writer did not finish in time"
    )


@pytest.mark.parametrize(
    "output",
    [
        {},
        {"status": "proposed", "content_item_id": ITEM, "cost_usd": "0"},
        {"status": "dry_run", "content_item_id": "not an id", "cost_usd": "0"},
        {"status": "dry_run", "content_item_id": ITEM, "cost_usd": 0.03},
    ],
)
def test_a_result_that_cannot_be_read_ends_the_run(output):
    with pytest.raises(ApplicationError) as failure:
        outcome_of(RUN, output)

    assert failure.value.type == steps.WRITING_FAILED
    assert failure.value.message == steps.RESULT_UNREADABLE
