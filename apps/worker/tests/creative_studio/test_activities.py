"""Tests for the activities the hooks of ``CreativeStudio`` call, run as plain functions.

They run in Temporal's activity environment against PostgreSQL; skipped when it is not
reachable.
"""

import uuid

import pytest
from studio_fakes import IMAGE_COST, QUICK
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from openmarketer_core.db.models import StudioRunStatus
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import studio_run
from openmarketer_core.studio.studio_agent.definitions import GenerateImage
from openmarketer_core.studio.studio_agent.rules import Technique
from openmarketer_core.studio.workflow_contract import StudioRunInput
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.creative_studio import steps
from openmarketer_worker.creative_studio.activities import StudioActivities


@pytest.fixture
def activities(sessions) -> StudioActivities:
    return StudioActivities(sessions, QUICK)


def the_run(sessions, run):
    with transaction(sessions) as session:
        return studio_run(
            session, workspace_id=run.workspace_id, project_id=run.project_id, run_id=run.run_id
        )


def test_start_marks_the_run_running_and_gives_the_workers_limits(
    activities, studio_database, sessions
):
    run = studio_database.requested().run

    assert ActivityEnvironment().run(activities.start_run, run) == QUICK
    assert ActivityEnvironment().run(activities.start_run, run) == QUICK
    assert the_run(sessions, run).status is StudioRunStatus.RUNNING


def test_start_of_a_run_not_stored_yet_is_tried_again(activities):
    missing = StudioRunInput(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())

    with pytest.raises(ApplicationError) as refused:
        ActivityEnvironment().run(activities.start_run, missing)

    assert (refused.value.type, refused.value.non_retryable) == (steps.RUN_NOT_FOUND_YET, False)


def test_failure_is_recorded_with_what_the_runs_stored_images_cost(
    activities, studio_database, sessions, work
):
    run = studio_database.requested(started=True).run
    payload = GenerateImage(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=run.run_id,
        version_id=uuid.uuid4(),
        asset_id=None,
        round=1,
        technique=Technique.GENERATED,
        prompt="A desk.",
    )
    work.generate_image(payload, ActionContext(action_id="studio-run:a:1"))

    ActivityEnvironment().run(
        activities.record_failure, steps.RunFailure(run=run, error="the studio did not finish")
    )

    stored = the_run(sessions, run)
    assert (stored.status, stored.error, stored.cost_usd) == (
        StudioRunStatus.FAILED,
        "the studio did not finish",
        IMAGE_COST,
    )


def test_end_of_a_run_a_chat_asked_for_names_the_chat(activities, studio_database):
    asked = studio_database.requested(from_a_chat=True)

    told = ActivityEnvironment().run(
        activities.record_end, steps.RunEnd(run=asked.run, reason="the studio stopped")
    )

    assert told == steps.RunEndRecorded(chat_id=asked.chat_id)


def test_end_of_a_run_of_another_workspace_tells_nobody(activities, studio_database):
    asked = studio_database.requested(from_a_chat=True)
    elsewhere = StudioRunInput(uuid.uuid4(), asked.run.project_id, asked.run.run_id)

    told = ActivityEnvironment().run(
        activities.record_end, steps.RunEnd(run=elsewhere, reason="the studio stopped")
    )

    assert told == steps.RunEndRecorded(chat_id=None)
