"""Tests for requesting analysis runs and recording how they went.

They need PostgreSQL (see the root ``conftest.py``).
"""

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.analysis_runs import (
    AnalysisAlreadyRunning,
    AnalysisRun,
    AnalysisRunFinished,
    AnalysisRunNotFound,
    AnalysisRunNotStarted,
    analysis_run,
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_analysis_run,
)
from openmarketer_core.db.evidence_store import (
    SavedAnalysis,
    save_analysis_of_project,
)
from openmarketer_core.db.models import (
    ANALYSIS_ERROR_MAX_LENGTH,
    AnalysisRunStatus,
    Project,
    RepoSnapshot,
    Workspace,
)
from openmarketer_core.db.profile_versions import latest_draft_profile
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.session import transaction
from openmarketer_core.intake import Snapshot
from openmarketer_core.profile import ProductProfile


@dataclass(frozen=True)
class ProjectInWorkspace:
    workspace_id: uuid.UUID
    project_id: uuid.UUID

    @property
    def scope(self) -> dict[str, uuid.UUID]:
        return {"workspace_id": self.workspace_id, "project_id": self.project_id}


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


def new_project(session: Session, workspace_id: uuid.UUID) -> ProjectInWorkspace:
    project = Project(
        workspace_id=workspace_id, name="Example App", source_repo_url="https://example.com/r.git"
    )
    session.add(project)
    session.flush()
    return ProjectInWorkspace(workspace_id, project.id)


@pytest.fixture
def project(session) -> ProjectInWorkspace:
    return new_project(session, new_workspace(session))


def stored_result(session: Session, project: ProjectInWorkspace) -> SavedAnalysis:
    return save_analysis_of_project(
        session,
        **project.scope,
        snapshot=Snapshot(
            root=Path("/unused"),
            source_url="https://example.com/r.git",
            commit_sha="a" * 40,
            ref="main",
        ),
        facts=[],
        profile=ProductProfile.model_validate({"product": {"name": "X", "type": "dev_tool"}}),
    )


def requested(session: Session, project: ProjectInWorkspace) -> AnalysisRun:
    return request_analysis_run(session, **project.scope)


def started(session: Session, project: ProjectInWorkspace) -> AnalysisRun:
    run = requested(session, project)
    return mark_run_started(session, **project.scope, run_id=run.id)


def succeeded(session: Session, project: ProjectInWorkspace) -> AnalysisRun:
    run = started(session, project)
    result = stored_result(session, project)
    return mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)


def failed(session: Session, project: ProjectInWorkspace) -> AnalysisRun:
    run = requested(session, project)
    return mark_run_failed(session, **project.scope, run_id=run.id, error="clone failed")


def test_requested_run_is_queued_and_has_no_outcome(session, project):
    run = requested(session, project)
    assert run.project_id == project.project_id
    assert run.status is AnalysisRunStatus.QUEUED
    assert run.created_at.tzinfo is not None
    assert (run.started_at, run.finished_at, run.error) == (None, None, None)
    assert (run.profile_id, run.profile_version, run.snapshot_id) == (None, None, None)


def test_run_cannot_be_requested_for_an_unknown_project(session, project):
    with pytest.raises(ProjectNotFound):
        request_analysis_run(session, workspace_id=project.workspace_id, project_id=uuid.uuid4())


def test_run_cannot_be_requested_for_a_project_of_another_workspace(session, project):
    with pytest.raises(ProjectNotFound):
        request_analysis_run(
            session, workspace_id=new_workspace(session), project_id=project.project_id
        )


def test_project_with_a_queued_run_gets_no_second_run(session, project):
    requested(session, project)
    with pytest.raises(AnalysisAlreadyRunning):
        requested(session, project)


def test_project_with_a_running_run_gets_no_second_run(session, project):
    started(session, project)
    with pytest.raises(AnalysisAlreadyRunning):
        requested(session, project)


def test_refused_request_leaves_the_transaction_usable(session, project):
    first = requested(session, project)
    with pytest.raises(AnalysisAlreadyRunning):
        requested(session, project)
    assert analysis_run(session, **project.scope, run_id=first.id) == first


@pytest.mark.parametrize("finished", [succeeded, failed])
def test_run_can_be_requested_again_after_the_last_one_finished(session, project, finished):
    earlier = finished(session, project)
    assert requested(session, project).id != earlier.id


def test_unfinished_run_of_one_project_does_not_block_another(session, project):
    requested(session, project)
    other = new_project(session, project.workspace_id)
    assert requested(session, other).status is AnalysisRunStatus.QUEUED


def test_started_run_is_running_since_the_start(session, project):
    run = started(session, project)
    assert run.status is AnalysisRunStatus.RUNNING
    assert run.started_at is not None
    assert run.finished_at is None


def test_start_of_a_running_run_can_be_reported_again(session, project):
    run = started(session, project)
    assert mark_run_started(session, **project.scope, run_id=run.id) == run


@pytest.mark.parametrize("finished", [succeeded, failed])
def test_finished_run_cannot_be_started(session, project, finished):
    run = finished(session, project)
    with pytest.raises(AnalysisRunFinished, match=f"already {run.status.value}"):
        mark_run_started(session, **project.scope, run_id=run.id)
    assert analysis_run(session, **project.scope, run_id=run.id) == run


def test_succeeded_run_names_the_profile_version_and_snapshot_it_stored(session, project):
    run = started(session, project)
    result = stored_result(session, project)
    done = mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)
    assert done.status is AnalysisRunStatus.SUCCEEDED
    assert done.profile_id == result.profile_id
    assert done.profile_version == result.profile_version
    assert done.snapshot_id == result.snapshot_id
    assert done.finished_at is not None
    assert done.error is None


def test_queued_run_cannot_succeed(session, project):
    run = requested(session, project)
    result = stored_result(session, project)
    with pytest.raises(AnalysisRunNotStarted):
        mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)
    assert analysis_run(session, **project.scope, run_id=run.id) == run


def test_same_success_can_be_reported_again(session, project):
    done = succeeded(session, project)
    assert done.profile_id is not None
    again = mark_run_succeeded(session, **project.scope, run_id=done.id, profile_id=done.profile_id)
    assert again == done


def test_succeeded_run_cannot_get_another_result(session, project):
    done = succeeded(session, project)
    other_result = stored_result(session, project)
    with pytest.raises(AnalysisRunFinished, match="already succeeded"):
        mark_run_succeeded(
            session, **project.scope, run_id=done.id, profile_id=other_result.profile_id
        )
    assert analysis_run(session, **project.scope, run_id=done.id) == done


def test_failed_run_cannot_succeed(session, project):
    run = failed(session, project)
    result = stored_result(session, project)
    with pytest.raises(AnalysisRunFinished, match="already failed"):
        mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)
    assert analysis_run(session, **project.scope, run_id=run.id) == run


def test_run_cannot_succeed_with_a_profile_of_another_project(session, project):
    run = started(session, project)
    other = new_project(session, project.workspace_id)
    other_result = stored_result(session, other)
    with pytest.raises(IntegrityError, match="fk_analysis_run_profile_id_product_profile"):
        mark_run_succeeded(
            session, **project.scope, run_id=run.id, profile_id=other_result.profile_id
        )


def test_queued_run_can_fail_without_having_started(session, project):
    run = failed(session, project)
    assert run.status is AnalysisRunStatus.FAILED
    assert run.error == "clone failed"
    assert run.started_at is None
    assert run.finished_at is not None


def test_running_run_fails_with_its_message(session, project):
    run = started(session, project)
    ended = mark_run_failed(session, **project.scope, run_id=run.id, error="model gave up")
    assert (ended.status, ended.error) == (AnalysisRunStatus.FAILED, "model gave up")
    assert ended.started_at == run.started_at
    assert ended.profile_id is None


def test_failure_message_is_shortened_to_the_limit(session, project):
    run = requested(session, project)
    message = "x" * ANALYSIS_ERROR_MAX_LENGTH + "the rest"
    ended = mark_run_failed(session, **project.scope, run_id=run.id, error=message)
    assert ended.error == "x" * ANALYSIS_ERROR_MAX_LENGTH


def test_second_failure_report_keeps_the_first_message(session, project):
    run = failed(session, project)
    again = mark_run_failed(session, **project.scope, run_id=run.id, error="something else")
    assert again == run


def test_succeeded_run_cannot_fail(session, project):
    done = succeeded(session, project)
    with pytest.raises(AnalysisRunFinished, match="already succeeded"):
        mark_run_failed(session, **project.scope, run_id=done.id, error="too late")
    assert analysis_run(session, **project.scope, run_id=done.id) == done


def test_run_is_read_back_as_it_was_stored(session, project):
    run = started(session, project)
    assert analysis_run(session, **project.scope, run_id=run.id) == run


def test_unknown_run_is_not_found(session, project):
    with pytest.raises(AnalysisRunNotFound):
        analysis_run(session, **project.scope, run_id=uuid.uuid4())


def read(session: Session, **run) -> AnalysisRun:
    return analysis_run(session, **run)


def start(session: Session, **run) -> AnalysisRun:
    return mark_run_started(session, **run)


def succeed(session: Session, **run) -> AnalysisRun:
    return mark_run_succeeded(session, **run, profile_id=uuid.uuid4())


def fail(session: Session, **run) -> AnalysisRun:
    return mark_run_failed(session, **run, error="clone failed")


@pytest.mark.parametrize("use", [read, start, succeed, fail])
def test_run_of_another_workspace_is_not_found(session, project, use):
    run = requested(session, project)
    with pytest.raises(AnalysisRunNotFound):
        use(
            session,
            workspace_id=new_workspace(session),
            project_id=project.project_id,
            run_id=run.id,
        )
    assert analysis_run(session, **project.scope, run_id=run.id) == run


@pytest.mark.parametrize("use", [read, start, succeed, fail])
def test_run_of_another_project_is_not_found(session, project, use):
    run = requested(session, project)
    other = new_project(session, project.workspace_id)
    with pytest.raises(AnalysisRunNotFound):
        use(session, **other.scope, run_id=run.id)
    assert analysis_run(session, **project.scope, run_id=run.id) == run


def committed_project(sessions: sessionmaker[Session]) -> ProjectInWorkspace:
    with sessions.begin() as setup:
        return new_project(setup, new_workspace(setup))


def test_result_and_success_are_stored_in_one_transaction(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)
    with transaction(sessions) as session:
        run = started(session, project)

    with transaction(sessions) as session:
        result = stored_result(session, project)
        mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)

    with sessions() as session:
        stored = analysis_run(session, **project.scope, run_id=run.id)
    assert (stored.status, stored.profile_id) == (AnalysisRunStatus.SUCCEEDED, result.profile_id)


def test_result_is_not_stored_when_its_success_is_refused(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)
    with transaction(sessions) as session:
        run = failed(session, project)

    with pytest.raises(AnalysisRunFinished), transaction(sessions) as session:
        result = stored_result(session, project)
        mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)

    with sessions() as session:
        assert latest_draft_profile(session, **project.scope) is None
        snapshots = select(func.count()).select_from(RepoSnapshot)
        assert session.scalar(snapshots.where(RepoSnapshot.project_id == project.project_id)) == 0


def test_concurrent_requests_for_a_project_add_one_run(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)
    refused: list[AnalysisAlreadyRunning] = []

    def request_later() -> None:
        with pytest.raises(AnalysisAlreadyRunning) as refusal, sessions.begin() as session:
            requested(session, project)
        refused.append(refusal.value)

    with sessions.begin() as session:
        run = requested(session, project)
        other_request = threading.Thread(target=request_later)
        other_request.start()
        other_request.join(timeout=0.5)  # long enough to add a run, were it not made to wait
        assert not refused
    other_request.join(timeout=10)

    assert len(refused) == 1
    with sessions() as session:
        assert analysis_run(session, **project.scope, run_id=run.id) == run


def test_concurrent_reports_of_one_run_are_applied_one_after_the_other(engine):
    sessions = sessionmaker(engine)
    project = committed_project(sessions)
    with sessions.begin() as session:
        run = started(session, project)
    refused: list[AnalysisRunFinished] = []

    def fail_later() -> None:
        with pytest.raises(AnalysisRunFinished) as refusal, sessions.begin() as session:
            mark_run_failed(session, **project.scope, run_id=run.id, error="timed out")
        refused.append(refusal.value)

    with sessions.begin() as session:
        result = stored_result(session, project)
        mark_run_succeeded(session, **project.scope, run_id=run.id, profile_id=result.profile_id)
        other_report = threading.Thread(target=fail_later)
        other_report.start()
        other_report.join(timeout=0.5)  # long enough to fail the run, were it not made to wait
        assert not refused
    other_report.join(timeout=10)

    assert len(refused) == 1
    with sessions() as session:
        stored = analysis_run(session, **project.scope, run_id=run.id)
    assert stored.status is AnalysisRunStatus.SUCCEEDED
