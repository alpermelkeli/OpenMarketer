"""The workflows of the agent runtime's tests, for the workflow sandbox to load.

``ScriptedAgent`` is what an agent's workflow looks like: it hands the run to
``run_graph_with_actions`` and returns what the graph finished with. Its limits
come in as its input, so each test sets its own. ``WritesToTheDatabase`` calls
``database_write`` the way the analyzer's workflow does. The two that end in ``Records`` are
workflows that say how a run ended, as an agent's does to record it: one catches what the
analyzer's workflow catches, the other every failure the driver raises.

The sandbox imports the module of a workflow by name, so ``conftest.py`` puts this folder on
the import path, and this module imports only what the sandbox can load: no graph, no database.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

# The driver and the workflow support are imported plainly here; ``scripted_passthrough_agent.py``
# imports the driver through the sandbox, and the tests run both.
from openmarketer_worker.agent_runtime.driver import run_graph_with_actions
from openmarketer_worker.agent_runtime.workflow_support import database_write, reason_shown

with workflow.unsafe.imports_passed_through():
    from openmarketer_core.agent_runtime.steps import GraphRunRef
    from openmarketer_worker.agent_runtime.vocabulary import (
        RUN_TEXTS,
        ActionPolicy,
        DatabaseWritePolicy,
        GraphRunPolicy,
    )

# The failure type of ``WritesToTheDatabase`` that its activity is not tried again after.
REFUSED = "Refused"


@dataclass(frozen=True)
class ScriptedRun:
    run: GraphRunRef
    actions: dict[str, ActionPolicy]
    policy: GraphRunPolicy


@dataclass(frozen=True)
class Write:
    activity: str
    limits: DatabaseWritePolicy


@workflow.defn
class ScriptedAgent:
    @workflow.run
    async def run(self, scripted: ScriptedRun) -> dict[str, Any]:
        return await run_graph_with_actions(
            scripted.run, actions=scripted.actions, policy=scripted.policy
        )


@workflow.defn
class WritesToTheDatabase:
    @workflow.run
    async def run(self, write: Write) -> str:
        return await database_write(
            write.activity, "the row", write.limits, not_retried=[REFUSED], result_type=str
        )


@workflow.defn
class RecordsLikeTheAnalyzer:
    """Says how the run ended, catching what the analyzer's workflow catches."""

    @workflow.run
    async def run(self, scripted: ScriptedRun) -> str:
        try:
            await run_graph_with_actions(
                scripted.run, actions=scripted.actions, policy=scripted.policy
            )
        except (ActivityError, asyncio.CancelledError) as failure:
            return reason_shown(failure, RUN_TEXTS)
        return "finished"


@workflow.defn
class RecordsEveryFailure:
    """Says how the run ended, catching every failure the driver raises."""

    @workflow.run
    async def run(self, scripted: ScriptedRun) -> str:
        try:
            await run_graph_with_actions(
                scripted.run, actions=scripted.actions, policy=scripted.policy
            )
        except (ActivityError, ApplicationError, asyncio.CancelledError) as failure:
            return reason_shown(failure, RUN_TEXTS)
        return "finished"
