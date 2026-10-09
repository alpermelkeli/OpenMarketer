"""Where the workflow engine is, as every process that talks to it reads it from the environment.

The API starts workflows and the worker executes them; both must name the same
server and namespace. This module is about the engine's address only: which
workflows exist and what they are given is each workflow's own contract (an
analysis run's is ``repository_analysis/workflow_contract.py``). It imports
nothing of the engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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
