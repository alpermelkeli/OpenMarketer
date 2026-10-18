"""Setting the creative studio up for this installation, when the worker starts.

Everything a studio run depends on is built here once (its settings, the model router, the
sandbox agent-written code runs in, the object store its images are kept in, access to the
runs' checkpoints) and handed to what runs it; the result says what the worker registers: the
agent's worker side, with the policy gate's and the creative budget's activities for its paid
operations, and the cleanup of its runs' left-over checkpoints. The process's own things come
in as arguments: the environment mapping, the session factory, the database URL, the
installation's credentials and the tracing.

No credential is read here. The model provider's key (the one key the studio uses: its chat
models and its image model are both OpenRouter's) can be stored from the dashboard after the
worker started, so it is resolved for the run's workspace when an attempt needs it
(``new_model``, ``new_media``), and so are the workspace's model choices. What one paid image
is reckoned to cost is read from the image role of the workspace's models, and needs no key.

``creative_studio`` reads the environment; ``registration`` takes what was built
(``StudioParts``) and declares the agent's worker side: a handler for each of its five
actions, the estimate of the two that spend, the steps of its graph (which record each node's
progress), the three activities of the workflow's hooks and the cleanup of its runs'
checkpoints.

This module holds no rule. It does not connect to Temporal, build the worker or turn a
failure into a start-up error: it raises ``SettingsError``, ``ConfigError`` or
``DatabaseError``, and the worker's ``main.py`` words them. The workflow never imports it: it
loads the graph and the database, which the sandbox refuses.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_studio_runs
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter
from openmarketer_core.studio.asset_store import AssetStore, S3AssetStore
from openmarketer_core.studio.docker_sandbox import DockerSandbox
from openmarketer_core.studio.media_provider import MediaProvider
from openmarketer_core.studio.openrouter_images import OpenRouterImages
from openmarketer_core.studio.sandbox import Sandbox
from openmarketer_core.studio.studio_agent import rules
from openmarketer_core.studio.studio_agent.definitions import (
    EditImage,
    GenerateImage,
    ReadText,
    RenderHtml,
    RunPython,
    agent,
)
from openmarketer_core.studio.studio_agent.graph import recursion_limit
from openmarketer_core.studio.workflow_contract import STUDIO_TASK_QUEUE
from openmarketer_core.tracing import Tracing
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext, SpendContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.creative_studio import steps
from openmarketer_worker.creative_studio.activities import StudioActivities
from openmarketer_worker.creative_studio.policy import StudioPolicy, longest_studio_seconds
from openmarketer_worker.creative_studio.settings import StudioSettings
from openmarketer_worker.creative_studio.studio import StudioWork, WordedSteps
from openmarketer_worker.creative_studio.workflow import CreativeStudio
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.workspace_settings import WorkspaceSettings


@dataclass(frozen=True)
class StudioParts:
    """What the studio's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    checkpoints: CheckpointAccess
    # The chat model and the image provider of one attempt, made for the workspace of the run
    # when they are needed, so no credential or choice of the workspace is held between
    # attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    new_media: Callable[[uuid.UUID], MediaProvider]
    # What one paid image is reckoned to cost under the workspace's models; needs no key.
    image_estimate: Callable[[uuid.UUID], Decimal]
    sandbox: Sandbox
    assets: AssetStore
    policy: StudioPolicy
    limits: rules.Limits
    # The process's environment: a cap of the creative budget set there wins over the
    # workspace's choice, and is read at each attempt.
    environ: Mapping[str, str]


def creative_studio(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    database_url: str,
    credentials: WorkspaceCredentials,
    tracing: Tracing,
) -> AgentRegistration:
    """The creative studio as this installation runs it, ready for the worker.

    The configuration is read and checked now, so a mistake in it stops the worker at
    start-up instead of failing every run. The credentials are not: a run asks for the ones
    of its workspace, and one that is missing ends that run. ``tracing`` is the worker's:
    each attempt's chat model and image provider are traced with it.
    """
    settings = StudioSettings.from_env(environ)
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    workspace_settings = WorkspaceSettings(sessions)

    def new_model(workspace_id: uuid.UUID) -> ChatModel:
        chosen = workspace_settings.router_for(workspace_id, router)
        key = credentials.model_key(workspace_id, chosen)
        return tracing.model(RouterChatModel(router=chosen, api_key=key))

    def new_media(workspace_id: uuid.UUID) -> MediaProvider:
        # The same key and the same workspace router as the chat model: the image role is
        # OpenRouter's like every other role.
        chosen = workspace_settings.router_for(workspace_id, router)
        key = credentials.model_key(workspace_id, chosen)
        return tracing.media(OpenRouterImages(router=chosen, api_key=key))

    def image_estimate(workspace_id: uuid.UUID) -> Decimal:
        chosen = workspace_settings.router_for(workspace_id, router)
        return OpenRouterImages(router=chosen).estimate("generate")

    return registration(
        StudioParts(
            sessions=sessions,
            checkpoints=checkpoints_in(database_url),
            new_model=new_model,
            new_media=new_media,
            image_estimate=image_estimate,
            sandbox=DockerSandbox(settings.sandbox),
            assets=S3AssetStore.from_env(environ),
            policy=settings.policy,
            limits=settings.limits,
            environ=environ,
        )
    )


def registration(parts: StudioParts) -> AgentRegistration:
    """What the worker registers to run the studio with ``parts``."""
    work = StudioWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        new_media=parts.new_media,
        image_estimate=parts.image_estimate,
        limits=parts.limits,
        sandbox=parts.sandbox,
        assets=parts.assets,
    )
    # One queue, the one the API starts the workflow on.
    worker = AgentWorker(agent, task_queue=STUDIO_TASK_QUEUE)

    # Each handler stores the version its payload names, keyed by the action's id: an attempt
    # that is tried again finds it and answers with it, so nothing is made or paid for twice.
    @worker.handler(GenerateImage)
    def generate_image(payload: GenerateImage, context: ActionContext) -> Mapping[str, Any]:
        return work.generate_image(payload, context)

    @worker.handler(EditImage)
    def edit_image(payload: EditImage, context: ActionContext) -> Mapping[str, Any]:
        return work.edit_image(payload, context)

    @worker.handler(RenderHtml)
    def render_html(payload: RenderHtml, context: ActionContext) -> Mapping[str, Any]:
        return work.render_html(payload, context)

    @worker.handler(RunPython)
    def run_python(payload: RunPython, context: ActionContext) -> Mapping[str, Any]:
        return work.run_python(payload, context)

    @worker.handler(ReadText)
    def read_text(payload: ReadText, context: ActionContext) -> Mapping[str, Any]:
        # Reading changes nothing: the action's id is not needed.
        return work.read_text(payload, context)

    # What a paid image costs is the image role's figure, never a number a model wrote.
    @worker.estimate(GenerateImage)
    def estimate_generation(payload: GenerateImage, context: SpendContext) -> Decimal:
        return work.estimate(context.scope.workspace_id)

    @worker.estimate(EditImage)
    def estimate_edit(payload: EditImage, context: SpendContext) -> Decimal:
        return work.estimate(context.scope.workspace_id)

    graph_steps = AgentSteps(
        agent,
        initial_state=work.initial_state,
        finish=work.finish,
        context=work.context,
        recursion_limit=recursion_limit(parts.limits),
        # What each node did, stored as it ends for the chat that asked for the run.
        record_progress=work.record_progress,
    )
    activities = StudioActivities(parts.sessions, parts.policy)
    longest_run = timedelta(seconds=longest_studio_seconds(parts.policy, parts.limits))
    return worker.registration(
        checkpoints=parts.checkpoints,
        graph=WordedSteps(graph_steps),
        workflows=[CreativeStudio],
        texts=steps.STUDIO_TEXTS,
        activities=[activities.start_run, activities.record_failure, activities.record_end],
        longest_run=longest_run,
        checkpoint_cleanups=[cleanup_of_studio_runs(longest_run)],
        sessions=parts.sessions,
        environ=parts.environ,
    )


def cleanup_of_studio_runs(longest_run: timedelta) -> CheckpointCleanup:
    """Core's rule for the checkpoints of studio runs, with how long a run lasts.

    An unfinished run that started longer ago than its workflow can last loses its
    checkpoints (``policy.longest_studio_seconds``).
    """

    def remove(session: Session) -> int:
        return remove_checkpoints_of_studio_runs(session, longest_run=longest_run)

    return CheckpointCleanup(runs="studio runs", remove=remove)
