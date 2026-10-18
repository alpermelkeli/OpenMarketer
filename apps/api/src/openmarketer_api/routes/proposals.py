"""Proposals: the actions the policy gate held for a person, and the person's decision.

An agent's run that asks to publish or to spend stores a proposal and waits. These routes list a
project's proposals, read one, and approve or reject one as the current user; the run is then
told, and runs the action or hears that it was refused. No request field names a decider, and
no route decides on behalf of a model. Editing what a proposal asks for is not here.

``workspace_router`` adds the workspace's own view, beside the project's: every pending proposal
of every project, which the dashboard tells a person about wherever they are.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from openmarketer_api.dependencies import DbSession, Sessions, TellRun, no_request_body
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentUserId, CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.agent_runtime.actions import ActionEffect
from openmarketer_core.agent_runtime.gate import ProposalStatus
from openmarketer_core.approvals.decision import decide_on_proposal
from openmarketer_core.db.proposals import (
    ActionProposal,
    get_proposal,
    list_proposals,
    list_waiting_proposals,
)

router = APIRouter(prefix="/projects/{project_id}/proposals", tags=["proposals"])
workspace_router = APIRouter(prefix="/proposals", tags=["proposals"])

StatusFilter = Annotated[
    ProposalStatus | SkipJsonSchema[None],
    Query(description="Only the proposals with this status. Leave out for all of them."),
]


class ProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    action_name: str = Field(
        description="The action the agent asked for, as the agent declared it."
    )
    effect: ActionEffect = Field(description="What the action does beyond the run.")
    payload: dict[str, Any] = Field(
        description="What the agent asked the action to do: what a person approves or rejects."
    )
    status: ProposalStatus
    run_kind: str = Field(description="The kind of run that asked, such as `analysis-run`.")
    run_id: uuid.UUID
    created_at: datetime
    expires_at: datetime = Field(
        description="Undecided by then, the proposal expires and the action does not run."
    )
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    estimate_usd: float | None = Field(
        description="What an action that spends was estimated to cost, in USD; null otherwise."
    )


class ProposalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[ProposalResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _response(proposal: ActionProposal) -> ProposalResponse:
    return ProposalResponse(
        id=proposal.id,
        project_id=proposal.project_id,
        action_name=proposal.action_name,
        effect=proposal.effect,
        payload=proposal.payload,
        status=proposal.status,
        run_kind=proposal.run_kind,
        run_id=proposal.run_id,
        created_at=proposal.created_at,
        expires_at=proposal.expires_at,
        decided_by=proposal.decided_by,
        decided_at=proposal.decided_at,
        estimate_usd=None if proposal.estimate_usd is None else float(proposal.estimate_usd),
    )


@router.get(
    "",
    operation_id="listProposals",
    response_model=ProposalListResponse,
    responses=problems(404),
)
def list_all(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    status: StatusFilter = None,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ProposalListResponse:
    """The project's proposals as they are now, newest first, one page at a time."""
    page = list_proposals(
        session,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=limit,
        status=status,
        before=before,
    )
    return ProposalListResponse(
        proposals=[_response(proposal) for proposal in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/{proposal_id}",
    operation_id="getProposal",
    response_model=ProposalResponse,
    responses=problems(404),
)
def get(
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ProposalResponse:
    """One proposal as it is now."""
    proposal = get_proposal(
        session, workspace_id=workspace_id, project_id=project_id, proposal_id=proposal_id
    )
    return _response(proposal)


@router.post(
    "/{proposal_id}/approval",
    operation_id="approveProposal",
    response_model=ProposalResponse,
    responses=problems(404, 409),
    dependencies=[Depends(no_request_body)],
)
def approve(
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    sessions: Sessions,
    workspace_id: CurrentWorkspaceId,
    decider: CurrentUserId,
    tell_run: TellRun,
) -> ProposalResponse:
    """Approve a pending proposal as the current user; its run then runs the action.

    409 `proposal_not_pending` when it was decided already or its deadline passed. The request
    takes no body; one is a 422.
    """
    return _decide(sessions, workspace_id, project_id, proposal_id, decider, tell_run, True)


@router.post(
    "/{proposal_id}/rejection",
    operation_id="rejectProposal",
    response_model=ProposalResponse,
    responses=problems(404, 409),
    dependencies=[Depends(no_request_body)],
)
def reject(
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    sessions: Sessions,
    workspace_id: CurrentWorkspaceId,
    decider: CurrentUserId,
    tell_run: TellRun,
) -> ProposalResponse:
    """Reject a pending proposal as the current user; its run is told the action was refused.

    409 `proposal_not_pending` when it was decided already or its deadline passed. The request
    takes no body; one is a 422.
    """
    return _decide(sessions, workspace_id, project_id, proposal_id, decider, tell_run, False)


def _decide(
    sessions: Sessions,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    proposal_id: uuid.UUID,
    decider: uuid.UUID,
    tell_run: TellRun,
    approved: bool,
) -> ProposalResponse:
    proposal = decide_on_proposal(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        proposal_id=proposal_id,
        approved=approved,
        decided_by=decider,
        tell_run=tell_run,
    )
    return _response(proposal)


@workspace_router.get(
    "",
    operation_id="listWaitingProposals",
    response_model=ProposalListResponse,
)
def list_waiting(
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ProposalListResponse:
    """Every pending proposal of the workspace's projects, newest first, one page at a time.

    What waits for a person across projects; the dashboard reads it to tell them wherever they
    are, and refreshes it as the workspace's event stream announces proposals.
    """
    page = list_waiting_proposals(session, workspace_id=workspace_id, limit=limit, before=before)
    return ProposalListResponse(
        proposals=[_response(proposal) for proposal in page.items],
        next_cursor=created_cursor(page.next_before),
    )
