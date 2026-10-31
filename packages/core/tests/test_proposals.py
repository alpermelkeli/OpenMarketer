"""Tests for storing the proposals the policy gate holds, and deciding them.

They need PostgreSQL (see the root ``conftest.py``).
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.actions import ActionEffect, ActionRequest
from openmarketer_core.agent_runtime.gate import ProposalStatus, RunScope
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.models import Project, ProposalRecord, RunEventRecord, Workspace
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.proposals import (
    ActionProposal,
    ProposalNotFound,
    ProposalNotPending,
    approves_request,
    decide_proposal,
    get_proposal,
    list_proposals,
    list_waiting_proposals,
    pending_proposal_of_run,
    proposal_of_action,
    propose_action,
    settle_proposal,
)
from openmarketer_core.run_events import (
    PROPOSAL_DECIDED,
    PROPOSAL_EXPIRED,
    PROPOSAL_WAITING,
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
    effect: ActionEffect = ActionEffect.PUBLISHES,
    estimate_usd: Decimal | None = None,
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
        effect=effect,
        wait_seconds=wait_seconds,
        estimate_usd=estimate_usd,
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


def second_project(session: Session, workspace_id: uuid.UUID) -> RunScope:
    project = Project(
        workspace_id=workspace_id, name="Other", source_repo_url="https://example.com/o.git"
    )
    session.add(project)
    session.flush()
    return RunScope(workspace_id=workspace_id, project_id=project.id)


def events_of(session: Session, proposal: ActionProposal) -> list[RunEventRecord]:
    stored = session.scalars(
        select(RunEventRecord)
        .where(RunEventRecord.run_id == proposal.run_id)
        .order_by(RunEventRecord.id)
    )
    return list(stored)


def test_proposed_action_waits_for_a_person_with_what_it_asked_for(session, scope):
    proposal = propose(session, scope, payload={"text": "v2 is out"})

    assert proposal.status is ProposalStatus.PENDING
    assert (proposal.action_name, proposal.effect) == ("publish_post", ActionEffect.PUBLISHES)
    assert proposal.payload == {"text": "v2 is out"}
    assert (proposal.decided_by, proposal.decided_at) == (None, None)


def test_proposed_spend_holds_what_it_was_estimated_to_cost(session, scope):
    proposal = propose(session, scope, effect=ActionEffect.SPENDS, estimate_usd=Decimal("0.400000"))

    assert proposal.estimate_usd == Decimal("0.4")
    assert get_proposal(session, **_ids(scope), proposal_id=proposal.id).estimate_usd == Decimal(
        "0.4"
    )


def test_proposal_that_does_not_spend_has_no_estimate(session, scope):
    assert propose(session, scope).estimate_usd is None


def test_database_refuses_an_estimate_on_an_action_that_does_not_spend(session, scope):
    with pytest.raises(IntegrityError), session.begin_nested():
        propose(session, scope, estimate_usd=Decimal(1))


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


def approved_request(session: Session, scope: RunScope, **changes: Any) -> ActionRequest:
    """A request of a run of ``writer-run`` that a person approved, as it was proposed."""
    run_id = uuid.uuid4()
    request = ActionRequest(f"writer-run:{run_id}:1", "publish_post", {"text": "v2 is out"})
    propose_action(
        session,
        scope=scope,
        run=GraphRunRef(kind="writer-run", run_id=run_id),
        workflow_id=f"write-{run_id}",
        request=request,
        effect=ActionEffect.PUBLISHES,
        wait_seconds=DAY,
    )
    proposal = proposal_of_action(session, **scope_of(scope), action_id=request.action_id)
    assert proposal is not None
    decide(session, scope, proposal, approved=True)
    return ActionRequest(
        changes.get("action_id", request.action_id),
        changes.get("name", request.name),
        changes.get("payload", request.payload),
    )


def test_request_a_person_approved_as_it_was_proposed_is_approved(session, scope):
    request = approved_request(session, scope)
    assert approves_request(session, request, run_kind="writer-run")


def test_request_whose_proposal_is_pending_or_rejected_is_not_approved(session, scope):
    pending = propose(session, scope)
    rejected = propose(session, scope)
    decide(session, scope, rejected, approved=False)

    for proposal in (pending, rejected):
        stored = session.get(ProposalRecord, proposal.id)
        request = ActionRequest(stored.action_id, proposal.action_name, proposal.payload)
        assert not approves_request(session, request, run_kind="writer-run")


def test_request_with_no_proposal_is_not_approved(session):
    request = ActionRequest(f"writer-run:{uuid.uuid4()}:1", "publish_post", {"text": "v2"})
    assert not approves_request(session, request, run_kind="writer-run")


def test_approved_key_with_another_action_name_approves_nothing(session, scope):
    request = approved_request(session, scope, name="delete_everything")
    assert not approves_request(session, request, run_kind="writer-run")


def test_approved_key_with_another_payload_approves_nothing(session, scope):
    request = approved_request(session, scope, payload={"text": "something else"})
    assert not approves_request(session, request, run_kind="writer-run")


def test_approved_request_of_another_kind_of_run_is_not_approved(session, scope):
    request = approved_request(session, scope)
    assert not approves_request(session, request, run_kind="studio-run")


def test_approved_proposal_of_another_run_than_the_key_names_approves_nothing(session, scope):
    # The proposal is stored for one run; a key naming another run's thread is not its request.
    run_id, other_run = uuid.uuid4(), uuid.uuid4()
    request = ActionRequest(f"writer-run:{other_run}:1", "publish_post", {"text": "v2 is out"})
    stored = propose_action(
        session,
        scope=scope,
        run=GraphRunRef(kind="writer-run", run_id=run_id),
        workflow_id=f"write-{run_id}",
        request=request,
        effect=ActionEffect.PUBLISHES,
        wait_seconds=DAY,
    )
    decide(session, scope, stored, approved=True)

    assert not approves_request(session, request, run_kind="writer-run")


def test_proposal_of_an_action_is_read_by_its_key_within_its_project(session, scope):
    proposal = propose(session, scope, action_id="writer-run:r:1")

    assert proposal_of_action(session, **scope_of(scope), action_id="writer-run:r:1") == (proposal)
    assert proposal_of_action(session, **scope_of(scope), action_id="writer-run:r:2") is None


def test_proposal_of_an_action_of_another_project_or_workspace_is_none(session, scope):
    propose(session, scope, action_id="writer-run:r:1")
    sibling = second_project(session, scope.workspace_id)
    stranger = new_scope(session)

    for other in (sibling, RunScope(stranger.workspace_id, scope.project_id)):
        assert proposal_of_action(session, **scope_of(other), action_id="writer-run:r:1") is None


def scope_of(scope: RunScope) -> dict[str, uuid.UUID]:
    return {"workspace_id": scope.workspace_id, "project_id": scope.project_id}


def test_the_pending_proposal_a_run_waits_on_is_read_by_its_run(session, scope):
    proposal = propose(session, scope)

    assert (
        pending_proposal_of_run(
            session, **scope_of(scope), run_kind=proposal.run_kind, run_id=proposal.run_id
        )
        == proposal.id
    )


def test_a_run_whose_proposal_was_decided_waits_on_nothing(session, scope):
    proposal = propose(session, scope)
    decide(session, scope, proposal, approved=True)

    assert (
        pending_proposal_of_run(
            session, **scope_of(scope), run_kind=proposal.run_kind, run_id=proposal.run_id
        )
        is None
    )


def test_the_pending_proposal_of_another_project_workspace_or_kind_is_none(session, scope):
    proposal = propose(session, scope)
    sibling = second_project(session, scope.workspace_id)
    stranger = new_scope(session)

    for other in (sibling, RunScope(stranger.workspace_id, scope.project_id)):
        assert (
            pending_proposal_of_run(
                session, **scope_of(other), run_kind=proposal.run_kind, run_id=proposal.run_id
            )
            is None
        )
    assert (
        pending_proposal_of_run(
            session, **scope_of(scope), run_kind="studio-run", run_id=proposal.run_id
        )
        is None
    )


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


def test_a_proposal_stored_now_is_recorded_on_its_run(session, scope):
    proposal = propose(session, scope, payload={"text": "v2 is out"})

    events = events_of(session, proposal)

    assert [event.kind for event in events] == [PROPOSAL_WAITING]
    assert events[0].detail == {
        "proposal_id": str(proposal.id),
        "action_name": "publish_post",
        "effect": "publishes",
    }


def test_proposing_the_same_action_again_records_no_event_again(session, scope):
    first = propose(session, scope, action_id="writer-run:r:1")
    propose(session, scope, action_id="writer-run:r:1")

    assert [event.kind for event in events_of(session, first)] == [PROPOSAL_WAITING]
    assert session.scalar(select(func.count()).select_from(RunEventRecord)) == 1


@pytest.mark.parametrize("approved", [True, False])
def test_a_persons_decision_is_recorded_on_its_run(session, scope, approved):
    proposal = propose(session, scope)

    decide(session, scope, proposal, approved=approved)

    events = events_of(session, proposal)
    assert [event.kind for event in events] == [PROPOSAL_WAITING, PROPOSAL_DECIDED]
    assert events[-1].detail == {"proposal_id": str(proposal.id), "approved": approved}


def test_a_decision_that_came_too_late_records_nothing(session, scope):
    proposal = propose(session, scope, wait_seconds=0.001)
    session.execute(select(func.pg_sleep(0.01)))

    with pytest.raises(ProposalNotPending):
        decide(session, scope, proposal, approved=True)
    assert [event.kind for event in events_of(session, proposal)] == [PROPOSAL_WAITING]


def test_an_expired_proposal_is_recorded_on_its_run(session, scope):
    proposal = propose(session, scope, action_id="writer-run:r:1")

    settle_proposal(session, scope=scope, action_id="writer-run:r:1", deadline_passed=True)

    events = events_of(session, proposal)
    assert [event.kind for event in events] == [PROPOSAL_WAITING, PROPOSAL_EXPIRED]
    assert events[-1].detail == {"proposal_id": str(proposal.id)}


def test_settling_a_proposal_that_did_not_expire_records_nothing(session, scope):
    proposal = propose(session, scope, action_id="writer-run:r:1")

    settle_proposal(session, scope=scope, action_id="writer-run:r:1", deadline_passed=False)

    assert [event.kind for event in events_of(session, proposal)] == [PROPOSAL_WAITING]


def test_waiting_list_holds_the_pending_proposals_of_every_project(session, scope):
    other = second_project(session, scope.workspace_id)
    waiting = propose(session, scope)
    also_waiting = propose(session, other)
    decided = propose(session, scope)
    decide(session, scope, decided, approved=True)

    page = list_waiting_proposals(session, workspace_id=scope.workspace_id, limit=10)

    assert {proposal.id for proposal in page.items} == {waiting.id, also_waiting.id}
    assert {proposal.project_id for proposal in page.items} == {scope.project_id, other.project_id}
    assert page.next_before is None


def test_waiting_list_does_not_hold_another_workspaces_proposals(session, scope):
    propose(session, new_scope(session))

    page = list_waiting_proposals(session, workspace_id=scope.workspace_id, limit=10)

    assert page.items == ()


def test_waiting_list_is_read_a_page_at_a_time(session, scope):
    for _ in range(3):
        propose(session, scope)

    first = list_waiting_proposals(session, workspace_id=scope.workspace_id, limit=2)
    rest = list_waiting_proposals(
        session, workspace_id=scope.workspace_id, limit=2, before=first.next_before
    )

    assert len(first.items) == 2
    assert first.next_before is not None
    assert len(rest.items) == 1
    assert rest.next_before is None
    assert not {proposal.id for proposal in first.items} & {proposal.id for proposal in rest.items}


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
