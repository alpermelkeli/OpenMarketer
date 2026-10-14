"""Temporal adapter of ``Workflows``; the only module of the API that imports Temporal.

It starts any workflow it is given, for every kind of run: the name, the task
queue, the id and the input come from the run's contract in core
(``WorkflowStart``), so adding a workflow adds nothing here. One connection
serves them all.

The connection is made on the first start and then kept. The API therefore
starts without a Temporal server, and everything that does not start a
workflow works without one; a start made while the server is unreachable fails
and the next one tries to connect again.

What holds for every workflow is decided here, once. A workflow id is never
used twice, so one run has one workflow even after it ended. No execution or
run timeout is set: Temporal terminates a workflow that exceeds one, and a
terminated workflow cannot record its failure on its run. The worker's own
limits end a run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from openmarketer_core.workflow_server import WorkflowServer
from openmarketer_core.workflow_start import WorkflowNotStarted, WorkflowStart

logger = logging.getLogger(__name__)

# A request waits this long for Temporal before the start counts as failed.
CONNECT_TIMEOUT_SECONDS = 5
START_TIMEOUT_SECONDS = 10


class TemporalWorkflows:
    def __init__(self, server: WorkflowServer) -> None:
        self._server = server
        self._client: Client | None = None
        self._connecting = asyncio.Lock()

    async def start(self, workflow: WorkflowStart) -> None:
        client = await self._connected()
        try:
            await client.start_workflow(
                workflow.workflow,
                workflow.input,
                id=workflow.workflow_id,
                task_queue=workflow.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=START_TIMEOUT_SECONDS),
            )
        except WorkflowAlreadyStartedError:
            return
        except RPCError as e:
            logger.error("Temporal did not start the workflow %s: %s", workflow.workflow_id, e)
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
