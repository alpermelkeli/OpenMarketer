"""Tests for media assets: storing a run's versions, their reviews, and a person's decision.

They need PostgreSQL (see the root ``conftest.py``). How a run ends with one of its versions
is in ``test_studio_runs.py``.
"""

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TypedDict

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from openmarketer_core.db.media_assets import (
    MediaAssetDecided,
    MediaAssetNotFound,
    MediaAssetVersionNotFound,
    MediaAssetVersionReviewed,
    NewAssetVersion,
    accept_asset,
    add_version,
    asset_version,
    asset_versions,
    list_media_assets,
    media_asset,
    record_version_review,
    reject_asset,
)
from openmarketer_core.db.models import (
    AssetTechnique,
    MediaAssetStatus,
    MediaAssetVersionRecord,
    Project,
    Workspace,
)
from openmarketer_core.db.studio_runs import (
    StudioRunFinished,
    StudioRunNotStarted,
    StudioRunUnfinished,
    mark_run_failed,
    mark_run_needs_review,
    mark_run_started,
    request_studio_run,
)
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest

REQUEST = AssetRequest(goal="Announce the launch", platform=Platform.INSTAGRAM_POST)
PERSON = uuid.uuid4()
CHECKS = {"size": {"passed": True}, "copy": {"passed": True}}
VERIFIER = {"legibility": {"passed": True, "severity": "none"}}


class ProjectIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID


class RunIds(ProjectIds):
    run_id: uuid.UUID


@dataclass(frozen=True)
class RunOfProject:
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID

    @property
    def project(self) -> ProjectIds:
        return ProjectIds(workspace_id=self.workspace_id, project_id=self.project_id)

    @property
    def scope(self) -> RunIds:
        return RunIds(
            workspace_id=self.workspace_id, project_id=self.project_id, run_id=self.run_id
        )


def new_run(session: Session, *, start: bool = True) -> RunOfProject:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    run = request_studio_run(
        session,
        workspace_id=workspace.id,
        project_id=project.id,
        request=REQUEST,
        requested_by="person",
    )
    found = RunOfProject(workspace.id, project.id, run.id)
    if start:
        mark_run_started(session, **found.scope)
    return found


def another_run_of(session: Session, run: RunOfProject) -> RunOfProject:
    other = request_studio_run(session, **run.project, request=REQUEST, requested_by="person")
    found = replace(run, run_id=other.id)
    mark_run_started(session, **found.scope)
    return found


@pytest.fixture
def run(session) -> RunOfProject:
    return new_run(session)


def candidate(run: RunOfProject, version_id: uuid.UUID, *, round: int = 1) -> NewAssetVersion:
    return NewAssetVersion(
        round=round,
        technique=AssetTechnique.BACKGROUND_OVERLAY,
        storage_key=asset_key(**run.scope, version_id=version_id, mime="image/png"),
        mime="image/png",
        width=1080,
        height=1080,
        size_bytes=2048,
        cost_usd=Decimal("0.04"),
        provider="openrouter",
        model="an-image-model",
        prompt="a calm gradient background",
        params={"seed": 7},
        code="from PIL import Image",
    )


def store(
    session: Session,
    run: RunOfProject,
    action: str,
    *,
    asset_id: uuid.UUID | None = None,
    round: int = 1,
):
    version_id = uuid.uuid4()
    return add_version(
        session,
        **run.scope,
        version_id=version_id,
        action_id=action,
        version=candidate(run, version_id, round=round),
        asset_id=asset_id,
    )


def ended(session: Session, run: RunOfProject, version_id: uuid.UUID) -> None:
    mark_run_needs_review(session, **run.scope, final_version_id=version_id, cost_usd=Decimal(1))


def test_a_first_version_makes_a_draft_asset_for_the_runs_platform(session, run):
    version = store(session, run, "studio-run:r:1")

    asset = media_asset(session, **run.project, asset_id=version.asset_id)
    assert asset.status is MediaAssetStatus.DRAFT
    assert asset.platform is Platform.INSTAGRAM_POST
    assert asset.run_id == run.run_id
    assert asset.current_version_id == version.id
    assert version.passed is None
    assert version.params == {"seed": 7}


def test_a_later_version_of_the_asset_becomes_its_current_one(session, run):
    first = store(session, run, "studio-run:r:1")
    second = store(session, run, "studio-run:r:2", asset_id=first.asset_id, round=2)

    asset = media_asset(session, **run.project, asset_id=first.asset_id)
    assert second.asset_id == first.asset_id
    assert asset.current_version_id == second.id


def test_an_action_tried_again_finds_the_version_it_stored(session, run):
    first = store(session, run, "studio-run:r:1")
    again = store(session, run, "studio-run:r:1")

    assert again == first
    assert session.scalar(select(func.count()).select_from(MediaAssetVersionRecord)) == 1


def test_a_version_is_added_only_to_an_asset_of_the_run(session, run):
    other = another_run_of(session, run)
    foreign = store(session, other, "studio-run:o:1")

    with pytest.raises(MediaAssetNotFound):
        store(session, run, "studio-run:r:1", asset_id=foreign.asset_id)


def test_a_queued_run_stores_no_version(session):
    queued = new_run(session, start=False)

    with pytest.raises(StudioRunNotStarted):
        store(session, queued, "studio-run:q:1")


def test_a_finished_run_stores_no_new_version(session, run):
    mark_run_failed(session, **run.scope, error="no")

    with pytest.raises(StudioRunFinished):
        store(session, run, "studio-run:r:1")


def test_an_image_outside_the_runs_folder_is_refused(session, run):
    version_id = uuid.uuid4()
    elsewhere = replace(candidate(run, version_id), storage_key=f"pages/{version_id}.png")

    with pytest.raises(ValueError):
        add_version(session, **run.scope, version_id=version_id, action_id="a", version=elsewhere)


def test_the_reviews_of_a_version_are_recorded_once(session, run):
    version = store(session, run, "studio-run:r:1")

    reviewed = record_version_review(
        session,
        **run.project,
        version_id=version.id,
        check_report=CHECKS,
        verifier_report=VERIFIER,
        passed=True,
    )
    again = record_version_review(
        session,
        **run.project,
        version_id=version.id,
        check_report=CHECKS,
        verifier_report=VERIFIER,
        passed=True,
    )

    assert reviewed.passed is True
    assert reviewed.check_report == CHECKS
    assert again == reviewed
    with pytest.raises(MediaAssetVersionReviewed):
        record_version_review(
            session,
            **run.project,
            version_id=version.id,
            check_report=CHECKS,
            verifier_report=None,
            passed=False,
        )


def test_a_version_failed_by_the_checks_needs_no_verifier(session, run):
    version = store(session, run, "studio-run:r:1")

    reviewed = record_version_review(
        session,
        **run.project,
        version_id=version.id,
        check_report={"copy": {"passed": False}},
        verifier_report=None,
        passed=False,
    )

    assert reviewed.verifier_report is None
    assert reviewed.passed is False


def test_a_person_accepts_an_asset_once_its_run_has_ended(session, run):
    version = store(session, run, "studio-run:r:1")
    ended(session, run, version.id)

    accepted = accept_asset(session, **run.project, asset_id=version.asset_id, decided_by=PERSON)
    again = accept_asset(session, **run.project, asset_id=version.asset_id, decided_by=PERSON)

    assert accepted.status is MediaAssetStatus.ACCEPTED
    assert accepted.decided_by == PERSON
    assert accepted.decided_at is not None
    assert again == accepted


def test_a_decided_asset_is_not_decided_the_other_way(session, run):
    version = store(session, run, "studio-run:r:1")
    ended(session, run, version.id)
    reject_asset(session, **run.project, asset_id=version.asset_id, decided_by=PERSON)

    with pytest.raises(MediaAssetDecided):
        accept_asset(session, **run.project, asset_id=version.asset_id, decided_by=PERSON)


def test_an_asset_is_not_decided_while_its_run_may_add_versions(session, run):
    version = store(session, run, "studio-run:r:1")

    with pytest.raises(StudioRunUnfinished):
        accept_asset(session, **run.project, asset_id=version.asset_id, decided_by=PERSON)


def test_an_asset_of_another_workspace_is_missing(session, run):
    version = store(session, run, "studio-run:r:1")
    ended(session, run, version.id)
    other = new_run(session)

    with pytest.raises(MediaAssetNotFound):
        media_asset(
            session,
            workspace_id=other.workspace_id,
            project_id=run.project_id,
            asset_id=version.asset_id,
        )
    with pytest.raises(MediaAssetNotFound):
        accept_asset(
            session,
            workspace_id=other.workspace_id,
            project_id=run.project_id,
            asset_id=version.asset_id,
            decided_by=PERSON,
        )
    with pytest.raises(MediaAssetVersionNotFound):
        asset_version(
            session,
            workspace_id=other.workspace_id,
            project_id=run.project_id,
            version_id=version.id,
        )


def test_a_version_is_read_back_with_where_its_image_is(session, run):
    version = store(session, run, "studio-run:r:1")

    found = asset_version(session, **run.project, version_id=version.id)

    assert found == version
    assert found.storage_key.endswith(f"{version.id}.png")


def test_the_library_lists_a_projects_assets_newest_first_one_page_at_a_time(session, run):
    other = another_run_of(session, run)
    assets = [
        store(session, run, "studio-run:r:1").asset_id,
        store(session, run, "studio-run:r:2").asset_id,
        store(session, other, "studio-run:o:1").asset_id,
    ]
    rows = [media_asset(session, **run.project, asset_id=a) for a in assets]
    newest_first = sorted(rows, key=lambda a: (a.created_at, a.id), reverse=True)

    first = list_media_assets(session, **run.project, limit=2)
    second = list_media_assets(session, **run.project, limit=2, before=first.next_before)

    assert [a.id for a in first.items + second.items] == [a.id for a in newest_first]
    assert second.next_before is None


def test_the_versions_of_an_asset_are_listed_newest_first(session, run):
    first = store(session, run, "studio-run:r:1")
    second = store(session, run, "studio-run:r:2", asset_id=first.asset_id, round=2)
    store(session, run, "studio-run:r:3")

    page = asset_versions(session, **run.project, asset_id=first.asset_id, limit=10)

    expected = sorted([first, second], key=lambda v: (v.created_at, v.id), reverse=True)
    assert [v.id for v in page.items] == [v.id for v in expected]
