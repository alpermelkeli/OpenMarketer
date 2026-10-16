"""Tests for the chat agent's reads: what an orchestrator chat turn may read of its project.

They need PostgreSQL (see the root ``conftest.py``).
"""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import ProposalStatus, RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.analysis_runs import (
    AnalysisRun,
    mark_run_failed,
    mark_run_started,
    request_analysis_run,
)
from openmarketer_core.db.models import (
    AnalysisRunRecord,
    AnalysisRunStatus,
    ProductProfileRecord,
    ProfileStatus,
    Project,
    ProjectStatus,
    Workspace,
)
from openmarketer_core.db.profile_versions import approve_profile_version
from openmarketer_core.db.proposals import ActionProposal, decide_proposal, propose_action
from openmarketer_core.orchestrator.reads import ChatReads, UnknownProfileSection
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.profile import ProductProfile

PERSON = uuid.uuid4()
DAY = 24 * 60 * 60
PROFILE = ProductProfile.model_validate({"product": {"name": "Memoria", "type": "consumer_app"}})


def new_scope(session: Session) -> RunScope:
    workspace = Workspace(name="Acme")
    session.add(workspace)
    session.flush()
    project = Project(
        workspace_id=workspace.id, name="Example", source_repo_url="https://example.com/r.git"
    )
    session.add(project)
    session.flush()
    return RunScope(workspace_id=workspace.id, project_id=project.id)


@pytest.fixture
def scope(session) -> RunScope:
    return new_scope(session)


def reads_of(session: Session, scope: RunScope) -> ChatReads:
    return PostgresChatReads(
        session=session, workspace_id=scope.workspace_id, project_id=scope.project_id
    )


def store_draft(
    session: Session, scope: RunScope, *, version: int = 1, profile: ProductProfile = PROFILE
) -> None:
    session.add(
        ProductProfileRecord(
            project_id=scope.project_id, version=version, content=profile.model_dump(mode="json")
        )
    )
    session.flush()


def store_approved(
    session: Session, scope: RunScope, *, version: int = 1, profile: ProductProfile = PROFILE
) -> None:
    store_draft(session, scope, version=version, profile=profile)
    approve_profile_version(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        version=version,
        approved_by=uuid.uuid4(),
    )


def propose(session: Session, scope: RunScope) -> ActionProposal:
    run = GraphRunRef(kind="writer-run", run_id=uuid.uuid4())
    request = ActionRequest(
        action_id=f"writer-run:{run.run_id}:{uuid.uuid4().hex}",
        name="publish_post",
        payload={"text": "v2 is out"},
    )
    return propose_action(
        session,
        scope=scope,
        run=run,
        workflow_id=f"write-{run.run_id}",
        request=request,
        effect=ActionEffect.PUBLISHES,
        wait_seconds=DAY,
    )


def fail_run(session: Session, scope: RunScope, run: AnalysisRun) -> AnalysisRun:
    """End the run as failed, with the message the reads are expected to carry."""
    return mark_run_failed(session, **_ids(scope), run_id=run.id, error="boom")


def backdate(session: Session, run_id: uuid.UUID, *, hours: int) -> None:
    """Move a run's request time into the past: one transaction gives every row the same clock."""
    session.execute(
        update(AnalysisRunRecord)
        .where(AnalysisRunRecord.id == run_id)
        .values(created_at=func.now() - timedelta(hours=hours))
    )


def test_status_is_the_project_and_where_its_review_stands(session, scope):
    status = reads_of(session, scope).project_status()

    assert status.project_id == scope.project_id
    assert status.name == "Example"
    assert status.repository_url == "https://example.com/r.git"
    assert status.status is ProjectStatus.ONBOARDING
    assert status.autonomy_level == 0
    assert status.product_url is None
    assert (status.latest_approved_version, status.latest_draft_version) == (None, None)
    assert status.unfinished_run_id is None
    assert status.latest_run is None
    assert (status.pending.count, status.pending.next_expiry) == (0, None)


def test_status_carries_the_latest_run_and_the_pending_count(session, scope):
    run = mark_run_started(
        session, **_ids(scope), run_id=request_analysis_run(session, **_ids(scope)).id
    )
    propose(session, scope)

    status = reads_of(session, scope).project_status()

    assert status.unfinished_run_id == run.id
    assert status.latest_run is not None
    assert status.latest_run.id == run.id
    assert status.latest_run.status is AnalysisRunStatus.RUNNING
    assert status.pending.count == 1
    assert status.pending.next_expiry is not None


def test_profile_is_none_until_something_is_stored(session, scope):
    assert reads_of(session, scope).profile() is None


def test_profile_reads_the_sections_and_the_review_state(session, scope):
    store_draft(session, scope)
    reads = reads_of(session, scope)

    whole = reads.profile()
    assert whole is not None
    assert (whole.version, whole.status, whole.content) == (1, ProfileStatus.DRAFT, None)
    expected = PROFILE.model_dump(mode="json")
    product = reads.profile("product")
    assert product is not None
    assert product.content == expected["product"]
    features = reads.profile("features")
    assert features is not None
    assert features.content == expected["features"]


def test_profile_falls_back_to_the_latest_draft(session, scope):
    store_draft(session, scope, version=1)
    store_draft(session, scope, version=2)

    reading = reads_of(session, scope).profile()

    assert reading is not None
    assert (reading.version, reading.status) == (2, ProfileStatus.DRAFT)


def test_profile_prefers_the_approved_version_over_a_newer_draft(session, scope):
    store_approved(session, scope, version=1)
    store_draft(session, scope, version=2)

    reading = reads_of(session, scope).profile()

    assert reading is not None
    assert (reading.version, reading.status) == (1, ProfileStatus.APPROVED)


def test_unknown_section_is_refused(session, scope):
    with pytest.raises(UnknownProfileSection):
        reads_of(session, scope).profile("pricing")


def test_pending_proposals_carry_what_was_asked(session, scope):
    first = propose(session, scope)
    second = propose(session, scope)
    decide_proposal(session, **_ids(scope), proposal_id=first.id, approved=True, decided_by=PERSON)
    reads = reads_of(session, scope)

    pending = reads.pending_proposals()

    assert [p.id for p in pending] == [second.id]
    asked = pending[0]
    assert asked.action_name == "publish_post"
    assert asked.effect is ActionEffect.PUBLISHES
    assert asked.payload == {"text": "v2 is out"}
    assert asked.run_kind == "writer-run"
    assert asked.run_id == second.run_id
    assert asked.status is ProposalStatus.PENDING
    assert reads.proposal(second.id) == asked
    decided = reads.proposal(first.id)
    assert decided is not None
    assert decided.status is ProposalStatus.APPROVED


def test_unknown_ids_read_as_none_not_errors(session, scope):
    reads = reads_of(session, scope)

    assert reads.proposal(uuid.uuid4()) is None
    assert reads.run(uuid.uuid4()) is None


def test_runs_are_read_newest_first_and_bounded(session, scope):
    older = request_analysis_run(session, **_ids(scope))
    older = mark_run_started(session, **_ids(scope), run_id=older.id)
    fail_run(session, scope, older)
    backdate(session, older.id, hours=1)
    newer = request_analysis_run(session, **_ids(scope))
    reads = reads_of(session, scope)

    assert [run.id for run in reads.recent_runs(10)] == [newer.id, older.id]
    assert [run.id for run in reads.recent_runs(1)] == [newer.id]
    failed = reads.run(older.id)
    assert failed is not None
    assert (failed.status, failed.error) == (AnalysisRunStatus.FAILED, "boom")
    queued = reads.run(newer.id)
    assert queued is not None
    assert queued.status is AnalysisRunStatus.QUEUED


def test_reads_of_one_project_do_not_reach_another(session, scope):
    other = new_scope(session)
    other_proposal = propose(session, other)
    other_run = request_analysis_run(session, **_ids(other))
    reads = reads_of(session, scope)

    assert reads.proposal(other_proposal.id) is None
    assert reads.run(other_run.id) is None


def _ids(scope: RunScope) -> dict[str, uuid.UUID]:
    return {"workspace_id": scope.workspace_id, "project_id": scope.project_id}
