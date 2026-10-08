"""The worker process: read the environment, build what activities need, serve the queue.

Everything an analysis depends on is built here once (database sessions, the
model router, the extractors, the repository tokens) and handed to the
activities. This module holds no rule: which token goes to which host, how a
repository is analysed and how a run changes state are all in core.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from openmarketer_core.analysis_workflow import ANALYSIS_TASK_QUEUE
from openmarketer_core.db.session import DatabaseError, session_factory
from openmarketer_core.extraction import discover_extractors
from openmarketer_core.intake import IntakeError, RepositoryTokens
from openmarketer_core.llm import RouterChatModel
from openmarketer_core.llm_config import DEFAULT_CONFIG_PATH, ConfigError, ModelRouter
from openmarketer_core.repository_analysis import RepositoryAnalysis, analyze_repository
from openmarketer_worker.activities import AnalyseRepository, AnalysisActivities
from openmarketer_worker.analyze_repository import AnalyzeRepository
from openmarketer_worker.settings import Settings, SettingsError

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """The worker cannot start with this environment; the message says what to change."""


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

    def analyse(repository_url: str, clone_into: Path) -> RepositoryAnalysis:
        # A chat model per run: it keeps the replies of the run it served.
        return analyze_repository(
            repository_url,
            clone_into,
            model=RouterChatModel(router=router),
            extractors=extractors,
            tokens=tokens,
        )

    return analyse


def build_worker(
    client: Client,
    activities: AnalysisActivities,
    *,
    task_queue: str,
    max_concurrent_activities: int,
) -> Worker:
    """A worker serving ``AnalyzeRepository`` and its activities on ``task_queue``."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[AnalyzeRepository],
        activities=[activities.start_run, activities.analyse_and_store, activities.record_failure],
        # The two short activities are blocking database calls and run in these threads.
        activity_executor=ThreadPoolExecutor(max_workers=max_concurrent_activities),
        max_concurrent_activities=max_concurrent_activities,
    )


async def serve(environ: Mapping[str, str]) -> None:
    """Run the worker until the process is asked to stop."""
    try:
        settings = Settings.from_env(environ)
        sessions = session_factory(settings.database_url)
        analyse = analysis_with_configured_models(environ)
    except (SettingsError, DatabaseError, ConfigError, IntakeError, OSError) as e:
        raise StartupError(str(e)) from e
    try:
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
    except RuntimeError as e:
        raise StartupError(
            f"Temporal is not reachable at {settings.temporal_address} (TEMPORAL_ADDRESS); "
            "the dev stack's server is localhost:7233 from the host (`make up`)"
        ) from e

    worker = build_worker(
        client,
        AnalysisActivities(sessions, analyse, settings.analysis_policy),
        task_queue=ANALYSIS_TASK_QUEUE,
        max_concurrent_activities=settings.max_concurrent_activities,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)
    async with worker:
        logger.info(
            "worker serving %s on %s (namespace %s)",
            ANALYSIS_TASK_QUEUE,
            settings.temporal_address,
            settings.temporal_namespace,
        )
        await stop.wait()
        logger.info("stopping; the process ends when analyses in progress have returned")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(serve(os.environ))
    except StartupError as e:
        raise SystemExit(f"error: {e}") from e
