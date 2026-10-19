"""Tests for the rows of the product's images: an analysis's and a person's.

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
from openmarketer_core.db.models import (
    Project,
    RepositoryImageRecord,
    RepositoryImageSource,
    Workspace,
)
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.repository_images import (
    NewRepositoryImage,
    RepositoryImageNotFound,
    TooManyPersonImages,
    keep_person_image,
    keep_repository_images,
    list_repository_images,
    person_image_key,
    remove_repository_image,
    repository_image,
    repository_image_key,
    run_has_repository_images,
    set_repository_image_kind,
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


PERSON = uuid.uuid4()


def upload(
    project: ProjectIds, n: int, *, kind=RepositoryImageKind.SCREENSHOT
) -> NewRepositoryImage:
    return NewRepositoryImage(
        path=f"upload-{n}.png",
        kind=kind,
        mime="image/png",
        width=800,
        height=600,
        size_bytes=2048,
        sha256=f"{n + 1000:064x}",
        storage_key=person_image_key(**project, upload_id=uuid.uuid4(), mime="image/png"),
    )


def keep_upload(session: Session, project: ProjectIds, n: int, *, most: int = 24):
    return keep_person_image(
        session, **project, image=upload(project, n), added_by=PERSON, most=most
    )


def first_image(session: Session, project: ProjectIds, path: str):
    page = list_repository_images(session, **project, limit=50)
    (found,) = [image for image in page.items if image.path == path]
    return found


def next_run(session: Session, project: ProjectIds, earlier: RunOfProject) -> RunOfProject:
    ended(session, earlier)
    return run_of(session, project)


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


# ----------------------------------------------------------------- a person's curation
def test_an_image_reads_back_as_an_analysiss_with_its_kind_not_chosen(session, project, run):
    keep(session, run, 1)
    found = first_image(session, project, "assets/image-1.png")
    assert (found.source, found.kind_chosen_by) == (RepositoryImageSource.ANALYSIS, None)


def test_a_person_chooses_an_images_kind(session, project, run):
    keep(session, run, 1)
    found = first_image(session, project, "assets/image-1.png")

    chosen = set_repository_image_kind(
        session, **project, image_id=found.id, kind=RepositoryImageKind.ICON, set_by=PERSON
    )

    assert (chosen.kind, chosen.kind_chosen_by) == (RepositoryImageKind.ICON, PERSON)
    assert repository_image(session, **project, image_id=found.id) == chosen


def test_a_removed_analysis_image_is_listed_and_read_no_more(session, project, run):
    keep(session, run, 1, 2)
    found = first_image(session, project, "assets/image-1.png")

    key = remove_repository_image(session, **project, image_id=found.id, removed_by=PERSON)

    assert key is None
    assert listed(session, project) == ["assets/image-2.png"]
    with pytest.raises(RepositoryImageNotFound):
        repository_image(session, **project, image_id=found.id)
    with pytest.raises(RepositoryImageNotFound):
        remove_repository_image(session, **project, image_id=found.id, removed_by=PERSON)


def test_a_later_analysis_keeps_an_image_a_person_removed_removed(session, project):
    first = run_of(session, project)
    keep(session, first, 1, 2)
    found = first_image(session, project, "assets/image-1.png")
    remove_repository_image(session, **project, image_id=found.id, removed_by=PERSON)
    second = next_run(session, project, first)

    kept = keep(session, second, 1, 2, 3)

    assert kept.kept
    assert listed(session, project) == ["assets/image-2.png", "assets/image-3.png"]
    removed = session.scalar(
        select(RepositoryImageRecord).where(
            RepositoryImageRecord.analysis_run_id == second.run_id,
            RepositoryImageRecord.sha256 == image(second, 1).sha256,
        )
    )
    assert removed is not None and removed.removed_by == PERSON


def test_a_later_analysis_keeps_the_kind_a_person_chose_for_the_same_bytes(session, project):
    first = run_of(session, project)
    keep(session, first, 1, 2)
    found = first_image(session, project, "assets/image-1.png")
    set_repository_image_kind(
        session, **project, image_id=found.id, kind=RepositoryImageKind.ICON, set_by=PERSON
    )
    second = next_run(session, project, first)

    keep(session, second, 1, 2)

    again = first_image(session, project, "assets/image-1.png")
    other = first_image(session, project, "assets/image-2.png")
    assert again.analysis_run_id == second.run_id
    assert (again.kind, again.kind_chosen_by) == (RepositoryImageKind.ICON, PERSON)
    assert (other.kind, other.kind_chosen_by) == (RepositoryImageKind.LOGO, None)


def test_a_person_uploads_an_image_of_the_kind_they_gave(session, project):
    kept = keep_upload(session, project, 1)

    assert kept.added
    found = kept.image
    assert (found.source, found.analysis_run_id, found.path) == (
        RepositoryImageSource.PERSON,
        None,
        "upload-1.png",
    )
    assert (found.kind, found.kind_chosen_by) == (RepositoryImageKind.SCREENSHOT, PERSON)
    assert listed(session, project) == ["upload-1.png"]


def test_the_same_upload_again_answers_the_image_kept_already(session, project):
    first = keep_upload(session, project, 1)
    again = keep_upload(session, project, 1)

    assert (again.added, again.image) == (False, first.image)
    assert listed(session, project) == ["upload-1.png"]


def test_a_project_keeps_at_most_so_many_uploads(session, project):
    keep_upload(session, project, 1, most=2)
    keep_upload(session, project, 2, most=2)
    with pytest.raises(TooManyPersonImages):
        keep_upload(session, project, 3, most=2)


def test_an_upload_outside_the_projects_person_folder_is_refused(session, project, run):
    outside = replace(upload(project, 1), storage_key=image(run, 1).storage_key)
    with pytest.raises(ValueError):
        keep_person_image(session, **project, image=outside, added_by=PERSON, most=24)


def test_an_upload_to_a_project_of_another_workspace_is_refused(session, project):
    other = new_project(session)
    with pytest.raises(ProjectNotFound):
        keep_person_image(
            session,
            workspace_id=other["workspace_id"],
            project_id=project["project_id"],
            image=upload(project, 1),
            added_by=PERSON,
            most=24,
        )


def test_uploads_survive_a_later_analysis(session, project):
    first = run_of(session, project)
    keep(session, first, 1)
    keep_upload(session, project, 7)
    second = next_run(session, project, first)

    kept = keep(session, second, 2)

    assert kept.keys_to_remove == (image(first, 1).storage_key,)
    assert listed(session, project) == ["assets/image-2.png", "upload-7.png"]


def test_removing_an_upload_deletes_its_row_and_answers_its_key(session, project):
    found = keep_upload(session, project, 1).image

    key = remove_repository_image(session, **project, image_id=found.id, removed_by=PERSON)

    assert key == found.storage_key
    assert session.get(RepositoryImageRecord, found.id) is None


def test_an_image_of_another_workspace_is_neither_chosen_nor_removed(session, project, run):
    keep(session, run, 1)
    found = first_image(session, project, "assets/image-1.png")
    other = new_project(session)

    with pytest.raises(RepositoryImageNotFound):
        set_repository_image_kind(
            session, **other, image_id=found.id, kind=RepositoryImageKind.ICON, set_by=PERSON
        )
    with pytest.raises(RepositoryImageNotFound):
        remove_repository_image(session, **other, image_id=found.id, removed_by=PERSON)


@pytest.mark.parametrize(
    "changes",
    [
        {"source": RepositoryImageSource.PERSON},  # a run, yet a person's
        {"removed_by": PERSON},  # removed by someone, at no time
    ],
)
def test_an_analysis_row_that_breaks_a_curation_check_is_refused(session, run, changes):
    keep(session, run, 1)
    record = session.scalar(select(RepositoryImageRecord))
    assert record is not None
    for name, value in changes.items():
        setattr(record, name, value)
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_upload_is_never_soft_removed(session, project):
    found = keep_upload(session, project, 1).image
    record = session.get(RepositoryImageRecord, found.id)
    assert record is not None
    record.removed_by = PERSON
    record.removed_at = record.created_at
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_upload_names_who_chose_its_kind(session, project):
    found = keep_upload(session, project, 1).image
    record = session.get(RepositoryImageRecord, found.id)
    assert record is not None
    record.kind_chosen_by = None
    with pytest.raises(IntegrityError):
        session.flush()
