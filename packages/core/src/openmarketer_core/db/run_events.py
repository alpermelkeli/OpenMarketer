"""Run events: storing what runs did, and reading back those of a chat thread.

Any kind of run appends its events (``openmarketer_core.run_events``) here,
named by its kind and id as a proposal names its run. A chat thread's events
are those of its turns (kind ``CHAT_TURN_KIND``) and of the research and studio
runs it asked for (kinds ``RESEARCH_RUN_KIND`` and ``STUDIO_RUN_KIND``, the runs
whose ``chat_thread_id`` is the thread), in the order they were stored.

The identifier of an event is the cursor a reader continues from: it reads the
events after the last one it has (``events_of_chat``), so it must never find a
smaller identifier committed after a larger one. That is why appending takes
the workspace lock, as every write of a workspace does: the events of one
workspace are then numbered and committed one transaction after another, so
they become visible in the order of their identifiers. A chat's events all
belong to one workspace, which is all the cursor needs.

Every function is scoped to a workspace and a project, takes a session and
never commits. A project the workspace does not have is ``ProjectNotFound``;
a chat the project does not have is ``ChatNotFound``. Appending does not check
that the run exists: a run is named without a foreign key, and the caller is
the run itself. Events are never changed; they are deleted with their project
(``db/project_removal.py``), and a chat's turns' events with the chat
(``db/chat.py``), while the events of a research or studio run stay when the
chat that asked for it is deleted: they are the run's.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from openmarketer_core.agent_runtime.threads import validate_kind
from openmarketer_core.db.chat import require_chat
from openmarketer_core.db.models import (
    ChatTurnRecord,
    ResearchRunRecord,
    RunEventRecord,
    StudioRunRecord,
)
from openmarketer_core.db.pages import MAX_PAGE_SIZE, Page, page_of, rows_to_fetch
from openmarketer_core.db.projects import get_project
from openmarketer_core.db.workspace_lock import wait_for_other_writes
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND
from openmarketer_core.research.workflow_contract import RESEARCH_RUN_KIND
from openmarketer_core.run_events import JsonValue, RunEvent
from openmarketer_core.studio.workflow_contract import STUDIO_RUN_KIND


@dataclass(frozen=True)
class StoredRunEvent:
    """One stored event of a run; ``id`` is its place in the order events were stored."""

    id: int
    project_id: uuid.UUID
    run_kind: str
    run_id: uuid.UUID
    kind: str
    detail: Mapping[str, JsonValue]
    created_at: datetime


def append_events(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_kind: str,
    run_id: uuid.UUID,
    events: Sequence[RunEvent],
) -> tuple[StoredRunEvent, ...]:
    """Store the events of run ``run_id`` of ``run_kind``, in the order given.

    Returns them as stored. Nothing is written, and no lock taken, for no
    events. Takes the workspace lock, which is what keeps the events' order
    of visibility that of their identifiers. Raises ``ProjectNotFound`` when
    the workspace has no such project, and ``ValueError`` for a kind of run
    that ``validate_kind`` refuses.
    """
    validate_kind(run_kind)
    if not events:
        return ()
    wait_for_other_writes(session, workspace_id)
    get_project(session, workspace_id=workspace_id, project_id=project_id)
    records = [
        RunEventRecord(
            project_id=project_id,
            run_kind=run_kind,
            run_id=run_id,
            kind=event.kind,
            detail=dict(event.detail),
        )
        for event in events
    ]
    session.add_all(records)
    session.flush()
    return tuple(_stored(record) for record in sorted(records, key=lambda r: r.id))


def events_of_chat(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    after: int | None,
    limit: int,
) -> list[StoredRunEvent]:
    """The chat's events stored after the event ``after``, oldest first.

    At most ``limit`` of them; from the first event when ``after`` is
    ``None``. A reader passes the identifier of the last event it got to read
    on. Raises ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat.
    """
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"a page holds between 1 and {MAX_PAGE_SIZE} rows, not {limit}")
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    query = _events_of_chat(project_id, chat_id)
    if after is not None:
        query = query.where(RunEventRecord.id > after)
    records = session.scalars(query.order_by(RunEventRecord.id).limit(limit))
    return [_stored(record) for record in records]


def latest_events_of_chat(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    limit: int,
    before: int | None = None,
) -> Page[StoredRunEvent, int]:
    """The chat's events, newest first, for a screen that shows its history.

    At most ``limit`` of them, starting before the event ``before`` (the
    ``next_before`` of the previous page) or at the newest. Raises
    ``ProjectNotFound`` when the workspace has no such project and
    ``ChatNotFound`` when the project has no such chat.
    """
    require_chat(session, workspace_id=workspace_id, project_id=project_id, chat_id=chat_id)
    query = _events_of_chat(project_id, chat_id)
    if before is not None:
        query = query.where(RunEventRecord.id < before)
    records = session.scalars(query.order_by(RunEventRecord.id.desc()).limit(rows_to_fetch(limit)))
    events = [_stored(record) for record in records]
    return page_of(events, limit, lambda last: last.id)


def _events_of_chat(project_id: uuid.UUID, chat_id: uuid.UUID) -> Select[RunEventRecord]:
    turns = select(ChatTurnRecord.id).where(
        ChatTurnRecord.project_id == project_id, ChatTurnRecord.thread_id == chat_id
    )
    research_runs = select(ResearchRunRecord.id).where(
        ResearchRunRecord.project_id == project_id, ResearchRunRecord.chat_thread_id == chat_id
    )
    studio_runs = select(StudioRunRecord.id).where(
        StudioRunRecord.project_id == project_id, StudioRunRecord.chat_thread_id == chat_id
    )
    return select(RunEventRecord).where(
        RunEventRecord.project_id == project_id,
        or_(
            and_(RunEventRecord.run_kind == CHAT_TURN_KIND, RunEventRecord.run_id.in_(turns)),
            and_(
                RunEventRecord.run_kind == RESEARCH_RUN_KIND,
                RunEventRecord.run_id.in_(research_runs),
            ),
            and_(
                RunEventRecord.run_kind == STUDIO_RUN_KIND,
                RunEventRecord.run_id.in_(studio_runs),
            ),
        ),
    )


def _stored(record: RunEventRecord) -> StoredRunEvent:
    return StoredRunEvent(
        id=record.id,
        project_id=record.project_id,
        run_kind=record.run_kind,
        run_id=record.run_id,
        kind=record.kind,
        detail=dict(record.detail),
        created_at=record.created_at,
    )
