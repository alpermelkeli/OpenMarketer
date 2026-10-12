"""Tests for the analysis of a real repository across attempts and across workers.

Git, the secret scan, the analyzer graph and the checkpoint store in PostgreSQL are real; the
model is scripted and the clone is made from a repository on this machine (see
``intake_of_a_local_repository`` in ``conftest.py`` of the worker's tests). The activities
run as plain functions. They need gitleaks on the PATH.
"""

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError

from openmarketer_core.db.models import AnalysisRunStatus
from openmarketer_core.llm import LLMError
from openmarketer_core.repository_analysis.intake import IntakeResult
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.clones import LocalClones

OUTAGE = LLMError("the model provider answered HTTP 503", status=503)


@pytest.fixture
def worker(analyzer_with, intake_of_a_local_repository):
    """The analyzer on the local repository, as one worker with a disk of its own runs it."""
    return analyzer_with(intake=intake_of_a_local_repository)


@pytest.fixture
def another_worker(analyzer_with, intake_of_a_local_repository, tmp_path):
    """The same analyzer on a worker whose disk has no clone of any run."""
    clones = LocalClones(tmp_path / "another-disk", intake_of_a_local_repository)
    return analyzer_with(clones=clones)


@pytest.fixture
def interrupted(worker, model, turns, started):
    """A run whose first attempt read the README and then lost the model provider.

    Gives the outcome of its clone, which the next attempt at the step is given again.
    """

    async def interrupted():
        model.turns.extend([turns.reads_the_readme, OUTAGE, turns.submits])
        cloned = await worker.cloned(started)
        with pytest.raises(ApplicationError) as failure:
            await worker.step(started, cloned)
        assert failure.value.type == steps.MODEL_UNAVAILABLE
        return cloned

    return interrupted


def push_another_commit(source_repo: Path) -> None:
    (source_repo / "README.md").write_text("# Renamed App\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "second"],
        cwd=source_repo,
        check=True,
        capture_output=True,
    )


async def test_interrupted_attempt_keeps_the_checkpoints_of_its_finished_turns(
    interrupted, started, database
):
    await interrupted()
    assert database.has_checkpoints(started)


async def test_next_attempt_asks_the_model_only_for_the_turn_that_did_not_finish(
    interrupted, worker, model, started
):
    cloned = await interrupted()
    await worker.step(started, cloned)
    first_turn, lost_turn, repeated_turn = model.requests
    assert repeated_turn == lost_turn
    assert len(repeated_turn) > len(first_turn)


async def test_next_attempt_continues_with_what_the_first_one_read(
    interrupted, worker, model, started
):
    cloned = await interrupted()
    await worker.step(started, cloned)
    read_by_the_first_attempt = [m for m in model.requests[-1] if m["role"] == "tool"]
    assert "Share memories offline." in read_by_the_first_attempt[0]["content"]


async def test_continued_run_succeeds_with_one_profile_and_is_forgotten(
    interrupted, worker, started, database
):
    cloned = await interrupted()
    await worker.step(started, cloned)
    await worker.forget(started)
    stored = database.run(started)
    assert (stored.status, stored.profile_version) == (AnalysisRunStatus.SUCCEEDED, 1)
    assert not database.has_checkpoints(started)


async def test_snapshot_of_the_run_is_the_commit_and_the_branch_that_were_cloned(
    worker, source_repo, started, database
):
    cloned = await worker.cloned(started)
    await worker.step(started, cloned)
    assert database.snapshots(started) == [(cloned.result["commit_sha"], "main")]


async def test_step_on_a_worker_without_the_clone_continues_on_the_commit_the_run_first_read(
    interrupted, another_worker, model, source_repo, started, database
):
    cloned = await interrupted()
    push_another_commit(source_repo)

    await another_worker.step(started, cloned)

    # The profile cites the README of the first commit, and is accepted: that is what was read.
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert len(model.requests) == 3
    # A commit fetched by its id is on no branch.
    assert database.snapshots(started) == [(cloned.result["commit_sha"], None)]


async def test_step_after_the_clone_was_lost_from_the_disk_reads_the_same_commit_again(
    interrupted, worker, source_repo, started, database
):
    cloned = await interrupted()
    shutil.rmtree(worker.clones.folder_of(started.run_id))
    push_another_commit(source_repo)

    await worker.step(started, cloned)

    assert [commit for commit, _ in database.snapshots(started)] == [cloned.result["commit_sha"]]


async def test_profile_that_was_not_stored_is_stored_by_the_next_attempt_without_the_model(
    analyzer_with, another_worker, intake_of_a_local_repository, model, started, database
):
    def unstorable(source: str, clone_into: Path, commit: str | None) -> IntakeResult:
        # PostgreSQL stores no NUL character in a text column.
        result = intake_of_a_local_repository(source, clone_into, commit)
        return replace(result, snapshot=replace(result.snapshot, ref="main\x00"))

    spoiled = analyzer_with(intake=unstorable)
    cloned = await spoiled.cloned(started)
    with pytest.raises(ApplicationError) as failure:
        await spoiled.step(started, cloned)
    await another_worker.step(started, cloned)

    assert failure.value.message == steps.NOT_STORED
    assert len(model.requests) == 1
    assert database.run(started).status is AnalysisRunStatus.SUCCEEDED
    assert database.profile_versions(started) == 1


async def test_profile_is_stored_once_when_the_last_step_is_repeated(
    worker, model, started, database
):
    cloned = await worker.cloned(started)
    first = await worker.step(started, cloned)
    again = await worker.step(started, cloned)
    assert again == first
    assert len(model.requests) == 1
    assert database.profile_versions(started) == 1
