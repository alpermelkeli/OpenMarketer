"""Tests for deleting a project. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.analysis_runs import (
    mark_run_failed,
    mark_run_started,
    mark_run_succeeded,
    request_analysis_run,
)
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    Evidence,
    ProductProfileRecord,
    Project,
    ProposalRecord,
    RepoSnapshot,
    Workspace,
)
from openmarketer_core.db.profile_versions import approve_profile_version, save_edited_profile
from openmarketer_core.db.project_removal import (
    ProjectHasPendingProposal,
    ProjectHasUnfinishedRun,
    delete_project,
)
from openmarketer_core.db.projects import ProjectNotFound, create_project
from openmarketer_core.db.proposals import decide_proposal, propose_action
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.extraction import ExtractedFact
from openmarketer_core.repository_analysis.intake import Snapshot

REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})
FACT = ExtractedFact(extractor="manifest", kind="manifest.name", value="app", file="package.json")


def new_workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    return workspace.id


@pytest.fixture
def workspace_id(session) -> uuid.UUID:
    return new_workspace(session)


def new_project(session: Session, workspace_id: uuid.UUID) -> uuid.UUID:
    return create_project(
        session,
        workspace_id=workspace_id,
        name="Example App",
        source_repo_url=f"https://example.com/acme/{uuid.uuid4()}.git",
    ).id


def analysed(session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Run an analysis of the project to success, storing a snapshot, its evidence and a version."""
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_analysis_run(session, **scope)
    mark_run_started(session, **scope, run_id=run.id)
    snapshot = Snapshot(
        root=Path("/unused"), source_url="https://unused", commit_sha="a" * 40, ref="main"
    )
    saved = save_analysis_of_project(
        session, **scope, snapshot=snapshot, facts=[FACT], profile=PROFILE
    )
    mark_run_succeeded(session, **scope, run_id=run.id, profile_id=saved.profile_id)


def proposed(session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID) -> uuid.UUID:
    """A pending proposal of an action a run of the project asked for."""
    run = GraphRunRef(kind="writer-run", run_id=uuid.uuid4())
    return propose_action(
        session,
        scope=RunScope(workspace_id=workspace_id, project_id=project_id),
        run=run,
        workflow_id=f"write-{run.run_id}",
        request=ActionRequest(
            action_id=f"writer-run:{run.run_id}:1", name="publish_post", payload={"text": "hi"}
        ),
        effect=ActionEffect.PUBLISHES,
        wait_seconds=60,
    ).id


def rows_of(session: Session, project_id: uuid.UUID) -> dict[str, int]:
    """How many rows each table holds for the project."""
    tables = {
        "project": Project.id,
        "analysis_run": AnalysisRunRecord.project_id,
        "evidence": Evidence.project_id,
        "product_profile": ProductProfileRecord.project_id,
        "repo_snapshot": RepoSnapshot.project_id,
        "proposal": ProposalRecord.project_id,
    }
    return {
        name: session.scalar(select(func.count()).where(column == project_id)) or 0
        for name, column in tables.items()
    }


NOTHING = {
    "project": 0,
    "analysis_run": 0,
    "evidence": 0,
    "product_profile": 0,
    "repo_snapshot": 0,
    "proposal": 0,
}


def test_project_without_history_is_deleted(session, workspace_id):
    project_id = new_project(session, workspace_id)
    delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_its_runs_evidence_snapshots_and_versions(session, workspace_id):
    project_id = new_project(session, workspace_id)
    analysed(session, workspace_id, project_id)
    assert all(
        count > 0 for name, count in rows_of(session, project_id).items() if name != "proposal"
    )
    delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert rows_of(session, project_id) == NOTHING


def test_approved_and_edited_versions_are_deleted_too(session, workspace_id):
    project_id = new_project(session, workspace_id)
    analysed(session, workspace_id, project_id)
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    approve_profile_version(session, **scope, version=1, approved_by=REVIEWER)
    save_edited_profile(session, **scope, edited_version=1, profile=PROFILE)
    delete_project(session, **scope)
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_its_decided_proposals(session, workspace_id):
    project_id = new_project(session, workspace_id)
    decide_proposal(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        proposal_id=proposed(session, workspace_id, project_id),
        approved=False,
        decided_by=REVIEWER,
    )
    delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert rows_of(session, project_id) == NOTHING


def test_project_with_a_pending_proposal_is_not_deleted(session, workspace_id):
    project_id = new_project(session, workspace_id)
    proposed(session, workspace_id, project_id)
    with pytest.raises(ProjectHasPendingProposal):
        delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert rows_of(session, project_id)["proposal"] == 1


def test_project_with_a_failed_run_is_deleted(session, workspace_id):
    project_id = new_project(session, workspace_id)
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_analysis_run(session, **scope)
    mark_run_failed(session, **scope, run_id=run.id, error="clone failed")
    delete_project(session, **scope)
    assert rows_of(session, project_id) == NOTHING


def test_other_projects_keep_their_rows(session, workspace_id):
    deleted = new_project(session, workspace_id)
    kept = new_project(session, workspace_id)
    analysed(session, workspace_id, deleted)
    analysed(session, workspace_id, kept)
    before = rows_of(session, kept)
    delete_project(session, workspace_id=workspace_id, project_id=deleted)
    assert rows_of(session, kept) == before


@pytest.mark.parametrize("started", [False, True])
def test_project_with_an_unfinished_run_is_not_deleted(session, workspace_id, started):
    project_id = new_project(session, workspace_id)
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_analysis_run(session, **scope)
    if started:
        mark_run_started(session, **scope, run_id=run.id)
    with pytest.raises(ProjectHasUnfinishedRun):
        delete_project(session, **scope)
    assert rows_of(session, project_id)["analysis_run"] == 1


def test_unknown_project_is_not_found(session, workspace_id):
    with pytest.raises(ProjectNotFound):
        delete_project(session, workspace_id=workspace_id, project_id=uuid.uuid4())


def test_project_of_another_workspace_is_not_found_and_stays(session, workspace_id):
    project_id = new_project(session, workspace_id)
    with pytest.raises(ProjectNotFound):
        delete_project(session, workspace_id=new_workspace(session), project_id=project_id)
    assert rows_of(session, project_id)["project"] == 1


def test_repository_of_a_deleted_project_can_have_a_new_project(session, workspace_id):
    repository = "https://example.com/acme/app.git"
    first = create_project(
        session, workspace_id=workspace_id, name="First", source_repo_url=repository
    )
    delete_project(session, workspace_id=workspace_id, project_id=first.id)
    again = create_project(
        session, workspace_id=workspace_id, name="Again", source_repo_url=repository
    )
    assert again.id != first.id


def test_versions_still_cannot_be_deleted_on_their_own(session, workspace_id):
    project_id = new_project(session, workspace_id)
    analysed(session, workspace_id, project_id)
    session.execute(delete(AnalysisRunRecord).where(AnalysisRunRecord.project_id == project_id))
    with (
        pytest.raises(IntegrityError, match="version 1 cannot be deleted"),
        session.begin_nested(),
    ):
        session.execute(
            delete(ProductProfileRecord).where(ProductProfileRecord.project_id == project_id)
        )


def test_leave_to_delete_versions_ends_with_the_deletion(session, workspace_id):
    project_id = new_project(session, workspace_id)
    delete_project(session, workspace_id=workspace_id, project_id=project_id)
    setting = session.scalar(text("SELECT current_setting('openmarketer.deleted_project', true)"))
    assert setting == ""


def test_leave_to_delete_versions_covers_only_the_project_it_names(session, workspace_id):
    project_id = new_project(session, workspace_id)
    analysed(session, workspace_id, project_id)
    session.execute(delete(AnalysisRunRecord).where(AnalysisRunRecord.project_id == project_id))
    session.execute(
        select(func.set_config("openmarketer.deleted_project", str(uuid.uuid4()), True))
    )
    with (
        pytest.raises(IntegrityError, match="version 1 cannot be deleted"),
        session.begin_nested(),
    ):
        session.execute(
            delete(ProductProfileRecord).where(ProductProfileRecord.project_id == project_id)
        )
