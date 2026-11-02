"""Tests for what a post is written with: the image and the research run its brief names.

The readings of an attempt's context (``post_image``, ``research_findings``), which read them
within the run's project as they are now and word why one cannot be used, and the handlers of
``store_draft``, which stores them with the draft, and ``publish_post``, which records only the
image the person approved and nobody rejected since. Against PostgreSQL, with the writer and the
critic scripted; the graph's steps run through the activities the worker registers.
"""

import uuid

import pytest
from sqlalchemy import text
from temporalio.exceptions import ApplicationError
from writer_fakes import FINDING, IMAGE_REQUEST, QUOTES

from openmarketer_core.agent_runtime.actions import ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import ContentItemStatus
from openmarketer_core.db.session import transaction
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.definitions import (
    PUBLISH_POST,
    STORE_DRAFT,
    PublishPost,
    StoreDraft,
)
from openmarketer_worker.writer import steps


def graph_run(run) -> GraphRunRef:
    return GraphRunRef(kind=WRITING_RUN_KIND, run_id=run.run_id)


def context_of(work, run):
    return work.context(graph_run(run), None)


def refused(work, *arguments) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        work(*arguments)
    return failure.value


@pytest.fixture
def activities(writer_registration, activities_of):
    return activities_of(writer_registration())


async def drafted(activities, run) -> ActionRequest:
    asked = (await activities.step(run)).action
    assert asked is not None and asked.name == STORE_DRAFT
    return asked


async def asked_to_publish(activities, run) -> ActionRequest:
    stored = await activities.act(await drafted(activities, run))
    asked = (await activities.step(run, stored)).action
    assert asked is not None and asked.name == PUBLISH_POST
    return asked


# ------------------------------------------------------------------------ the post's image
def test_image_is_read_as_its_run_was_asked_with_its_current_version(work, writer_database):
    asked = writer_database.requested(started=True, with_image=True)
    assert asked.image is not None

    image = context_of(work, asked.run).post_image(asked.image.asset_id)

    assert (image.id, image.version_id) == (asked.image.asset_id, asked.image.version_id)
    assert (image.status, image.platform) == ("draft", "instagram_post")
    assert (image.goal, tuple(image.exact_copy)) == (
        IMAGE_REQUEST.goal,
        tuple(IMAGE_REQUEST.exact_copy),
    )


def test_image_a_person_rejected_since_cannot_go_with_the_post(work, writer_database):
    asked = writer_database.requested(started=True, with_image=True)
    assert asked.image is not None
    writer_database.reject(asked.run, asked.image)

    with pytest.raises(rules.PostImageNotUsable) as refusal:
        context_of(work, asked.run).post_image(asked.image.asset_id)

    assert str(refusal.value) == (
        f"the asset {asked.image.asset_id} cannot go with the post: a person rejected it"
    )


def test_image_of_another_project_is_not_the_projects(work, writer_database):
    asked = writer_database.requested(started=True)
    other = writer_database.requested(with_image=True).image
    assert other is not None

    with pytest.raises(rules.PostImageNotUsable) as refusal:
        context_of(work, asked.run).post_image(other.asset_id)

    assert str(refusal.value).endswith("cannot go with the post: the project has no such asset")


# ---------------------------------------------------------------------- the post's research
def test_findings_are_read_bounded_with_how_many_were_left_out(work, writer_database):
    asked = writer_database.requested(started=True)
    run_id = writer_database.another_research_run(asked.run, findings=rules.MAX_POST_FINDINGS + 2)

    research = context_of(work, asked.run).research_findings(run_id)

    assert research.run_id == run_id
    assert (len(research.findings), research.left_out) == (rules.MAX_POST_FINDINGS, 2)
    first = research.findings[0]
    assert first.statement == FINDING
    assert [tuple(source.quotes) for source in first.sources] == [QUOTES[: rules.MAX_SITE_QUOTES]]


def test_research_run_that_has_not_succeeded_cannot_be_drawn_on(work, writer_database):
    asked = writer_database.requested(started=True)
    run_id = writer_database.another_research_run(asked.run, succeeded=False)

    with pytest.raises(rules.ResearchNotUsable) as refusal:
        context_of(work, asked.run).research_findings(run_id)

    assert str(refusal.value) == (
        f"the research run {run_id} cannot be drawn on: it is queued, not succeeded"
    )


def test_research_run_of_another_project_is_not_the_projects(work, writer_database):
    asked = writer_database.requested(started=True)
    other = writer_database.requested(with_research=True).research_run_id
    assert other is not None

    with pytest.raises(rules.ResearchNotUsable) as refusal:
        context_of(work, asked.run).research_findings(other)

    assert str(refusal.value).endswith("cannot be drawn on: the project has no such run")


def test_findings_stored_in_another_shape_cannot_be_drawn_on(work, writer_database, sessions):
    asked = writer_database.requested(started=True, with_research=True)
    assert asked.research_run_id is not None
    with transaction(sessions) as session:
        session.execute(
            text("UPDATE research_report SET findings = :findings WHERE project_id = :project"),
            {"findings": '[{"statement": ""}]', "project": asked.run.project_id},
        )

    with pytest.raises(rules.ResearchNotUsable) as refusal:
        context_of(work, asked.run).research_findings(asked.research_run_id)

    assert str(refusal.value).endswith("its stored findings do not fit their schema")


async def test_image_rejected_before_the_run_reads_it_ends_the_run_saying_why(
    activities, writer_database
):
    asked = writer_database.requested(started=True, with_image=True)
    assert asked.image is not None
    writer_database.reject(asked.run, asked.image)

    with pytest.raises(ApplicationError) as failure:
        await activities.step(asked.run)

    assert (failure.value.type, failure.value.non_retryable) == (steps.WRITING_FAILED, True)
    assert failure.value.message.endswith("cannot go with the post: a person rejected it")


# ----------------------------------------------------------------------------- store_draft
async def test_draft_is_stored_with_the_image_its_version_and_the_research(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True, with_image=True, with_research=True)
    assert asked.image is not None
    payload = StoreDraft.model_validate((await drafted(activities, asked.run)).payload)

    work.store_draft(payload)

    item = writer_database.item(asked.run)
    assert (item.asset_id, item.asset_version_id, item.research_run_id) == (
        asked.image.asset_id,
        asked.image.version_id,
        asked.research_run_id,
    )


async def test_draft_naming_an_image_of_another_project_is_refused_for_good(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True, with_image=True)
    payload = StoreDraft.model_validate((await drafted(activities, asked.run)).payload)
    other = writer_database.requested(with_image=True).image
    assert other is not None

    failure = refused(
        work.store_draft,
        payload.model_copy(
            update={"asset_id": other.asset_id, "asset_version_id": other.version_id}
        ),
    )

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)


# ---------------------------------------------------------------------------- publish_post
async def test_post_is_recorded_with_the_image_version_the_person_approved(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True, with_image=True)
    assert asked.image is not None
    request = await asked_to_publish(activities, asked.run)
    writer_database.held(asked.run, request)
    payload = PublishPost.model_validate(request.payload)
    assert payload.asset_version_id == asked.image.version_id

    work.publish_post(payload, request.action_id)

    item = writer_database.item(asked.run)
    assert (item.status, item.asset_version_id) == (
        ContentItemStatus.DRY_RUN,
        asked.image.version_id,
    )


async def test_post_whose_image_version_is_not_the_stored_one_is_refused_and_not_recorded(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True, with_image=True)
    request = await asked_to_publish(activities, asked.run)
    writer_database.held(asked.run, request)
    payload = PublishPost.model_validate(request.payload)

    failure = refused(
        work.publish_post,
        payload.model_copy(update={"asset_version_id": uuid.uuid4()}),
        request.action_id,
    )

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED


async def test_post_whose_image_was_rejected_since_is_refused_and_not_recorded(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True, with_image=True)
    assert asked.image is not None
    request = await asked_to_publish(activities, asked.run)
    writer_database.held(asked.run, request)
    payload = PublishPost.model_validate(request.payload)
    writer_database.reject(asked.run, asked.image)

    failure = refused(work.publish_post, payload, request.action_id)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert failure.message == (
        f"the asset {asked.image.asset_id} cannot go with the post: a person rejected it"
    )
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED
