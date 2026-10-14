"""The worker process: read its settings, connect, register the agents, serve until stopped.

This module holds what belongs to the process and to no agent: its settings,
the database sessions, how the installation's credentials are resolved for a
run's workspace (``credentials.py``), the connection to Temporal, a ``Worker`` for each
agent with its executor and concurrency, the signals that stop them, the schedule of
checkpoint cleanups that runs while they serve, and the wording of a start that cannot
happen (``StartupError``). How an agent is set up (its settings, models,
tokens, workflows, activities and the cleanup of its runs' checkpoints)
is in the agent's ``wiring.py``, called here by name in ``agents``; this module reads no
agent's settings or policy and holds no rule.

Each agent is served by a ``Worker`` of its own, on its own task queue. The runtime's
activities have the same names for every agent (``run_graph_step``, ``run_action``,
``forget_run``), which one ``Worker`` refuses twice, and a workflow schedules its activities
on its own queue: so the queue is what keeps one agent's steps from reaching another's
graph. Two agents on one queue are refused when the process starts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack

from sqlalchemy.orm import Session, sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from openmarketer_core.credentials.cipher import EncryptionKeyInvalid
from openmarketer_core.db.session import DatabaseError, session_factory
from openmarketer_core.llm_config import ConfigError
from openmarketer_core.repository_analysis.intake import IntakeError
from openmarketer_worker import AgentRegistration
from openmarketer_worker.checkpoint_cleanup import (
    CLEANUP_EVERY_SECONDS,
    CheckpointCleanup,
    keep_removing_leftover_checkpoints,
)
from openmarketer_worker.credentials import WorkspaceCredentials
from openmarketer_worker.repository_analyzer.wiring import repository_analyzer
from openmarketer_worker.settings import Settings, SettingsError

logger = logging.getLogger(__name__)


class StartupError(Exception):
    """The worker cannot start with this environment; the message says what to change."""


def agents(
    environ: Mapping[str, str],
    sessions: sessionmaker[Session],
    database_url: str,
    credentials: WorkspaceCredentials,
) -> list[AgentRegistration]:
    """Every agent this process serves, each set up by its own wiring."""
    return [repository_analyzer(environ, sessions, database_url, credentials)]


def build_workers(
    client: Client, registrations: Sequence[AgentRegistration], *, max_concurrent_activities: int
) -> list[Worker]:
    """A worker for each agent, each on the agent's own task queue.

    Raises ``StartupError`` for two agents on one queue: their activities have the same names,
    and a step of one could be taken by the other.
    """
    queues = [registration.task_queue for registration in registrations]
    shared = sorted({queue for queue in queues if queues.count(queue) > 1})
    if shared:
        raise StartupError(
            f"agents share the task queue {', '.join(shared)}: each agent needs a queue of its own"
        )
    return [
        build_worker(client, registration, max_concurrent_activities=max_concurrent_activities)
        for registration in registrations
    ]


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
        credentials = WorkspaceCredentials(sessions, environ, settings.credential_cipher)
        served = agents(environ, sessions, settings.database_url, credentials)
    except (
        SettingsError,
        EncryptionKeyInvalid,
        DatabaseError,
        ConfigError,
        IntakeError,
        OSError,
    ) as e:
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

    workers = build_workers(
        client, served, max_concurrent_activities=settings.max_concurrent_activities
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop.set)
    logger.info(
        "worker serving %s on %s (namespace %s)",
        ", ".join(registration.task_queue for registration in served),
        settings.temporal_address,
        settings.temporal_namespace,
    )
    cleanups = [cleanup for registration in served for cleanup in registration.checkpoint_cleanups]
    await serve_until(stop, workers, sessions, cleanups)


async def serve_until(
    stop: asyncio.Event,
    workers: Sequence[Worker],
    sessions: sessionmaker[Session],
    checkpoint_cleanups: Sequence[CheckpointCleanup],
) -> None:
    """Serve every worker's queue, and run the checkpoint cleanups, until ``stop`` is set.

    The workers stop together, each once the work it has in progress returned.
    """
    cleanup = asyncio.create_task(
        keep_removing_leftover_checkpoints(
            sessions, checkpoint_cleanups, every_seconds=CLEANUP_EVERY_SECONDS
        )
    )
    try:
        async with AsyncExitStack() as serving:
            for worker in workers:
                await serving.enter_async_context(worker)
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
