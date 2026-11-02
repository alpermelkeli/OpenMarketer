"""Setting the orchestrator's task up for this installation, when the worker starts.

Everything a task depends on is built here once (its settings, the model router, access to the
tasks' checkpoints) and handed to what runs it; the result says what the worker registers: the
agent's worker side, with the policy gate's activities for the confirmation of its plan, the
prepare callables of the runs it starts, and the cleanup of its tasks' left-over checkpoints.
The process's own things come in as arguments: the environment mapping, the session factory,
the database URL, the installation's credentials (``WorkspaceCredentials``) and its tracing.

No credential is read here. The model provider's key can be stored from the dashboard after the
worker started, so it is resolved for the task's workspace when an attempt needs it
(``new_model``): the worker starts without a model key, and a task that finds none is told so.
The same holds for what a workspace chose in the dashboard's settings, its models: the
configuration file is read now, and the workspace's choices are laid over it at each attempt.

The runs a task starts are served by their own agents' workers, on their own task queues: this
worker creates their records and the task's workflow waits for their workflows. Each of those
workers refuses to start when its runs can last longer than its contract says, which is what
the task counted (``longest_task_seconds``).

This module holds no rule: what a plan may hold and how a task changes state are core's. It
does not connect to Temporal, build the worker or turn a failure into a start-up error: it
raises ``SettingsError``, ``ConfigError`` or ``DatabaseError``, and the worker's ``main.py``
words them. The workflow never imports it: it loads the graph and the database, which the
sandbox refuses.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_tasks
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ConfigError, ModelRouter
from openmarketer_core.orchestrator.task.task_agent import rules
from openmarketer_core.orchestrator.task.task_agent.definitions import (
    ConfirmPlan,
    RunResearch,
    RunStudio,
    RunWriting,
    agent,
)
from openmarketer_core.orchestrator.task.task_agent.graph import recursion_limit
from openmarketer_core.orchestrator.task.workflow_contract import TASK_TASK_QUEUE
from openmarketer_core.tracing import Tracing
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext, RunContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.orchestrator_task import steps
from openmarketer_worker.orchestrator_task.activities import TaskActivities
from openmarketer_worker.orchestrator_task.policy import TaskPolicy, longest_task_seconds
from openmarketer_worker.orchestrator_task.settings import TaskSettings
from openmarketer_worker.orchestrator_task.task import KeptSteps, TaskWork
from openmarketer_worker.orchestrator_task.workflow import OrchestratorTask
from openmarketer_worker.workspace_settings import WorkspaceSettings


@dataclass(frozen=True)
class TaskParts:
    """What the task's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    checkpoints: CheckpointAccess
    # The chat model of one attempt, made for the workspace of the task when a node first asks
    # it something, so no credential or choice of the workspace is held between attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    policy: TaskPolicy = field(default_factory=TaskPolicy)
    limits: rules.TaskLimits = field(default_factory=rules.TaskLimits)


def orchestrator_task(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    database_url: str,
    credentials: WorkspaceCredentials,
    tracing: Tracing,
) -> AgentRegistration:
    """The orchestrator's task as this installation runs it, ready for the worker.

    The configuration is read and checked now, so a mistake in it stops the worker at start-up
    instead of failing every task. The credentials are not: a task asks for the ones of its
    workspace, and one that is missing ends that task. ``tracing`` is the worker's: each
    attempt's chat model is traced with it.
    """
    settings = TaskSettings.from_env(environ)
    checkpoints = checkpoints_in(database_url)
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    require_the_role(router)
    workspace_settings = WorkspaceSettings(sessions)

    def new_model(workspace_id: uuid.UUID) -> ChatModel:
        chosen = workspace_settings.router_for(workspace_id, router)
        key = credentials.model_key(workspace_id, chosen)
        # Traced when tracing is on: each call is a generation under the node that made it.
        return tracing.model(RouterChatModel(router=chosen, api_key=key))

    return registration(
        TaskParts(
            sessions=sessions,
            checkpoints=checkpoints,
            new_model=new_model,
            policy=settings.policy,
            limits=settings.limits,
        )
    )


def require_the_role(router: ModelRouter) -> None:
    """Raises ``ConfigError`` for a configuration without the role the task's model answers."""
    try:
        router.resolve(rules.ROLE)
    except KeyError as e:
        raise ConfigError(f"the orchestrator's task needs the role {rules.ROLE}: {e}") from None


def registration(parts: TaskParts) -> AgentRegistration:
    """What the worker registers to carry tasks out with ``parts``."""
    work = TaskWork(
        parts.sessions, parts.checkpoints, new_model=parts.new_model, limits=parts.limits
    )
    # One queue, the one the chat's request action starts the workflow on.
    worker = AgentWorker(agent, task_queue=TASK_TASK_QUEUE)

    @worker.handler(ConfirmPlan)
    def confirm_plan(payload: ConfirmPlan, context: ActionContext) -> Mapping[str, Any]:
        # Being run is all it is for: the gate ran it because a person confirmed the plan.
        return work.confirm_plan(payload, context)

    # Each creates the run of its step keyed by the action's id: an attempt that is tried
    # again finds the run it made, and the step is recorded as dispatched with it once.
    @worker.prepare_run(RunResearch)
    def prepare_research(payload: RunResearch, context: RunContext) -> uuid.UUID:
        return work.prepare_research(payload, context)

    @worker.prepare_run(RunStudio)
    def prepare_studio(payload: RunStudio, context: RunContext) -> uuid.UUID:
        return work.prepare_studio(payload, context)

    @worker.prepare_run(RunWriting)
    def prepare_writing(payload: RunWriting, context: RunContext) -> uuid.UUID:
        return work.prepare_writing(payload, context)

    graph_steps = AgentSteps(
        agent,
        initial_state=work.initial_state,
        finish=work.finish,
        context=work.context,
        recursion_limit=recursion_limit(),
        # What each node did, stored as it ends for the chat that asked for the task.
        record_progress=work.record_progress,
    )
    activities = TaskActivities(parts.sessions, parts.policy, spent=work.spent)
    longest_run = timedelta(seconds=longest_task_seconds(parts.policy))
    return worker.registration(
        checkpoints=parts.checkpoints,
        graph=KeptSteps(graph_steps, parts.sessions),
        workflows=[OrchestratorTask],
        texts=steps.TASK_TEXTS,
        activities=[activities.start_task, activities.record_end, activities.record_failure],
        longest_run=longest_run,
        checkpoint_cleanups=[cleanup_of_tasks(longest_run)],
        # The gate stores the proposal of ``confirm_plan`` and checks its approval there.
        sessions=parts.sessions,
    )


def cleanup_of_tasks(longest_run: timedelta) -> CheckpointCleanup:
    """Core's rule for the checkpoints of tasks, with how long a task lasts.

    An unfinished task that started longer ago than its workflow can last, the waits for a
    person and for the runs it started included, loses its checkpoints
    (``longest_task_seconds``).
    """

    def remove(session: Session) -> int:
        return remove_checkpoints_of_tasks(session, longest_run=longest_run)

    return CheckpointCleanup(runs="orchestrator tasks", remove=remove)
