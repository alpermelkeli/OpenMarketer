"""What the ``Research`` workflow and the ``ChatFollowUp`` workflow agree on: names and input.

A research run that a chat started tells that chat when it ends by starting the follow-up
workflow as a child it abandons (``researcher/workflow.py``). Both workflows are the worker's
and nothing else starts the follow-up, so the names are kept in the worker, here, in a module
the workflow sandbox can load: plain values, with no import of the workflow engine, the
database or an activity. The API would need them in core only if it ever started one.

The input names the chat and the run and nothing else: the history is stored and readable, so
no message, report or credential belongs in it. Its annotations are real types, not text,
because Temporal decodes the input from them: hence no ``from __future__ import annotations``.
"""

import uuid
from dataclasses import dataclass

CHAT_FOLLOW_UP_WORKFLOW = "ChatFollowUp"
CHAT_FOLLOW_UP_TASK_QUEUE = "openmarketer-orchestrator"


@dataclass(frozen=True)
class ChatFollowUpInput:
    """The chat to follow up in, and the research run it started that has ended."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    chat_id: uuid.UUID
    run_id: uuid.UUID


def follow_up_workflow_id(run_id: uuid.UUID) -> str:
    """The follow-up of a research run. Derived from the run: one run, one follow-up, ever."""
    return f"chat-follow-up-{run_id}"


@dataclass(frozen=True)
class FollowUpNotTaken:
    """A follow-up that ended before it got to a turn, and why, in a fixed sentence."""

    follow_up: ChatFollowUpInput
    cause: str
