"""Setting the writer up for this installation, when the worker starts.

Everything a writing run depends on is built here once (its settings, the model router, access
to the runs' checkpoints, what a post is reckoned to cost, the object storage of the posts'
images and each platform's publisher) and handed to what runs it; the result says what the worker
registers: the agent's worker side, with the policy gate's activities for its post, and the
cleanup of its runs' left-over checkpoints. The process's own things come in as arguments: the
environment mapping, the session factory, the database URL, the installation's credentials
(``WorkspaceCredentials``) and its tracing.

No credential is read here. The model provider's key can be stored from the dashboard after the
worker started, so it is resolved for the run's workspace when an attempt needs it
(``new_model``): the worker starts without a model key, and a run that finds none is told so.
The same holds for what a workspace chose in the dashboard's settings, its models: the
configuration file is read now, and the workspace's choices are laid over it at each attempt.
A configuration without the role ``writer`` stops the worker at start-up, and so does a
publishing configuration (``PUBLISHING_CONFIG_PATH``, ``config/publishing.yaml`` by default)
that does not fit. The installation's app on a platform and an account's token are not read
here either: ``publish_post``'s handler resolves them for the run's workspace at the attempt
that sends.

``writer`` reads the environment; ``registration`` takes what was built (``WriterParts``) and
declares the agent's worker side: the handlers of its two actions, the steps of its graph
(which record each node's progress), the four activities of the workflow's hooks and the
cleanup of its runs' checkpoints.

This module holds no rule: what a post may say and how a run changes state are core's. It does
not connect to Temporal, build the worker or turn a failure into a start-up error: it raises
``SettingsError``, ``ConfigError`` or ``DatabaseError``, and the worker's ``main.py`` words
them. The workflow never imports it: it loads the graph and the database, which the sandbox
refuses.
"""

from __future__ import annotations

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
from openmarketer_core.social_accounts.catalog import SocialPlatform
from openmarketer_core.social_accounts.publishing_costs import (
    DEFAULT_PUBLISHING_CONFIG_PATH,
    PublishingCosts,
    PublishingCostsInvalid,
    read_publishing_costs,
)
from openmarketer_core.social_accounts.x.publishing import XPublisher
from openmarketer_core.studio.asset_store import S3AssetStore
from openmarketer_core.tracing import Tracing
from openmarketer_core.writing.workflow_contract import (
    WRITING_LONGEST_RUN_SECONDS,
    WRITING_TASK_QUEUE,
)
from openmarketer_core.writing.writer_agent import rules
from openmarketer_core.writing.writer_agent.definitions import PublishPost, StoreDraft, agent
from openmarketer_core.writing.writer_agent.graph import recursion_limit
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.settings import require_a_run_within
from openmarketer_worker.workspace_settings import WorkspaceSettings
from openmarketer_worker.writer import steps
from openmarketer_worker.writer.activities import WriterActivities
from openmarketer_worker.writer.policy import WriterPolicy, longest_writing_seconds
from openmarketer_worker.writer.publishing import PostPublishing
from openmarketer_worker.writer.settings import WriterSettings
from openmarketer_worker.writer.workflow import WritePost
from openmarketer_worker.writer.writing import WordedSteps, WritingWork


@dataclass(frozen=True)
class WriterParts:
    """What the writer's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    checkpoints: CheckpointAccess
    # The chat model of one attempt, made for the workspace of the run when a node first asks
    # it something, so no credential or choice of the workspace is held between attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    # What publishes an approved post once its send began, and what a post is reckoned to cost.
    publishing: PostPublishing
    publishing_costs: PublishingCosts
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
    require_the_writer_role(router)
    publishing_costs = publishing_costs_of(environ)
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
            publishing=PostPublishing(
                sessions,
                credentials,
                S3AssetStore.from_env(environ),
                {SocialPlatform.X: XPublisher()},
            ),
            publishing_costs=publishing_costs,
            policy=settings.policy,
            limits=settings.limits,
        )
    )


def require_the_writer_role(router: ModelRouter) -> None:
    """Raises ``ConfigError`` for a configuration that lacks the role ``writer``: every run
    would fail."""
    try:
        router.resolve(rules.WRITER_ROLE)
    except KeyError as e:
        raise ConfigError(f"the writer needs the role writer: {e}") from None


def publishing_costs_of(environ: Mapping[str, str]) -> PublishingCosts:
    """What a post is reckoned to cost, from ``PUBLISHING_CONFIG_PATH`` or the default file.

    Raises ``ConfigError`` for a file that does not fit: every proposal would name a wrong
    estimate, or none.
    """
    path = environ.get("PUBLISHING_CONFIG_PATH") or DEFAULT_PUBLISHING_CONFIG_PATH
    try:
        return read_publishing_costs(path)
    except PublishingCostsInvalid as e:
        raise ConfigError(str(e)) from None


def _model_of_workspace(
    router: ModelRouter, workspace_id: uuid.UUID, credentials: WorkspaceCredentials
) -> ChatModel:
    """The chat model of one attempt: ``router``, with the key its workspace has now."""
    return RouterChatModel(router=router, api_key=credentials.model_key(workspace_id, router))


def registration(parts: WriterParts) -> AgentRegistration:
    """What the worker registers to write posts with ``parts``.

    Raises ``SettingsError`` when a run can last longer than a task that waits for one counts.
    """
    require_a_run_within(
        longest_writing_seconds(parts.policy), WRITING_LONGEST_RUN_SECONDS, runs="writing"
    )
    work = WritingWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        limits=parts.limits,
        publishing=parts.publishing,
        publishing_costs=parts.publishing_costs,
    )
    # One queue, the one the API starts the workflow on.
    worker = AgentWorker(agent, task_queue=WRITING_TASK_QUEUE)

    @worker.handler(StoreDraft)
    def store_draft(payload: StoreDraft, context: ActionContext) -> Mapping[str, Any]:
        # The run is the key: it stores one item, and the same draft again gives it back.
        return work.store_draft(payload)

    @worker.handler(PublishPost)
    def publish_post(payload: PublishPost, context: ActionContext) -> Mapping[str, Any]:
        # The item is the key: it is published, or recorded as a dry run, once; again changes
        # nothing. The action's id finds the proposal the gate held it as, which the item names.
        return work.publish_post(payload, context.action_id)

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
        activities=[
            activities.start_run,
            activities.record_outcome,
            activities.record_failure,
            activities.record_end,
        ],
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
