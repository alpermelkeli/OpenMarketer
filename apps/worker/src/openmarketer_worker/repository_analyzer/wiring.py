"""Setting the repository analyzer up for this installation, when the worker starts.

Everything an analysis depends on is built here once (the analyzer's settings,
the model router, the extractors, access to the runs' checkpoints, the place
of the runs' clones on this worker's disk) and handed to what runs it; the
result says what the worker registers and how the checkpoints its runs leave
behind are removed. The process's own things come in as arguments: the
environment mapping, the session factory, the database URL and the
installation's credentials (``WorkspaceCredentials``).

No credential is read here. The model provider's key and the repository tokens
can be stored from the dashboard after the worker started, so each is resolved
for the run's workspace when an attempt needs it: the worker starts without a
model key, and a run that finds none is told so. The same holds for what a
workspace chose in the dashboard's settings, its models and the limits of an
analysis (``WorkspaceSettings``): the configuration file is read now, and the
workspace's choices are laid over it at each attempt. ``GITLAB_HOST``, which is
not a credential, is read and checked now (``repository_tokens.py``).

``repository_analyzer`` reads the environment; ``registration`` takes what was
built (``AnalyzerParts``) and declares the agent's worker side: the handler of
its one action, what releases a run that ended, the steps of its graph, and the
two activities of the workflow's hooks. Clones that runs of an earlier process
left on the disk are swept when the worker starts.

This module holds no rule: which token goes to which host, how a repository is
analysed and how a run changes state are all in core. It does not connect to
Temporal, build the worker or turn a failure into a start-up error: it raises
``SettingsError``, ``ConfigError``, ``IntakeError``, ``DatabaseError`` or
``OSError``, and the worker's ``main.py`` words them. The workflow never
imports it: it loads the graph and the database, which the sandbox refuses.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.agent_runtime.langgraph_agent import AgentSteps
from openmarketer_core.agent_runtime.steps import GraphRunRef
from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_analysis_runs
from openmarketer_core.llm import ChatModel, RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter
from openmarketer_core.repository_analysis.analyzer_agent import Limits
from openmarketer_core.repository_analysis.analyzer_agent.definitions import (
    CloneRepository,
    agent,
)
from openmarketer_core.repository_analysis.analyzer_agent.graph import recursion_limit
from openmarketer_core.repository_analysis.extraction import Extractor, discover_extractors
from openmarketer_core.repository_analysis.intake import (
    IntakeResult,
    RepositoryTokens,
    run_intake,
)
from openmarketer_core.repository_analysis.workflow_contract import ANALYSIS_TASK_QUEUE
from openmarketer_core.tracing import Tracing
from openmarketer_core.workspace_settings.analysis_limits import LARGEST_LIMITS
from openmarketer_worker import AgentRegistration
from openmarketer_worker.agent_runtime.activity_support import CheckpointAccess, checkpoints_in
from openmarketer_worker.agent_runtime.agent_worker import AgentWorker
from openmarketer_worker.agent_runtime.routes import ActionContext
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.repository_analyzer import steps
from openmarketer_worker.repository_analyzer.activities import AnalysisActivities
from openmarketer_worker.repository_analyzer.analysis import AnalysisWork, WordedSteps
from openmarketer_worker.repository_analyzer.clones import LocalClones
from openmarketer_worker.repository_analyzer.policy import (
    AnalysisPolicy,
    longest_analysis_seconds,
)
from openmarketer_worker.repository_analyzer.repository_tokens import WorkspaceRepositoryTokens
from openmarketer_worker.repository_analyzer.settings import RepositoryAnalyzerSettings
from openmarketer_worker.repository_analyzer.workflow import AnalyzeRepository
from openmarketer_worker.workspace_settings import WorkspaceSettings

# Under the system's temporary folder, where the runs of this worker keep their clones.
CLONES_FOLDER = "openmarketer-analysis"


@dataclass(frozen=True)
class AnalyzerParts:
    """What the analyzer's worker side is made of, built once when the worker starts."""

    sessions: sessionmaker[Session]
    checkpoints: CheckpointAccess
    clones: LocalClones
    # The chat model of one attempt, the tokens one clone may carry and the limits of one
    # attempt: each made for the workspace of the run when it is needed, so no credential or
    # choice of the workspace is held between attempts.
    new_model: Callable[[uuid.UUID], ChatModel]
    tokens_of: Callable[[uuid.UUID], RepositoryTokens]
    extractors: Sequence[Extractor]
    policy: AnalysisPolicy
    limits_of: Callable[[uuid.UUID], Limits] = field(default=lambda workspace_id: Limits())


def repository_analyzer(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    database_url: str,
    credentials: WorkspaceCredentials,
    tracing: Tracing,
) -> AgentRegistration:
    """The repository analyzer as this installation runs it, ready for the worker.

    The configuration is read and checked now, so a mistake in it stops the
    worker at start-up instead of failing every run. The credentials are not:
    a run asks for the ones of its workspace, and one that is missing ends
    that run. ``tracing`` is the worker's: each attempt's model is traced with it.
    """
    policy = RepositoryAnalyzerSettings.from_env(environ).policy
    checkpoints = checkpoints_in(database_url)
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    tokens = WorkspaceRepositoryTokens(credentials, environ)
    settings = WorkspaceSettings(sessions)
    clones = LocalClones(Path(tempfile.gettempdir()) / CLONES_FOLDER, intake)
    # A run that could still be in progress is younger than this, so its clone is left alone.
    clones.sweep(older_than=timedelta(seconds=longest_analysis_seconds(policy)))
    return registration(
        AnalyzerParts(
            sessions=sessions,
            checkpoints=checkpoints,
            clones=clones,
            # A chat model per attempt: it keeps the replies of the attempt it served, and
            # carries the models and the key its workspace has at that moment.
            # Traced when tracing is on: each call is a generation under the node that made it.
            new_model=lambda workspace_id: tracing.model(
                model_of_workspace(
                    settings.router_for(workspace_id, router), workspace_id, credentials
                )
            ),
            tokens_of=tokens.of,
            extractors=list(discover_extractors()),
            policy=policy,
            limits_of=settings.analysis_limits,
        )
    )


def model_of_workspace(
    router: ModelRouter, workspace_id: uuid.UUID, credentials: WorkspaceCredentials
) -> ChatModel:
    """The chat model of one attempt: ``router``, with the key its workspace has now."""
    return RouterChatModel(router=router, api_key=credentials.model_key(workspace_id, router))


def registration(parts: AnalyzerParts) -> AgentRegistration:
    """What the worker registers to run analyses with ``parts``."""
    work = AnalysisWork(
        parts.sessions,
        parts.checkpoints,
        parts.clones,
        new_model=parts.new_model,
        tokens_of=parts.tokens_of,
        extractors=parts.extractors,
        limits_of=parts.limits_of,
    )
    # One queue, the one the API starts the workflow on.
    worker = AgentWorker(agent, task_queue=ANALYSIS_TASK_QUEUE)

    @worker.handler(CloneRepository)
    def clone_repository(payload: CloneRepository, context: ActionContext) -> Mapping[str, Any]:
        # The action's id is not needed to make the clone once: the run's folder is the key.
        return work.clone(payload)

    @worker.run_ended
    def release_the_clone(run: GraphRunRef) -> None:
        work.release(run)

    graph_steps = AgentSteps(
        agent,
        initial_state=work.initial_state,
        finish=work.finish,
        context=work.context,
        # Set once for every run, so from the largest limits a workspace can choose.
        recursion_limit=recursion_limit(LARGEST_LIMITS),
    )
    activities = AnalysisActivities(parts.sessions, parts.policy)
    return worker.registration(
        checkpoints=parts.checkpoints,
        graph=WordedSteps(graph_steps, parts.clones),
        workflows=[AnalyzeRepository],
        texts=steps.ANALYSIS_TEXTS,
        activities=[activities.start_run, activities.record_failure],
        checkpoint_cleanups=[cleanup_of_analysis_runs(parts.policy)],
    )


def cleanup_of_analysis_runs(policy: AnalysisPolicy) -> CheckpointCleanup:
    """Core's rule for the checkpoints of analysis runs, with how long a run lasts under ``policy``.

    An unfinished run that started longer ago than its workflow can last
    loses its checkpoints (``policy.longest_analysis_seconds``).
    """
    longest_run = timedelta(seconds=longest_analysis_seconds(policy))

    def remove(session: Session) -> int:
        return remove_checkpoints_of_analysis_runs(session, longest_run=longest_run)

    return CheckpointCleanup(runs="analysis runs", remove=remove)


def intake(
    source: str, clone_into: Path, commit: str | None, tokens: RepositoryTokens
) -> IntakeResult:
    """Intake as the worker does it: with the tokens of the run, each sent only to its own host."""
    return run_intake(source, clone_into, tokens=tokens, commit=commit)
