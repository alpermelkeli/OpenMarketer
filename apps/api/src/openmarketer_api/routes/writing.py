"""The writer: request a post, poll its run, and read the posts the runs proposed.

A writing run is executed by the worker; its state and its content item are read from the
database only, and no route asks the workflow engine about a run. A project may have several
writing runs at once, and each proposes one post, its content item. Whether a post is sent is
decided on its proposal (``routes/proposals.py``), by a person; nothing here approves,
rejects or edits a post, and in the writer's first step an approved post is recorded as a dry
run and sent nowhere.

A single run is read with its brief, read back strictly; a stored brief that no longer fits
the schema is answered as no brief rather than as an error. The list of runs leaves the brief
out. A content item is listed without the record of how it was written (its plan, the other
variants, its claims with their evidence and the two reports), which is read with the item
alone. The profile version a run and its item write from is named by its number, never by
its row. Cancelling or deleting a run is not here; runs are deleted with their project.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from openmarketer_api.dependencies import DbSession, Sessions, StartWorkflow
from openmarketer_api.errors import problems
from openmarketer_api.identity import CurrentWorkspaceId
from openmarketer_api.pagination import DEFAULT_PAGE_SIZE, CreatedBefore, PageLimit, created_cursor
from openmarketer_core.db.content_items import ContentItem, content_item, list_content_items
from openmarketer_core.db.models import ContentItemStatus, WritingRunStatus
from openmarketer_core.db.writing_runs import (
    WritingRun,
    list_writing_runs,
    requested_brief,
    writing_run,
)
from openmarketer_core.writing.brief import (
    StoredWritingBriefInvalid,
    WritingBrief,
    WritingPlatform,
)
from openmarketer_core.writing.request import request_post

router = APIRouter(prefix="/projects/{project_id}", tags=["writing"])


class WritingRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    status: WritingRunStatus
    platform: WritingPlatform
    requested_by: str = Field(
        description="Who asked for the run: `person`, `orchestrator` (the chat) or `run`."
    )
    error: str | None
    profile_version: int | None = Field(
        description="The approved profile version the run writes from, once it has read it."
    )
    content_item_id: uuid.UUID | None = Field(
        description="The post the run proposed, once it has stored it."
    )
    cost_usd: float = Field(description="What the run's model calls cost, in USD, once it ended.")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class WritingRunDetailResponse(WritingRunResponse):
    brief: WritingBrief | None = Field(
        description=(
            "What the run was asked to write; null when the stored brief no longer fits the "
            "brief's schema, which a later version may have tightened."
        )
    )


class WritingRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: list[WritingRunResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


class ContentItemResponse(BaseModel):
    """A post a run proposed, without the record of how it was written."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    platform: WritingPlatform
    status: ContentItemStatus = Field(
        description=(
            "`proposed` while it waits for a person; `dry_run` once approved and recorded as "
            "it would have been sent (nothing is sent yet), `rejected`, `expired` or `failed`."
        )
    )
    text: str = Field(description="The post as it would be sent.")
    profile_version: int = Field(description="The approved profile version it was written from.")
    proposal_id: uuid.UUID | None = Field(
        description="The proposal a person decides the post on, once the item names it."
    )
    asset_id: uuid.UUID | None = Field(
        description="The Studio asset whose image the post goes with; null for a post alone."
    )
    asset_version_id: uuid.UUID | None = Field(
        description=(
            "The version of that asset the post goes with, the image a person approves with "
            "the text; null exactly when `asset_id` is."
        )
    )
    research_run_id: uuid.UUID | None = Field(
        description="The research run whose findings the post drew on; null when none."
    )
    cost_usd: float
    created_at: datetime
    settled_at: datetime | None


class ContentItemDetailResponse(ContentItemResponse):
    """A post with the record of how it was written, as the worker stored it."""

    plan: dict[str, Any] = Field(description="The plan the post was drafted from.")
    plan_changes: list[str] = Field(description="What the code changed in the writer's plan.")
    alternatives: list[dict[str, Any]] = Field(
        description="The other variants, each with what the checks and the critic found of it."
    )
    claims: list[dict[str, Any]] = Field(
        description="What the post claims, each with the features and the evidence it rests on."
    )
    check_report: dict[str, Any] = Field(description="What the code's rules found of the post.")
    critic_report: dict[str, Any] = Field(description="What the critic found of the post.")
    flagged_numbers: list[str] = Field(
        description="Numbers in the post the profile does not hold, for a person to check."
    )


class ContentItemListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ContentItemResponse]
    next_cursor: str | None = Field(
        description="Null on the last page; otherwise the `cursor` of the next request."
    )


def _run_response(run: WritingRun) -> WritingRunResponse:
    return WritingRunResponse(
        id=run.id,
        project_id=run.project_id,
        status=run.status,
        platform=run.platform,
        requested_by=run.requested_by,
        error=run.error,
        profile_version=run.profile_version,
        content_item_id=run.content_item_id,
        cost_usd=float(run.cost_usd),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _item_response(item: ContentItem) -> ContentItemResponse:
    return ContentItemResponse(
        id=item.id,
        project_id=item.project_id,
        run_id=item.run_id,
        platform=item.platform,
        status=item.status,
        text=item.text,
        profile_version=item.profile_version,
        proposal_id=item.proposal_id,
        asset_id=item.asset_id,
        asset_version_id=item.asset_version_id,
        research_run_id=item.research_run_id,
        cost_usd=float(item.cost_usd),
        created_at=item.created_at,
        settled_at=item.settled_at,
    )


def _item_detail_response(item: ContentItem) -> ContentItemDetailResponse:
    return ContentItemDetailResponse(
        **_item_response(item).model_dump(),
        plan=dict(item.plan),
        plan_changes=list(item.plan_changes),
        alternatives=list(item.alternatives),
        claims=list(item.claims),
        check_report=dict(item.check_report),
        critic_report=dict(item.critic_report),
        flagged_numbers=list(item.flagged_numbers),
    )


@router.post(
    "/writing-runs",
    operation_id="startWritingRun",
    status_code=202,
    response_model=WritingRunResponse,
    responses=problems(404, 409, 503),
)
def start(
    project_id: uuid.UUID,
    workspace_id: CurrentWorkspaceId,
    sessions: Sessions,
    start_workflow: StartWorkflow,
    brief: WritingBrief,
) -> WritingRunResponse:
    """Queue a writing run for the brief and return the run to poll.

    A worker executes it later: it writes from the project's approved profile, and a brief
    that names a feature the profile does not hold as live ends the run failed, saying so.
    The post it proposes waits for a person on its proposal.

    The brief may name `asset`, an image of the project's Studio library the post goes with,
    and `research_run_id`, a research run of the project whose findings it may draw on. One
    the project does not have is a 404 (`media_asset_not_found`, `research_not_found`); 409
    `media_asset_not_usable` when the asset was rejected, has no image or is still being
    made, and 409 `research_not_usable` when the research run has not succeeded. 503
    `writing_not_started` when
    the run could not be handed to a worker: it is then stored as failed and a new one can be
    started.
    """
    run = request_post(
        sessions,
        workspace_id=workspace_id,
        project_id=project_id,
        brief=brief,
        start_workflow=start_workflow,
    )
    return _run_response(run)


@router.get(
    "/writing-runs",
    operation_id="listWritingRuns",
    response_model=WritingRunListResponse,
    responses=problems(404),
)
def list_runs(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> WritingRunListResponse:
    """The project's writing runs as they are now, newest first, one page at a time."""
    page = list_writing_runs(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return WritingRunListResponse(
        runs=[_run_response(run) for run in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/writing-runs/{run_id}",
    operation_id="getWritingRun",
    response_model=WritingRunDetailResponse,
    responses=problems(404),
)
def get_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> WritingRunDetailResponse:
    """The run as it is now, with its brief.

    Queued, running, succeeded (with its post) or failed.
    """
    run = writing_run(session, workspace_id=workspace_id, project_id=project_id, run_id=run_id)
    try:
        brief = requested_brief(
            session, workspace_id=workspace_id, project_id=project_id, run_id=run_id
        )
    except StoredWritingBriefInvalid:
        brief = None
    return WritingRunDetailResponse(**_run_response(run).model_dump(), brief=brief)


@router.get(
    "/content-items",
    operation_id="listContentItems",
    response_model=ContentItemListResponse,
    responses=problems(404),
)
def list_items(
    project_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
    before: CreatedBefore,
    limit: PageLimit = DEFAULT_PAGE_SIZE,
) -> ContentItemListResponse:
    """The posts the project's writing runs proposed, newest first, one page at a time."""
    page = list_content_items(
        session, workspace_id=workspace_id, project_id=project_id, limit=limit, before=before
    )
    return ContentItemListResponse(
        items=[_item_response(item) for item in page.items],
        next_cursor=created_cursor(page.next_before),
    )


@router.get(
    "/content-items/{item_id}",
    operation_id="getContentItem",
    response_model=ContentItemDetailResponse,
    responses=problems(404),
)
def get_item(
    project_id: uuid.UUID,
    item_id: uuid.UUID,
    session: DbSession,
    workspace_id: CurrentWorkspaceId,
) -> ContentItemDetailResponse:
    """One post as it is now, with the record of how it was written."""
    item = content_item(session, workspace_id=workspace_id, project_id=project_id, item_id=item_id)
    return _item_detail_response(item)
