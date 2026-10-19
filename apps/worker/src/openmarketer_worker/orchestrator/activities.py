"""The activity of ``ChatFollowUp``: one follow-up turn, with the model of the chat's workspace.

It calls core's ``follow_up_on_run`` for the run's kind (a research run or a studio run) and
holds no rule of its own: what the turn is told,
what it stores, that a run already followed up on is not followed up again, and that a busy
chat is ``ChatBusy`` are all core's. What it adds is what an activity owes the engine: the
model is resolved for the chat's workspace at each attempt, a failure leaves with a type that
says whether another attempt is worth it and a message that may be stored, and the turn runs
in a thread while the activity sends heartbeats.

No credential is kept here: ``new_model`` is asked for the workspace's model, with the key
it has now, at every attempt (``wiring.py``), and the model is used for this attempt only. A
model key that is set nowhere, a stored value that does not open and a stored model choice
that breaks a rule end the follow-up in the words of their error, which name what to set and
hold no value. An error of the model provider is told in core's fixed sentence for it
(``rules.failure_reason``), never in what the provider wrote.

How many attempts may spend a model's turn is core's: a chat starts at most
``MAX_FOLLOW_UP_TURNS`` turns about one run (``FollowUpGivenUp`` after that), whichever kind
of failure the attempts before were tried again for. A run that has not ended yet has nothing
to follow up on.

``record_follow_up_not_taken`` is the second activity: when the follow-up ended before it got
to a turn, it tells the chat why, in the workflow's fixed sentence (``follow_up.py``'s
``record_follow_up_not_taken``).

A thread cannot be stopped: an attempt Temporal gave up on may still be answering when the
next one starts. The chat takes one turn at a time, so that next attempt is ``ChatBusy`` and
waits; once the first has answered, the next finds the run followed up on and does nothing.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.chat import ChatNotFound
from openmarketer_core.db.chat_turns import ChatBusy, FollowUpGivenUp
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.research_runs import ResearchRunNotFound
from openmarketer_core.db.session import transaction
from openmarketer_core.db.studio_runs import StudioRunNotFound
from openmarketer_core.llm import ChatModel, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.orchestrator.chat_agent.rules import (
    ChatLimits,
    ChatTurnError,
    failure_reason,
)
from openmarketer_core.orchestrator.follow_up import (
    RunNotEnded,
    follow_up_on_run,
    record_follow_up_not_taken,
)
from openmarketer_core.orchestrator.reads_postgres import PostgresChatReads
from openmarketer_core.tracing import Tracing
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_worker.agent_runtime.activity_support import run_in_thread, worded_failures
from openmarketer_worker.orchestrator import steps
from openmarketer_worker.orchestrator.contract import ChatFollowUpInput, FollowUpNotTaken

logger = logging.getLogger(__name__)

# What the credentials and settings of the chat's workspace are refused with: the model
# provider's key is set nowhere, a stored value does not open, or a stored model choice breaks
# a rule. None passes by itself, and each is worded for the operator.
WORKSPACE_FAILURES = (ModelKeyNotSet, CredentialUnreadable, StoredModelChoiceInvalid)
# What there is no chat or run to follow up on is told with: no such project, chat or run of
# the chat, whichever kind of run it is.
NOTHING_THERE = (ProjectNotFound, ChatNotFound, ResearchRunNotFound, StudioRunNotFound)


class FollowUpActivities:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        tracing: Tracing,
        limits: ChatLimits | None = None,
    ) -> None:
        """``new_model`` makes the chat model of one attempt for a workspace.

        It may raise one of ``WORKSPACE_FAILURES``, which ends the follow-up. ``tracing`` is
        the worker's: the turn's graph is traced under it.
        """
        self._sessions = sessions
        self._new_model = new_model
        self._tracing = tracing
        self._limits = limits

    @activity.defn(name=steps.FOLLOW_UP_ON_RUN)
    async def follow_up_on_run(self, follow_up: ChatFollowUpInput) -> None:
        """Take the chat's turn about the run that ended, unless it was taken."""
        subject = subject_of(follow_up)
        with worded_failures(steps.FOLLOW_UP_TEXTS, subject=subject):
            await run_in_thread(lambda: self._follow_up(follow_up), subject=subject)

    @activity.defn(name=steps.RECORD_FOLLOW_UP_NOT_TAKEN)
    async def record_follow_up_not_taken(self, not_taken: FollowUpNotTaken) -> None:
        """Tell the chat that the follow-up ended before it got to a turn, and why."""
        subject = subject_of(not_taken.follow_up)
        with worded_failures(steps.FOLLOW_UP_TEXTS, subject=subject):
            await run_in_thread(lambda: self._record_not_taken(not_taken), subject=subject)

    def _record_not_taken(self, not_taken: FollowUpNotTaken) -> None:
        follow_up = not_taken.follow_up
        try:
            record_follow_up_not_taken(
                self._sessions,
                workspace_id=follow_up.workspace_id,
                project_id=follow_up.project_id,
                chat_id=follow_up.chat_id,
                run_kind=follow_up.run_kind,
                run_id=follow_up.run_id,
                cause=not_taken.cause,
            )
        except NOTHING_THERE as e:
            logger.info("%s has no chat to tell: %s", subject_of(follow_up), e)

    def _follow_up(self, follow_up: ChatFollowUpInput) -> None:
        try:
            model = self._new_model(follow_up.workspace_id)
            with self._tracing.traced(), transaction(self._sessions) as reading:
                answered = follow_up_on_run(
                    self._sessions,
                    PostgresChatReads(reading, follow_up.workspace_id, follow_up.project_id),
                    workspace_id=follow_up.workspace_id,
                    project_id=follow_up.project_id,
                    chat_id=follow_up.chat_id,
                    run_kind=follow_up.run_kind,
                    run_id=follow_up.run_id,
                    model=model,
                    limits=self._limits,
                )
        except ChatBusy:
            raise ApplicationError(steps.CHAT_IS_BUSY, type=steps.CHAT_BUSY) from None
        except (*NOTHING_THERE, RunNotEnded) as e:
            logger.info("%s has nothing to follow up on: %s", subject_of(follow_up), e)
            raise _ended(steps.NOTHING_TO_FOLLOW_UP, steps.NOTHING_TO_FOLLOW_UP_ON) from None
        except FollowUpGivenUp as e:
            logger.info("%s is given up: %s", subject_of(follow_up), e)
            raise _ended(steps.FOLLOW_UP_FAILED, steps.FOLLOW_UP_GIVEN_UP) from None
        except LLMError as e:
            logger.info("%s: the model could not be used: %s", subject_of(follow_up), e)
            if e.may_pass:
                raise ApplicationError(failure_reason(e), type=steps.MODEL_UNAVAILABLE) from None
            raise _ended(steps.FOLLOW_UP_FAILED, failure_reason(e)) from None
        except ChatTurnError as e:
            raise _ended(steps.FOLLOW_UP_FAILED, failure_reason(e)) from None
        except (*WORKSPACE_FAILURES, ConfigError) as e:
            # Each names what to set or reset, and holds no value.
            raise _ended(steps.FOLLOW_UP_FAILED, str(e)) from None
        if answered is None:
            logger.info("%s was followed up on before", subject_of(follow_up))


def _ended(kind: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=kind, non_retryable=True)


def subject_of(follow_up: ChatFollowUpInput) -> str:
    """How the log names the follow-up's work."""
    return f"the follow-up of {follow_up.run_kind} {follow_up.run_id} in chat {follow_up.chat_id}"
