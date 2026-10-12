"""Tests for what the runtime's activities do for a run of an analysis, as plain functions.

The activities are the ones the worker registers (``run_graph_step``, ``run_action``,
``forget_run``), run in Temporal's activity environment, which needs no server, against
PostgreSQL. The graph, its rules, the checkpoints and the clones' folders are real; the model
and the clone itself are scripted (``conftest.py`` of the worker's tests).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError

from openmarketer_core.agent_runtime.actions import ActionOutcome, ActionRequest
from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.db.session import DatabaseError
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis.analyzer_agent import AnalysisError
from openmarketer_core.repository_analysis.analyzer_agent.definitions import CLONE_REPOSITORY
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_core.repository_analysis.workflow_contract import AnalyzeRepositoryInput
from openmarketer_worker.agent_runtime import vocabulary
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.vocabulary import (
    ATTEMPT_STILL_RUNNING,
    DATABASE_UNAVAILABLE,
    UNEXPECTED,
)
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.clones import LocalClones

OUTAGE = LLMError("the model provider answered HTTP 503", status=503)
COMMIT = "a" * 40  # what the scripted intake says it cloned


def cannot_forget(checkpoints: CheckpointAccess) -> CheckpointAccess:
    """The same checkpoints, with a database that fails whenever a run is to be forgotten."""

    def forget(thread_id: str) -> None:
        raise DatabaseError("connection is closed")

    return replace(checkpoints, forget=forget)


def without_the_hold(checkpoints: CheckpointAccess) -> CheckpointAccess:
    """The same checkpoints, as when no attempt's hold on a run keeps another out."""

    @contextmanager
    def of_attempt(thread_id: str) -> Iterator[RunCheckpoints]:
        with checkpoints.of_attempt(f"{thread_id}-{uuid.uuid4()}") as held_elsewhere:
            yield replace(held_elsewhere, thread_id=thread_id)

    return replace(checkpoints, of_attempt=of_attempt)


async def refused(work: Awaitable[object]) -> ApplicationError:
    with pytest.raises(ApplicationError) as failure:
        await work
    return failure.value


def clone_asked_for(run: AnalyzeRepositoryInput, **changed: uuid.UUID) -> ActionRequest:
    """The clone as the graph of ``run`` asks for it, or with identifiers of somebody else's."""
    payload = {
        "workspace_id": str(run.workspace_id),
        "project_id": str(run.project_id),
        "run_id": str(run.run_id),
        **{name: str(value) for name, value in changed.items()},
    }
    return ActionRequest(action_id="thread:1", name=CLONE_REPOSITORY, payload=payload)


def told_the_clone_failed(asked: ActionRequest, error: str) -> ActionOutcome:
    return ActionOutcome(asked.action_id, succeeded=False, result={}, error=error)


# ------------------------------------------------------------------ the clone
async def test_first_step_asks_for_the_clone_with_the_identifiers_of_the_run(analyzer, started):
    asked = (await analyzer.step(started)).action
    assert (asked.name, asked.payload) == (
        CLONE_REPOSITORY,
        {
            "workspace_id": str(started.workspace_id),
            "project_id": str(started.project_id),
            "run_id": str(started.run_id),
        },
    )


async def test_clone_is_of_the_repository_of_the_project(analyzer, intake, started):
    await analyzer.cloned(started)
    assert [source for source, _, _ in intake.cloned] == ["https://example.com/acme/app.git"]


async def test_clone_answers_with_the_commit_and_nothing_else(analyzer, started):
    outcome = await analyzer.cloned(started)
    assert (outcome.succeeded, outcome.result) == (True, {"commit_sha": COMMIT})


async def test_clone_is_kept_in_the_folder_of_the_run(analyzer, intake, started):
    await analyzer.cloned(started)
    ((_, cloned_into, _),) = intake.cloned
    assert cloned_into.parent == analyzer.clones.folder_of(started.run_id)


async def test_repeated_clone_answers_with_the_same_commit_and_clones_once(
    analyzer, intake, started
):
    asked = (await analyzer.step(started)).action
    first = await analyzer.act(asked)
    again = await analyzer.act(asked)
    assert again == first
    assert len(intake.cloned) == 1


async def test_refused_repository_is_not_worth_another_attempt_at_the_clone(
    analyzer, intake, started
):
    intake.failures.append(IntakeError("the repository was not found"))
    failure = await refused(analyzer.cloned(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == "the repository was not found"


async def test_refused_repository_ends_the_analysis_with_what_the_clone_said(analyzer, started):
    asked = (await analyzer.step(started)).action
    told = told_the_clone_failed(asked, "the repository was not found")
    failure = await refused(analyzer.step(started, told))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == "the repository was not found"


async def test_failure_of_the_clone_does_not_name_the_folder_of_the_clone(
    analyzer, intake, started
):
    intake.failures.append(
        lambda folder: IntakeError(f"git clone failed: Cloning into '{folder}'... not found")
    )
    failure = await refused(analyzer.cloned(started))
    assert failure.message == "git clone failed: Cloning into '<clone>/repo'... not found"


async def test_failure_of_the_clone_carries_no_cause_into_the_history(analyzer, intake, started):
    intake.failures.append(IntakeError("git clone failed"))
    failure = await refused(analyzer.cloned(started))
    assert failure.__cause__ is None


async def test_clone_that_failed_leaves_nothing_on_the_disk(analyzer, intake, started):
    intake.failures.append(IntakeError("git clone failed"))
    await refused(analyzer.cloned(started))
    assert not analyzer.clones.folder_of(started.run_id).exists()


async def test_unexpected_error_of_the_clone_is_reported_without_its_detail(
    analyzer, intake, started
):
    intake.failures.append(RuntimeError("/Users/someone/secret"))
    failure = await refused(analyzer.cloned(started))
    assert (failure.type, failure.non_retryable) == (UNEXPECTED, False)
    assert failure.message == vocabulary.ACTION_TEXTS.stopped_unexpectedly
    assert failure.__cause__ is None


@pytest.mark.parametrize("repository", ["file:///etc", "/srv/repositories/app"])
async def test_project_stored_with_a_local_folder_is_not_cloned(
    analyzer, intake, database, repository
):
    run = database.requested_run(repository)
    analyzer.start(run)
    failure = await refused(analyzer.cloned(run))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert intake.cloned == []


async def test_clone_asked_for_from_another_workspace_touches_nothing(
    analyzer, intake, started, database
):
    elsewhere = clone_asked_for(started, workspace_id=database.new_workspace())
    failure = await refused(analyzer.act(elsewhere))
    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert intake.cloned == []
    assert database.run(started).status is AnalysisRunStatus.RUNNING


async def test_clone_asked_for_with_another_project_touches_nothing(
    analyzer, intake, started, database
):
    other = database.requested_run()
    failure = await refused(analyzer.act(clone_asked_for(started, project_id=other.project_id)))
    assert failure.type == steps.RUN_UNAVAILABLE
    assert intake.cloned == []


async def test_clone_of_a_run_that_is_over_is_refused(analyzer, intake, started):
    analyzer.record_failure(started, "no")
    failure = await refused(analyzer.act(clone_asked_for(started)))
    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert intake.cloned == []


async def test_step_of_a_run_that_does_not_exist_is_refused_for_good(analyzer, some_run):
    failure = await refused(analyzer.step(some_run()))
    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)


# ----------------------------------------------------------- the model's work
async def test_analysis_stores_one_profile_version_and_the_run_succeeds_with_it(
    analyzer, started, database
):
    await analyzer.to_the_end(started)
    stored = database.run(started)
    assert (stored.status, stored.profile_version) == (AnalysisRunStatus.SUCCEEDED, 1)
    assert database.profile_versions(started) == 1


async def test_last_step_answers_with_the_id_of_the_stored_profile_and_nothing_else(
    analyzer, started, database
):
    output = await analyzer.to_the_end(started)
    assert output == {"profile_id": str(database.run(started).profile_id)}


async def test_model_is_not_asked_before_the_repository_is_cloned(analyzer, model, started):
    await analyzer.step(started)
    assert model.requests == []


async def test_repeated_last_step_neither_asks_the_model_nor_stores_again(
    analyzer, model, intake, started, database
):
    output = await analyzer.to_the_end(started)
    first = database.run(started)
    again = await analyzer.step(started)
    assert again.output == output
    assert (len(model.requests), len(intake.cloned)) == (1, 1)
    assert database.profile_versions(started) == 1
    assert database.run(started) == first


async def test_attempt_is_refused_while_an_earlier_one_is_still_running(
    analyzer_with, model, started
):
    analyzer = analyzer_with()
    cloned = await analyzer.cloned(started)
    refusals: list[ApplicationError] = []
    asked_before: list[int] = []

    def second_attempt() -> None:
        asked_before.append(len(model.requests))
        refusals.append(asyncio.run(refused(analyzer_with().step(started, cloned))))

    model.meanwhile = second_attempt
    await analyzer.step(started, cloned)
    (refusal,) = refusals
    assert (refusal.type, refusal.non_retryable) == (ATTEMPT_STILL_RUNNING, False)
    # The one request is the first attempt's: the refused one asked the model for nothing.
    assert (asked_before, len(model.requests)) == ([1], 1)


async def test_attempt_refused_for_an_earlier_one_leaves_the_run_to_that_one(
    analyzer_with, model, started, database
):
    analyzer = analyzer_with()
    cloned = await analyzer.cloned(started)
    model.meanwhile = lambda: asyncio.run(refused(analyzer_with().step(started, cloned)))
    await analyzer.step(started, cloned)
    assert database.run(started).profile_version == 1
    assert database.profile_versions(started) == 1


async def test_of_two_attempts_that_both_analysed_one_result_is_stored(
    analyzer_with, checkpoints, model, started, database
):
    unheld = without_the_hold(checkpoints)
    overtaken, other = analyzer_with(reaching=unheld), analyzer_with(reaching=unheld)
    cloned = await overtaken.cloned(started)
    outputs: list[dict] = []
    model.meanwhile = lambda: outputs.append(asyncio.run(other.step(started, cloned)).output)
    late = await overtaken.step(started, cloned)
    assert database.profile_versions(started) == 1
    assert database.run(started).profile_version == 1
    assert outputs == [late.output]


async def test_result_of_a_run_that_failed_meanwhile_is_not_stored(
    analyzer, model, started, database
):
    cloned = await analyzer.cloned(started)
    model.meanwhile = lambda: analyzer.record_failure(started, steps.ANALYSIS_TEXTS.timed_out)
    failure = await refused(analyzer.step(started, cloned))
    assert (failure.type, failure.non_retryable) == (steps.RUN_UNAVAILABLE, True)
    assert database.profile_versions(started) == 0
    assert database.run(started).error == steps.ANALYSIS_TEXTS.timed_out


@pytest.mark.parametrize(
    "error",
    [
        AnalysisError("cost limit reached ($0.500) before a profile was accepted"),
        ConfigError("unknown role 'repo_analyzer'"),
    ],
)
async def test_refused_profile_is_not_worth_another_attempt(analyzer, model, started, error):
    model.turns.append(error)
    failure = await refused(analyzer.to_the_end(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)
    assert failure.message == str(error)


async def test_model_provider_failure_is_worth_another_attempt(analyzer, model, started):
    model.turns.append(LLMError("the model provider answered HTTP 429", status=429))
    failure = await refused(analyzer.to_the_end(started))
    assert (failure.type, failure.non_retryable) == (steps.MODEL_UNAVAILABLE, False)
    assert failure.message == "the model provider answered HTTP 429"


@pytest.mark.parametrize("status", [401, 402, 403, 404])
async def test_request_the_model_provider_refuses_is_not_worth_another_attempt(
    analyzer, model, started, status
):
    model.turns.append(LLMError(f"the model provider answered HTTP {status}", status=status))
    failure = await refused(analyzer.to_the_end(started))
    assert (failure.type, failure.non_retryable) == (steps.ANALYSIS_FAILED, True)


async def test_model_provider_that_did_not_answer_is_worth_another_attempt(
    analyzer, model, started
):
    model.turns.append(LLMError("request failed: ReadTimeout"))
    failure = await refused(analyzer.to_the_end(started))
    assert (failure.type, failure.non_retryable) == (steps.MODEL_UNAVAILABLE, False)


async def test_failed_attempt_leaves_the_run_running_for_the_workflow_to_settle(
    analyzer, model, started, database
):
    model.turns.append(OUTAGE)
    await refused(analyzer.to_the_end(started))
    assert database.run(started).status is AnalysisRunStatus.RUNNING


async def test_failure_of_a_step_does_not_name_the_folder_of_the_clone(analyzer, model, started):
    folder = analyzer.clones.folder_of(started.run_id)
    model.turns.append(AnalysisError(f"could not read {folder}/repo/README.md"))
    failure = await refused(analyzer.to_the_end(started))
    assert failure.message == "could not read <clone>/repo/README.md"


async def test_failure_of_a_step_carries_no_cause_into_the_history(analyzer, model, started):
    model.turns.append(OUTAGE)
    failure = await refused(analyzer.to_the_end(started))
    assert failure.__cause__ is None


async def test_unexpected_error_of_a_step_is_reported_without_its_detail(analyzer, model, started):
    model.turns.append(RuntimeError("/Users/someone/secret"))
    failure = await refused(analyzer.to_the_end(started))
    assert (failure.type, failure.message) == (
        UNEXPECTED,
        steps.ANALYSIS_TEXTS.stopped_unexpectedly,
    )
    assert not failure.non_retryable
    assert failure.__cause__ is None


async def test_result_the_database_refuses_is_reported_without_its_detail(
    analyzer_with, scripted_intake, started, database
):
    # PostgreSQL stores no NUL character in a text column.
    unstorable = analyzer_with(intake=scripted_intake(ref="main\x00"))
    failure = await refused(unstorable.to_the_end(started))
    assert (failure.type, failure.message) == (DATABASE_UNAVAILABLE, steps.NOT_STORED)
    assert not failure.non_retryable
    assert database.profile_versions(started) == 0
    assert database.run(started).status is AnalysisRunStatus.RUNNING


async def test_each_attempt_is_given_a_chat_model_of_its_own(analyzer_with, model, started):
    made: list[object] = []

    def new_model(workspace_id: uuid.UUID):
        made.append(model)
        return model

    analyzer = analyzer_with(new_model=new_model)
    model.turns.append(OUTAGE)
    cloned = await analyzer.cloned(started)
    await refused(analyzer.step(started, cloned))
    await analyzer.step(started, cloned)
    # One for the step that asked for the clone, and one for each attempt at the next.
    assert len(made) == 3


# ------------------------------------------- the run's checkpoints across attempts
async def test_attempt_that_failed_in_a_way_that_may_pass_keeps_the_checkpoints(
    analyzer, model, started, database
):
    model.turns.append(OUTAGE)
    await refused(analyzer.to_the_end(started))
    assert database.has_checkpoints(started)


async def test_next_attempt_continues_and_does_not_pay_for_finished_turns_again(
    analyzer, model, turns, started, database
):
    model.turns.extend([turns.reads_the_readme, OUTAGE])
    cloned = await analyzer.cloned(started)
    await refused(analyzer.step(started, cloned))
    await analyzer.step(started, cloned)
    first_turn, lost_turn, repeated_turn = model.requests
    assert repeated_turn == lost_turn
    assert len(repeated_turn) > len(first_turn)
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED


async def test_attempt_whose_profile_was_not_stored_keeps_the_checkpoints(
    analyzer_with, scripted_intake, started, database
):
    unstorable = analyzer_with(intake=scripted_intake(ref="main\x00"))
    await refused(unstorable.to_the_end(started))
    assert database.has_checkpoints(started)


async def test_run_that_succeeded_leaves_no_checkpoints_once_it_is_forgotten(
    analyzer, started, database
):
    await analyzer.to_the_end(started)
    await analyzer.forget(started)
    assert not database.has_checkpoints(started)


async def test_run_that_failed_leaves_no_checkpoints_once_it_is_forgotten(
    analyzer, model, started, database
):
    model.turns.append(AnalysisError("no profile after 40 steps"))
    failure = await refused(analyzer.to_the_end(started))
    analyzer.record_failure(started, failure.message)
    await analyzer.forget(started)
    assert not database.has_checkpoints(started)


async def test_attempt_still_running_after_its_run_failed_leaves_no_checkpoints(
    analyzer, model, started, database
):
    cloned = await analyzer.cloned(started)

    def the_run_ends_meanwhile() -> None:
        analyzer.record_failure(started, steps.ANALYSIS_TEXTS.timed_out)
        asyncio.run(analyzer.forget(started))

    # The attempt writes checkpoints after the run was forgotten.
    model.meanwhile = the_run_ends_meanwhile
    await refused(analyzer.step(started, cloned))
    assert not database.has_checkpoints(started)


async def test_run_is_forgotten_without_a_failure_when_its_checkpoints_cannot_be_removed(
    analyzer_with, checkpoints, started, database
):
    analyzer = analyzer_with(reaching=cannot_forget(checkpoints))
    await analyzer.to_the_end(started)
    await analyzer.forget(started)
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert database.has_checkpoints(started)


# ------------------------------------------------------- the clone on the disk
async def test_clone_stays_on_the_disk_while_the_run_can_be_tried_again(analyzer, model, started):
    model.turns.append(OUTAGE)
    await refused(analyzer.to_the_end(started))
    assert analyzer.clones.folder_of(started.run_id).is_dir()


async def test_next_attempt_reads_the_clone_that_is_there_and_does_not_clone_again(
    analyzer, model, intake, started
):
    model.turns.append(OUTAGE)
    cloned = await analyzer.cloned(started)
    await refused(analyzer.step(started, cloned))
    await analyzer.step(started, cloned)
    assert len(intake.cloned) == 1


async def test_step_on_a_worker_without_the_clone_clones_the_commit_of_the_run_again(
    analyzer_with, model, intake, tmp_path, started, database
):
    first_worker = analyzer_with()
    model.turns.append(OUTAGE)
    cloned = await first_worker.cloned(started)
    await refused(first_worker.step(started, cloned))

    another_worker = analyzer_with(clones=LocalClones(tmp_path / "another", intake))
    await another_worker.step(started, cloned)

    assert [commit for _, _, commit in intake.cloned] == [None, COMMIT]
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED


@pytest.mark.parametrize("ending", ["succeeded", "failed"])
async def test_clone_is_removed_when_the_run_is_forgotten(analyzer, model, started, ending):
    if ending == "failed":
        model.turns.append(AnalysisError("no profile after 40 steps"))
        await refused(analyzer.to_the_end(started))
    else:
        await analyzer.to_the_end(started)
    folder: Path = analyzer.clones.folder_of(started.run_id)
    assert folder.is_dir()
    await analyzer.forget(started)
    assert not folder.exists()


async def test_attempt_still_running_after_its_run_failed_leaves_no_clone(analyzer, model, started):
    cloned = await analyzer.cloned(started)

    def the_run_ends_meanwhile() -> None:
        analyzer.record_failure(started, steps.ANALYSIS_TEXTS.timed_out)
        asyncio.run(analyzer.forget(started))

    model.meanwhile = the_run_ends_meanwhile
    await refused(analyzer.step(started, cloned))
    assert not analyzer.clones.folder_of(started.run_id).exists()
