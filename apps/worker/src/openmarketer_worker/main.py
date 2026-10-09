"""The worker process: read its settings, connect, register the agents, serve until stopped.

This module holds what belongs to the process and to no agent: its settings,
the database sessions, the connection to Temporal, the ``Worker`` with its
executor and concurrency, the signals that stop it, the schedule of checkpoint
cleanups that runs while it serves, and the wording of a start that cannot
happen (``StartupError``). How an agent is set up (its settings, models,
tokens, workflows, activities and the cleanup of its runs' checkpoints) is in
the agent's ``wiring.py``, called here by name; this module reads no agent's
settings or policy and holds no rule.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from openmarketer_core.db.session import DatabaseError, session_factory
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_worker import AgentRegistration
from openmarketer_worker.checkpoint_cleanup import (
    CLEANUP_EVERY_SECONDS,
    CheckpointCleanup,
    keep_removing_leftover_checkpoints,
)
from openmarketer_worker.repository_analyzer.wiring import repository_analyzer
from openmarketer_worker.settings import Settings, SettingsError

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """The worker cannot start with this environment; the message says what to change."""


def build_worker(
    client: Client, agent: AgentRegistration, *, max_concurrent_activities: int
) -> Worker:
    """A worker serving the agent's workflows and activities on the agent's task queue."""
    return Worker(
        client,
        task_queue=agent.task_queue,
        workflows=agent.workflows,
        activities=agent.activities,
        # Activities that are blocking calls (short database writes) run in these threads.
        activity_executor=ThreadPoolExecutor(max_workers=max_concurrent_activities),
        max_concurrent_activities=max_concurrent_activities,
    )


async def serve(environ: Mapping[str, str]) -> None:
    """Run the worker until the process is asked to stop."""
    try:
        settings = Settings.from_env(environ)
        sessions = session_factory(settings.database_url)
        agent = repository_analyzer(environ, sessions, settings.database_url)
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
        client, agent, max_concurrent_activities=settings.max_concurrent_activities
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)
    logger.info(
        "worker serving %s on %s (namespace %s)",
        agent.task_queue,
        settings.temporal_address,
        settings.temporal_namespace,
    )
    await serve_until(stop, worker, sessions, agent.checkpoint_cleanups)


async def serve_until(
    stop: asyncio.Event,
    worker: Worker,
    sessions: sessionmaker[Session],
    checkpoint_cleanups: Sequence[CheckpointCleanup],
) -> None:
    """Serve the worker's queue, and run the checkpoint cleanups, until ``stop`` is set."""
    cleanup = asyncio.create_task(
        keep_removing_leftover_checkpoints(
            sessions, checkpoint_cleanups, every_seconds=CLEANUP_EVERY_SECONDS
        )
    )
    try:
        async with worker:
            await stop.wait()
            logger.info("stopping; the process ends when work in progress has returned")
    finally:
        cleanup.cancel()
        # Collected, so the task does not outlive the worker; its cancellation is the outcome.
        await asyncio.gather(cleanup, return_exceptions=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        asyncio.run(serve(os.environ))
    except StartupError as e:
        raise SystemExit(f"error: {e}") from e
