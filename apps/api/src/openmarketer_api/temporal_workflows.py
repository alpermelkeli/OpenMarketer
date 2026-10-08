"""Temporal adapter of ``AnalysisWorkflows``; the only module of the API that imports Temporal.

The connection is made on the first start and then kept. The API therefore
starts without a Temporal server, and everything that does not start an
analysis works without one; a start made while the server is unreachable fails
and the next one tries to connect again.

No execution or run timeout is set on the workflow: Temporal terminates a
workflow that exceeds one, and a terminated workflow cannot record its failure
on the run. The worker's own limits end an analysis.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from openmarketer_core.analysis_request import WorkflowNotStarted
from openmarketer_core.analysis_workflow import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    WorkflowServer,
    analysis_workflow_id,
)

logger = logging.getLogger(__name__)

# A request waits this long for Temporal before the start counts as failed.
CONNECT_TIMEOUT_SECONDS = 5
START_TIMEOUT_SECONDS = 10


class TemporalAnalysisWorkflows:
    def __init__(self, server: WorkflowServer, *, task_queue: str) -> None:
        self._server = server
        self._task_queue = task_queue
        self._client: Client | None = None
        self._connecting = asyncio.Lock()

    async def start(self, run: AnalyzeRepositoryInput) -> None:
        client = await self._connected()
        try:
            await client.start_workflow(
                ANALYZE_REPOSITORY_WORKFLOW,
                run,
                id=analysis_workflow_id(run.run_id),
                task_queue=self._task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=START_TIMEOUT_SECONDS),
            )
        except WorkflowAlreadyStartedError:
            return
        except RPCError as e:
            logger.error("Temporal did not start the workflow of run %s: %s", run.run_id, e)
            raise WorkflowNotStarted(f"Temporal refused: {e.status.name}") from e

    async def _connected(self) -> Client:
        async with self._connecting:
            if self._client is None:
                self._client = await self._connect()
            return self._client

    async def _connect(self) -> Client:
        try:
            return await asyncio.wait_for(
                Client.connect(self._server.address, namespace=self._server.namespace),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except (RuntimeError, TimeoutError) as e:
            logger.error("Temporal is not reachable at %s", self._server.address)
            raise WorkflowNotStarted(f"Temporal is not reachable at {self._server.address}") from e
