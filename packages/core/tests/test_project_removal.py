"""Tests for deleting a project. They need PostgreSQL (see the root ``conftest.py``)."""

import uuid
from decimal import Decimal
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
from openmarketer_core.db.chat import append_exchange, create_chat
from openmarketer_core.db.chat_turns import finish_turn, start_turn
from openmarketer_core.db.evidence_store import save_analysis_of_project
from openmarketer_core.db.media_assets import NewAssetVersion, accept_asset, add_version
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    AssetTechnique,
    ChatMessageRecord,
    ChatThreadRecord,
    ChatTurnRecord,
    ChatTurnTrigger,
    Evidence,
    MediaAssetRecord,
    MediaAssetVersionRecord,
    ProductProfileRecord,
    Project,
    ProposalRecord,
    RepositoryImageRecord,
    RepoSnapshot,
    ResearchPageRecord,
    ResearchReportRecord,
    ResearchRunRecord,
    RunEventRecord,
    StudioRunRecord,
    Workspace,
)
from openmarketer_core.db.profile_versions import approve_profile_version, save_edited_profile
from openmarketer_core.db.project_removal import (
    ProjectHasPendingProposal,
    ProjectHasUnfinishedResearch,
    ProjectHasUnfinishedRun,
    ProjectHasUnfinishedStudioRun,
    delete_project,
)
from openmarketer_core.db.projects import ProjectNotFound, create_project
from openmarketer_core.db.proposals import decide_proposal, propose_action
from openmarketer_core.db.repository_images import (
    NewRepositoryImage,
    keep_repository_images,
    repository_image_key,
)
from openmarketer_core.db.research_pages import NewPage, store_page
from openmarketer_core.db.research_runs import (
    mark_run_failed as mark_research_failed,
)
from openmarketer_core.db.research_runs import (
    mark_run_started as mark_research_started,
)
from openmarketer_core.db.research_runs import (
    mark_run_succeeded as mark_research_succeeded,
)
from openmarketer_core.db.research_runs import (
    request_research_run,
)
from openmarketer_core.db.research_store import save_research_report
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.studio_runs import (
    mark_run_needs_review as mark_studio_needs_review,
)
from openmarketer_core.db.studio_runs import (
    mark_run_started as mark_studio_started,
)
from openmarketer_core.db.studio_runs import (
    request_studio_run,
)
from openmarketer_core.profile import ProductProfile
from openmarketer_core.repository_analysis.extraction import ExtractedFact
from openmarketer_core.repository_analysis.intake import Snapshot
from openmarketer_core.repository_analysis.repository_images.selection import (
    RepositoryImageKind,
)
from openmarketer_core.research.questions import ResearchRequest
from openmarketer_core.research.research_agent.rules import Report
from openmarketer_core.run_events import TURN_ANSWERED, TURN_STARTED, RunEvent
from openmarketer_core.studio.asset_store import asset_key
from openmarketer_core.studio.platforms import Platform
from openmarketer_core.studio.requests import AssetRequest

REVIEWER = uuid.UUID("11111111-1111-4111-8111-111111111111")
PROFILE = ProductProfile.model_validate({"product": {"name": "Example App", "type": "dev_tool"}})
FACT = ExtractedFact(extractor="manifest", kind="manifest.name", value="app", file="package.json")
RESEARCH_REQUEST = ResearchRequest(questions=["What did competitors post last month?"])
ASSET_REQUEST = AssetRequest(goal="Announce the launch", platform=Platform.OG_IMAGE)


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


def analysed(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, *, images: int = 0
) -> tuple[str, ...]:
    """Run an analysis of the project to success, storing a snapshot, its evidence and a version.

    With ``images``, the run keeps that many repository images first; their keys are answered.
    """
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_analysis_run(session, **scope)
    mark_run_started(session, **scope, run_id=run.id)
    keys = tuple(
        repository_image_key(**scope, analysis_run_id=run.id, sha256=f"{n:064x}", mime="image/png")
        for n in range(images)
    )
    keep_repository_images(
        session,
        **scope,
        analysis_run_id=run.id,
        images=[
            NewRepositoryImage(
                path=f"assets/logo-{n}.png",
                kind=RepositoryImageKind.LOGO,
                mime="image/png",
                width=512,
                height=512,
                size_bytes=4096,
                sha256=f"{n:064x}",
                storage_key=key,
            )
            for n, key in enumerate(keys)
        ],
    )
    snapshot = Snapshot(
        root=Path("/unused"), source_url="https://unused", commit_sha="a" * 40, ref="main"
    )
    saved = save_analysis_of_project(
        session, **scope, snapshot=snapshot, facts=[FACT], profile=PROFILE
    )
    mark_run_succeeded(session, **scope, run_id=run.id, profile_id=saved.profile_id)
    return keys


def researched(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[uuid.UUID, list[str]]:
    """Run a research of the project to success, storing a report and a page per key."""
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_research_run(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        request=RESEARCH_REQUEST,
        requested_by="person",
    )
    mark_research_started(session, **scope, run_id=run.id)
    keys = ["pages/a.html", "pages/b.html"]
    for key in keys:
        store_page(
            session,
            **scope,
            run_id=run.id,
            page_id=uuid.uuid4(),
            page=NewPage(
                url=f"https://example.com/{key}",
                title="A page",
                content_type="text/html",
                size_bytes=10,
                storage_key=key,
                sha256="a" * 64,
                flags=(),
            ),
        )
    report = save_research_report(
        session,
        **scope,
        run_id=run.id,
        report=Report(
            questions=list(RESEARCH_REQUEST.questions),
            findings=[],
            notes=[],
            cost_usd=0.01,
            model="fake/model",
        ),
    )
    mark_research_succeeded(session, **scope, run_id=run.id, report_id=report.id)
    return run.id, keys


def studio_run_of(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, *, chat_id=None
) -> uuid.UUID:
    """A started studio run of the project, as a chat asked for it when ``chat_id`` is given."""
    run = request_studio_run(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        request=ASSET_REQUEST,
        requested_by="person",
        chat_thread_id=chat_id,
    )
    mark_studio_started(session, workspace_id=workspace_id, project_id=project_id, run_id=run.id)
    return run.id


def made_asset(
    session: Session, workspace_id: uuid.UUID, project_id: uuid.UUID, *, chat_id=None
) -> tuple[str, ...]:
    """A studio run of the project that ended with two versions of an accepted asset."""
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run_id = studio_run_of(session, workspace_id, project_id, chat_id=chat_id)
    keys: list[str] = []
    asset_id = None
    for round in (1, 2):
        version_id = uuid.uuid4()
        key = asset_key(**scope, run_id=run_id, version_id=version_id, mime="image/png")
        version = add_version(
            session,
            **scope,
            run_id=run_id,
            version_id=version_id,
            action_id=f"studio-run:{run_id}:{round}",
            asset_id=asset_id,
            version=NewAssetVersion(
                round=round,
                technique=AssetTechnique.COMPOSED,
                storage_key=key,
                mime="image/png",
                width=1200,
                height=630,
                size_bytes=100,
                cost_usd=Decimal(0),
            ),
        )
        asset_id = version.asset_id
        keys.append(key)
    mark_studio_needs_review(
        session, **scope, run_id=run_id, final_version_id=version_id, cost_usd=Decimal(0)
    )
    assert asset_id is not None
    accept_asset(session, **scope, asset_id=asset_id, decided_by=REVIEWER)
    return tuple(keys)


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
        "chat_thread": ChatThreadRecord.project_id,
        "chat_message": ChatMessageRecord.project_id,
        "chat_turn": ChatTurnRecord.project_id,
        "run_event": RunEventRecord.project_id,
        "research_run": ResearchRunRecord.project_id,
        "research_page": ResearchPageRecord.project_id,
        "research_report": ResearchReportRecord.project_id,
        "studio_run": StudioRunRecord.project_id,
        "media_asset": MediaAssetRecord.project_id,
        "media_asset_version": MediaAssetVersionRecord.project_id,
        "repository_image": RepositoryImageRecord.project_id,
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
    "chat_thread": 0,
    "chat_message": 0,
    "chat_turn": 0,
    "run_event": 0,
    "research_run": 0,
    "research_page": 0,
    "research_report": 0,
    "studio_run": 0,
    "media_asset": 0,
    "media_asset_version": 0,
    "repository_image": 0,
}

WITHOUT_HISTORY = ("proposal", "chat_thread", "chat_message", "chat_turn", "run_event") + (
    "research_run",
    "research_page",
    "research_report",
    "studio_run",
    "media_asset",
    "media_asset_version",
    "repository_image",
)


def test_project_without_history_is_deleted(session, workspace_id):
    project_id = new_project(session, workspace_id)
    keys = delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert keys == ()
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_its_runs_evidence_snapshots_and_versions(session, workspace_id):
    project_id = new_project(session, workspace_id)
    analysed(session, workspace_id, project_id)
    assert all(
        count > 0
        for name, count in rows_of(session, project_id).items()
        if name not in WITHOUT_HISTORY
    )
    delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_its_chats_turns_messages_and_events(session, workspace_id):
    project_id = new_project(session, workspace_id)
    chat = create_chat(session, workspace_id=workspace_id, project_id=project_id)
    ids = {"workspace_id": workspace_id, "project_id": project_id, "chat_id": chat.id}
    turn = start_turn(session, **ids, trigger=ChatTurnTrigger.MESSAGE)
    append_exchange(session, **ids, turn_id=turn.id, question="Is it ready?", answer="Yes.")
    finish_turn(session, **ids, turn_id=turn.id)
    start_turn(session, **ids, trigger=ChatTurnTrigger.MESSAGE)  # one still running
    append_events(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        run_kind="orchestrator-chat",
        run_id=turn.id,
        events=[RunEvent(TURN_STARTED), RunEvent(TURN_ANSWERED)],
    )
    assert rows_of(session, project_id)["chat_thread"] == 1
    assert rows_of(session, project_id)["chat_message"] == 2
    assert rows_of(session, project_id)["chat_turn"] == 2
    assert rows_of(session, project_id)["run_event"] == 2
    delete_project(session, workspace_id=workspace_id, project_id=project_id)
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_its_research_reports_and_pages(session, workspace_id):
    project_id = new_project(session, workspace_id)
    _, keys = researched(session, workspace_id, project_id)
    assert rows_of(session, project_id)["research_page"] == 2
    assert rows_of(session, project_id)["research_report"] == 1

    answered = delete_project(session, workspace_id=workspace_id, project_id=project_id)

    assert sorted(answered) == sorted(keys)
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_a_research_run_a_chat_asked_for(session, workspace_id):
    project_id = new_project(session, workspace_id)
    chat = create_chat(session, workspace_id=workspace_id, project_id=project_id)
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_research_run(
        session,
        **scope,
        request=RESEARCH_REQUEST,
        requested_by="orchestrator",
        chat_thread_id=chat.id,
    )
    mark_research_started(session, **scope, run_id=run.id)
    mark_research_failed(session, **scope, run_id=run.id, error="boom")

    delete_project(session, **scope)

    assert rows_of(session, project_id) == NOTHING


def test_project_with_a_failed_research_is_deleted(session, workspace_id):
    project_id = new_project(session, workspace_id)
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_research_run(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        request=RESEARCH_REQUEST,
        requested_by="person",
    )
    mark_research_started(session, **scope, run_id=run.id)
    mark_research_failed(session, **scope, run_id=run.id, error="boom")

    keys = delete_project(session, **scope)

    assert keys == ()
    assert rows_of(session, project_id) == NOTHING


@pytest.mark.parametrize("started", [False, True])
def test_project_with_an_unfinished_research_is_not_deleted(session, workspace_id, started):
    project_id = new_project(session, workspace_id)
    scope = {"workspace_id": workspace_id, "project_id": project_id}
    run = request_research_run(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        request=RESEARCH_REQUEST,
        requested_by="person",
    )
    if started:
        mark_research_started(session, **scope, run_id=run.id)

    with pytest.raises(ProjectHasUnfinishedResearch):
        delete_project(session, **scope)

    assert rows_of(session, project_id)["research_run"] == 1


def test_project_is_deleted_with_its_studio_runs_and_accepted_assets(session, workspace_id):
    project_id = new_project(session, workspace_id)
    _, page_keys = researched(session, workspace_id, project_id)
    asset_keys = made_asset(session, workspace_id, project_id)
    assert rows_of(session, project_id)["media_asset_version"] == 2

    answered = delete_project(session, workspace_id=workspace_id, project_id=project_id)

    assert sorted(answered) == sorted([*page_keys, *asset_keys])
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_its_repository_images(session, workspace_id):
    project_id = new_project(session, workspace_id)
    image_keys = analysed(session, workspace_id, project_id, images=2)
    assert rows_of(session, project_id)["repository_image"] == 2

    answered = delete_project(session, workspace_id=workspace_id, project_id=project_id)

    assert sorted(answered) == sorted(image_keys)
    assert rows_of(session, project_id) == NOTHING


def test_project_is_deleted_with_a_studio_run_a_chat_asked_for(session, workspace_id):
    project_id = new_project(session, workspace_id)
    chat = create_chat(session, workspace_id=workspace_id, project_id=project_id)
    made_asset(session, workspace_id, project_id, chat_id=chat.id)

    delete_project(session, workspace_id=workspace_id, project_id=project_id)

    assert rows_of(session, project_id) == NOTHING


def test_project_with_an_unfinished_studio_run_is_not_deleted(session, workspace_id):
    project_id = new_project(session, workspace_id)
    studio_run_of(session, workspace_id, project_id)

    with pytest.raises(ProjectHasUnfinishedStudioRun):
        delete_project(session, workspace_id=workspace_id, project_id=project_id)

    assert rows_of(session, project_id)["studio_run"] == 1


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
    researched(session, workspace_id, kept)
    made_asset(session, workspace_id, deleted)
    made_asset(session, workspace_id, kept)
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
