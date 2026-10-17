"""Setting the researcher up for this installation, when the worker starts.

Everything a research run depends on is built here once (its settings, the
model router, the installation's search and rendering services, the object
store its pages are kept in, access to the runs' checkpoints) and handed to
what runs it; the result says what the worker registers: the agent's worker
side, the cleanup of its runs' left-over checkpoints, and the schedule that
expires its stored pages. The process's own things come in as arguments: the
environment mapping, the session factory, the database URL and the
installation's credentials (``WorkspaceCredentials``).

No credential is read here. The model provider's key can be stored from the
dashboard after the worker started, so it is resolved for the run's workspace
when an attempt needs it (``new_model``): the worker starts without a model
key, and a run that finds none is told so. The same holds for what a workspace
chose in the dashboard's settings, its models: the configuration file is read
now, and the workspace's choices are laid over it at each attempt.

``researcher`` reads the environment; ``registration`` takes what was built
(``ResearcherParts``) and declares the agent's worker side: the handlers of its
two actions, the steps of its graph, the two activities of the workflow's
hooks, the cleanup of its runs' checkpoints and the page expiry task.

This module holds no rule: what may be fetched, what a finding must rest on and
how a run changes state are all in core. It does not connect to Temporal, build
the worker or turn a failure into a start-up error: it raises ``SettingsError``,
``ConfigError`` or ``DatabaseError``, and the worker's ``main.py`` words them.
The workflow never imports it: it loads the graph and the database, which the
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
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_research_runs
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter
from openmarketer_core.research.page_fetch import PageFetcher, PublicPageFetcher
from openmarketer_core.research.page_render import CdpPageRenderer
from openmarketer_core.research.page_store import PageStore, S3PageStore
from openmarketer_core.research.research_agent import rules
from openmarketer_core.research.research_agent.definitions import (
    FetchPage,
    SearchWeb,
    agent,
)
from openmarketer_core.research.research_agent.graph import recursion_limit
from openmarketer_core.research.web_search import SearchEngine, SearxngSearch
from openmarketer_core.research.workflow_contract import RESEARCH_TASK_QUEUE
from openmarketer_core.tracing import Tracing
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.researcher import steps
from openmarketer_worker.researcher.activities import ResearchActivities
from openmarketer_worker.researcher.policy import (
    ResearchPolicy,
    longest_research_seconds,
)
from openmarketer_worker.researcher.research import ResearchWork, WordedSteps
from openmarketer_worker.researcher.retention import page_retention_task
from openmarketer_worker.researcher.settings import ResearcherSettings
from openmarketer_worker.researcher.workflow import Research
from openmarketer_worker.workspace_settings import WorkspaceSettings


@dataclass(frozen=True)
class ResearcherParts:
    """What the researcher's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    checkpoints: CheckpointAccess
    # The chat model of one attempt, made for the workspace of the run when it is needed, so
    # no credential or choice of the workspace is held between attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    search: SearchEngine
    fetcher: PageFetcher
    pages: PageStore
    policy: ResearchPolicy
    retention: timedelta
    limits: rules.Limits = field(default_factory=rules.Limits)


def researcher(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    database_url: str,
    credentials: WorkspaceCredentials,
    tracing: Tracing,
) -> AgentRegistration:
    """The researcher as this installation runs it, ready for the worker.

    The configuration is read and checked now, so a mistake in it stops the
    worker at start-up instead of failing every run. The credentials are not:
    a run asks for the ones of its workspace, and one that is missing ends that
    run. ``tracing`` is the worker's: each attempt's model is traced with it.
    """
    settings = ResearcherSettings.from_env(environ)
    checkpoints = checkpoints_in(database_url)
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    workspace_settings = WorkspaceSettings(sessions)
    return registration(
        ResearcherParts(
            sessions=sessions,
            checkpoints=checkpoints,
            # A chat model per attempt: it keeps the replies of the attempt it served, and
            # carries the models and the key its workspace has at that moment.
            # Traced when tracing is on: each call is a generation under the node that made it.
            new_model=lambda workspace_id: tracing.model(
                _model_of_workspace(
                    workspace_settings.router_for(workspace_id, router), workspace_id, credentials
                )
            ),
            search=SearxngSearch(settings.searxng_url),
            fetcher=PublicPageFetcher(renderer=CdpPageRenderer(settings.lightpanda_url)),
            pages=S3PageStore.from_env(environ),
            policy=settings.policy,
            retention=settings.page_retention,
        )
    )


def _model_of_workspace(
    router: ModelRouter, workspace_id: uuid.UUID, credentials: WorkspaceCredentials
) -> ChatModel:
    """The chat model of one attempt: ``router``, with the key its workspace has now."""
    return RouterChatModel(router=router, api_key=credentials.model_key(workspace_id, router))


def registration(parts: ResearcherParts) -> AgentRegistration:
    """What the worker registers to run research with ``parts``."""
    work = ResearchWork(
        parts.sessions,
        parts.checkpoints,
        new_model=parts.new_model,
        limits=parts.limits,
        search=parts.search,
        fetcher=parts.fetcher,
        pages=parts.pages,
    )
    # One queue, the one the API starts the workflow on.
    worker = AgentWorker(agent, task_queue=RESEARCH_TASK_QUEUE)

    @worker.handler(SearchWeb)
    def search_web(payload: SearchWeb, context: ActionContext) -> Mapping[str, Any]:
        # The action's id is not needed: a search reads and changes nothing.
        return work.search(payload)

    @worker.handler(FetchPage)
    def fetch_page(payload: FetchPage, context: ActionContext) -> Mapping[str, Any]:
        # The action's id is not needed to fetch once: the run's page at that URL is the key.
        return work.fetch(payload)

    graph_steps = AgentSteps(
        agent,
        initial_state=work.initial_state,
        finish=work.finish,
        context=work.context,
        recursion_limit=recursion_limit(parts.limits),
    )
    activities = ResearchActivities(parts.sessions, parts.policy)
    return worker.registration(
        checkpoints=parts.checkpoints,
        graph=WordedSteps(graph_steps),
        workflows=[Research],
        texts=steps.RESEARCH_TEXTS,
        activities=[activities.start_run, activities.record_failure],
        longest_run=timedelta(seconds=longest_research_seconds(parts.policy, parts.limits)),
        checkpoint_cleanups=[cleanup_of_research_runs(parts.policy, parts.limits)],
        periodic_tasks=[
            page_retention_task(parts.sessions, parts.pages, retention=parts.retention)
        ],
    )


def cleanup_of_research_runs(policy: ResearchPolicy, limits: rules.Limits) -> CheckpointCleanup:
    """Core's rule for the checkpoints of research runs, with how long a run lasts.

    An unfinished run that started longer ago than its workflow can last loses
    its checkpoints (``policy.longest_research_seconds``).
    """
    longest_run = timedelta(seconds=longest_research_seconds(policy, limits))

    def remove(session: Session) -> int:
        return remove_checkpoints_of_research_runs(session, longest_run=longest_run)

    return CheckpointCleanup(runs="research runs", remove=remove)
