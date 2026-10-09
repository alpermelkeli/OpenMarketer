"""Setting the repository analyzer up for this installation, when the worker starts.

Everything an analysis depends on is built here once (the analyzer's settings,
the model router, the extractors, the repository tokens, access to the runs'
checkpoints) and handed to the activities; the result says what the worker
registers and how the checkpoints its runs leave behind are removed. The
process's own things come in as arguments: the environment mapping, the
session factory and the database URL.

This module holds no rule: which token goes to which host, how a repository is
analysed and how a run changes state are all in core. It does not connect to
Temporal, build the worker or turn a failure into a start-up error: it raises
``SettingsError``, ``ConfigError``, ``IntakeError``, ``DatabaseError`` or
``OSError``, and the worker's ``main.py`` words them. The workflow never
imports it: it loads the activities, and with them code the sandbox refuses.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from openmarketer_core.db.checkpoint_cleanup import remove_checkpoints_of_analysis_runs
from openmarketer_core.graph_checkpoints import RunCheckpoints
from openmarketer_core.graph_checkpoints.postgres import (
    driver_connection_string,
    forget_checkpoints,
    run_checkpoints,
)
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ModelRouter
from openmarketer_core.repository_analysis.extraction import discover_extractors
from openmarketer_core.repository_analysis.intake import RepositoryTokens
from openmarketer_core.repository_analysis.pipeline import RepositoryAnalysis, analyze_repository
from openmarketer_core.repository_analysis.workflow_contract import ANALYSIS_TASK_QUEUE
from openmarketer_worker import AgentRegistration
from openmarketer_worker.checkpoint_cleanup import CheckpointCleanup
from openmarketer_worker.repository_analyzer.activities import (
    AnalyseRepository,
    AnalysisActivities,
    CheckpointAccess,
)
from openmarketer_worker.repository_analyzer.policy import (
    AnalysisPolicy,
    longest_analysis_seconds,
)
from openmarketer_worker.repository_analyzer.settings import RepositoryAnalyzerSettings
from openmarketer_worker.repository_analyzer.workflow import AnalyzeRepository


def repository_analyzer(
    environ: Mapping[str, str], sessions: sessionmaker[Session], database_url: str
) -> AgentRegistration:
    """The repository analyzer as this installation runs it, ready for the worker.

    Everything is read and checked now, so a mistake in the configuration
    stops the worker at start-up instead of failing every run.
    """
    policy = RepositoryAnalyzerSettings.from_env(environ).policy
    checkpoints = checkpoints_in(database_url)
    analyse = analysis_with_configured_models(environ)
    return registration(AnalysisActivities(sessions, analyse, checkpoints, policy), policy)


def registration(activities: AnalysisActivities, policy: AnalysisPolicy) -> AgentRegistration:
    """What the worker registers to run analyses with ``activities`` under ``policy``."""
    return AgentRegistration(
        # One queue, the one the API starts the workflow on.
        task_queue=ANALYSIS_TASK_QUEUE,
        workflows=[AnalyzeRepository],
        activities=[activities.start_run, activities.analyse_and_store, activities.record_failure],
        checkpoint_cleanups=[cleanup_of_analysis_runs(policy)],
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


def analysis_with_configured_models(environ: Mapping[str, str]) -> AnalyseRepository:
    """How this installation analyses a repository: its models, extractors and tokens.

    The configuration is read once, so a mistake in it stops the worker at
    start-up instead of failing every run.
    """
    router = ModelRouter.from_file(
        environ.get("LLM_CONFIG_PATH") or DEFAULT_CONFIG_PATH, env=environ
    )
    # Asked for here, so a missing provider key stops the worker and not every run.
    router.headers()
    tokens = RepositoryTokens.from_environment(environ)
    extractors = list(discover_extractors())

    def analyse(
        repository_url: str, clone_into: Path, checkpoints: RunCheckpoints
    ) -> RepositoryAnalysis:
        # A chat model per attempt: it keeps the replies of the attempt it served.
        return analyze_repository(
            repository_url,
            clone_into,
            model=RouterChatModel(router=router),
            extractors=extractors,
            tokens=tokens,
            checkpoints=checkpoints,
        )

    return analyse


def checkpoints_in(database_url: str) -> CheckpointAccess:
    """The checkpoints of analysis runs, kept in the database the runs themselves are in."""
    # Asked for here, so a URL the checkpoint store cannot use stops the worker and not every run.
    driver_connection_string(database_url)

    # Functions, not ``functools.partial``: the text of a partial shows its arguments,
    # and the URL holds the password.
    def of_attempt(thread_id: str) -> AbstractContextManager[RunCheckpoints]:
        return run_checkpoints(database_url, thread_id)

    def forget(thread_id: str) -> None:
        forget_checkpoints(database_url, thread_id)

    return CheckpointAccess(of_attempt=of_attempt, forget=forget)
