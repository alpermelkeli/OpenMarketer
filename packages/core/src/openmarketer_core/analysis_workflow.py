"""What the API and the worker agree on to run an analysis as a durable workflow.

The API starts a workflow for a run it has stored (``db.analysis_runs``); the
worker executes it. Entry points do not import each other, so the names both
need are here: plain values, with no import of the workflow engine, and the
address of the engine as both read it from the environment.

The input names the run and nothing else. The engine stores every input in its
history, where it can be read, so no repository URL, token or profile ever
belongs in it; the worker reads what it needs from the database.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

ANALYZE_REPOSITORY_WORKFLOW = "AnalyzeRepository"
ANALYSIS_TASK_QUEUE = "openmarketer-analysis"


@dataclass(frozen=True)
class AnalyzeRepositoryInput:
    """The run to execute: a run of ``db.analysis_runs``, with its project and workspace."""

    workspace_id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID


def analysis_workflow_id(run_id: uuid.UUID) -> str:
    """The workflow of a run. It is derived, never stored: one run, one workflow."""
    return f"analyze-repository-{run_id}"


# The dev stack's server as seen from the host. ``.env.example`` names the
# compose-network host instead, which only resolves inside that network.
DEV_STACK_WORKFLOW_SERVER = "localhost:7233"
DEFAULT_NAMESPACE = "default"


@dataclass(frozen=True)
class WorkflowServer:
    """Where the workflow engine is. The API and the worker must name the same one."""

    address: str = DEV_STACK_WORKFLOW_SERVER
    namespace: str = DEFAULT_NAMESPACE

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> WorkflowServer:
        """``TEMPORAL_ADDRESS`` and ``TEMPORAL_NAMESPACE``; an empty value means the default."""
        return cls(
            address=environ.get("TEMPORAL_ADDRESS") or DEV_STACK_WORKFLOW_SERVER,
            namespace=environ.get("TEMPORAL_NAMESPACE") or DEFAULT_NAMESPACE,
        )
