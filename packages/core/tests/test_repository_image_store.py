"""Tests for the rows of the product's images an analysis found in its repository.

They need PostgreSQL (see the root ``conftest.py``).
"""

import uuid
from dataclasses import dataclass, replace
from typing import TypedDict

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.db.analysis_runs import (
    AnalysisRunFinished,
    AnalysisRunNotFound,
    AnalysisRunNotStarted,
    mark_run_failed,
    mark_run_started,
    request_analysis_run,
)
from openmarketer_core.db.models import Project, RepositoryImageRecord, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.repository_images import (
    NewRepositoryImage,
    RepositoryImageNotFound,
    keep_repository_images,
    list_repository_images,
    repository_image,
    repository_image_key,
    run_has_repository_images,
)
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)


class ProjectIds(TypedDict):
    workspace_id: uuid.UUID
    project_id: uuid.UUID


class RunIds(ProjectIds):
    analysis_run_id: uuid.UUID


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
            workspace_id=self.workspace_id, project_id=self.project_id, analysis_run_id=self.run_id
        )


def new_project(session: Session) -> ProjectIds:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="App", source_repo_url="https://example.com/a.git"
    )
    session.add(project)
    session.flush()
    return ProjectIds(workspace_id=workspace.id, project_id=project.id)


def run_of(session: Session, project: ProjectIds, *, start: bool = True) -> RunOfProject:
    run = request_analysis_run(session, **project)
    found = RunOfProject(project["workspace_id"], project["project_id"], run.id)
    if start:
        mark_run_started(session, **project, run_id=run.id)
    return found


def ended(session: Session, run: RunOfProject) -> None:
    mark_run_failed(session, **run.project, run_id=run.run_id, error="stopped")


def image(run: RunOfProject, n: int, *, kind=RepositoryImageKind.LOGO) -> NewRepositoryImage:
    sha256 = f"{n:064x}"
    return NewRepositoryImage(
        path=f"assets/image-{n}.png",
        kind=kind,
        mime="image/png",
        width=512,
        height=256,
        size_bytes=4096,
        sha256=sha256,
        storage_key=repository_image_key(**run.scope, sha256=sha256, mime="image/png"),
    )


def keep(session: Session, run: RunOfProject, *numbers: int):
    return keep_repository_images(session, **run.scope, images=[image(run, n) for n in numbers])


@pytest.fixture
def project(session) -> ProjectIds:
    return new_project(session)


@pytest.fixture
def run(session, project) -> RunOfProject:
    return run_of(session, project)


def listed(session: Session, project: ProjectIds) -> list[str]:
    page = list_repository_images(session, **project, limit=50)
    return sorted(found.path for found in page.items)


def test_a_running_analysis_keeps_its_images(session, project, run):
    kept = keep(session, run, 1, 2)

    assert (kept.kept, kept.keys_to_remove) == (True, ())
    assert listed(session, project) == ["assets/image-1.png", "assets/image-2.png"]
    assert run_has_repository_images(session, **run.scope)


def test_an_image_reads_back_as_it_was_kept(session, project, run):
    keep(session, run, 1)
    (found,) = list_repository_images(session, **project, limit=1).items

    again = repository_image(session, **project, image_id=found.id)
    assert again == found
    assert (found.kind, found.mime, found.width, found.height, found.size_bytes) == (
        RepositoryImageKind.LOGO,
        "image/png",
        512,
        256,
        4096,
    )
    assert found.storage_key == image(run, 1).storage_key
    assert found.analysis_run_id == run.run_id


def test_keeping_the_images_of_a_run_again_changes_nothing(session, run):
    keep(session, run, 1)
    again = keep(session, run, 2, 3)

    assert (again.kept, again.keys_to_remove) == (False, ())
    assert session.scalar(select(func.count()).select_from(RepositoryImageRecord)) == 1


def test_a_later_analysis_replaces_the_images_and_answers_the_replaced_keys(session, project):
    first = run_of(session, project)
    keep(session, first, 1, 2)
    ended(session, first)
    second = run_of(session, project)

    kept = keep(session, second, 3)

    assert kept.kept
    assert sorted(kept.keys_to_remove) == sorted(image(first, n).storage_key for n in (1, 2))
    assert listed(session, project) == ["assets/image-3.png"]


def test_an_analysis_that_found_no_image_keeps_the_earlier_ones(session, project):
    first = run_of(session, project)
    keep(session, first, 1)
    ended(session, first)
    second = run_of(session, project)

    kept = keep(session, second)

    assert (kept.kept, kept.keys_to_remove) == (False, ())
    assert listed(session, project) == ["assets/image-1.png"]


def test_a_finished_run_keeps_no_images(session, run):
    ended(session, run)
    with pytest.raises(AnalysisRunFinished):
        keep(session, run, 1)


def test_a_queued_run_keeps_no_images(session, project):
    queued = run_of(session, project, start=False)
    with pytest.raises(AnalysisRunNotStarted):
        keep(session, queued, 1)


def test_a_run_of_another_workspace_keeps_no_images(session, run):
    other = new_project(session)
    with pytest.raises(AnalysisRunNotFound):
        keep_repository_images(
            session,
            **other,
            analysis_run_id=run.run_id,
            images=[image(replace(run, **other), 1)],
        )


def test_a_key_outside_the_runs_folder_is_refused(session, run):
    outside = replace(image(run, 1), storage_key="assets/elsewhere/1.png")
    with pytest.raises(ValueError):
        keep_repository_images(session, **run.scope, images=[outside])


def test_a_run_keeps_a_path_once(session, run):
    twice = [image(run, 1), replace(image(run, 2), path=image(run, 1).path)]
    with pytest.raises(IntegrityError):
        keep_repository_images(session, **run.scope, images=twice)


def test_a_run_keeps_identical_bytes_once(session, run):
    twice = [image(run, 1), replace(image(run, 1), path="assets/copy.png")]
    with pytest.raises(IntegrityError):
        keep_repository_images(session, **run.scope, images=twice)


@pytest.mark.parametrize(
    "changes",
    [
        {"mime": "image/jpeg"},
        {"sha256": "not-a-hash"},
        {"path": ""},
        {"path": "a" * 1001},
        {"width": 0},
        {"size_bytes": 0},
    ],
)
def test_a_row_that_breaks_a_check_is_refused(session, run, changes):
    with pytest.raises(IntegrityError):
        keep_repository_images(session, **run.scope, images=[replace(image(run, 1), **changes)])


def test_the_images_of_another_workspace_are_not_read(session, project, run):
    keep(session, run, 1)
    (found,) = list_repository_images(session, **project, limit=1).items
    other = new_project(session)

    with pytest.raises(RepositoryImageNotFound):
        repository_image(session, **other, image_id=found.id)
    with pytest.raises(ProjectNotFound):
        list_repository_images(
            session, workspace_id=other["workspace_id"], project_id=project["project_id"], limit=5
        )


def test_the_list_is_read_a_page_at_a_time(session, project, run):
    keep(session, run, 1, 2, 3)

    first = list_repository_images(session, **project, limit=2)
    assert first.next_before is not None
    rest = list_repository_images(session, **project, limit=2, before=first.next_before)

    paths = [found.path for found in (*first.items, *rest.items)]
    assert sorted(paths) == [f"assets/image-{n}.png" for n in (1, 2, 3)]
    assert rest.next_before is None


def test_a_project_without_images_lists_none(session, project):
    assert list_repository_images(session, **project, limit=5).items == ()
