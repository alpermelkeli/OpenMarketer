"""What the workflows of runs a chat starts and ``ChatFollowUp`` agree on: names and input.

A research run, a studio run or an orchestrator task that a chat started tells that chat when it
ends by starting the follow-up workflow as a child it abandons (``researcher/workflow.py``,
``creative_studio/workflow.py``, ``orchestrator_task/workflow.py``). Those workflows are the
worker's and nothing else starts the follow-up, so the names are kept in the worker, here, in a
module the workflow sandbox can load: plain values, with no import of the workflow engine, the
database or an activity. The API would need them in core only if it ever started one.

The input names the chat, the kind of the run (core's ``RESEARCH_RUN_KIND``, ``STUDIO_RUN_KIND`` or
``TASK_RUN_KIND``) and the run, and, for a task that tells its chat about one of its steps while
it goes on, the step's id in its plan, and nothing else: the history is stored and readable, so
no message, report, image or credential belongs in it. Its annotations are real types, not
text, because Temporal decodes the input from them: hence no ``from __future__ import
annotations``.
"""

import uuid
from dataclasses import dataclass

CHAT_FOLLOW_UP_WORKFLOW = "ChatFollowUp"
CHAT_FOLLOW_UP_TASK_QUEUE = "openmarketer-orchestrator"


@dataclass(frozen=True)
class ChatFollowUpInput:
    """The chat to follow up in, and the run of ``run_kind`` it started that has ended.

    ``step`` is ``None`` for the run itself. For a task (``TASK_RUN_KIND``) it may be the id
    of one of its steps whose run ended while the task goes on.
    """

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    chat_id: uuid.UUID
    run_kind: str
    run_id: uuid.UUID
    step: str | None = None


def follow_up_workflow_id(run_kind: str, run_id: uuid.UUID, step: str | None = None) -> str:
    """The follow-up of a run, or of one step of a task. Derived from the run and the step:
    one run, one follow-up, ever, and one for each step of a task."""
    if step is None:
        return f"chat-follow-up-{run_kind}-{run_id}"
    return f"chat-follow-up-{run_kind}-{run_id}-step-{step}"


@dataclass(frozen=True)
class FollowUpNotTaken:
    """A follow-up that ended before it got to a turn, and why, in a fixed sentence."""

    follow_up: ChatFollowUpInput
    cause: str
