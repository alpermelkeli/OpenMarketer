"""Setting the writer up for this installation, when the worker starts.

Everything a writing run depends on is built here once (its settings, the model router, access
to the runs' checkpoints) and handed to what runs it; the result says what the worker
registers: the agent's worker side, with the policy gate's activities for its post, and the
cleanup of its runs' left-over checkpoints. The process's own things come in as arguments: the
environment mapping, the session factory, the database URL, the installation's credentials
(``WorkspaceCredentials``) and its tracing.

No credential is read here. The model provider's key can be stored from the dashboard after the
worker started, so it is resolved for the run's workspace when an attempt needs it
(``new_model``): the worker starts without a model key, and a run that finds none is told so.
The same holds for what a workspace chose in the dashboard's settings, its models: the
configuration file is read now, and the workspace's choices are laid over it at each attempt.

That the roles ``writer`` and ``critic`` resolve to one model in the configuration file is
logged as a warning when the worker starts, and refuses nothing: a critic of the same model
judges the writer's text less independently, which an operator may accept.

``writer`` reads the environment; ``registration`` takes what was built (``WriterParts``) and
declares the agent's worker side: the handlers of its two actions, the steps of its graph
(which record each node's progress), the three activities of the workflow's hooks and the
cleanup of its runs' checkpoints.

This module holds no rule: what a post may say and how a run changes state are core's. It does
not connect to Temporal, build the worker or turn a failure into a start-up error: it raises
``SettingsError``, ``ConfigError`` or ``DatabaseError``, and the worker's ``main.py`` words
them. The workflow never imports it: it loads the graph and the database, which the sandbox
refuses.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_writing_runs
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ConfigError, ModelRouter
from openmarketer_core.tracing import Tracing
from openmarketer_core.writing.workflow_contract import WRITING_TASK_QUEUE
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.definitions import PublishPost, StoreDraft, agent
from openmarketer_core.writing.writer_agent.graph import recursion_limit
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.workspace_settings import WorkspaceSettings
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.activities import WriterActivities
from openmarketer_worker.writer.policy import WriterPolicy, longest_writing_seconds
from openmarketer_worker.writer.settings import WriterSettings
from openmarketer_worker.writer.workflow import WritePost
from openmarketer_worker.writer.writing import WordedSteps, WritingWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriterParts:
    """What the writer's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    checkpoints: CheckpointAccess
    # The chat model of one attempt, made for the workspace of the run when a node first asks
    # it something, so no credential or choice of the workspace is held between attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    policy: WriterPolicy = field(default_factory=WriterPolicy)
    limits: rules.Limits = field(default_factory=rules.Limits)


def writer(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    database_url: str,
    credentials: WorkspaceCredentials,
    tracing: Tracing,
) -> AgentRegistration:
    """The writer as this installation runs it, ready for the worker.

    The configuration is read and checked now, so a mistake in it stops the worker at start-up
    instead of failing every run. The credentials are not: a run asks for the ones of its
    workspace, and one that is missing ends that run. ``tracing`` is the worker's: each
    attempt's chat model is traced with it.
    """
    settings = WriterSettings.from_env(environ)
    checkpoints = checkpoints_in(database_url)
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    warn_of_one_model_for_both_roles(router)
    workspace_settings = WorkspaceSettings(sessions)
    return registration(
        WriterParts(
            sessions=sessions,
            checkpoints=checkpoints,
            # Traced when tracing is on: each call is a generation under the node that made it.
            new_model=lambda workspace_id: tracing.model(
                _model_of_workspace(
                    workspace_settings.router_for(workspace_id, router), workspace_id, credentials
                )
            ),
            policy=settings.policy,
            limits=settings.limits,
        )
    )


def warn_of_one_model_for_both_roles(router: ModelRouter) -> None:
    """Log a warning when ``writer`` and ``critic`` resolve to the same model; refuse nothing.

    Raises ``ConfigError`` for a configuration that lacks either role: every run would fail.
    """
    try:
        writing, critique = router.resolve(rules.WRITER_ROLE), router.resolve(rules.CRITIC_ROLE)
    except KeyError as e:
        raise ConfigError(f"the writer needs the roles writer and critic: {e}") from None
    if writing.model == critique.model:
        logger.warning(
            "the roles %s and %s both resolve to %s: the critic reviews posts with the model "
            "that wrote them; give the critic another model in the model configuration",
            rules.WRITER_ROLE,
            rules.CRITIC_ROLE,
            writing.model,
        )


def _model_of_workspace(
    router: ModelRouter, workspace_id: uuid.UUID, credentials: WorkspaceCredentials
) -> ChatModel:
    """The chat model of one attempt: ``router``, with the key its workspace has now."""
    return RouterChatModel(router=router, api_key=credentials.model_key(workspace_id, router))


def registration(parts: WriterParts) -> AgentRegistration:
    """What the worker registers to write posts with ``parts``."""
    work = WritingWork(
        parts.sessions, parts.checkpoints, new_model=parts.new_model, limits=parts.limits
    )
    # One queue, the one the API starts the workflow on.
    worker = AgentWorker(agent, task_queue=WRITING_TASK_QUEUE)

    @worker.handler(StoreDraft)
    def store_draft(payload: StoreDraft, context: ActionContext) -> Mapping[str, Any]:
        # The run is the key: it stores one item, and the same draft again gives it back.
        return work.store_draft(payload)

    @worker.handler(PublishPost)
    def publish_post(payload: PublishPost, context: ActionContext) -> Mapping[str, Any]:
        # The item is the key: it is recorded as a dry run once, and again changes nothing.
        return work.publish_post(payload)

    graph_steps = AgentSteps(
        agent,
        initial_state=work.initial_state,
        finish=work.finish,
        context=work.context,
        recursion_limit=recursion_limit(parts.limits),
        # What each node did, stored as it ends.
        record_progress=work.record_progress,
    )
    activities = WriterActivities(parts.sessions, parts.policy)
    longest_run = timedelta(seconds=longest_writing_seconds(parts.policy))
    return worker.registration(
        checkpoints=parts.checkpoints,
        graph=WordedSteps(graph_steps),
        workflows=[WritePost],
        texts=steps.WRITING_TEXTS,
        activities=[activities.start_run, activities.record_outcome, activities.record_failure],
        longest_run=longest_run,
        checkpoint_cleanups=[cleanup_of_writing_runs(longest_run)],
        # The gate stores the proposal of ``publish_post`` and checks its approval there.
        sessions=parts.sessions,
    )


def cleanup_of_writing_runs(longest_run: timedelta) -> CheckpointCleanup:
    """Core's rule for the checkpoints of writing runs, with how long a run lasts.

    An unfinished run that started longer ago than its workflow can last, the wait for a
    person's decision on its post included, loses its checkpoints (``longest_writing_seconds``).
    """

    def remove(session: Session) -> int:
        return remove_checkpoints_of_writing_runs(session, longest_run=longest_run)

    return CheckpointCleanup(runs="writing runs", remove=remove)
