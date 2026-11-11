"""Tests for the writer's settings, its longest run, its wiring and how the workflow reads a result.

All plain functions: the settings from an environment mapping, the policy's arithmetic, what
the registration holds, the roles the wiring needs, and ``outcome_of``, which is
workflow code that calls nothing of Temporal's.
"""

import uuid
from pathlib import Path

import pytest
import yaml
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import DEFAULT_APPROVAL_WAIT_SECONDS
from openmarketer_core.llm_config import ConfigError, ModelRouter
from openmarketer_core.social_accounts.catalog import SocialPlatform
from openmarketer_core.writing.workflow_contract import WRITING_TASK_QUEUE, WritingRunInput
from openmarketer_core.writing.writer_agent.definitions import PUBLISH_POST, agent
from openmarketer_core.writing.writer_agent.rules import Limits
from openmarketer_worker.agent_runtime.vocabulary import (
    PROPOSE_ACTION,
    RETRY_BACKOFF,
    SETTLE_PROPOSAL,
)
from openmarketer_worker.settings import SettingsError
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.policy import (
    LONGEST_PLATFORM_WAIT_SECONDS,
    WriterPolicy,
    longest_writing_seconds,
)
from openmarketer_worker.writer.settings import WriterSettings
from openmarketer_worker.writer.wiring import publishing_costs_of, require_the_writer_role
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


def test_a_configuration_without_the_writer_stops_the_worker(tmp_path):
    config = yaml.safe_load(MODELS.read_text())
    del config["roles"]["writer"]
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(config))

    with pytest.raises(ConfigError, match="the role writer"):
        require_the_writer_role(ModelRouter.from_file(path, env={}))


def test_the_publishing_configuration_of_the_repository_is_read_by_default(monkeypatch):
    monkeypatch.chdir(MODELS.parents[1])  # the worker runs from the repository's root

    costs = publishing_costs_of({})

    assert costs.estimate(SocialPlatform.X, with_image=False) > 0


@pytest.mark.parametrize("content", ["version: 1\nplatforms: {}\n", "version: [\n"])
def test_a_publishing_configuration_that_does_not_fit_stops_the_worker(tmp_path, content):
    path = tmp_path / "publishing.yaml"
    path.write_text(content)

    with pytest.raises(ConfigError, match="publishing.yaml"):
        publishing_costs_of({"PUBLISHING_CONFIG_PATH": str(path)})


def test_longest_run_covers_the_longest_waits_a_platform_may_ask_for_between_publishing():
    publish = agent.policies[PUBLISH_POST]
    waits = (publish.max_attempts - 1) * LONGEST_PLATFORM_WAIT_SECONDS
    without_publishing = longest_writing_seconds(WriterPolicy()) - (
        publish.max_attempts * publish.attempt_timeout_seconds + waits
    )

    assert without_publishing > DEFAULT_APPROVAL_WAIT_SECONDS


def test_publishing_is_tried_through_a_quarter_of_an_hour_of_rate_limit():
    publish = agent.policies[PUBLISH_POST]
    waits = sum(
        publish.retry_after_seconds * RETRY_BACKOFF**n for n in range(publish.max_attempts - 1)
    )

    assert waits >= 15 * 60


def test_a_configuration_without_the_critic_is_enough_for_the_writer(tmp_path):
    config = yaml.safe_load(MODELS.read_text())
    del config["roles"]["critic"]
    path = tmp_path / "models.yaml"
    path.write_text(yaml.safe_dump(config))

    require_the_writer_role(ModelRouter.from_file(path, env={}))


# ------------------------------------------------------------------ the result, as read
@pytest.mark.parametrize("status", ["published", "dry_run", "rejected", "expired"])
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
