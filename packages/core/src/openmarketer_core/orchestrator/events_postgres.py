"""``TurnEvents`` stored in PostgreSQL: the adapter a chat turn's use cases build for one turn.

Each event is stored in a short transaction of its own (``db/run_events.py``, under the run
kind ``CHAT_TURN_KIND`` and the turn's id), so a screen that reads the chat's events from
another connection sees it while the turn is still running, and nothing the turn holds open
delays it. An event that cannot be stored (the database is down, or the project was removed
while the turn ran) is logged and dropped: what the person watching misses is a line on a
screen, and the turn goes on, to be answered or to fail for a reason of its own.

This module imports SQLAlchemy and the store; the chat agent reaches it only through the port.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.orchestrator.chat_agent.definitions import CHAT_TURN_KIND
from openmarketer_core.run_events import RunEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PostgresTurnEvents:
    """The events of one turn of one chat, each committed as it is recorded."""

    sessions: sessionmaker[Session]
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    turn_id: uuid.UUID

    def record(self, event: RunEvent) -> None:
        try:
            with transaction(self.sessions) as session:
                append_events(
                    session,
                    workspace_id=self.workspace_id,
                    project_id=self.project_id,
                    run_kind=CHAT_TURN_KIND,
                    run_id=self.turn_id,
                    events=[event],
                )
        except (DatabaseError, ProjectNotFound) as e:
            logger.warning(
                "event %s of chat turn %s was not stored: %s", event.kind, self.turn_id, e
            )
