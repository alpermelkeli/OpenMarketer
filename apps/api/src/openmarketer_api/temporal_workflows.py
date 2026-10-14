"""Temporal adapters of ``AnalysisWorkflows`` and ``RunSignals``; the only module of the API that
imports Temporal.

The connection is made on the first start or signal and then kept, and both adapters of one
process share it. The API therefore starts without a Temporal server, and everything that does
not start an analysis or tell a run of a decision works without one; a call made while the
server is unreachable fails and the next one tries to connect again.

No execution or run timeout is set on the workflow: Temporal terminates a
workflow that exceeds one, and a terminated workflow cannot record its failure
on the run. The worker's own limits end an analysis.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from openmarketer_core.agent_runtime.gate import PROPOSAL_DECIDED_SIGNAL
from openmarketer_core.approvals.decision import RunNotTold
from openmarketer_core.repository_analysis.request import WorkflowNotStarted
from openmarketer_core.repository_analysis.workflow_contract import (
    ANALYZE_REPOSITORY_WORKFLOW,
    AnalyzeRepositoryInput,
    analysis_workflow_id,
)
from openmarketer_core.workflow_server import WorkflowServer

logger = logging.getLogger(__name__)

# A request waits this long for Temporal before the start counts as failed.
CONNECT_TIMEOUT_SECONDS = 5
START_TIMEOUT_SECONDS = 10
SIGNAL_TIMEOUT_SECONDS = 10


class TemporalUnreachable(Exception):
    """The workflow server did not answer the connection in time."""


class TemporalConnection:
    """One client of the workflow server, connected when it is first needed."""

    def __init__(self, server: WorkflowServer) -> None:
        self.server = server
        self._client: Client | None = None
        self._connecting = asyncio.Lock()

    async def client(self) -> Client:
        """The client; raises ``TemporalUnreachable`` while the server cannot be reached."""
        async with self._connecting:
            if self._client is None:
                self._client = await self._connect()
            return self._client

    async def _connect(self) -> Client:
        try:
            return await asyncio.wait_for(
                Client.connect(self.server.address, namespace=self.server.namespace),
                timeout=CONNECT_TIMEOUT_SECONDS,
            )
        except (RuntimeError, TimeoutError) as e:
            logger.error("Temporal is not reachable at %s", self.server.address)
            raise TemporalUnreachable(f"Temporal is not reachable at {self.server.address}") from e


class TemporalAnalysisWorkflows:
    def __init__(self, connection: TemporalConnection, *, task_queue: str) -> None:
        self._connection = connection
        self._task_queue = task_queue

    async def start(self, run: AnalyzeRepositoryInput) -> None:
        try:
            client = await self._connection.client()
        except TemporalUnreachable as e:
            raise WorkflowNotStarted(str(e)) from e
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


class TemporalRunSignals:
    def __init__(self, connection: TemporalConnection) -> None:
        self._connection = connection

    async def tell_decided(self, workflow_id: str, proposal_id: uuid.UUID) -> None:
        try:
            client = await self._connection.client()
            await client.get_workflow_handle(workflow_id).signal(
                PROPOSAL_DECIDED_SIGNAL,
                proposal_id,
                rpc_timeout=timedelta(seconds=SIGNAL_TIMEOUT_SECONDS),
            )
        except TemporalUnreachable as e:
            raise RunNotTold(str(e)) from e
        except RPCError as e:
            # A workflow that ended is told nothing; its proposal was expired when it ended.
            logger.warning("Temporal did not signal workflow %s: %s", workflow_id, e)
            raise RunNotTold(f"Temporal refused: {e.status.name}") from e
