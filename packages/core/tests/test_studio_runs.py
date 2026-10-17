"""Tests for studio runs: requesting one, how it moves, reading it back and deleting it.

They need PostgreSQL (see the root ``conftest.py``). The assets and versions a run stores are
covered by ``test_media_assets.py``; here they are only what a run ends with or takes with it.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.chat import ChatNotFound, create_chat
from openmarketer_core.db.media_assets import NewAssetVersion, add_version, media_asset
from openmarketer_core.db.models import (
    AssetTechnique,
    MediaAssetRecord,
    MediaAssetVersionRecord,
    Project,
    StudioRunRecord,
    StudioRunStatus,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.studio_runs import (
    FinalVersionNotFound,
    ReferenceAssetNotFound,
    StudioRunFinished,
    StudioRunNotFound,
    StudioRunNotStarted,
    StudioRunUnfinished,
    delete_studio_run,
    list_studio_runs,
    mark_run_failed,
    mark_run_needs_review,
    mark_run_started,
    mark_run_succeeded,
    request_studio_run,
    requested_asset,
    run_thread_id,
    run_to_execute,
    studio_run,
)
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest, StoredAssetRequestInvalid

REQUEST = AssetRequest(
    goal="Announce the Android launch", exact_copy=["Now on Android"], platform=Platform.X_POST
)


@dataclass(frozen=True)
class ProjectInWorkspace:
    workspace_id: uuid.UUID
    project_id: uuid.UUID


def new_project(session: Session) -> ProjectInWorkspace:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return ProjectInWorkspace(workspace_id=workspace.id, project_id=project.id)


@pytest.fixture
def project(session) -> ProjectInWorkspace:
    return new_project(session)


def requested(
    session: Session,
    project: ProjectInWorkspace,
    *,
    origin_action_id: str | None = None,
    chat_thread_id: uuid.UUID | None = None,
):
    return request_studio_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=REQUEST,
        requested_by="person",
        origin_action_id=origin_action_id,
        chat_thread_id=chat_thread_id,
    )


def started(session: Session, project: ProjectInWorkspace):
    run = requested(session, project)
    return mark_run_started(
        session, workspace_id=project.workspace_id, project_id=project.project_id, run_id=run.id
    )


def stored_version(
    session: Session, project: ProjectInWorkspace, run_id: uuid.UUID, action: str
) -> uuid.UUID:
    version_id = uuid.uuid4()
    key = asset_key(
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        run_id=run_id,
        version_id=version_id,
        mime="image/png",
    )
    add_version(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        run_id=run_id,
        version_id=version_id,
        action_id=action,
        version=NewAssetVersion(
            round=1,
            technique=AssetTechnique.GENERATED,
            storage_key=key,
            mime="image/png",
            width=1600,
            height=900,
            size_bytes=1024,
            cost_usd=Decimal("0.04"),
        ),
    )
    return version_id


class RunIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def scope(project: ProjectInWorkspace, run_id: uuid.UUID) -> RunIds:
    return RunIds(workspace_id=project.workspace_id, project_id=project.project_id, run_id=run_id)


def test_a_requested_run_is_queued_with_its_request(session, project):
    run = requested(session, project)

    assert run.status is StudioRunStatus.QUEUED
    assert run.platform is Platform.X_POST
    assert run.cost_usd == 0
    assert requested_asset(session, **scope(project, run.id)) == REQUEST


def test_an_action_that_asks_again_finds_the_run_it_made(session, project):
    first = requested(session, project, origin_action_id="writer-run:1:2")
    again = requested(session, project, origin_action_id="writer-run:1:2")

    assert again.id == first.id


def test_a_run_of_a_project_the_workspace_does_not_have_is_refused(session, project):
    other = new_project(session)
    with pytest.raises(ProjectNotFound):
        request_studio_run(
            session,
            workspace_id=other.workspace_id,
            project_id=project.project_id,
            request=REQUEST,
            requested_by="person",
        )


def test_a_run_a_chat_asked_for_names_the_chat(session, project):
    chat = create_chat(session, workspace_id=project.workspace_id, project_id=project.project_id)

    run = requested(session, project, chat_thread_id=chat.id)

    assert run.chat_thread_id == chat.id


def test_a_run_cannot_name_a_chat_of_another_project(session, project):
    other = new_project(session)
    chat = create_chat(session, workspace_id=other.workspace_id, project_id=other.project_id)

    with pytest.raises(ChatNotFound):
        requested(session, project, chat_thread_id=chat.id)


def test_a_reference_asset_must_be_the_projects(session, project):
    other = new_project(session)
    run = started(session, other)
    stored_version(session, other, run.id, "a:1")
    asset_id = session.scalar(select(MediaAssetRecord.id).where(MediaAssetRecord.run_id == run.id))

    with pytest.raises(ReferenceAssetNotFound):
        request_studio_run(
            session,
            workspace_id=project.workspace_id,
            project_id=project.project_id,
            request=REQUEST.model_copy(update={"reference_asset_ids": [asset_id]}),
            requested_by="person",
        )


def test_a_reference_asset_of_the_project_is_kept_with_the_request(session, project):
    earlier = started(session, project)
    stored_version(session, project, earlier.id, "a:1")
    asset_id = session.scalar(
        select(MediaAssetRecord.id).where(MediaAssetRecord.run_id == earlier.id)
    )
    request = REQUEST.model_copy(update={"reference_asset_ids": [asset_id]})

    run = request_studio_run(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        request=request,
        requested_by="person",
    )

    assert requested_asset(session, **scope(project, run.id)).reference_asset_ids == [asset_id]


def test_starting_a_run_twice_changes_nothing(session, project):
    run = started(session, project)

    again = mark_run_started(session, **scope(project, run.id))

    assert again == run
    assert run.status is StudioRunStatus.RUNNING
    assert run.started_at is not None


def test_a_run_ends_with_the_version_that_passed(session, project):
    run = started(session, project)
    first = stored_version(session, project, run.id, "a:1")
    stored_version(session, project, run.id, "a:2")

    ended = mark_run_succeeded(
        session, **scope(project, run.id), final_version_id=first, cost_usd=Decimal("0.12")
    )

    assert ended.status is StudioRunStatus.SUCCEEDED
    assert ended.cost_usd == Decimal("0.12")
    assert ended.finished_at is not None
    assert ended.final_asset_id is not None
    asset = media_asset(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        asset_id=ended.final_asset_id,
    )
    assert asset.current_version_id == first


def test_reporting_the_same_success_again_changes_nothing(session, project):
    run = started(session, project)
    version = stored_version(session, project, run.id, "a:1")
    ended = mark_run_succeeded(
        session, **scope(project, run.id), final_version_id=version, cost_usd=Decimal(1)
    )

    again = mark_run_succeeded(
        session, **scope(project, run.id), final_version_id=version, cost_usd=Decimal(1)
    )

    assert again == ended


def test_a_finished_run_is_not_given_another_outcome(session, project):
    run = started(session, project)
    first = stored_version(session, project, run.id, "a:1")
    second = stored_version(session, project, run.id, "a:2")
    mark_run_succeeded(
        session, **scope(project, run.id), final_version_id=first, cost_usd=Decimal(1)
    )

    with pytest.raises(StudioRunFinished):
        mark_run_succeeded(
            session, **scope(project, run.id), final_version_id=second, cost_usd=Decimal(1)
        )
    with pytest.raises(StudioRunFinished):
        mark_run_needs_review(
            session, **scope(project, run.id), final_version_id=first, cost_usd=Decimal(1)
        )
    with pytest.raises(StudioRunFinished):
        mark_run_failed(session, **scope(project, run.id), error="late")
    with pytest.raises(StudioRunFinished):
        mark_run_started(session, **scope(project, run.id))


def test_a_run_that_hit_a_limit_waits_with_its_best_candidate(session, project):
    run = started(session, project)
    best = stored_version(session, project, run.id, "a:1")

    ended = mark_run_needs_review(
        session, **scope(project, run.id), final_version_id=best, cost_usd=Decimal("0.9")
    )

    assert ended.status is StudioRunStatus.NEEDS_REVIEW
    assert ended.final_asset_id is not None


def test_a_queued_run_cannot_end_with_an_asset(session, project):
    run = requested(session, project)

    with pytest.raises(StudioRunNotStarted):
        mark_run_succeeded(
            session, **scope(project, run.id), final_version_id=uuid.uuid4(), cost_usd=Decimal(0)
        )


def test_a_run_ends_only_with_a_version_of_its_own(session, project):
    other_run = started(session, project)
    foreign = stored_version(session, project, other_run.id, "b:1")
    run = started(session, project)

    with pytest.raises(FinalVersionNotFound):
        mark_run_succeeded(
            session, **scope(project, run.id), final_version_id=foreign, cost_usd=Decimal(0)
        )


def test_a_failure_keeps_its_first_message_and_its_cost(session, project):
    run = started(session, project)

    failed = mark_run_failed(
        session, **scope(project, run.id), error="the provider refused", cost_usd=Decimal("0.3")
    )
    again = mark_run_failed(session, **scope(project, run.id), error="another")

    assert failed.status is StudioRunStatus.FAILED
    assert again.error == "the provider refused"
    assert again.cost_usd == Decimal("0.3")


def test_a_queued_run_can_fail(session, project):
    run = requested(session, project)

    failed = mark_run_failed(session, **scope(project, run.id), error="not handed to a worker")

    assert failed.status is StudioRunStatus.FAILED
    assert failed.started_at is None


def test_a_run_of_another_workspace_is_missing(session, project):
    run = requested(session, project)
    other = new_project(session)

    with pytest.raises(StudioRunNotFound):
        studio_run(
            session, workspace_id=other.workspace_id, project_id=project.project_id, run_id=run.id
        )
    with pytest.raises(StudioRunNotFound):
        mark_run_started(
            session, workspace_id=other.workspace_id, project_id=project.project_id, run_id=run.id
        )


def test_the_worker_finds_a_runs_scope_and_request_from_its_id(session, project):
    run = requested(session, project)

    found = run_to_execute(session, run.id)

    assert found is not None
    assert (found.workspace_id, found.project_id, found.run_id) == (
        project.workspace_id,
        project.project_id,
        run.id,
    )
    assert found.request == REQUEST
    assert run_to_execute(session, uuid.uuid4()) is None


def test_a_stored_request_that_no_longer_fits_is_refused(session, project):
    run = requested(session, project)
    session.execute(
        update(StudioRunRecord)
        .where(StudioRunRecord.id == run.id)
        .values(request={"goal": "x", "platform": "fax"})
    )

    with pytest.raises(StoredAssetRequestInvalid):
        run_to_execute(session, run.id)


def test_runs_are_listed_newest_first_one_page_at_a_time(session, project):
    runs = [requested(session, project) for _ in range(3)]
    newest_first = sorted(runs, key=lambda r: (r.created_at, r.id), reverse=True)

    first = list_studio_runs(
        session, workspace_id=project.workspace_id, project_id=project.project_id, limit=2
    )
    second = list_studio_runs(
        session,
        workspace_id=project.workspace_id,
        project_id=project.project_id,
        limit=2,
        before=first.next_before,
    )

    assert [r.id for r in first.items + second.items] == [r.id for r in newest_first]
    assert second.next_before is None


def test_the_thread_of_a_run_is_named_for_its_kind(project):
    run_id = uuid.uuid4()

    assert run_thread_id(run_id) == f"studio-run:{run_id}"


def test_the_database_refuses_a_success_without_an_asset(session, project):
    run = started(session, project)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(StudioRunRecord)
            .where(StudioRunRecord.id == run.id)
            .values(status=StudioRunStatus.SUCCEEDED, finished_at=func.now())
        )


def test_the_database_refuses_a_final_asset_of_another_run(session, project):
    other_run = started(session, project)
    stored_version(session, project, other_run.id, "b:1")
    foreign = session.scalar(
        select(MediaAssetRecord.id).where(MediaAssetRecord.run_id == other_run.id)
    )
    run = started(session, project)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(StudioRunRecord)
            .where(StudioRunRecord.id == run.id)
            .values(
                status=StudioRunStatus.SUCCEEDED, final_asset_id=foreign, finished_at=func.now()
            )
        )


def test_deleting_a_finished_run_removes_its_assets_and_answers_their_keys(session, project):
    run = started(session, project)
    version = stored_version(session, project, run.id, "a:1")
    mark_run_succeeded(
        session, **scope(project, run.id), final_version_id=version, cost_usd=Decimal(0)
    )
    key = session.scalar(
        select(MediaAssetVersionRecord.storage_key).where(MediaAssetVersionRecord.id == version)
    )

    keys = delete_studio_run(session, **scope(project, run.id))

    assert keys == (key,)
    assert session.scalar(select(func.count()).select_from(StudioRunRecord)) == 0
    assert session.scalar(select(func.count()).select_from(MediaAssetRecord)) == 0
    assert session.scalar(select(func.count()).select_from(MediaAssetVersionRecord)) == 0


def test_deleting_one_run_keeps_the_projects_other_runs(session, project):
    kept = started(session, project)
    stored_version(session, project, kept.id, "k:1")
    gone = requested(session, project)
    mark_run_failed(session, **scope(project, gone.id), error="no")

    delete_studio_run(session, **scope(project, gone.id))

    assert studio_run(session, **scope(project, kept.id)) == kept
    assert session.scalar(select(func.count()).select_from(MediaAssetVersionRecord)) == 1


def test_an_unfinished_run_is_not_deleted(session, project):
    run = started(session, project)

    with pytest.raises(StudioRunUnfinished):
        delete_studio_run(session, **scope(project, run.id))


def test_deleting_a_run_of_another_project_is_not_found(session, project):
    run = requested(session, project)
    mark_run_failed(session, **scope(project, run.id), error="no")
    other = new_project(session)

    with pytest.raises(StudioRunNotFound):
        delete_studio_run(
            session, workspace_id=other.workspace_id, project_id=other.project_id, run_id=run.id
        )
