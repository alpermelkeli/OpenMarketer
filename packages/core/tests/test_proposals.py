"""Tests for storing the proposals the policy gate holds, and deciding them.

They need PostgreSQL (see the root ``conftest.py``).
"""

import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import ProposalStatus, RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import Project, ProposalRecord, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.proposals import (
    ActionProposal,
    ProposalNotFound,
    ProposalNotPending,
    decide_proposal,
    get_proposal,
    list_proposals,
    proposal_status_of_action,
    propose_action,
    settle_proposal,
)

PERSON = uuid.uuid4()
DAY = 24 * 60 * 60


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


def propose(
    session: Session,
    scope: RunScope,
    *,
    action_id: str | None = None,
    payload: dict | None = None,
    wait_seconds: float = DAY,
) -> ActionProposal:
    run = GraphRunRef(kind="writer-run", run_id=uuid.uuid4())
    request = ActionRequest(
        action_id=action_id or f"writer-run:{run.run_id}:{uuid.uuid4().hex}",
        name="publish_post",
        payload=payload or {"text": "v2 is out"},
    )
    return propose_action(
        session,
        scope=scope,
        run=run,
        workflow_id=f"write-{run.run_id}",
        request=request,
        effect=ActionEffect.PUBLISHES,
        wait_seconds=wait_seconds,
    )


def decide(session: Session, scope: RunScope, proposal: ActionProposal, *, approved: bool):
    return decide_proposal(
        session,
        workspace_id=scope.workspace_id,
        project_id=scope.project_id,
        proposal_id=proposal.id,
        approved=approved,
        decided_by=PERSON,
    )


def test_proposed_action_waits_for_a_person_with_what_it_asked_for(session, scope):
    proposal = propose(session, scope, payload={"text": "v2 is out"})

    assert proposal.status is ProposalStatus.PENDING
    assert (proposal.action_name, proposal.effect) == ("publish_post", ActionEffect.PUBLISHES)
    assert proposal.payload == {"text": "v2 is out"}
    assert (proposal.decided_by, proposal.decided_at) == (None, None)


def test_deadline_is_the_wait_from_now(session, scope):
    proposal = propose(session, scope, wait_seconds=DAY)

    assert proposal.expires_at - proposal.created_at == timedelta(seconds=DAY)


def test_proposing_the_same_action_again_returns_the_first_proposal(session, scope):
    first = propose(session, scope, action_id="writer-run:r:1", payload={"text": "a"})
    again = propose(session, scope, action_id="writer-run:r:1", payload={"text": "a"})

    assert again == first
    assert session.scalar(select(func.count()).select_from(ProposalRecord)) == 1


def test_action_of_another_project_is_not_returned_as_this_projects_proposal(session, scope):
    propose(session, scope, action_id="writer-run:r:1")

    with pytest.raises(ProposalNotFound):
        propose(session, new_scope(session), action_id="writer-run:r:1")


def test_proposal_for_a_project_the_workspace_does_not_have_is_refused(session, scope):
    other = RunScope(workspace_id=uuid.uuid4(), project_id=scope.project_id)

    with pytest.raises(ProjectNotFound):
        propose(session, other)


@pytest.mark.parametrize(
    ("approved", "status"), [(True, ProposalStatus.APPROVED), (False, ProposalStatus.REJECTED)]
)
def test_person_decides_a_pending_proposal(session, scope, approved, status):
    decided = decide(session, scope, propose(session, scope), approved=approved)

    assert decided.status is status
    assert decided.decided_by == PERSON
    assert decided.decided_at is not None


def test_decided_proposal_is_not_decided_again(session, scope):
    proposal = propose(session, scope)
    decide(session, scope, proposal, approved=False)

    with pytest.raises(ProposalNotPending) as refused:
        decide(session, scope, proposal, approved=True)
    assert refused.value.status is ProposalStatus.REJECTED
    assert get_proposal(session, **_ids(scope), proposal_id=proposal.id).status is (
        ProposalStatus.REJECTED
    )


def test_proposal_past_its_deadline_is_not_decided(session, scope):
    proposal = propose(session, scope, wait_seconds=0.001)
    session.execute(select(func.pg_sleep(0.01)))

    with pytest.raises(ProposalNotPending) as refused:
        decide(session, scope, proposal, approved=True)
    assert refused.value.status is ProposalStatus.EXPIRED


def test_proposal_of_another_workspace_is_not_found(session, scope):
    proposal = propose(session, scope)
    other = new_scope(session)

    with pytest.raises(ProposalNotFound):
        decide(session, RunScope(other.workspace_id, scope.project_id), proposal, approved=True)
    with pytest.raises(ProposalNotFound):
        get_proposal(
            session,
            workspace_id=other.workspace_id,
            project_id=scope.project_id,
            proposal_id=proposal.id,
        )


def test_run_expires_a_proposal_nobody_decided_once_its_deadline_passed(session, scope):
    proposal = propose(session, scope, action_id="writer-run:r:1")

    still = settle_proposal(session, scope=scope, action_id="writer-run:r:1", deadline_passed=False)
    expired = settle_proposal(
        session, scope=scope, action_id="writer-run:r:1", deadline_passed=True
    )

    assert (still, expired) == (ProposalStatus.PENDING, ProposalStatus.EXPIRED)
    stored = get_proposal(session, **_ids(scope), proposal_id=proposal.id)
    assert stored.decided_by is None
    assert stored.decided_at is not None


def test_decision_a_person_made_is_not_expired_by_the_run(session, scope):
    proposal = propose(session, scope, action_id="writer-run:r:1")
    decide(session, scope, proposal, approved=True)

    status = settle_proposal(session, scope=scope, action_id="writer-run:r:1", deadline_passed=True)

    assert status is ProposalStatus.APPROVED


def test_settling_an_action_with_no_proposal_is_refused(session, scope):
    with pytest.raises(ProposalNotFound):
        settle_proposal(session, scope=scope, action_id="writer-run:r:9", deadline_passed=False)


def test_status_of_an_action_is_read_by_its_key(session, scope):
    proposal = propose(session, scope, action_id="writer-run:r:1")
    decide(session, scope, proposal, approved=True)

    assert proposal_status_of_action(session, "writer-run:r:1") is ProposalStatus.APPROVED
    assert proposal_status_of_action(session, "writer-run:r:2") is None


def test_list_can_hold_the_proposals_of_one_status_only(session, scope):
    first = propose(session, scope)
    second = propose(session, scope)
    decide(session, scope, first, approved=True)

    everything = list_proposals(session, **_ids(scope), limit=10)
    pending = list_proposals(session, **_ids(scope), limit=10, status=ProposalStatus.PENDING)

    assert {p.id for p in everything.items} == {first.id, second.id}
    assert [p.id for p in pending.items] == [second.id]


def test_list_is_read_a_page_at_a_time(session, scope):
    for _ in range(3):
        propose(session, scope)

    first = list_proposals(session, **_ids(scope), limit=2)
    rest = list_proposals(session, **_ids(scope), limit=2, before=first.next_before)

    assert len(first.items) == 2
    assert first.next_before is not None
    assert len(rest.items) == 1
    assert rest.next_before is None
    assert not {p.id for p in first.items} & {p.id for p in rest.items}


def test_list_of_a_project_the_workspace_does_not_have_is_refused(session, scope):
    with pytest.raises(ProjectNotFound):
        list_proposals(session, workspace_id=uuid.uuid4(), project_id=scope.project_id, limit=10)


def test_database_refuses_an_approval_with_nobody_who_approved(session, scope):
    proposal = propose(session, scope)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(ProposalRecord)
            .where(ProposalRecord.id == proposal.id)
            .values(status="approved", decided_at=func.now())
        )


def test_database_refuses_a_proposal_of_an_action_that_works_for_the_run_itself(session, scope):
    proposal = propose(session, scope)

    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(
            update(ProposalRecord).where(ProposalRecord.id == proposal.id).values(effect="internal")
        )


def _ids(scope: RunScope) -> dict[str, Any]:
    return {"workspace_id": scope.workspace_id, "project_id": scope.project_id}
