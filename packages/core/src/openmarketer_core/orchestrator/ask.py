"""Asking the chat agent one message and storing the exchange when it answers.

``ask_project`` is the use case behind the dashboard's chat: it starts a turn of the chat
(one at a time per chat: ``ChatBusy`` otherwise), reads the chat's stored conversation, hands
it to the chat agent together with the operator's new message, and - when the turn produces
an answer - stores the exchange, the question and the answer, with one write, as the turn's.
A turn that fails stores no message and is recorded as failed, so sending the message again
answers it again, exactly as a turn that was lost (a process that died) is started again by
sending it again. How a turn starts, records its events and ends is ``turn_lifecycle.py``:
every row of it is written in a short transaction of its own, from the factory the caller
gives, so a screen reading the chat sees the turn while it runs.

The reads the agent makes go through ``ChatReads``, built by the caller; the model is the
``ChatModel`` the caller resolved. A request action (starting a research run) reaches the
caller through ``ChatRequests``, given in by whoever holds a workflow server; without one the
turn can only read. This module starts no workflow and reaches no workflow engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.chat import ChatMessage, append_exchange
from openmarketer_core.db.models import ChatTurnTrigger
from openmarketer_core.llm import ChatModel
from openmarketer_core.orchestrator.chat_agent.graph import answer_message
from openmarketer_core.orchestrator.chat_agent.rules import ChatLimits, ChatTurn
from openmarketer_core.orchestrator.events import TurnEvents
from openmarketer_core.orchestrator.reads import ChatReads
from openmarketer_core.orchestrator.requests import ChatRequests
from openmarketer_core.orchestrator.turn_lifecycle import StoredAnswer, TurnOf, take_turn


@dataclass(frozen=True)
class ChatExchange:
    """One answered exchange as it is stored, and what the turn cost to produce it.

    ``model`` and ``cost_usd`` are the turn's, not stored with the messages.
    """

    question: ChatMessage
    answer: ChatMessage
    model: str
    cost_usd: float


def ask_project(
    sessions: sessionmaker[Session],
    reads: ChatReads,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    chat_id: uuid.UUID,
    message: str,
    model: ChatModel,
    limits: ChatLimits | None = None,
    requests: ChatRequests | None = None,
) -> ChatExchange:
    """Answer ``message`` in one chat of the project, and store the exchange there.

    The turn is given the chat's stored conversation up to ``message``, and ``requests``, when
    one is given, for the request action that starts a research run. Raises ``ChatBusy`` while
    another turn of the chat runs, ``ProjectNotFound`` when the workspace has no such project
    and ``ChatNotFound`` when the project has no such chat (no turn is started in those
    cases); ``ChatTurnError`` when no answer was given within the limits and ``LLMError`` when
    the model cannot be used, after the turn is recorded as failed and nothing is stored.
    """

    def answer(history: Sequence[ChatMessage], events: TurnEvents) -> ChatTurn:
        conversation: list[dict[str, Any]] = [
            {"role": stored.role.value, "content": stored.content} for stored in history
        ]
        conversation.append({"role": "user", "content": message})
        return answer_message(
            reads, model, conversation, limits=limits, requests=requests, events=events
        )

    def store(
        session: Session, turn_id: uuid.UUID, turn: ChatTurn
    ) -> StoredAnswer[tuple[ChatMessage, ChatMessage]]:
        question, stored = append_exchange(
            session,
            workspace_id=workspace_id,
            project_id=project_id,
            chat_id=chat_id,
            turn_id=turn_id,
            question=message,
            answer=turn.answer,
        )
        return StoredAnswer((question, stored), stored.id)

    turn_of = TurnOf(workspace_id, project_id, chat_id, ChatTurnTrigger.MESSAGE)
    (question, stored), turn = take_turn(sessions, turn_of, answer=answer, store=store)
    return ChatExchange(question=question, answer=stored, model=turn.model, cost_usd=turn.cost_usd)
