"""What the runtime's activities do for a writing run: the worker's side of the writer's graph.

The writer's graph and its rules are core's. ``WritingWork`` is what stands between them and
this worker's database and models, one method for each thing the agent runtime asks an agent
for:

- ``initial_state``: what a run starts from, which is the run's own brief;
- ``context``: what the nodes of one attempt are given: a chat model of the attempt's own,
  which answers the roles ``writer`` and ``critic``, the run's limits, and the reading of the
  project's approved profile, which records the version the run writes from on the run;
- ``record_progress``: storing the events of each node as it ends (``progress.py`` in core);
- ``store_draft``: the handler of the action ``store_draft``: the draft stored as the run's
  content item, ``proposed``;
- ``publish_post``: the handler of the action ``publish_post``, which runs only once a person
  approved the post at the policy gate. In the writer's first step it sends nothing anywhere:
  it records the post as it would have been sent (``dry_run``), and holds no credential;
- ``finish``: the small projection of the graph's result that the workflow's ``after`` settles.

``WordedSteps`` wraps the steps of the graph so that what a writing run fails with leaves the
activity as a failure with a type and a message that may be stored and shown.

They hold no rule of their own: what a post may say, how it is chosen and how a run and its
item change state are core's. Everything can run twice without doing its work twice: the
version is recorded once (the same again changes nothing), the draft is stored once for its run
(the same text again gives back the stored item, so the run is its key), and a post recorded as
a dry run is settled once (the item is its key). The actions' ids are not needed as keys: a run
has one item, and an item is settled once.

The version the run writes from is recorded when ``prepare`` reads the approved profile, in
the same transaction, and not by a hook or by ``store_draft``: ``before`` runs before any
profile is read, ``after`` after the item that needs it was stored, and the draft the graph
stores does not name the version. Reading it again (an attempt that died before its checkpoint)
finds the same version, unless a newer one was approved meanwhile: the run then ends, worded,
rather than write from one version and record another.

No credential is kept here or put in the graph's state: the model of an attempt is made for the
run's workspace (``new_model``) the first time a node asks it something, so a step that calls no
model (the one that resumes once a person decided, days later) needs no key, and a key or a
model set from the dashboard is the one the next attempt uses. A key that is set nowhere, or a
credential that is stored and cannot be read (``WORKSPACE_FAILURES``), ends the run in the
words of its error, which name what to set and hold no value.

The proposal's id is not recorded on the item when the post is recorded as a dry run: a handler
is given the action's id and its payload, not the proposal, and core has no read of a proposal
by its action that is scoped to the run's project. The item keeps ``proposal_id`` empty.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.actions import ActionOutcome
from openmarketer_core.agent_runtime.steps import GraphRunRef, StepOutcome
from openmarketer_core.credentials.resolution import CredentialUnreadable, ModelKeyNotSet
from openmarketer_core.db.content_items import (
    ContentItemNotFound,
    ContentItemSettled,
    ContentItemStored,
    NewContentItem,
    ProfileVersionNotChosen,
    ProposalOfAnotherRun,
    content_item,
    settle_content_item,
    store_proposed_content_item,
)
from openmarketer_core.db.models import ContentItemStatus
from openmarketer_core.db.profile_versions import latest_approved_profile
from openmarketer_core.db.projects import ProjectNotFound
from openmarketer_core.db.run_events import append_events
from openmarketer_core.db.session import DatabaseError, transaction
from openmarketer_core.db.writing_runs import (
    WritingRunFinished,
    WritingRunNotFound,
    WritingRunNotStarted,
    WritingRunToExecute,
    WritingRunVersionChosen,
    mark_run_started,
    run_to_execute,
)
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.llm import ChatModel, ChatReply, LLMError
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.run_events import RunEvent
from openmarketer_core.workspace_settings.model_settings import StoredModelChoiceInvalid
from openmarketer_core.writing.brief import StoredWritingBriefInvalid
from openmarketer_core.writing.failures import WRITING_FAILURES
from openmarketer_core.writing.workflow_contract import WRITING_RUN_KIND, WritingRunInput
from openmarketer_core.writing.writer_agent.definitions import (
    ApprovedProfile,
    PublishPost,
    StoreDraft,
    WriterContext,
    WriterState,
    initial_state,
    result_of,
)
from openmarketer_core.writing.writer_agent.rules import Limits
from openmarketer_worker.agent_runtime.activities import AgentGraph
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess
from openmarketer_worker.agent_runtime.vocabulary import DATABASE_UNAVAILABLE, MAX_ERROR_CHARACTERS
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.activities import failure

logger = logging.getLogger(__name__)

# What the credentials and choices of a run's workspace are refused with: the model provider's
# key is set nowhere, a stored value does not open, or a stored model choice breaks a rule. None
# passes by itself, and each is worded for whoever asked for the post.
WORKSPACE_FAILURES = (ModelKeyNotSet, CredentialUnreadable, StoredModelChoiceInvalid)

NEWER_VERSION_APPROVED = (
    "a newer version of the product profile was approved while the run was starting: "
    "request the post again"
)


class ModelOfTheAttempt:
    """The chat model of one attempt, made the first time a node asks it something.

    A step that asks no model makes none, and so resolves no key.
    """

    def __init__(self, make: Callable[[], ChatModel]) -> None:
        self._make = make
        self._model: ChatModel | None = None

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
    ) -> ChatReply:
        if self._model is None:
            self._model = self._make()
        return self._model.chat(role, messages, tools=tools, tool_choice=tool_choice)

    def __repr__(self) -> str:
        return "ModelOfTheAttempt()"


class WritingWork:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        checkpoints: CheckpointAccess,
        *,
        new_model: Callable[[uuid.UUID], ChatModel],
        limits: Limits,
    ) -> None:
        """``new_model`` makes the chat model of one attempt for the run's workspace.

        It may raise one of ``WORKSPACE_FAILURES``, which ends the run.
        """
        self._sessions = sessions
        self._checkpoints = checkpoints
        self._new_model = new_model
        self._limits = limits

    def initial_state(self, run: GraphRunRef) -> WriterState:
        """What the graph of ``run`` starts from: the run and the brief it answers."""
        found = self._run_to_execute(run)
        return initial_state(
            WritingRunInput(
                workspace_id=found.workspace_id, project_id=found.project_id, run_id=found.run_id
            ),
            found.brief,
        )

    def context(self, run: GraphRunRef, checkpoints: RunCheckpoints) -> WriterContext:
        """What the nodes of one attempt at a step of ``run`` are given."""
        found = self._run_to_execute(run)
        return WriterContext(
            model=ModelOfTheAttempt(lambda: self._new_model(found.workspace_id)),
            limits=self._limits,
            approved_profile=lambda: self._approved_profile(found),
        )

    def record_progress(self, run: GraphRunRef, events: Sequence[RunEvent]) -> None:
        """Store the events of one node of ``run``, in a short transaction of their own.

        The run is handed by its id alone, so its workspace and project are read from its row
        in the same transaction. A run that is not there records nothing. Whatever this raises
        is logged by the step and never fails it (``AgentSteps``).
        """
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
            if found is None:
                logger.info("progress of writing run %s not recorded: no such run", run.run_id)
                return
            append_events(
                session,
                workspace_id=found.workspace_id,
                project_id=found.project_id,
                run_kind=WRITING_RUN_KIND,
                run_id=run.run_id,
                events=events,
            )

    def store_draft(self, payload: StoreDraft) -> Mapping[str, Any]:
        """Store the run's draft as its content item, ``proposed``; answers the item's id.

        The item takes the version the run recorded when it read the profile. A draft the run
        stored before is given back, so a repeat stores one; another draft for the same run is
        refused for good.
        """
        item = NewContentItem(
            text=payload.text,
            plan=payload.plan.model_dump(mode="json"),
            check_report=payload.check_report.model_dump(mode="json"),
            critic_report=payload.critic_report.model_dump(mode="json"),
            cost_usd=Decimal(str(payload.cost_usd)),
            plan_changes=list(payload.plan_changes),
            alternatives=[shown.model_dump(mode="json") for shown in payload.alternatives],
            claims=[claim.model_dump(mode="json") for claim in payload.claims],
            flagged_numbers=list(payload.flagged_numbers),
        )
        try:
            with transaction(self._sessions) as session:
                stored = store_proposed_content_item(
                    session,
                    workspace_id=payload.workspace_id,
                    project_id=payload.project_id,
                    run_id=payload.run_id,
                    item=item,
                )
        except (
            WritingRunNotFound,
            WritingRunNotStarted,
            WritingRunFinished,
            ProjectNotFound,
        ) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except (ProfileVersionNotChosen, ContentItemStored, ProposalOfAnotherRun) as e:
            raise failure(steps.WRITING_FAILED, str(e)) from None
        except DatabaseError:
            logger.exception("the draft of writing run %s was not stored", payload.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.DRAFT_NOT_STORED) from None
        return {"content_item_id": str(stored.id)}

    def publish_post(self, payload: PublishPost) -> Mapping[str, Any]:
        """Record the approved post as it would have been sent (``dry_run``); sends nothing.

        The post must be the run's own item with the text the person approved, so what is
        recorded as sent is what was approved. Recording it again changes nothing.
        """
        try:
            with transaction(self._sessions) as session:
                stored = content_item(
                    session,
                    workspace_id=payload.workspace_id,
                    project_id=payload.project_id,
                    item_id=payload.content_item_id,
                )
                if stored.run_id != payload.run_id or stored.text != payload.text:
                    raise failure(
                        steps.WRITING_FAILED,
                        f"the approved post is not content item {payload.content_item_id} "
                        f"of writing run {payload.run_id} as it was stored",
                    )
                settled = settle_content_item(
                    session,
                    workspace_id=payload.workspace_id,
                    project_id=payload.project_id,
                    item_id=payload.content_item_id,
                    status=ContentItemStatus.DRY_RUN,
                )
        except (
            ContentItemNotFound,
            ContentItemSettled,
            WritingRunNotFound,
            WritingRunFinished,
            ProjectNotFound,
        ) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        except DatabaseError:
            logger.exception("the post of writing run %s was not recorded", payload.run_id)
            raise failure(DATABASE_UNAVAILABLE, steps.POST_NOT_RECORDED) from None
        return {"content_item_id": str(settled.id), "status": settled.status.value}

    def finish(self, run: GraphRunRef, final_state: Mapping[str, Any]) -> dict[str, Any]:
        """The small projection of the result the workflow's ``after`` settles the post with.

        Identifiers, a status, the cost as exact text and the action's error; never the post,
        the plan or the reports, which the content item holds. It stores nothing, so it is
        safe to repeat.
        """
        result = result_of(final_state)
        return {
            "status": result.status.value,
            "content_item_id": result.content_item_id,
            "profile_version_id": result.profile_version_id,
            "cost_usd": str(Decimal(str(result.cost_usd))),
            "rounds": result.rounds,
            "passed_critic": result.passed_critic,
            "error": result.action_error[:MAX_ERROR_CHARACTERS],
        }

    def _approved_profile(self, run: WritingRunToExecute) -> ApprovedProfile | None:
        """The project's latest approved profile, its version recorded on the run; or ``None``."""
        try:
            with transaction(self._sessions) as session:
                approved = latest_approved_profile(
                    session, workspace_id=run.workspace_id, project_id=run.project_id
                )
                if approved is None:
                    return None
                mark_run_started(
                    session,
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.run_id,
                    profile_version_id=approved.id,
                )
        except WritingRunVersionChosen:
            raise failure(steps.WRITING_FAILED, NEWER_VERSION_APPROVED) from None
        except (WritingRunNotFound, WritingRunFinished, ProjectNotFound) as e:
            raise failure(steps.RUN_UNAVAILABLE, str(e)) from None
        return ApprovedProfile(version_id=approved.id, profile=approved.profile)

    def _run_to_execute(self, run: GraphRunRef) -> WritingRunToExecute:
        with transaction(self._sessions) as session:
            found = run_to_execute(session, run.run_id)
        if found is None:
            raise failure(steps.RUN_UNAVAILABLE, f"there is no writing run {run.run_id}")
        return found


class WordedSteps:
    """The steps of the writer's graph, with a writing run's failures worded for its run."""

    def __init__(self, graph: AgentGraph) -> None:
        self._graph = graph

    def advance(
        self, run: GraphRunRef, checkpoints: RunCheckpoints, resume: ActionOutcome | None
    ) -> StepOutcome:
        """Take the step, as ``AgentGraph.advance`` says.

        A project with no approved profile, a brief that announces a feature that is not live,
        answers of a model that cannot be read, a draft nothing can be proposed of, a brief that
        no longer fits its schema, a model whose key is not set and a configuration that refuses
        the call end the run (``WRITING_FAILED``); only the model provider fails in ways that
        pass, an outage or a rate limit (``MODEL_UNAVAILABLE``, tried again). The message is
        the error's own, which names no value.
        """
        try:
            return self._graph.advance(run, checkpoints, resume)
        except (
            *WRITING_FAILURES,
            StoredWritingBriefInvalid,
            *WORKSPACE_FAILURES,
            ConfigError,
        ) as e:
            logger.info("writing run %s failed: %s", run.run_id, e)
            passing = isinstance(e, LLMError) and e.may_pass
            kind = steps.MODEL_UNAVAILABLE if passing else steps.WRITING_FAILED
            raise failure(kind, str(e)) from None
