"""Tests for what the runtime's activities do for a writing run, as plain functions.

The context of an attempt (its model and the reading of the approved profile), the handlers of
``store_draft`` and ``publish_post``, and the projection a finished run reports, against
PostgreSQL, with the writer and the critic scripted. The graph's own steps are run through the
activities the worker registers, in Temporal's activity environment, which needs no server.
"""

import uuid

import pytest
from temporalio.exceptions import ApplicationError
from writer_fakes import POST, ScriptedChat, a_draft, a_plan, a_review, one_round_that_passes

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.credentials.resolution import ModelKeyNotSet
from openmarketer_core.db.content_items import settle_content_item
from openmarketer_core.db.models import ContentItemStatus
from openmarketer_core.db.session import transaction
from openmarketer_core.llm import LLMError
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND
from openmarketer_core.writing.writer_agent.definitions import (
    PUBLISH_POST,
    STORE_DRAFT,
    PublishPost,
    StoreDraft,
)
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.writing import WritingWork


def graph_run(run) -> GraphRunRef:
    return GraphRunRef(kind=WRITING_RUN_KIND, run_id=run.run_id)


async def drafted(activities, run) -> ActionRequest:
    """The run's first step, taken: the ``store_draft`` it asks for."""
    asked = (await activities.step(run)).action
    assert asked is not None and asked.name == STORE_DRAFT
    return asked


def refused(work, *arguments) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        work(*arguments)
    return failure.value


@pytest.fixture
def activities(writer_registration, activities_of):
    return activities_of(writer_registration())


# ------------------------------------------------------------------ the context of an attempt
def test_reading_the_approved_profile_records_its_version_on_the_run(work, writer_database):
    asked = writer_database.requested(started=True)

    read = work.context(graph_run(asked.run), None).approved_profile()  # type: ignore[arg-type]

    assert read is not None and read.version_id == asked.version_id
    assert read.profile.product.name == "Acme Notes"
    assert writer_database.run(asked.run).profile_version_id == asked.version_id


def test_reading_the_profile_again_finds_the_version_recorded_before(work, writer_database):
    asked = writer_database.requested(started=True)
    context = work.context(graph_run(asked.run), None)  # type: ignore[arg-type]

    first, again = context.approved_profile(), context.approved_profile()

    assert first is not None and again is not None
    assert first.version_id == again.version_id == asked.version_id


def test_newer_version_approved_before_the_profile_is_read_again_ends_the_run(
    work, writer_database
):
    asked = writer_database.requested(started=True)
    context = work.context(graph_run(asked.run), None)  # type: ignore[arg-type]
    context.approved_profile()
    writer_database.approve_another_version(asked.run)

    failure = refused(context.approved_profile)

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "request the post again" in failure.message
    assert writer_database.run(asked.run).profile_version_id == asked.version_id


def test_project_without_an_approved_profile_reads_none_and_records_no_version(
    work, writer_database
):
    asked = writer_database.requested(approved=False, started=True)

    read = work.context(graph_run(asked.run), None).approved_profile()  # type: ignore[arg-type]

    assert read is None
    assert writer_database.run(asked.run).profile_version_id is None


def test_context_makes_no_model_and_resolves_no_key_until_a_node_asks_one(
    writer_parts, writer_database
):
    asked = writer_database.requested(started=True)
    made: list[uuid.UUID] = []

    def new_model(workspace_id: uuid.UUID):
        made.append(workspace_id)
        raise ModelKeyNotSet("OPENROUTER_API_KEY")

    parts = writer_parts(new_model=new_model)
    work = WritingWork(parts.sessions, parts.checkpoints, new_model=new_model, limits=parts.limits)
    context = work.context(graph_run(asked.run), None)  # type: ignore[arg-type]
    assert made == []
    with pytest.raises(ModelKeyNotSet):
        context.model.chat("writer", [])
    assert made == [asked.run.workspace_id]


def test_context_of_a_run_that_does_not_exist_is_refused_for_good(work):
    failure = refused(work.context, GraphRunRef(kind=WRITING_RUN_KIND, run_id=uuid.uuid4()), None)

    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)


# ----------------------------------------------------------------------------- store_draft
async def test_draft_is_stored_as_the_runs_proposed_item_with_its_version_and_cost(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    request = await drafted(activities, asked.run)

    answer = work.store_draft(StoreDraft.model_validate(request.payload))

    item = writer_database.item(asked.run)
    assert answer == {"content_item_id": str(item.id)}
    assert (item.status, item.text, item.proposal_id) == (ContentItemStatus.PROPOSED, POST, None)
    assert item.profile_version_id == asked.version_id
    assert str(item.cost_usd) == "0.030000"  # a plan, a draft and a review, a cent each
    assert item.plan["feature_ids"] == ["offline-sync"]
    assert item.claims[0]["evidence"][0]["file"] == "src/sync.ts"


async def test_draft_stored_again_gives_back_the_item_stored_first(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    payload = StoreDraft.model_validate((await drafted(activities, asked.run)).payload)

    first, again = work.store_draft(payload), work.store_draft(payload)

    assert first == again


async def test_another_draft_for_a_run_that_stored_one_is_refused_for_good(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    payload = StoreDraft.model_validate((await drafted(activities, asked.run)).payload)
    work.store_draft(payload)

    failure = refused(work.store_draft, payload.model_copy(update={"text": "Another post."}))

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)


async def test_draft_of_a_run_that_recorded_no_version_is_refused_for_good(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    payload = StoreDraft.model_validate((await drafted(activities, asked.run)).payload)
    other = writer_database.requested(started=True).run

    failure = refused(
        work.store_draft,
        payload.model_copy(
            update={
                "workspace_id": other.workspace_id,
                "project_id": other.project_id,
                "run_id": other.run_id,
            }
        ),
    )

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert "has not chosen its profile version" in failure.message


async def test_draft_of_a_run_of_another_workspace_is_refused_for_good(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    payload = StoreDraft.model_validate((await drafted(activities, asked.run)).payload)

    failure = refused(work.store_draft, payload.model_copy(update={"workspace_id": uuid.uuid4()}))

    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)


# ---------------------------------------------------------------------------- publish_post
async def asked_to_publish(activities, run) -> ActionRequest:
    """The run's draft stored, and the ``publish_post`` its next step asks for."""
    request = await drafted(activities, run)
    stored = await activities.act(request)
    asked = (await activities.step(run, stored)).action
    assert asked is not None and asked.name == PUBLISH_POST
    return asked


async def stored_post(activities, run) -> PublishPost:
    return PublishPost.model_validate((await asked_to_publish(activities, run)).payload)


async def test_approved_post_is_recorded_as_a_dry_run_and_sent_nowhere(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    payload = await stored_post(activities, asked.run)

    answer = work.publish_post(payload)

    item = writer_database.item(asked.run)
    assert answer == {"content_item_id": str(item.id), "status": "dry_run"}
    assert item.status is ContentItemStatus.DRY_RUN and item.settled_at is not None
    # The handler is not given the proposal, so the item names none.
    assert item.proposal_id is None


async def test_post_recorded_again_changes_nothing(work, activities, writer_database):
    asked = writer_database.requested(started=True)
    payload = await stored_post(activities, asked.run)
    work.publish_post(payload)
    settled_at = writer_database.item(asked.run).settled_at

    work.publish_post(payload)

    assert writer_database.item(asked.run).settled_at == settled_at


async def test_post_whose_text_is_not_the_stored_one_is_refused_and_not_recorded(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    payload = await stored_post(activities, asked.run)

    failure = refused(work.publish_post, payload.model_copy(update={"text": "Something else."}))

    assert (failure.type, failure.non_retryable) == (steps.WRITING_FAILED, True)
    assert writer_database.item(asked.run).status is ContentItemStatus.PROPOSED


async def test_post_settled_another_way_before_is_refused_for_good(
    work, activities, writer_database, sessions
):
    asked = writer_database.requested(started=True)
    payload = await stored_post(activities, asked.run)
    with transaction(sessions) as session:
        settle_content_item(
            session,
            workspace_id=asked.run.workspace_id,
            project_id=asked.run.project_id,
            item_id=payload.content_item_id,
            status=ContentItemStatus.EXPIRED,
        )

    failure = refused(work.publish_post, payload)

    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert writer_database.item(asked.run).status is ContentItemStatus.EXPIRED


# ---------------------------------------------------------------------------- the projection
async def test_finished_run_reports_identifiers_status_and_cost_and_no_post(
    work, activities, writer_database
):
    asked = writer_database.requested(started=True)
    request = await asked_to_publish(activities, asked.run)
    payload = PublishPost.model_validate(request.payload)
    published = ActionOutcome(request.action_id, True, work.publish_post(payload), "")

    output = (await activities.step(asked.run, published)).output

    assert output == {
        "status": "dry_run",
        "content_item_id": str(payload.content_item_id),
        "profile_version_id": str(asked.version_id),
        "cost_usd": "0.03",
        "rounds": 1,
        "passed_critic": True,
        "error": "",
    }


# ----------------------------------------------------------------------- the steps, worded
async def test_model_provider_that_refuses_the_request_ends_the_run_in_its_words(
    writer_registration, activities_of, writer_database
):
    refusal = LLMError("the model provider refused the request (HTTP 400)", status=400)
    activities = activities_of(writer_registration(model=ScriptedChat(writer=[refusal])))
    asked = writer_database.requested(started=True)

    with pytest.raises(ApplicationError) as failure:
        await activities.step(asked.run)

    assert (failure.value.type, failure.value.non_retryable) == (steps.WRITING_FAILED, True)
    assert failure.value.message == str(refusal)


async def test_model_provider_that_limited_the_rate_is_worth_another_attempt(
    writer_registration, activities_of, writer_database
):
    limited = LLMError("the model provider answered HTTP 429", status=429)
    model = ScriptedChat(writer=[limited, a_plan(), a_draft()], critic=[a_review()])
    activities = activities_of(writer_registration(model=model))
    asked = writer_database.requested(started=True)

    with pytest.raises(ApplicationError) as failure:
        await activities.step(asked.run)
    again = await activities.step(asked.run)

    assert (failure.value.type, failure.value.non_retryable) == (steps.MODEL_UNAVAILABLE, False)
    assert again.action is not None and again.action.name == STORE_DRAFT


async def test_project_without_an_approved_profile_ends_the_run_saying_so(
    writer_registration, activities_of, writer_database
):
    activities = activities_of(writer_registration(model=one_round_that_passes()))
    asked = writer_database.requested(approved=False, started=True)

    with pytest.raises(ApplicationError) as failure:
        await activities.step(asked.run)

    assert (failure.value.type, failure.value.non_retryable) == (steps.WRITING_FAILED, True)
    assert failure.value.message == "the project has no approved product profile to write from"
