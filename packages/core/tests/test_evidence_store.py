"""Tests for storing an analysis run. They need PostgreSQL (see the root ``conftest.py``)."""

import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.evidence_store import (
    AnalysisProjectNotFound,
    SavedAnalysis,
    local_workspace_id,
    save_analysis,
    save_analysis_of_project,
)
from openmarketer_core.db.models import (
    Evidence,
    ProductProfileRecord,
    ProfileStatus,
    Project,
    RepoSnapshot,
    Workspace,
)
from openmarketer_core.extraction import ExtractedFact
from openmarketer_core.intake import Snapshot
from openmarketer_core.profile import ProductProfile

REPOSITORY = "https://example.com/acme/app.git"
FACTS = [
    ExtractedFact(
        extractor="package_json",
        kind="manifest.name",
        value="example-app",
        file="package.json",
        start_line=2,
        end_line=2,
    ),
    ExtractedFact(
        extractor="generic",
        kind="repo.languages",
        value={"TypeScript": 12, "CSS": 3},
        file="README.md",
    ),
]


def snapshot_of(source_url: str = REPOSITORY, commit_sha: str = "a" * 40) -> Snapshot:
    return Snapshot(root=Path("/unused"), source_url=source_url, commit_sha=commit_sha, ref="main")


def profile_named(name: str = "Example App") -> ProductProfile:
    return ProductProfile.model_validate({"product": {"name": name, "type": "dev_tool"}})


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


def save(session: Session, workspace_id: uuid.UUID, **changes) -> SavedAnalysis:
    run = {"snapshot": snapshot_of(), "facts": FACTS, "profile": profile_named()} | changes
    return save_analysis(session, workspace_id=workspace_id, **run)


def test_first_save_creates_the_project_named_after_the_product(session, workspace_id):
    saved = save(session, workspace_id)
    project = session.get_one(Project, saved.project_id)
    assert project.workspace_id == workspace_id
    assert project.name == "Example App"
    assert project.source_repo_url == REPOSITORY


def test_snapshot_records_the_commit_and_ref(session, workspace_id):
    saved = save(session, workspace_id, snapshot=snapshot_of(commit_sha="b" * 40))
    snapshot = session.get_one(RepoSnapshot, saved.snapshot_id)
    assert snapshot.project_id == saved.project_id
    assert snapshot.commit_sha == "b" * 40
    assert snapshot.ref == "main"


def test_every_fact_becomes_evidence_with_its_location(session, workspace_id):
    saved = save(session, workspace_id)
    rows = session.scalars(
        select(Evidence).where(Evidence.snapshot_id == saved.snapshot_id).order_by(Evidence.kind)
    ).all()
    stored = [
        ExtractedFact(
            extractor=row.extractor,
            kind=row.kind,
            value=row.value,
            file=row.file,
            start_line=row.start_line,
            end_line=row.end_line,
        )
        for row in rows
    ]
    assert stored == FACTS
    assert {row.project_id for row in rows} == {saved.project_id}
    assert saved.evidence_count == 2


def test_first_profile_is_version_one_and_a_draft_of_that_snapshot(session, workspace_id):
    saved = save(session, workspace_id)
    record = session.get_one(ProductProfileRecord, saved.profile_id)
    session.refresh(record)
    assert saved.profile_version == record.version == 1
    assert record.status is ProfileStatus.DRAFT
    assert record.approved_at is None
    assert record.snapshot_id == saved.snapshot_id


def test_stored_profile_content_is_the_same_profile(session, workspace_id):
    profile = profile_named()
    saved = save(session, workspace_id, profile=profile)
    session.expire_all()
    record = session.get_one(ProductProfileRecord, saved.profile_id)
    assert ProductProfile.model_validate(record.content) == profile


def test_second_save_of_a_repository_reuses_the_project_and_adds_version_two(session, workspace_id):
    first = save(session, workspace_id)
    second = save(session, workspace_id, snapshot=snapshot_of(commit_sha="c" * 40))
    assert second.project_id == first.project_id
    assert second.snapshot_id != first.snapshot_id
    assert second.profile_version == 2


def test_project_keeps_its_name_when_a_later_profile_renames_the_product(session, workspace_id):
    first = save(session, workspace_id)
    save(session, workspace_id, profile=profile_named("Renamed"))
    assert session.get_one(Project, first.project_id).name == "Example App"


def test_another_repository_gets_its_own_project(session, workspace_id):
    first = save(session, workspace_id)
    other = save(session, workspace_id, snapshot=snapshot_of("https://example.com/acme/other.git"))
    assert other.project_id != first.project_id
    assert other.profile_version == 1


def test_same_repository_in_another_workspace_gets_its_own_project(session, workspace_id):
    first = save(session, workspace_id)
    other = save(session, new_workspace(session))
    assert other.project_id != first.project_id
    assert other.profile_version == 1


def test_run_without_facts_is_saved_without_evidence(session, workspace_id):
    saved = save(session, workspace_id, facts=[])
    count = session.scalar(
        select(func.count()).select_from(Evidence).where(Evidence.project_id == saved.project_id)
    )
    assert saved.evidence_count == count == 0
    assert saved.profile_version == 1


def new_project(session: Session, workspace_id: uuid.UUID, source_repo_url: str) -> uuid.UUID:
    project = Project(
        workspace_id=workspace_id, name="Named By Hand", source_repo_url=source_repo_url
    )
    session.add(project)
    session.flush()
    return project.id


def save_of_project(session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID):
    return save_analysis_of_project(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        snapshot=snapshot_of(),
        facts=FACTS,
        profile=profile_named(),
    )


def test_analysis_of_a_project_is_stored_under_that_project(session, workspace_id):
    project_id = new_project(session, workspace_id, "https://example.com/acme/renamed.git")
    saved = save_of_project(session, workspace_id, project_id)
    assert saved.project_id == project_id
    assert saved.profile_version == 1
    assert saved.evidence_count == 2
    assert session.get_one(RepoSnapshot, saved.snapshot_id).project_id == project_id
    assert session.get_one(ProductProfileRecord, saved.profile_id).project_id == project_id


def test_analysis_of_a_project_ignores_another_project_of_the_same_repository(
    session, workspace_id
):
    by_repository = save(session, workspace_id)
    project_id = new_project(session, workspace_id, REPOSITORY)
    saved = save_of_project(session, workspace_id, project_id)
    assert saved.project_id == project_id != by_repository.project_id
    assert saved.profile_version == 1


def test_second_analysis_of_a_project_adds_version_two(session, workspace_id):
    project_id = new_project(session, workspace_id, REPOSITORY)
    save_of_project(session, workspace_id, project_id)
    assert save_of_project(session, workspace_id, project_id).profile_version == 2


def test_analysis_of_an_unknown_project_is_not_stored(session, workspace_id):
    with pytest.raises(AnalysisProjectNotFound):
        save_of_project(session, workspace_id, uuid.uuid4())
    assert session.scalar(select(func.count()).select_from(RepoSnapshot)) == 0


def test_analysis_is_not_stored_under_a_project_of_another_workspace(session, workspace_id):
    project_id = new_project(session, workspace_id, REPOSITORY)
    with pytest.raises(AnalysisProjectNotFound):
        save_of_project(session, new_workspace(session), project_id)
    assert session.scalar(select(func.count()).select_from(RepoSnapshot)) == 0


def test_local_workspace_is_created_once(session):
    first = local_workspace_id(session)
    assert local_workspace_id(session) == first
    assert session.get_one(Workspace, first).name == "local"


def test_concurrent_first_saves_of_a_repository_share_one_project(engine):
    sessions = sessionmaker(engine)
    with sessions.begin() as setup:
        workspace_id = new_workspace(setup)
    later: list[SavedAnalysis] = []

    def save_later() -> None:
        with sessions.begin() as session:
            later.append(save(session, workspace_id))

    with sessions.begin() as session:
        earlier = save(session, workspace_id)
        other_run = threading.Thread(target=save_later)
        other_run.start()
        other_run.join(timeout=0.5)  # long enough to save, were it not made to wait
        assert not later
    other_run.join(timeout=10)

    assert later[0].project_id == earlier.project_id
    assert (earlier.profile_version, later[0].profile_version) == (1, 2)
